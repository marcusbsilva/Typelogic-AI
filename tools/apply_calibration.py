"""Write models/<profile>/calibration.json from a completed local evaluation (temperature fitted on calibration only).

The file is bound to the profile's effective weights: the engine refuses it if any backbone, adapter or
checkpoint file changes. The report must come from the same adapter (hashes are compared), must be complete
(not --limit) and its temperature must not sit on the search-grid boundary. Existing files are not overwritten.

    python tools/apply_calibration.py --report research/runs/b580-xpu-catalog-v2-v1/evaluation-local-xpu/report.json
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def report_name(path):
    """Project-relative report path when possible, so the file carries no user folder names."""
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--model-dir', type=Path, help='Profile folder (default: the active profile)')
    args = p.parse_args()
    from typelogic.engine import checkpoint_fingerprint
    from typelogic.profiles import active_model_dir
    model_dir = (args.model_dir or active_model_dir(ROOT)).resolve()
    report = json.loads(args.report.read_text(encoding='utf-8'))
    if report.get('partial'):
        raise SystemExit('Refusing a partial (--limit) evaluation')
    if report['calibration'].get('fit_split') != 'calibration' or report['calibration'].get('grid_boundary'):
        raise SystemExit('Temperature was not fitted on the calibration split or sits on the grid boundary')
    for name, digest in report['identity']['adapter_sha256'].items():
        if sha(model_dir / 'adapter' / name) != digest:
            raise SystemExit(f'Report was produced with a different adapter file: {name}')
    temperature = float(report['calibration']['temperature'])
    if not 0 < temperature < 100:
        raise SystemExit('Invalid temperature')
    destination = model_dir / 'calibration.json'
    if destination.exists():
        raise SystemExit(f'{destination} exists; remove it explicitly to replace it')
    files, fingerprint = checkpoint_fingerprint(model_dir)
    value = {'checkpoint_sha256': fingerprint, 'temperature': temperature, 'fit_split': 'calibration',
             'calibration_metrics': {'baseline': report['calibration']['baseline'], 'fitted': report['calibration']['fitted']},
             'test_metrics': report['test'], 'report': report_name(args.report), 'report_sha256': sha(args.report),
             'device': report['identity'].get('device'), 'files_sha256': files,
             'scope': 'Temperature from the calibration split of the training data; local validation, not a general guarantee.'}
    with destination.open('x', encoding='utf-8') as out:
        json.dump(value, out, indent=2)
    print(json.dumps({'written': str(destination), 'temperature': temperature, 'checkpoint_sha256': fingerprint}, indent=2))


if __name__ == '__main__':
    main()
