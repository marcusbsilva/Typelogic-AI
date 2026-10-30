"""Convert the TypeLogic specialist (Qwen3.5-2B + LoRA + pointer head) to one GGUF file, fully offline.

llama.cpp v0.8.0 serves this architecture natively as a "kev" decision model through /v1/systemone: its converter
(conversion/lev.py, KevModel) merges the LoRA into the base weights, stores the pointer head as the classifier
([q | k]) and writes the prompt template. The converter normally downloads the base model from Hugging Face; this
wrapper points it at the backbone that TypeLogic 1.x already has on disk, after checking every file against the
backbone's SHA-256 provenance, and blocks the network for the whole run.

Run with the Python of TypeLogic 1.x (it has torch, transformers and safetensors):

    python convert_specialist.py --profile <v1>/models/decision-2b-distill-s1024 --out models/specialist-F16.gguf

The calibrated temperature (calibration.json, checked against the checkpoint fingerprint) is written into the
GGUF, so /v1/systemone returns the same probabilities as the PyTorch engine.
"""
import argparse
import hashlib
import json
import os
import runpy
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LLAMA_SRC = HERE / 'third_party' / 'llama.cpp'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def verify_backbone(backbone):
    """-> (repo, revision) after checking every file listed in asset-provenance.json."""
    provenance = json.loads((backbone / 'asset-provenance.json').read_text(encoding='utf-8'))
    for name, digest in provenance['files'].items():
        path = backbone / name
        if not path.is_file():
            raise SystemExit(f'Backbone file missing: {path}')
        if name.endswith('.safetensors') or os.environ.get('SPIKE_VERIFY_ALL', '1') == '1':
            actual = sha256(path)
            if actual != digest:
                raise SystemExit(f'Backbone file modified: {name} ({actual} != {digest})')
    return provenance['repo'], provenance['revision']


def calibrated_temperature(profile, v1_root):
    """Temperature from calibration.json when it belongs to this checkpoint (same rule as typelogic.engine)."""
    path = profile / 'calibration.json'
    if not path.is_file():
        return None, 'no calibration.json: head.pt temperature kept'
    sys.path.insert(0, str(v1_root))
    from typelogic.engine import checkpoint_fingerprint
    _, fingerprint = checkpoint_fingerprint(profile)
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('checkpoint_sha256') != fingerprint:
        raise SystemExit('calibration.json belongs to a different checkpoint')
    return float(data['temperature']), 'calibration.json (fingerprint verified)'


def stage_adapter(profile, work, repo, revision, temperature):
    """A Kev-style adapter folder: adapter_config + LoRA weights + head.pt + training_config (detection marker)."""
    import torch
    adapter = profile / 'adapter'
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    config = json.loads((adapter / 'adapter_config.json').read_text(encoding='utf-8'))
    if config.get('peft_type') != 'LORA' or config.get('trainable_token_indices') or config.get('modules_to_save'):
        raise SystemExit('Only a plain LoRA adapter can be converted')
    config['base_model_name_or_path'] = repo
    config['revision'] = revision
    (work / 'adapter_config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    shutil.copyfile(adapter / 'adapter_model.safetensors', work / 'adapter_model.safetensors')
    meta = torch.load(adapter / 'head.pt', map_location='cpu', weights_only=True)
    if temperature is not None:
        meta['temperature'] = temperature
    torch.save(meta, work / 'head.pt')
    (work / 'training_config.json').write_text(json.dumps({'args': {'head_dim': int(meta['head_dim'])}, 'base_revision': revision}), encoding='utf-8')
    return float(meta['temperature']), int(meta['head_dim'])


def offline_hub(backbone, repo, revision):
    """Serve the two Hugging Face calls of the converter from the local backbone; refuse anything else."""
    import huggingface_hub

    def check(repo_id, rev):
        if repo_id != repo or (rev not in (None, revision)):
            raise RuntimeError(f'offline conversion: unexpected request for {repo_id}@{rev}')

    def hf_hub_download(repo_id, filename, revision=None, **_):
        check(repo_id, revision)
        path = backbone / filename
        if not path.is_file():
            raise FileNotFoundError(path)
        return str(path)

    def snapshot_download(repo_id, revision=None, **_):
        check(repo_id, revision)
        return str(backbone)

    huggingface_hub.hf_hub_download = hf_hub_download
    huggingface_hub.snapshot_download = snapshot_download


def lazy_lora_merge():
    """Keep the LoRA merge lazy: the converter computes every delta B @ A eagerly in float32 before writing (about
    5 GB for this model held at once); here each delta is computed when its tensor is written. Same arithmetic."""
    from conversion import lev
    from conversion.base import LazyTorchTensor

    def modify_tensors(self, data_torch, name, bid):
        key = name[name.index('layers.'):] if 'layers.' in name else None
        lora = self.lora.get(key) if key else None
        if lora is not None:
            assert set(lora) == {'A', 'B'} and data_torch.shape == (lora['B'].shape[0], lora['A'].shape[1])
            delta = (LazyTorchTensor.from_eager(lora['B']) @ LazyTorchTensor.from_eager(lora['A'])) * self.lora_scale
            data_torch = data_torch.float() + delta
            self.lora_merged.add(key)
        yield from super(lev._DecisionLoraMixin, self).modify_tensors(data_torch, name, bid)

    lev._DecisionLoraMixin.modify_tensors = modify_tensors


def block_network():
    import socket

    def refuse(*_, **__):
        raise OSError('network disabled during the offline conversion')
    socket.create_connection = refuse
    socket.socket.connect = refuse


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--profile', type=Path, required=True, help='TypeLogic 1.x model profile folder (backbone/, adapter/)')
    p.add_argument('--v1', type=Path, required=True, help='TypeLogic 1.x project folder (for the checkpoint fingerprint)')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--outtype', default='f16', choices=('f16', 'bf16', 'f32', 'q8_0'))
    p.add_argument('--work', type=Path, default=HERE / 'work' / 'specialist-adapter')
    p.add_argument('--dry-run', action='store_true', help='check the conversion (every LoRA tensor merged) without writing the GGUF')
    args = p.parse_args()
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    profile, backbone = args.profile.resolve(), args.profile.resolve() / 'backbone'
    repo, revision = verify_backbone(backbone)
    temperature, source = calibrated_temperature(profile, args.v1.resolve())
    temperature, head_dim = stage_adapter(profile, args.work.resolve(), repo, revision, temperature)
    print(f'base {repo}@{revision[:12]}  head_dim {head_dim}  temperature {temperature:.6f} ({source})', flush=True)
    offline_hub(backbone, repo, revision)
    block_network()
    sys.path[:0] = [str(LLAMA_SRC), str(LLAMA_SRC / 'gguf-py')]
    lazy_lora_merge()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    sys.argv = ['convert_hf_to_gguf.py', str(args.work.resolve()), '--outfile', str(args.out.resolve()), '--outtype', args.outtype]
    if args.dry_run:
        sys.argv.append('--dry-run')
    runpy.run_path(str(LLAMA_SRC / 'convert_hf_to_gguf.py'), run_name='__main__')   # a dry run exits inside the converter
    info = {'gguf': args.out.name, 'bytes': args.out.stat().st_size, 'sha256': sha256(args.out), 'outtype': args.outtype,
            'base': f'{repo}@{revision}', 'temperature': temperature, 'temperature_source': source, 'head_dim': head_dim,
            'adapter_sha256': sha256(profile / 'adapter' / 'adapter_model.safetensors'), 'head_sha256': sha256(profile / 'adapter' / 'head.pt')}
    args.out.with_suffix('.json').write_text(json.dumps(info, indent=2), encoding='utf-8')
    print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
