"""Download and verify the models TypeLogic AI needs. This is the only networked setup step; the app stays offline.

    python tools/fetch_models.py                       # backbone + adapter + tokenizer + Plumb-4B + extractor + Needle
    python tools/fetch_models.py --only backbone       # one step: backbone, adapter, tokenizer, plumb, extractor, needle, weights-4b
    python tools/fetch_models.py --adapter-zip C:\\Users\\me\\Downloads\\typelogic-adapter-decision-2b-distill-s1024.zip

Steps (all files are checked against SHA-256 hashes stored in this repository):
  backbone   Qwen/Qwen3.5-2B at a pinned revision (about 4.6 GB) -> models/<profile>/backbone
  adapter    TypeLogic specialist adapter (LoRA + decision head, about 88 MB) from the GitHub release zip
             -> models/<profile>/adapter. Give --adapter-zip PATH or URL, or leave the zip in the project folder
             or in your Downloads folder.
  tokenizer  Qwen/Qwen3.5-4B tokenizer and chat template, no weights (about 20 MB) -> models/qwen35-4b
  plumb      crh225/plumb-4b through the local Ollama server (about 5.2 GB, needs Ollama running)
  extractor  qwen3.5:4b through the local Ollama server (about 2.6 GB): extracts evidence from case text
  needle     Needle 3 by Cactus Compute for field extraction (about 36 MB) -> models/needle: the weights and the
             native engine taken out of the pinned wheel for this platform. The needle Python package is not installed.
  weights-4b optional, not part of the default: Qwen/Qwen3.5-4B weights (about 9 GB) for a generalist on the CPU
             without Ollama
Existing verified files are kept; a file with a different hash is never overwritten.
"""
import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GENERALIST_REPO, GENERALIST_REVISION = 'Qwen/Qwen3.5-4B', '851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a'
OLLAMA_MODEL = 'crh225/plumb-4b'
OLLAMA = 'http://127.0.0.1:11434'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def profile_dir():
    from typelogic.profiles import active_model_dir
    return active_model_dir(ROOT)


def backbone(folder):
    """Pinned Qwen3.5-2B files listed in research/qwen35-2b-assets.json, then the same verification as at startup."""
    from huggingface_hub import hf_hub_download
    from typelogic.model_profile import asset_manifest, prepare_backbone
    manifest = asset_manifest()
    target = folder / 'backbone'
    target.mkdir(parents=True, exist_ok=True)
    for name, record in manifest['files'].items():
        path = target / name
        if path.is_file() and path.stat().st_size == record['bytes'] and sha256(path) == record['sha256']:
            print(f'  ok        {name}')
            continue
        if path.exists():
            raise SystemExit(f'{path} exists with a different hash; move it away and run again.')
        print(f'  download  {name} ({record["bytes"] / 1e6:,.1f} MB)', flush=True)
        hf_hub_download(manifest['repo'], filename=name, revision=manifest['revision'], local_dir=str(target))
        if sha256(path) != record['sha256']:
            path.unlink()
            raise SystemExit(f'Hash mismatch for {name}; the file was removed. Run again.')
    result = prepare_backbone(target)
    print(f'  verified  {manifest["repo"]}@{manifest["revision"][:8]} (complete={result["complete"]})')


def find_adapter_zip(given):
    if given:
        return given
    name = f'typelogic-adapter-{profile_dir().name}*.zip'
    for folder in (ROOT, Path.home() / 'Downloads'):
        hits = sorted(folder.glob(name))
        if hits:
            return str(hits[-1])
    return None


def adapter(folder, given):
    """Install adapter/ from the release zip after checking every file against models/<profile>/manifest.json."""
    expected = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))['files']
    names = [n.split('/', 1)[1] for n in expected if n.startswith('adapter/')]   # every adapter file the manifest lists
    target = folder / 'adapter'
    if target.is_dir() and all((target / n).is_file() and sha256(target / n) == expected[f'adapter/{n}']['sha256'] for n in names):
        print('  ok        adapter already installed and verified')
        return
    if target.exists():
        raise SystemExit(f'{target} exists but does not match manifest.json; move it away and run again.')
    source = find_adapter_zip(given)
    if not source:
        raise SystemExit(f'Adapter zip not found. Download typelogic-adapter-{folder.name}.zip from the GitHub release page, '
                         'then run: python tools/fetch_models.py --only adapter --adapter-zip PATH')
    with tempfile.TemporaryDirectory(dir=folder) as stage:
        stage = Path(stage)
        if source.startswith('https://'):
            print(f'  download  {source}', flush=True)
            with urllib.request.urlopen(source) as response, (stage / 'adapter.zip').open('wb') as out:
                shutil.copyfileobj(response, out, 1 << 20)
            source = str(stage / 'adapter.zip')
        with zipfile.ZipFile(source) as archive:
            members = [n for n in archive.namelist() if not n.endswith('/')]
            for member in members:
                parts = Path(member).parts
                if '..' in parts or Path(member).is_absolute():
                    raise SystemExit(f'Unsafe path in zip: {member}')
            archive.extractall(stage / 'x')
        found = next((p.parent for p in (stage / 'x').rglob('head.pt')), None)
        if found is None:
            raise SystemExit('The zip has no adapter/head.pt.')
        for name in names:
            if not (found / name).is_file() or sha256(found / name) != expected[f'adapter/{name}']['sha256']:
                raise SystemExit(f'adapter/{name} is missing or does not match manifest.json (wrong release?).')
        target.mkdir()
        for name in names:
            shutil.copy2(found / name, target / name)
    print(f'  verified  adapter installed in {target.relative_to(ROOT)}')


def generalist(with_weights):
    from huggingface_hub import snapshot_download
    patterns = ['*.json', '*.jinja', 'merges.txt', 'vocab.json', 'LICENSE', 'README.md'] + (['*.safetensors'] if with_weights else [])
    path = snapshot_download(GENERALIST_REPO, revision=GENERALIST_REVISION, local_dir=str(ROOT / 'models/qwen35-4b'), allow_patterns=patterns)
    print(f'  verified  {GENERALIST_REPO}@{GENERALIST_REVISION[:8]} {"with weights" if with_weights else "tokenizer only"} -> {path}')


def needle_platform():
    """Wheel platform tag of this computer for the Needle native engine."""
    import platform
    machine = platform.machine().lower()
    arm = machine in ('arm64', 'aarch64')
    if sys.platform == 'win32':
        return 'win_arm64' if arm else 'win_amd64'
    if sys.platform == 'darwin':
        return 'macosx_11_0_arm64' if arm else 'macosx_11_0_x86_64'
    musl = platform.libc_ver()[0] != 'glibc' and any(Path('/lib').glob('ld-musl-*'))
    return ('musllinux_1_2_' if musl else 'manylinux2014_') + ('aarch64' if arm else 'x86_64')


def _download(url, target, expected, size=None):
    print(f'  download  {url.rsplit("/", 1)[1]}' + (f' ({size / 1e6:,.1f} MB)' if size else ''), flush=True)
    tmp = target.with_name(target.name + '.part')
    with urllib.request.urlopen(url, timeout=60) as response, tmp.open('wb') as out:
        shutil.copyfileobj(response, out, 1 << 20)
    if sha256(tmp) != expected:
        tmp.unlink()
        raise SystemExit(f'Hash mismatch for {target.name}; the download was removed. Run again.')
    tmp.replace(target)


def needle(tag=None):
    """Needle 3 weights and native engine at the revision pinned in models/needle/manifest.json."""
    folder = ROOT / 'models' / 'needle'
    record = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    tag = tag or needle_platform()
    if tag not in record['platforms']:
        raise SystemExit(f'Needle has no native engine for this platform ({tag}).')
    base = f'https://huggingface.co/{record["repo"]}/resolve/{record["revision"]}/'
    weights = folder / record['weights']['file']
    if weights.is_file() and sha256(weights) == record['weights']['sha256']:
        print(f'  ok        {weights.name}')
    elif weights.exists():
        raise SystemExit(f'{weights} exists with a different hash; move it away and run again.')
    else:
        _download(base + record['weights']['file'], weights, record['weights']['sha256'], record['weights']['bytes'])
    license_file = folder / record['license_file']['file']
    if not (license_file.is_file() and sha256(license_file) == record['license_file']['sha256']):
        _download(base + record['license_file']['file'], license_file, record['license_file']['sha256'])
    entry = record['platforms'][tag]
    library = folder / Path(entry['member']).name
    if library.is_file() and sha256(library) == entry['sha256']:
        print(f'  ok        {library.name} ({tag})')
    else:
        if library.exists():
            raise SystemExit(f'{library} exists with a different hash; move it away and run again.')
        with tempfile.TemporaryDirectory(dir=folder) as stage:
            wheel = Path(stage) / Path(entry['wheel']).name
            _download(base + entry['wheel'], wheel, entry['wheel_sha256'])
            with zipfile.ZipFile(wheel) as archive:   # only the engine is read; no Python code from the wheel is used
                data = archive.read(entry['member'])
            if hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise SystemExit(f'{entry["member"]} in the wheel does not match manifest.json.')
            library.write_bytes(data)
    print(f'  verified  Needle 3 {record["engine_version"]} ({record["repo"]}@{record["revision"][:8]}) in {folder.relative_to(ROOT)}')


def plumb(model, size='5.2 GB'):
    try:
        with urllib.request.urlopen(OLLAMA + '/api/tags', timeout=3) as response:
            tags = {m['name'] for m in json.load(response).get('models', [])}
    except OSError:
        print(f'  skipped   Ollama is not running. Install it from https://ollama.com, open it, then run: ollama pull {model}')
        return
    if model in tags or f'{model}:latest' in tags:
        print(f'  ok        {model} already in Ollama')
        return
    print(f'  download  {model} through Ollama (about {size})', flush=True)
    request = urllib.request.Request(OLLAMA + '/api/pull', data=json.dumps({'model': model}).encode(), headers={'Content-Type': 'application/json'})
    last = ''
    with urllib.request.urlopen(request) as response:
        for line in response:
            event = json.loads(line)
            if 'error' in event:
                raise SystemExit('Ollama: ' + event['error'])
            status = event.get('status', '')
            if event.get('total'):
                status += f' {100 * event.get("completed", 0) / event["total"]:.0f}%'
            if status != last:
                print('   ', status, flush=True)
                last = status
    print(f'  verified  {model} pulled')


def main():
    steps = ('backbone', 'adapter', 'tokenizer', 'plumb', 'extractor', 'needle', 'weights-4b')
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--only', choices=steps, action='append', help='Run only these steps (repeatable)')
    p.add_argument('--adapter-zip', help='Path or https URL of typelogic-adapter-<profile>.zip')
    p.add_argument('--ollama-model', default=OLLAMA_MODEL)
    p.add_argument('--extract-model', default='qwen3.5:4b', help='Ollama model used to extract evidence')
    p.add_argument('--needle-platform', help='Wheel platform tag of the Needle engine (default: this computer), e.g. win_amd64')
    args = p.parse_args()
    chosen = args.only or ['backbone', 'adapter', 'tokenizer', 'plumb', 'extractor', 'needle']
    folder = profile_dir()
    print(f'Specialist profile: {folder.relative_to(ROOT) if folder.is_relative_to(ROOT) else folder}')
    for step in steps:
        if step not in chosen:
            continue
        print(f'[{step}]', flush=True)
        if step == 'backbone':
            backbone(folder)
        elif step == 'adapter':
            adapter(folder, args.adapter_zip)
        elif step == 'tokenizer':
            generalist(False)
        elif step == 'weights-4b':
            generalist(True)
        elif step == 'needle':
            needle(args.needle_platform)
        elif step == 'extractor':
            plumb(args.extract_model, '2.6 GB')
        else:
            plumb(args.ollama_model)
    print('Done. Start the app with TypeLogic.bat (Windows) or ./typelogic.sh (Linux).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
