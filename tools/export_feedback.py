"""Export corrected decisions as training rows (JSON Lines) for tools/build_training_mix.py --feedback. No network.

    python tools/export_feedback.py --out research/data/feedback-2026-11.jsonl          (all workspaces)
    python tools/run_app.py training-export --all-spaces --output research/data/feedback-2026-11.jsonl   (same rows)

Without --space this tool exports every workspace; the CLI command exports --space (default "default") unless
--all-spaces is given.

Corrections recorded in the interface or CLI are the targets of their questions. Check the labels before training;
nothing is retrained here.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--db', type=Path, default=Path.home() / '.typelogic-ai/typelogic.sqlite3')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--space', help='Only this workspace (default: all)')
    args = p.parse_args()
    if not args.db.is_file() or args.out.exists():
        raise SystemExit('Database missing or output already exists')
    from typelogic.store import Store
    store = Store(args.db)
    try:
        result = store.training_records(args.space)
    finally:
        store.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as out:
        for row in result['records']:
            out.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'records'} | {'records': len(result['records'])}))


if __name__ == '__main__':
    main()
