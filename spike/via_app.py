"""Answer the suites through a running TypeLogic.exe (POST /v1/decide) and write results/<out>/<suite>.jsonl in the
format of bench.py, so report.py compares the application with the models measured alone.

    python via_app.py --v1 <TypeLogic-AI> --url http://127.0.0.1:8765 --engine generalist --out results/app-plumb --suites jevbench,test2
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import suites  # noqa: E402


def post(url, body, timeout=1800):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'HTTP {exc.code}: {exc.read().decode("utf-8", "replace")[:400]}') from None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--v1', type=Path, required=True)
    p.add_argument('--url', default='http://127.0.0.1:8765')
    p.add_argument('--engine', default='auto', help='auto, generalist, specialist or clef')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--suites', default='jevbench,test2,banking77')
    p.add_argument('--banking', type=int, default=1000)
    p.add_argument('--limit', type=int)
    p.add_argument('--combine', choices=('route', 'ensemble'), default='route', help='ensemble: questions with up to 16 options read by two models')
    p.add_argument('--permutation-check', action='store_true', help='also run the order diagnostic (slower)')
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    query = ([] if args.engine == 'auto' else [f'engine={args.engine}']) + ([] if args.combine == 'route' else [f'combine={args.combine}'])
    url = f'{args.url.rstrip("/")}/v1/decide' + ('?' + '&'.join(query) if query else '')
    for name in [s for s in args.suites.split(',') if s]:
        items = suites.load(args.v1, name, (args.banking or None) if name == 'banking77' else None)[:args.limit]
        engines = {}
        with (args.out / f'{name}.jsonl').open('w', encoding='utf-8') as out:
            for n, item in enumerate(items, 1):
                row = {'id': item['id'], 'suite': name}
                t = time.perf_counter()
                try:
                    reply = post(url, {**suites.payload(item), 'permutation_check': args.permutation_check})
                    row['dist'] = {k: suites.from_typelogic(item['questions'][k], reply['answers'][k]) for k in item['questions']}
                    row['engines'] = {k: reply['answers'][k].get('engine') for k in item['questions']}
                    row['input_tokens'] = reply.get('usage', {}).get('input_tokens')
                    for e in row['engines'].values():
                        engines[e] = engines.get(e, 0) + 1
                except Exception as exc:
                    row['error'] = f'{type(exc).__name__}: {str(exc)[:400]}'
                row['latency_s'] = time.perf_counter() - t
                out.write(json.dumps(row) + '\n')
                if n % 50 == 0:
                    print(f'  {args.out.name} {name}: {n}/{len(items)}', flush=True)
        print(f'{args.out.name} {name}: done, engines {engines}', flush=True)
    (args.out / 'target.json').write_text(json.dumps({'via': 'TypeLogic.exe', 'url': url, 'engine': args.engine, 'combine': args.combine}, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
