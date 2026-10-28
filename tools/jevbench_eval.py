"""Run the public JevBench decisions (MIT, 231 items) against a local TypeLogic engine. No network.

Systems:
  typelogic  the trained Qwen3.5-2B adapter + pointer head (active profile unless --model-dir)
  direct     zero-shot direct option-letter logits of an instruction model (typelogic/direct.py);
             --model-dir is a local Hugging Face folder, or --gguf a llama.cpp file (with --model-dir for the tokenizer)

    python tools/jevbench_eval.py --system typelogic --device xpu --out research/results/jevbench/typelogic-2b-xpu
    python tools/jevbench_eval.py --system direct --model-dir models/qwen35-4b --device xpu --out research/results/jevbench/direct-4b-xpu

Metrics follow JevBench's conventions: accuracy with invalid answers counted wrong (strict) and over valid answers,
multi-class Brier, ECE over 10 top-probability bins, ordinal MAE of the expected level for score questions, and
p50/p95 latency of this process (model already loaded). Items whose state does not fit are reported as invalid,
never truncated.
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
DATA = ROOT / 'research/data/jevbench-public'
FILES = ['original', 'easy', 'hard']


def load_tasks(names, limit=None):
    tasks = []
    for name in names:
        for line in (DATA / f'{name}.jsonl').read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                row['tier'] = name
                tasks.append(row)
    return tasks[:limit] if limit else tasks


def payload(task):
    state = task['state'] if isinstance(task['state'], str) else json.dumps(task['state'], ensure_ascii=False, indent=1)
    return {'state': state, 'questions': {'q': task['question']}, 'permutation_check': False}


def label_probabilities(task, answer):
    """JevBench label -> probability, from a TypeLogic answer keyed by its own option keys."""
    p = answer['probabilities']
    if task['question']['type'] == 'noul':
        return {'yes': p['true'], 'no': p['false']}
    return {label: p[label] for label in task['labels']}


def wilson(k, n, z=1.96):
    if not n:
        return [0.0, 0.0]
    phat = k / n
    centre = (phat + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [centre - half, centre + half]


def summarize(rows):
    n = len(rows)
    valid = [r for r in rows if r['valid']]
    correct = sum(r['correct'] for r in rows)
    brier = [r['brier'] for r in valid]
    bins = [[] for _ in range(10)]
    for r in valid:
        bins[min(9, int(r['confidence'] * 10))].append(r)
    ece = sum(len(b) / len(valid) * abs(statistics.fmean(x['correct'] for x in b) - statistics.fmean(x['confidence'] for x in b))
              for b in bins if b) if valid else None
    ordinal = [r['ordinal_abs_error'] for r in valid if r.get('ordinal_abs_error') is not None]
    latency = sorted(r['latency_s'] for r in valid)
    return {'items': n, 'valid': len(valid), 'correct': correct, 'accuracy_strict': correct / n if n else None,
            'accuracy_strict_wilson_95': wilson(correct, n), 'accuracy_on_valid': correct / len(valid) if valid else None,
            'brier_on_valid': statistics.fmean(brier) if brier else None, 'ece_10_bins_on_valid': ece,
            'ordinal_mae': statistics.fmean(ordinal) if ordinal else None,
            'latency_p50_s': latency[len(latency) // 2] if latency else None,
            'latency_p95_s': latency[min(len(latency) - 1, int(.95 * len(latency)))] if latency else None}


def score(task, probabilities):
    labels = task['labels'] if task['question']['type'] != 'noul' else ['no', 'yes']
    expected = str(task['expected']) if task['question']['type'] == 'score' else task['expected']
    predicted = max(labels, key=lambda label: probabilities[label])
    out = {'predicted': predicted, 'correct': predicted == expected, 'confidence': probabilities[predicted],
           'brier': sum((probabilities[label] - (1.0 if label == expected else 0.0)) ** 2 for label in labels)}
    if task['question']['type'] == 'score':
        out['ordinal_abs_error'] = abs(sum(int(label) * probabilities[label] for label in labels) - int(expected))
    return out


def build_engine(args):
    if args.system == 'typelogic':
        from typelogic.engine import DecisionEngine
        from typelogic.profiles import active_model_dir
        return DecisionEngine(args.model_dir or active_model_dir(ROOT), readout=args.readout, device=args.device, max_state=args.max_state)
    from typelogic.direct import DirectEngine
    return DirectEngine(args.model_dir, device=args.device, dtype=args.dtype, gguf=args.gguf, max_tokens=args.max_tokens,
                        ollama_model=args.ollama_model, orders=args.orders)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--system', choices=('typelogic', 'direct'), required=True)
    p.add_argument('--model-dir', type=Path)
    p.add_argument('--gguf', type=Path)
    p.add_argument('--ollama-model', help='direct: tag of a model pulled in the local Ollama (loopback only)')
    p.add_argument('--device', default='cpu', choices=('cpu', 'xpu', 'auto'))
    p.add_argument('--dtype', choices=('float32', 'bfloat16', 'float16'))
    p.add_argument('--readout', default='torch', choices=('onnx', 'rust', 'torch'))
    p.add_argument('--max-state', type=int, default=2048, help='typelogic: state token limit (trained with 384)')
    p.add_argument('--max-tokens', type=int, default=4096, help='direct: prompt token limit')
    p.add_argument('--orders', type=int, default=1, choices=(1, 2), help='direct: 2 = average the canonical and reversed option order')
    p.add_argument('--tiers', default=','.join(FILES))
    p.add_argument('--limit', type=int)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    if args.system == 'direct' and not args.model_dir:
        p.error('--model-dir is required for --system direct (the tokenizer folder when --gguf is used)')
    tasks = load_tasks([t for t in args.tiers.split(',') if t], args.limit)
    args.out.mkdir(parents=True, exist_ok=False)
    engine = build_engine(args)
    started = time.monotonic()
    engine.load()
    load_seconds = time.monotonic() - started
    rows = []
    with (args.out / 'results.jsonl').open('x', encoding='utf-8') as out:
        for index, task in enumerate(tasks, 1):
            row = {'id': task['id'], 'tier': task['tier'], 'family': task['family'], 'type': task['question']['type'], 'expected': task['expected']}
            began = time.perf_counter()
            try:
                answer = engine.evaluate(payload(task))['answers']['q']
                row['latency_s'] = time.perf_counter() - began
                row['probabilities'] = label_probabilities(task, answer)
                row.update(score(task, row['probabilities']), valid=True)
            except Exception as exc:  # over-length state or an engine error: invalid, counted wrong
                row.update(valid=False, correct=False, error=f'{type(exc).__name__}: {str(exc)[:300]}', latency_s=time.perf_counter() - began)
            rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + '\n'); out.flush()
            if index % 20 == 0 or index == len(tasks):
                done = sum(r['correct'] for r in rows)
                print(f'{index}/{len(tasks)}  correct {done}  ({time.monotonic() - started:.0f} s)', flush=True)
    report = {'system': args.system, 'engine': engine.health(), 'load_seconds': load_seconds,
              'dataset': 'JevBench public (original 72 + easy 48 + hard 111), MIT, fstandhartinger/jevbench@609a664e',
              'overall': summarize(rows),
              'by_tier': {t: summarize([r for r in rows if r['tier'] == t]) for t in sorted({r['tier'] for r in rows})},
              'by_family': {f: summarize([r for r in rows if r['family'] == f]) for f in sorted({r['family'] for r in rows})},
              'by_type': {t: summarize([r for r in rows if r['type'] == t]) for t in sorted({r['type'] for r in rows})},
              'errors': sorted({r['error'].split(':')[0] for r in rows if not r['valid']})}
    prefix = json.dumps(str(ROOT) + ('\\' if '\\' in str(ROOT) else '/'))[1:-1]  # the project folder as it appears inside JSON text
    text = json.dumps(report, indent=2, ensure_ascii=False).replace(prefix, '')
    (args.out / 'summary.json').write_text(text, encoding='utf-8')  # project-relative paths only
    print(json.dumps({'overall': report['overall'], 'by_tier': {k: v['accuracy_strict'] for k, v in report['by_tier'].items()}}, indent=2))


if __name__ == '__main__':
    main()
