"""PyTorch reference of the specialist: TypeLogic 1.x DecisionEngine on the suites (run with the 1.x Python).

Same weights, calibration and device protocol as the 1.x application (XPU float16 autocast by default, head in
float32). Writes one JSON line per item to <out>/<suite>.jsonl; an interrupted run resumes.

    python reference_torch.py --v1 <TypeLogic-AI> --out results/torch --suites jevbench,test2,banking77 --banking 1000
"""
import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suites  # noqa: E402


def done_ids(path):
    if not path.is_file():
        return set()
    return {json.loads(s)['id'] for s in path.read_text(encoding='utf-8').splitlines() if s.strip()}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--profile', type=Path, help='model profile (default: the active 1.x profile)')
    p.add_argument('--device', default='auto', choices=('auto', 'xpu', 'cpu'), help='auto: the Intel Arc GPU when this Python has PyTorch XPU')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--suites', default=','.join(suites.SUITES))
    p.add_argument('--limit', type=int, help='smoke test: first N items of every suite')
    p.add_argument('--banking', type=int, default=1000, help='BANKING77 rows (0 = all 2896)')
    args = p.parse_args()
    sys.path.insert(0, str(args.v1.resolve()))
    from typelogic.engine import DecisionEngine
    from typelogic.profiles import active_model_dir
    profile = args.profile or active_model_dir(args.v1.resolve())
    engine = DecisionEngine(profile, readout='torch', device=args.device, max_state=2048)
    began = time.monotonic()
    engine.load()
    health = engine.health()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'engine.json').write_text(json.dumps({'load_seconds': time.monotonic() - began, 'health': health}, indent=2, default=str), encoding='utf-8')
    print(f"loaded {profile} on {health['device']} ({health['precision']}), temperature {health['temperature']}", flush=True)
    for name in [s for s in args.suites.split(',') if s]:
        items = suites.load(args.v1, name, (args.banking or None) if name == 'banking77' else None)[:args.limit]
        path = args.out / f'{name}.jsonl'
        skip = done_ids(path)
        with path.open('a', encoding='utf-8') as out:
            for n, item in enumerate(items, 1):
                if item['id'] in skip:
                    continue
                row = {'id': item['id'], 'suite': name}
                t = time.perf_counter()
                try:
                    result = engine.evaluate({**suites.payload(item), 'permutation_check': False})
                    row['latency_s'] = time.perf_counter() - t
                    row['dist'] = {k: suites.from_typelogic(item['questions'][k], result['answers'][k]) for k in item['questions']}
                    row['input_tokens'] = result['usage'].get('input_tokens')
                except Exception as exc:  # over-length state or engine error: reported, never truncated
                    row.update(error=f'{type(exc).__name__}: {str(exc)[:300]}', latency_s=time.perf_counter() - t)
                out.write(json.dumps(row) + '\n')
                out.flush()
                if n % 50 == 0:
                    print(f'  {name}: {n}/{len(items)}', flush=True)
        print(f'{name}: done ({len(items)} items)', flush=True)


if __name__ == '__main__':
    main()
