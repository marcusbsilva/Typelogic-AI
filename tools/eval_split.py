"""Score a TypeLogic-format split (state, questions, targets) with the specialist or a generalist. No network except the
local Ollama when --ollama-model is used.

    python tools/eval_split.py --split research/data/typelogic-distill-v1/test.jsonl --system direct \
        --model-dir models/qwen35-4b --ollama-model crh225/plumb-4b --out research/results/split/plumb-distill-test.json

Accuracy is against the target's argmax; NLL and Brier against the full (soft) target. Rows that do not fit are counted
as invalid (wrong) and listed, never truncated.
"""
import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--split', type=Path, required=True)
    p.add_argument('--system', choices=('typelogic', 'direct'), required=True)
    p.add_argument('--model-dir', type=Path)
    p.add_argument('--ollama-model')
    p.add_argument('--device', default='cpu')
    p.add_argument('--dtype')
    p.add_argument('--readout', default='torch')
    p.add_argument('--max-state', type=int, default=2048)
    p.add_argument('--limit', type=int)
    p.add_argument('--orders', type=int, default=1, choices=(1, 2), help='direct: 2 = average the canonical and reversed option order')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--dump', type=Path, help='Also write one JSON line per question (keys, soft target, probabilities)')
    args = p.parse_args()
    from typelogic.core import validate
    if args.system == 'typelogic':
        from typelogic.engine import DecisionEngine
        from typelogic.profiles import active_model_dir
        engine = DecisionEngine(args.model_dir or active_model_dir(ROOT), readout=args.readout, device=args.device, max_state=args.max_state)
    else:
        from typelogic.direct import DirectEngine
        engine = DirectEngine(args.model_dir, device=args.device, dtype=args.dtype, ollama_model=args.ollama_model, orders=args.orders)
    engine.load()
    rows = [json.loads(l) for l in args.split.read_text(encoding='utf-8').splitlines() if l.strip()][:args.limit]
    scored, invalid, latency, dump = [], [], [], []
    started = time.monotonic()
    for i, row in enumerate(rows, 1):
        payload = {'state': row['state'], 'questions': row['questions'], 'permutation_check': False}
        try:
            _, questions, _ = validate(payload)
            t = time.perf_counter()
            answers = engine.evaluate(payload)['answers']
            latency.append(time.perf_counter() - t)
        except Exception as exc:
            invalid.append({'id': row.get('id'), 'error': f'{type(exc).__name__}: {str(exc)[:200]}', 'questions': len(row['questions'])})
            for qid, q in row['questions'].items():
                dump.append({'id': row.get('id'), 'qid': qid, 'type': q.get('type'), 'invalid': True})
            continue
        for q in questions:
            target = row['targets'][q['id']]
            if isinstance(target, list):                      # distribution in the question's key order
                target = dict(zip(q['keys'], map(float, target)))
            elif not isinstance(target, dict):                # a single gold key
                target = {k: float(k == str(target)) for k in q['keys']}
            if set(target) != set(q['keys']) or not math.isclose(sum(target.values()), 1.0, abs_tol=1e-6):
                raise SystemExit(f"{row.get('id')}/{q['id']}: target does not match the question's options")
            probs = answers[q['id']]['probabilities']
            gold = max(target, key=target.get)
            top = max(probs, key=probs.get)
            dump.append({'id': row.get('id'), 'qid': q['id'], 'type': q['type'], 'language': row.get('language'), 'keys': list(q['keys']),
                         'target': {k: target[k] for k in q['keys']}, 'probabilities': {k: probs[k] for k in q['keys']}})
            scored.append({'id': row.get('id'), 'qid': q['id'], 'type': q['type'], 'language': row.get('language'), 'source': row.get('source_id'),
                           'keys': list(q['keys']), 'probabilities': {k: probs[k] for k in q['keys']}, 'target': target,
                           'correct': top == gold, 'confidence': probs[top],
                           'nll': -sum(target[k] * math.log(max(probs[k], 1e-12)) for k in q['keys']),
                           'brier': sum((probs[k] - target[k]) ** 2 for k in q['keys'])})
        if i % 50 == 0:
            print(f'{i}/{len(rows)} ({time.monotonic() - started:.0f} s)', flush=True)

    def summary(items):
        if not items:
            return {'questions': 0}
        bins = [[] for _ in range(10)]
        for x in items:
            bins[min(9, int(x['confidence'] * 10))].append(x)
        ece = sum(len(b) / len(items) * abs(statistics.fmean(y['correct'] for y in b) - statistics.fmean(y['confidence'] for y in b)) for b in bins if b)
        return {'questions': len(items), 'accuracy': statistics.fmean(x['correct'] for x in items), 'nll': statistics.fmean(x['nll'] for x in items),
                'brier': statistics.fmean(x['brier'] for x in items), 'ece_10_bins': ece}
    invalid_questions = sum(x['questions'] for x in invalid)
    overall = summary(scored)
    overall['invalid_questions'] = invalid_questions
    overall['accuracy_strict'] = sum(x['correct'] for x in scored) / max(1, len(scored) + invalid_questions)
    report = {'split': str(args.split), 'system': args.system, 'model_dir': str(args.model_dir) if args.model_dir else None,
              'ollama_model': args.ollama_model, 'device': args.device, 'orders': args.orders,
              'engine': engine.health(), 'overall': overall,
              'by_type': {t: summary([x for x in scored if x['type'] == t]) for t in sorted({x['type'] for x in scored})},
              'by_language': {l: summary([x for x in scored if x['language'] == l]) for l in sorted({str(x['language']) for x in scored})},
              'by_source': {s: summary([x for x in scored if x['source'] == s]) for s in sorted({str(x['source']) for x in scored})},
              'latency_p50_s': sorted(latency)[len(latency) // 2] if latency else None, 'invalid': invalid[:50]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    prefix = json.dumps(str(ROOT) + ('\\' if '\\' in str(ROOT) else '/'))[1:-1]  # project-relative paths only
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False).replace(prefix, ''), encoding='utf-8')
    if args.dump:
        args.dump.parent.mkdir(parents=True, exist_ok=True)
        args.dump.write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in dump), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('overall', 'by_type', 'by_language')}, indent=2))


if __name__ == '__main__':
    main()
