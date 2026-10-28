"""Fit the temperature of the generalist (direct readout) on labeled typed decisions, and evaluate it. No network.

Input rows are Open-Jev records (https://huggingface.co/datasets/ZefanCai/Open-Jev, CC0-1.0; .jsonl or .jsonl.gz
with state_json/question/kind/options/target) or TypeLogic training records (state/questions/targets; every question
of a row is used, soft targets are scored against their most probable option). Only questions with 2-16 options are
used. The temperature minimises NLL on --fit rows; --eval rows are scored with the fitted value. --orders 2 fits the
two-order averaged readout; --per-type also fits one temperature per question type (types with fewer than
--min-type-rows rows keep the global value). The result goes to
<model-dir>/direct-calibration-<backend>[-<model>][-avg2].json (never overwritten).

    python tools/calibrate_direct.py --model-dir models/qwen35-4b --device xpu \
        --fit research/data/open-jev/calibration.jsonl.gz --eval research/data/open-jev/test.jsonl.gz --limit 1500
"""
import argparse
import gzip
import hashlib
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read_rows(path):
    opener = gzip.open if str(path).endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def as_payloads(row):
    """[(payload with one question, gold option id, source)] for one input row."""
    if 'kind' in row:  # Open-Jev
        target, options, kind = row['target'], row['options'], row['kind']
        if not options or max(target) < 1 or len(options) != len(target) or not 2 <= len(options) <= 16:
            return None
        state = row.get('state_json')
        state = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        if kind == 'noul':
            question = {'type': 'noul', 'instructions': row['question']}
            gold = 'true' if options[target.index(1.0)] == 'yes' else 'false'
        elif kind == 'score':
            question = {'type': 'score', 'instructions': row['question'], 'criteria': list(options)}
            gold = str(target.index(1.0))
        else:
            criteria = {}
            for option in options:
                key, _, text = option.partition(': ')
                criteria[key] = text or key
            if len(criteria) != len(options):
                return None
            question = {'type': 'choice', 'instructions': row['question'], 'criteria': criteria}
            gold = list(criteria)[target.index(1.0)]
        return [({'state': state, 'questions': {'q': question}}, gold, row.get('source', 'open-jev'))]
    out = []
    for qid, question in (row.get('questions') or {}).items():
        target = row['targets'][qid]
        if isinstance(target, dict):
            target = max(target, key=target.get)
        out.append(({'state': row['state'], 'questions': {'q': question}}, str(target).lower() if question['type'] == 'noul' else str(target),
                    row.get('source_id', 'typelogic')))
    return out


def collect(engine, path, limit, seed, sources=None):
    from typelogic.core import validate
    from typelogic.direct import options_of, view_orders
    rows = [x for r in read_rows(path) for x in (as_payloads(r) or []) if x and (not sources or x[2].startswith(tuple(sources)))]
    random.Random(seed).shuffle(rows)
    rows = rows[:limit] if limit else rows
    print(f'{path.name}: {len(rows)} rows after filtering', flush=True)
    out = []
    started = time.monotonic()
    for i, (payload, gold, source) in enumerate(rows, 1):
        try:
            state, questions, _ = validate(payload)
            q = questions[0]
            views, canonical = [], None
            for order in view_orders(len(options_of(q)[0]), engine.orders):
                ids, option_ids, _ = engine.encode(state, q, order)
                logits = dict(zip(option_ids, engine.logits(ids, len(option_ids))))
                canonical = canonical or option_ids
                views.append([logits[k] for k in canonical])
        except ValueError as exc:  # over-length prompt or unsupported option count: skipped and counted
            out.append({'skipped': str(exc)[:120]})
            continue
        if gold not in canonical:
            out.append({'skipped': 'gold not among options'})
            continue
        out.append({'logits': views[0], 'views': views, 'gold': canonical.index(gold), 'type': q['type'], 'source': source})
        if i % 200 == 0:
            print(f'{path.name}: {i}/{len(rows)} ({time.monotonic() - started:.0f} s)', flush=True)
    return out


def probabilities(row, temperature):
    """Option-wise mean of the per-order softmaxes (one view = the plain single-order readout)."""
    from typelogic.direct import softmax
    views = [softmax(v, temperature) for v in row.get('views') or [row['logits']]]
    return [sum(v[i] for v in views) / len(views) for i in range(len(views[0]))]


def metrics(rows, temperature, by_type=None):
    scored = [r for r in rows if 'logits' in r]
    if not scored:
        return {'rows': 0}
    nll, correct, brier, conf = [], [], [], []
    for r in scored:
        p = probabilities(r, (by_type or {}).get(r['type'], temperature))
        top = max(range(len(p)), key=p.__getitem__)
        nll.append(-math.log(max(p[r['gold']], 1e-12)))
        correct.append(top == r['gold'])
        conf.append(p[top])
        brier.append(sum((pi - (1.0 if i == r['gold'] else 0.0)) ** 2 for i, pi in enumerate(p)))
    bins = [[] for _ in range(10)]
    for c, ok in zip(conf, correct):
        bins[min(9, int(c * 10))].append((c, ok))
    ece = sum(len(b) / len(scored) * abs(statistics.fmean(x[1] for x in b) - statistics.fmean(x[0] for x in b)) for b in bins if b)
    return {'rows': len(scored), 'skipped': len(rows) - len(scored), 'accuracy': statistics.fmean(correct), 'nll': statistics.fmean(nll),
            'brier': statistics.fmean(brier), 'ece_10_bins': ece}


def fit(rows):
    grid = [round(0.25 + 0.05 * i, 2) for i in range(116)]  # 0.25 .. 6.0
    best = min(grid, key=lambda t: metrics(rows, t)['nll'])
    return best, best in (grid[0], grid[-1])


def fit_by_type(rows, minimum):
    """{type: temperature} for types with enough rows and an interior optimum."""
    out = {}
    for kind in sorted({r['type'] for r in rows if 'logits' in r}):
        part = [r for r in rows if r.get('type') == kind]
        if len(part) >= minimum:
            value, boundary = fit(part)
            if not boundary:
                out[kind] = value
    return out


def model_identity(model_dir):
    files = {}
    for path in sorted(Path(model_dir).iterdir()):
        if path.is_file() and path.suffix in ('.json', '.safetensors'):
            if path.suffix == '.safetensors':
                files[path.name] = {'bytes': path.stat().st_size}
            else:
                files[path.name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return files


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--model-dir', type=Path, required=True)
    p.add_argument('--device', default='cpu')
    p.add_argument('--dtype')
    p.add_argument('--ollama-model')
    p.add_argument('--fit', type=Path, required=True)
    p.add_argument('--eval', type=Path)
    p.add_argument('--limit', type=int, default=1500, help='Rows per split after filtering (0 = all)')
    p.add_argument('--sources', help='Comma-separated source prefixes to keep (e.g. workflow-controls-v1,customer-control-v1)')
    p.add_argument('--seed', type=int, default=2026)
    p.add_argument('--report', type=Path, help='Write the JSON report here as well')
    p.add_argument('--dry-run', action='store_true', help='Do not write direct-calibration.json')
    p.add_argument('--orders', type=int, default=1, choices=(1, 2), help='2 = fit the two-order averaged readout')
    p.add_argument('--per-type', action='store_true', help='Also fit one temperature per question type')
    p.add_argument('--min-type-rows', type=int, default=60)
    args = p.parse_args()
    from typelogic.direct import DirectEngine
    engine = DirectEngine(args.model_dir, device=args.device, dtype=args.dtype, ollama_model=args.ollama_model, orders=args.orders)
    engine.temperature, engine.temperature_by_type = 1.0, {}
    engine.load()
    sources = [x for x in (args.sources or '').split(',') if x]
    fit_rows = collect(engine, args.fit, args.limit, args.seed, sources)
    temperature, boundary = fit(fit_rows)
    by_type = fit_by_type(fit_rows, args.min_type_rows) if args.per_type else {}
    report = {'sources': sources or 'all', 'model_dir': str(args.model_dir), 'backend': engine.backend, 'device': engine.device, 'dtype': engine.dtype,
              'orders': args.orders, 'fit_file': args.fit.name, 'temperature': temperature, 'temperature_by_type': by_type, 'grid_boundary': boundary,
              'fit': {'raw': metrics(fit_rows, 1.0), 'calibrated': metrics(fit_rows, temperature)}}
    if by_type:
        report['fit']['calibrated_by_type'] = metrics(fit_rows, temperature, by_type)
    if args.eval:
        eval_rows = collect(engine, args.eval, args.limit, args.seed + 1, sources)
        report['eval_file'] = args.eval.name
        report['eval'] = {'raw': metrics(eval_rows, 1.0), 'calibrated': metrics(eval_rows, temperature)}
        if by_type:
            report['eval']['calibrated_by_type'] = metrics(eval_rows, temperature, by_type)
        report['eval_types'] = {t: {'raw': metrics([r for r in eval_rows if r.get('type') == t], 1.0),
                                    'calibrated': metrics([r for r in eval_rows if r.get('type') == t], temperature, by_type)}
                                for t in sorted({r['type'] for r in eval_rows if 'type' in r})}
        sources = sorted({r['source'] for r in eval_rows if 'source' in r})
        report['eval_by_source'] = {s: metrics([r for r in eval_rows if r.get('source') == s], temperature, by_type) for s in sources}
    print(json.dumps({k: v for k, v in report.items() if k != 'eval_by_source'}, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    if args.dry_run or boundary:
        return 0 if args.dry_run else 1
    from typelogic.direct import calibration_name
    destination = args.model_dir / calibration_name(engine.backend, engine.ollama_model, engine.gguf, args.orders)
    if destination.exists():
        raise SystemExit(f'{destination} exists; remove it explicitly to replace it')
    value = {'temperature': temperature, 'orders': args.orders, 'fit_file': args.fit.name, 'fit_rows': report['fit']['raw']['rows'], 'metrics': report,
             'sources': sources or 'all', 'backend': engine.backend, 'model_files': model_identity(args.model_dir),
             'scope': 'Temperature fitted on held-out typed decisions; not a guarantee for your domain.'}
    if by_type:
        value['temperature_by_type'] = by_type
    with destination.open('x', encoding='utf-8') as out:
        json.dump(value, out, indent=2)
    print(json.dumps({'written': str(destination), 'temperature': temperature}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
