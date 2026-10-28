"""Evaluate a trained run on this computer (Intel XPU, CUDA or CPU) with the protocol of evaluate_colab_v2.py.

Same steps and metrics (imported from evaluate_colab_v2): reproduce the training validation accuracy, fit the
temperature on calibration only, then score the test split once. Weights are never modified. Per-split logits
are checksummed so an interrupted run resumes with --resume. No network.

    python tools/evaluate_local.py --device xpu --data-dir research/data/intent-choice-en-v2 \
        --train-file research/data/jev-banking77-catalog-v2-audited/train.jsonl \
        --run research/runs/b580-xpu-distill-s1024 --base-dir models/decision-2b-distill-s1024/backbone

Use --limit N for a quick diagnostic on the first N rows of each split (reported as partial, never final).
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from evaluate_colab_v2 import check_dataset_identity, fit_temperature, metrics, sha, validation_reproduction, write_json  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--run', type=Path, default=ROOT / 'research/runs/b580-xpu-distill-s1024')
    p.add_argument('--base-dir', type=Path, help='Backbone folder (default: the active profile backbone)')
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--train-file', type=Path)
    p.add_argument('--out', type=Path)
    p.add_argument('--device', choices=('xpu', 'cuda', 'cpu'), default='xpu')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--limit', type=int, help='Diagnostic subset: first N rows per split (partial report)')
    p.add_argument('--allow-dataset-mismatch', action='store_true')
    p.add_argument('--allow-reproduction-drift', type=float, default=0.0,
                   help='Accepted |validation accuracy - training value| (default 0: exact, as in evaluate_colab_v2)')
    args = p.parse_args()
    from typelogic.offline import install
    install()
    from typelogic.learning import assert_disjoint, select_checkpoint, training_record
    from typelogic.model_profile import verify_backbone
    from typelogic.profiles import active_model_dir
    meta = select_checkpoint(args.run); cfg = meta['configuration']; adapter = args.run / 'adapter'
    base = args.base_dir or active_model_dir(ROOT) / 'backbone'
    out = Path(args.out) if args.out else args.run / (f'evaluation-local-{args.device}' + (f'-limit{args.limit}' if args.limit else ''))
    paths = {name: args.data_dir / f'{name}.jsonl' for name in ['train', 'validation', 'calibration', 'test']}
    if args.train_file:
        paths['train'] = args.train_file
    hashes = {name: sha(path) for name, path in paths.items()}
    dataset_matches = check_dataset_identity(hashes, meta['datasets'], args.allow_dataset_mismatch)
    splits = {name: [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()] for name, path in paths.items()}
    assert_disjoint(splits)
    for rows in splits.values():
        for row in rows:
            training_record(row)
    if args.limit:
        splits = {k: (v if k == 'train' else v[:args.limit]) for k, v in splits.items()}
    import torch, transformers, peft
    if args.device == 'xpu':
        if not (hasattr(torch, 'xpu') and torch.xpu.is_available()):
            raise RuntimeError('Intel XPU not available in this PyTorch build; use the TypeLogic-XPU environment')
        backend, device_name = torch.xpu, torch.xpu.get_device_name(0)
    elif args.device == 'cuda':
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA not available')
        backend, device_name = torch.cuda, torch.cuda.get_device_name(0)
    else:
        backend, device_name = None, 'cpu'
    amp_dtype = torch.float16 if meta['precision'] == 'fp16' else torch.bfloat16
    use_amp = args.device != 'cpu'
    identity = {'script_sha256': sha(__file__), 'protocol_sha256': sha(ROOT / 'tools/evaluate_colab_v2.py'), 'datasets': hashes,
                'checkpoint_datasets': meta['datasets'], 'dataset_matches_checkpoint': dataset_matches,
                'checkpoint_sha256': sha(args.run / 'checkpoint.json'),
                'adapter_sha256': {name: sha(adapter / name) for name in ['adapter_config.json', 'adapter_model.safetensors', 'head.pt']},
                'precision': meta['precision'] if use_amp else 'fp32', 'device': args.device, 'accelerator': device_name, 'limit': args.limit,
                'torch': torch.__version__, 'transformers': transformers.__version__, 'peft': peft.__version__}
    if out.exists():
        if not args.resume:
            raise ValueError('Output exists; add --resume or select a new --out directory')
        if json.loads((out / 'identity.json').read_text()) != identity:
            raise ValueError('Evaluation identity mismatch')
        if (out / 'report.json').is_file():
            print((out / 'report.json').read_text()); return
    else:
        out.mkdir(parents=True); write_json(out / 'identity.json', identity)
    print('Verifying local backbone...', flush=True)
    verify_backbone(base)
    from typelogic.vendor.pointer import DecisionModel, load_tokenizer
    from peft import PeftModel
    head = torch.load(adapter / 'head.pt', map_location='cpu', weights_only=True)
    if head['head_dim'] != cfg['head_dim']:
        raise ValueError('Head dimension mismatch')
    print('Loading epoch', meta['best_epoch'], 'adapter on', device_name, flush=True)
    tok = load_tokenizer(str(base))
    model = DecisionModel(str(base), tok, args.device, head_dim=head['head_dim'], dtype=torch.float32, direct_load=True)
    model.lm = PeftModel.from_pretrained(model.lm, str(adapter), is_trainable=False, local_files_only=True)
    model.head.load_state_dict(head['head'], strict=True); model.head.temperature = 1.
    if any(not torch.isfinite(v).all() for v in model.head.parameters()):
        raise ValueError('Invalid head values')
    model.lm.config.use_cache = False
    original_head = model.head.forward

    def fp32_head(*values, **kwargs):
        with torch.autocast(args.device, enabled=False):
            return original_head(*(v.float() if isinstance(v, torch.Tensor) else v for v in values), **kwargs)
    model.head.forward = fp32_head
    model.eval()
    if backend is not None:
        backend.reset_peak_memory_stats()
    start = time.monotonic()

    def infer(name):
        destination, checksum = out / (name + '-logits.json'), out / (name + '-logits.sha256')
        if destination.is_file():
            if not checksum.is_file() or checksum.read_text() != sha(destination):
                raise ValueError('Cached logits checksum mismatch')
            cached = json.loads(destination.read_text())
            if len(cached) != sum(len(training_record(r)['questions']) for r in splits[name]):
                raise ValueError('Incomplete cached split')
            print('Reusing verified', name, 'logits', flush=True); return cached
        result = []
        with torch.no_grad():
            for index, row in enumerate(splits[name]):
                rec = training_record(row)
                enc = model.encode(tok, rec, max_state=cfg['max_state'], max_branch=cfg['max_branch'], strict=True)
                with torch.autocast(args.device, dtype=amp_dtype, enabled=use_amp):
                    values = model(enc)
                if len(values) != len(rec['questions']):
                    raise ValueError('Wrong number of question outputs')
                for question, z in zip(rec['questions'], values):
                    if not torch.isfinite(z).all():
                        raise FloatingPointError('Non-finite evaluation logits')
                    result.append({'record_index': index, 'question_id': question['id'], 'options': question['options'],
                                   'logits': z.float().cpu().tolist(), 'target': question['target'],
                                   'language': row.get('language', 'en'), 'type': question['type'], 'source_id': row.get('source_id', 'unknown')})
                if (index + 1) % 50 == 0 or index + 1 == len(splits[name]):
                    elapsed = time.monotonic() - start
                    print(f'{name}: {index + 1}/{len(splits[name])}  ({elapsed:.0f} s elapsed)', flush=True)
        write_json(destination, result); checksum.write_text(sha(destination)); return result

    validation = infer('validation')
    baseline_val = metrics(validation, 1.)
    expected = meta['history'][meta['best_epoch'] - 1]['validation']['accuracy']
    reproduction = validation_reproduction(expected, baseline_val['accuracy'], dataset_matches and not args.limit)
    reproduction['tolerance'] = args.allow_reproduction_drift
    if dataset_matches and not args.limit and reproduction['absolute_difference'] > max(1e-8, args.allow_reproduction_drift):
        write_json(out / 'validation-mismatch.json', reproduction)
        raise RuntimeError('Validation accuracy differs from training beyond tolerance. Inspect validation-mismatch.json before testing.')
    calibration = infer('calibration'); temperature = fit_temperature(calibration)
    calibration_report = {'temperature': temperature, 'fit_split': 'calibration', 'baseline': metrics(calibration, 1.),
                          'fitted': metrics(calibration, temperature), 'grid_boundary': temperature <= .100000001 or temperature >= 19.999999,
                          'applied_to_original_checkpoint': False}
    write_json(out / 'temperature-fit.json', calibration_report)
    test = infer('test')
    report = {'base_model': meta['base_model'], 'best_epoch': meta['best_epoch'], 'training_steps': meta['training_steps'],
              'identity': identity, 'partial': bool(args.limit), 'validation_reproduction': reproduction,
              'validation': {'raw': baseline_val, 'calibrated': metrics(validation, temperature)},
              'calibration': calibration_report, 'test': {'raw': metrics(test, 1.), 'calibrated': metrics(test, temperature)},
              'seconds_this_execution': time.monotonic() - start,
              'peak_accelerator_memory_gib': backend.max_memory_allocated() / 1024 ** 3 if backend is not None else None,
              'deployment_eligible': False, 'checkpoint_modified': False,
              'scope': ('Checkpoint evaluated without updating weights. The test split was used for earlier diagnostics, so this is a '
                        'held-out diagnostic, not an independent benchmark. ' + ('Partial subset; not a final result. ' if args.limit else '')
                        + ('Accelerator mixed precision; CPU deployment parity is checked separately.' if use_amp else 'CPU fp32.'))}
    for key, fn in (('by_language_type', lambda r: r['language'] + '/' + r['type']), ('by_source', lambda r: r['source_id'])):
        report[key] = {}
        for split_name, records in (('validation', validation), ('calibration', calibration), ('test', test)):
            groups = sorted({fn(r) for r in records})
            report[key][split_name] = {g: {'raw': metrics([r for r in records if fn(r) == g], 1.),
                                           'calibrated': metrics([r for r in records if fn(r) == g], temperature)} for g in groups}
    write_json(out / 'report.json', report)
    summary = {'validation_accuracy': baseline_val['accuracy'], 'test_accuracy_raw': report['test']['raw']['accuracy'],
               'test_accuracy_wilson_95': report['test']['raw']['accuracy_wilson_95'], 'temperature': temperature,
               'test_nll_raw': report['test']['raw']['nll'], 'test_nll_calibrated': report['test']['calibrated']['nll'],
               'test_ece_raw': report['test']['raw']['ece_10_bins'], 'test_ece_calibrated': report['test']['calibrated']['ece_10_bins'],
               'partial': bool(args.limit), 'report': str(out / 'report.json')}
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
