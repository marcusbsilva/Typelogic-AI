"""Mix the distilled typed-decision set with a replay of the BANKING77 catalog set (keeps the specialist's catalog skill).

    python tools/build_training_mix.py --distill research/data/typelogic-distill-v1 \
        --replay-train research/data/jev-banking77-catalog-v2-audited/train.jsonl \
        --replay-splits research/data/intent-choice-en-v2 --out research/data/typelogic-mix-v1

Split rows keep their group_id; replay rows are sampled with a fixed seed. Nothing is overwritten.
--feedback adds reviewed decisions exported from the app (tools/export_feedback.py or `training-export`): one in ten
decisions (by a hash of its id) goes to validation, the rest to training; none goes to calibration or test.
"""
import argparse
import hashlib
import json
import random
from pathlib import Path


def read(path):
    return [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--distill', type=Path, required=True)
    p.add_argument('--replay-train', type=Path, required=True)
    p.add_argument('--replay-splits', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--replay-ratio', type=float, default=1.0, help='Replay rows per distilled training row')
    p.add_argument('--eval-replay', type=int, default=300, help='Replay rows added to validation/calibration/test each')
    p.add_argument('--seed', type=int, default=1701)
    p.add_argument('--tokenizer', type=Path, help='Backbone folder; drops distilled rows whose state exceeds --max-state tokens')
    p.add_argument('--max-state', type=int, default=1536)
    p.add_argument('--max-branch', type=int, default=4096)
    p.add_argument('--feedback', type=Path, action='append', default=[], help='Training rows exported from the app (repeatable)')
    args = p.parse_args()
    if args.out.exists():
        raise SystemExit(f'{args.out} exists; choose a new folder')
    rng = random.Random(args.seed)
    distill = {name: read(args.distill / f'{name}.jsonl') for name in ('train', 'validation', 'calibration', 'test')}
    dropped = {}
    feedback = [r for path in args.feedback for r in read(path)]
    if feedback:
        def held_out(row):
            return int(hashlib.sha256(str(row['group_id']).encode()).hexdigest(), 16) % 10 == 0
        distill['validation'] += [r for r in feedback if held_out(r)]
        distill['train'] += [r for r in feedback if not held_out(r)]
    if args.tokenizer:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from typelogic.learning import training_record
        from typelogic.vendor.pointer import load_tokenizer, encode, ContextOverflow
        tok = load_tokenizer(str(args.tokenizer))
        for name, rows in distill.items():
            kept = []
            for row in rows:
                try:
                    encode(tok, training_record(row), max_state=args.max_state, max_branch=args.max_branch, strict=True)
                    kept.append(row)
                except (ContextOverflow, ValueError):
                    pass
            dropped[name] = len(rows) - len(kept)
            distill[name] = kept
    replay_train = read(args.replay_train)
    rng.shuffle(replay_train)
    mix = {'train': distill['train'] + replay_train[:int(len(distill['train']) * args.replay_ratio)]}
    for name in ('validation', 'calibration', 'test'):
        rows = read(args.replay_splits / f'{name}.jsonl')
        rng.shuffle(rows)
        mix[name] = distill[name] + rows[:args.eval_replay]
    args.out.mkdir(parents=True)
    manifest = {'distill': str(args.distill), 'replay_train': str(args.replay_train), 'replay_splits': str(args.replay_splits),
                'replay_ratio': args.replay_ratio, 'eval_replay': args.eval_replay, 'seed': args.seed,
                'max_state': args.max_state, 'dropped_over_length': dropped, 'feedback': [str(x) for x in args.feedback],
                'feedback_rows': len(feedback), 'splits': {}}
    for name, rows in mix.items():
        rng.shuffle(rows)
        path = args.out / f'{name}.jsonl'
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
        manifest['splits'][name] = {'rows': len(rows), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                    'distilled': sum(r.get('source_id') == 'typelogic-distill-v1' for r in rows),
                                    'feedback': sum(r.get('source_id') == 'typelogic-feedback' for r in rows)}
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
