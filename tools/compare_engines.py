"""Compare and combine two engines from per-question dumps of tools/eval_split.py (--dump). No model, no network.

    python tools/compare_engines.py --a research/results/split/per-question/specialist-distill-s1024-{split}.jsonl \
        --b research/results/split/per-question/plumb-4b-{split}.jsonl \
        --tune distill-calibration,distill-test --test test2 --out research/results/split/combination.json

"{split}" is replaced by each split name. Combinations (A = specialist, B = generalist):
  linear w      p = w*B + (1-w)*A
  loglinear w   p ~ B^w * A^(1-w)                (product of experts, renormalised)
  gate t        B when max(B) >= t, otherwise the linear mix with the tuned w
The weight and threshold are chosen on the --tune splits only (lowest mean NLL against the soft target) and then
reported once on --test. Questions that either engine could not read (over-length) are scored with the other engine.
A paired bootstrap gives the 95% interval of the accuracy difference against B alone on --test.
"""
import argparse
import json
import math
import random
import statistics
from pathlib import Path


def load(path):
    out = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip():
            r = json.loads(line)
            out[(r['id'], r['qid'])] = r
    return out


def joined(a_path, b_path):
    a, b = load(a_path), load(b_path)
    rows = []
    for key in sorted(set(a) | set(b)):
        ra, rb = a.get(key), b.get(key)
        ref = next((r for r in (ra, rb) if r and not r.get('invalid')), None)
        if ref is None:
            continue  # neither engine read it: excluded from both
        rows.append({'key': key, 'type': ref['type'], 'keys': ref['keys'], 'target': ref['target'],
                     'a': None if not ra or ra.get('invalid') else ra['probabilities'],
                     'b': None if not rb or rb.get('invalid') else rb['probabilities']})
    return rows


def combine(row, method, w=0.5, t=0.0):
    a, b, keys = row['a'], row['b'], row['keys']
    if a is None or method == 'b':
        return b if b is not None else a
    if b is None or method == 'a':
        return a
    if method == 'gate':
        if max(b.values()) >= t:
            return b
        method = 'linear'
    if method == 'linear':
        return {k: w * b[k] + (1 - w) * a[k] for k in keys}
    logs = {k: w * math.log(max(b[k], 1e-12)) + (1 - w) * math.log(max(a[k], 1e-12)) for k in keys}
    top = max(logs.values())
    z = sum(math.exp(v - top) for v in logs.values())
    return {k: math.exp(logs[k] - top) / z for k in keys}


def metrics(rows, method, w=0.5, t=0.0):
    if not rows:
        return {'questions': 0}
    correct, nll, brier, conf = [], [], [], []
    for r in rows:
        p = combine(r, method, w, t)
        gold = max(r['target'], key=r['target'].get)
        top = max(p, key=p.get)
        correct.append(top == gold)
        conf.append(p[top])
        nll.append(-sum(r['target'][k] * math.log(max(p[k], 1e-12)) for k in r['keys']))
        brier.append(sum((p[k] - r['target'][k]) ** 2 for k in r['keys']))
    bins = [[] for _ in range(10)]
    for c, ok in zip(conf, correct):
        bins[min(9, int(c * 10))].append((c, ok))
    ece = sum(len(x) / len(rows) * abs(statistics.fmean(o for _, o in x) - statistics.fmean(c for c, _ in x)) for x in bins if x)
    return {'questions': len(rows), 'accuracy': statistics.fmean(correct), 'nll': statistics.fmean(nll),
            'brier': statistics.fmean(brier), 'ece_10_bins': ece, '_correct': correct}


def public(m):
    return {k: v for k, v in m.items() if not k.startswith('_')}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--a', required=True, help='Dump pattern of engine A (specialist), with {split}')
    p.add_argument('--b', required=True, help='Dump pattern of engine B (generalist), with {split}')
    p.add_argument('--tune', required=True, help='Comma-separated split names used to choose w and t')
    p.add_argument('--test', required=True, help='Split name reported once with the chosen settings')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--seed', type=int, default=1701)
    args = p.parse_args()
    tune = [r for s in args.tune.split(',') for r in joined(args.a.format(split=s), args.b.format(split=s))]
    test = joined(args.a.format(split=args.test), args.b.format(split=args.test))
    grid_w = [round(0.05 * i, 2) for i in range(21)]
    grid_t = [round(0.5 + 0.025 * i, 3) for i in range(21)]
    best = {}
    for method in ('linear', 'loglinear'):
        w = min(grid_w, key=lambda x: metrics(tune, method, x)['nll'])
        best[method] = {'w': w}
    w = best['linear']['w']
    best['gate'] = {'w': w, 't': min(grid_t, key=lambda x: metrics(tune, 'gate', w, x)['nll'])}
    settings = {'a': {}, 'b': {}, **best}
    report = {'a': args.a, 'b': args.b, 'tune_splits': args.tune, 'tune_questions': len(tune), 'test_split': args.test,
              'test_questions': len(test), 'chosen': best, 'tune': {}, 'test': {}, 'test_by_type': {}}
    for name, cfg in settings.items():
        method = name if name in ('a', 'b') else name
        report['tune'][name] = public(metrics(tune, method, **cfg))
        report['test'][name] = public(metrics(test, method, **cfg))
        report['test_by_type'][name] = {t: public(metrics([r for r in test if r['type'] == t], method, **cfg))
                                        for t in sorted({r['type'] for r in test})}
    base = metrics(test, 'b')['_correct']
    rng = random.Random(args.seed)
    for name, cfg in best.items():
        other = metrics(test, name, **cfg)['_correct']
        diffs = []
        for _ in range(2000):
            idx = [rng.randrange(len(base)) for _ in base]
            diffs.append(sum(other[i] - base[i] for i in idx) / len(idx))
        diffs.sort()
        report['test'][name]['accuracy_minus_b_95'] = [diffs[50], diffs[1949]]
    report['agreement_test'] = {'both_right': sum(1 for r, x, y in zip(test, metrics(test, 'a')['_correct'], base) if x and y),
                                'only_a_right': sum(1 for x, y in zip(metrics(test, 'a')['_correct'], base) if x and not y),
                                'only_b_right': sum(1 for x, y in zip(metrics(test, 'a')['_correct'], base) if y and not x),
                                'both_wrong': sum(1 for x, y in zip(metrics(test, 'a')['_correct'], base) if not x and not y)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('chosen', 'test', 'agreement_test')}, indent=2))


if __name__ == '__main__':
    main()
