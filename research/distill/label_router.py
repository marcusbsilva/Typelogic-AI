"""Label a fixed sample of generated questions with typesafe/jev-router (the Jev family); records the routed model."""
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get('TYPELOGIC_DISTILL_WORK', ROOT / 'research/distill/work'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orclient import chat, spent

from typelogic.core import validate
from typelogic.direct import LETTERS, messages, options_of

RAW = (WORK / 'raw/generated.jsonl')
OUT = (WORK / 'raw/labels.jsonl')
BUDGET = float(sys.argv[1])
COUNT = int(sys.argv[2])
TEACHER = 'typesafe/jev-router'


def main():
    rows = [json.loads(l) for l in RAW.read_text(encoding='utf-8').splitlines()]
    items = [(r, qid, spec) for r in rows for qid, spec in r['questions'].items()]
    random.Random(7).shuffle(items)
    done = {(json.loads(l)['id'], json.loads(l)['qid']) for l in OUT.read_text().splitlines() if json.loads(l)['teacher'] == TEACHER}
    ok = bad = 0
    with OUT.open('a', encoding='utf-8') as f:
        for row, qid, spec in items[:COUNT]:
            if (row['id'], qid) in done:
                continue
            try:
                state, questions, _ = validate({'state': row['state'], 'questions': {qid: spec}})
                q = questions[0]
                option_ids, descriptions = options_of(q)
                out = chat(TEACHER, messages(state, q['instr'], descriptions), BUDGET, max_tokens=400, temperature=0)
                text = (out['choices'][0]['message'].get('content') or '').strip().strip('"*').upper()
                letter = text[:1] if text[:1] in LETTERS[:len(option_ids)] else None
                dist = [1.0 if l == letter else 0.0 for l in LETTERS[:len(option_ids)]] if letter else None
                rec = {'id': row['id'], 'qid': qid, 'teacher': TEACHER, 'returned': out.get('model'), 'option_ids': option_ids,
                       'distribution': dist, 'how': 'letter' if dist else 'unparsed', 'cost': (out.get('usage') or {}).get('cost')}
                f.write(json.dumps(rec, ensure_ascii=False) + '\n'); f.flush()
                ok += bool(dist); bad += not dist
            except Exception as exc:
                bad += 1
                print('error', str(exc)[:200], flush=True)
                if 'budget exhausted' in str(exc):
                    break
    print(f'router: {ok} parsed, {bad} unparsed/failed, total spent ${spent():.3f}')


if __name__ == '__main__':
    main()
