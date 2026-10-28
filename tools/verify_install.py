"""End-to-end verification of an installed copy (runs on the user's computer; no network, no writes to the real database).

Steps: environment and active profile, automated tests, real decisions with the active model (temporary data
folder), readout parity ONNX vs Rust vs PyTorch, curated example sanity check. Writes verification-<timestamp>.json.

    python tools/verify_install.py            # everything that the installed dependencies allow
    python tools/verify_install.py --quick    # skip the test suite and the curated examples
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXAMPLES = ['billing', 'it-support', 'insufficient-evidence']


def run(args, env=None, timeout=7200, keep_stdout=False):
    started = time.monotonic()
    process = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
                             env={**os.environ, 'ORT_DISABLE_TELEMETRY': '1', 'PYTHONUTF8': '1', **(env or {})}, timeout=timeout)
    result = {'command': ' '.join(args), 'exit_code': process.returncode, 'seconds': round(time.monotonic() - started, 1),
              'stdout_tail': process.stdout[-4000:], 'stderr_tail': process.stderr[-4000:]}
    if keep_stdout:
        result['_stdout'] = process.stdout
    return result


def environment():
    from typelogic import core, profiles
    info = {'python': sys.version.split()[0], 'platform': platform.platform(), 'machine': platform.machine(), 'core': core.backend(),
            'active_profile': str(profiles.active_model_dir(ROOT)), 'profiles': profiles.installed(ROOT)}
    for name in ('numpy', 'torch', 'transformers', 'peft', 'onnx', 'onnxruntime', 'safetensors'):
        try:
            from importlib.metadata import version
            info[name] = version(name)
        except Exception as exc:
            info[name] = f'missing ({type(exc).__name__})'
    try:
        import torch
        info['torch_threads'] = torch.get_num_threads()
        info['xpu_available'] = bool(getattr(torch, 'xpu', None) and torch.xpu.is_available())
    except Exception:
        pass
    return info


def decisions(data, readout, env=None):
    from typelogic.examples import catalogue
    rows = {r['id']: r['payload'] for r in catalogue()['examples']}
    out = []
    for name in EXAMPLES:
        path = Path(data) / f'{name}.json'
        path.write_text(json.dumps(rows[name], ensure_ascii=False), encoding='utf-8')
        result = run(['tools/run_app.py', 'decide', '--data', str(data), '--input', str(path), '--readout', readout], timeout=3600, keep_stdout=True, env=env)
        try:
            response = json.loads(result.pop('_stdout').strip().splitlines()[-1])  # a decision line exceeds the 4000-char tail
            result['summary'] = {'decision_id': response['decision_id'], 'integrity_verified': response['integrity']['verified'],
                                 'latency_ms': response['latency_ms'], 'backend': response.get('backend'), 'model': response.get('model'),
                                 'checkpoint_sha256': response.get('checkpoint_sha256'), 'routing': response.get('routing', {}).get('questions'),
                                 'answers': {k: {'type': a['type'], 'answer': a.get('choice', a.get('noul', a.get('score'))),
                                                 'probabilities': a['probabilities'], 'recommendation': a['recommendation']}
                                             for k, a in response['answers'].items()}}
        except Exception as exc:
            reason = (result['stderr_tail'].strip().splitlines() or [f'{type(exc).__name__}: {exc}'])[-1]
            result['summary'] = {'error': reason}
        out.append(result)
    return out


def parity(by_readout):
    """Largest probability difference between readouts on the same example inputs."""
    reference = by_readout.get('onnx') or next(iter(by_readout.values()))
    report = {}
    for readout, rows in by_readout.items():
        worst = 0.0; same_answers = True
        for a, b in zip(reference, rows):
            sa, sb = a.get('summary', {}), b.get('summary', {})
            if 'answers' not in sa or 'answers' not in sb:
                return {'error': 'missing answers for parity'}
            for qid, ans in sa['answers'].items():
                other = sb['answers'][qid]
                x, y = ans['answer'], other['answer']
                # choice answers are labels; noul and score answers are probabilities compared with a tolerance
                same_answers &= abs(x - y) < 1e-3 if isinstance(x, float) and isinstance(y, float) else x == y
                worst = max(worst, max(abs(ans['probabilities'][k] - other['probabilities'][k]) for k in ans['probabilities']))
        report[readout] = {'max_abs_probability_difference_vs_onnx': worst, 'same_answers': same_answers}
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--quick', action='store_true')
    p.add_argument('--readouts', default='onnx,rust,torch')
    p.add_argument('--hybrid', action='store_true', help='Also run the example decisions through the hybrid engine '
                   '(TYPELOGIC_GENERALIST, default models/qwen35-4b; Ollama when TYPELOGIC_OLLAMA_MODEL is set)')
    args = p.parse_args()
    report = {'started': datetime.now().isoformat(timespec='seconds'), 'environment': environment()}
    print(json.dumps({'step': 'environment', 'core': report['environment']['core'], 'active_profile': report['environment']['active_profile']}), flush=True)
    if not args.quick:
        report['tests'] = run(['-m', 'pytest', 'tests', '-q', '-p', 'no:cacheprovider', '-rs'], env={'TYPELOGIC_REAL_INFERENCE': '1'})
        print(json.dumps({'step': 'tests', 'exit_code': report['tests']['exit_code'], 'result': report['tests']['stdout_tail'].strip().splitlines()[-1:]}), flush=True)
    readouts = [r for r in args.readouts.split(',') if r]
    if 'rust' in readouts and report['environment']['core']['backend'] != 'rust':
        readouts.remove('rust'); report['rust_readout'] = 'skipped: Rust extension not built on this computer (TypeLogic.bat build-native)'
    by_readout = {}
    with tempfile.TemporaryDirectory(prefix='typelogic-verify-') as data:
        for readout in readouts:
            (Path(data) / readout).mkdir(parents=True)
            by_readout[readout] = decisions(Path(data) / readout, readout)
            ok = [r['summary'].get('integrity_verified') for r in by_readout[readout]]
            print(json.dumps({'step': f'decisions[{readout}]', 'ok': ok, 'seconds': [r['seconds'] for r in by_readout[readout]]}), flush=True)
        if args.hybrid:
            env = {'TYPELOGIC_ENGINE': 'hybrid', 'TYPELOGIC_GENERALIST': os.environ.get('TYPELOGIC_GENERALIST', 'models/qwen35-4b')}
            (Path(data) / 'hybrid').mkdir(parents=True)
            report['hybrid_decisions'] = decisions(Path(data) / 'hybrid', readouts[0] if readouts else 'torch', env)
            print(json.dumps({'step': 'decisions[hybrid]', 'ok': [r['summary'].get('integrity_verified') for r in report['hybrid_decisions']],
                              'routing': [r['summary'].get('routing') for r in report['hybrid_decisions']]}), flush=True)
    report['decisions'] = by_readout
    report['readout_parity'] = parity(by_readout)
    print(json.dumps({'step': 'readout_parity', **report['readout_parity']}), flush=True)
    if not args.quick:
        with tempfile.TemporaryDirectory(prefix='typelogic-examples-') as data:
            output = Path(data) / 'examples.json'
            report['curated_examples'] = run(['tools/check_examples.py', '--output', str(output)], timeout=7200)
            if output.is_file():
                value = json.loads(output.read_text(encoding='utf-8'))
                report['curated_examples']['matched'] = f"{value['correct']}/{value['total']}"
        print(json.dumps({'step': 'curated_examples', 'matched': report['curated_examples'].get('matched')}), flush=True)
        if args.hybrid:
            with tempfile.TemporaryDirectory(prefix='typelogic-examples-hybrid-') as data:
                output = Path(data) / 'examples.json'
                env = {'TYPELOGIC_ENGINE': 'hybrid', 'TYPELOGIC_GENERALIST': os.environ.get('TYPELOGIC_GENERALIST', 'models/qwen35-4b')}
                report['curated_examples_hybrid'] = run(['tools/check_examples.py', '--output', str(output)], env=env, timeout=7200)
                if output.is_file():
                    value = json.loads(output.read_text(encoding='utf-8'))
                    report['curated_examples_hybrid']['matched'] = f"{value['correct']}/{value['total']}"
            print(json.dumps({'step': 'curated_examples[hybrid]', 'matched': report['curated_examples_hybrid'].get('matched')}), flush=True)
    report['finished'] = datetime.now().isoformat(timespec='seconds')
    destination = ROOT / f"verification-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    with destination.open('x', encoding='utf-8') as out:
        json.dump(report, out, indent=2, ensure_ascii=False)
    failed = (report.get('tests', {}).get('exit_code', 0) != 0) or any(not r['summary'].get('integrity_verified') for rows in by_readout.values() for r in rows) \
        or any(not r['summary'].get('integrity_verified') for r in report.get('hybrid_decisions', []))
    print(json.dumps({'report': str(destination), 'passed': not failed}), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
