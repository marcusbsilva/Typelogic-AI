"""Explicit online research step (never run by the app): pick a sample of typelogic-distill-v1-test2 and ask one teacher
model to declare, for each document, the fields a user would extract before answering its questions, with the values
written in the document. Values that are not written in the document (computed or invented) are dropped afterwards by
the same evidence check the app uses, so the "oracle" fields only restate facts of the text.

    OPENROUTER_API_KEY=... python research/needle-ab/design_fields.py --questions 100 --budget 2

Output: research/needle-ab/cases.json (documents, questions, targets, field definitions, oracle values).
"""
import argparse
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'research/distill'))
from typelogic import needle  # noqa: E402

MODEL = 'google/gemma-4-26b-a4b-it'   # non-reasoning teacher: short, cheap answers
PROMPT = """You prepare structured fields for a decision review. Below is a document and the questions a reviewer must answer about it.

Declare between 3 and 8 flat fields that capture the facts from the document a reviewer needs for these questions:
amounts, counts, dates, deadlines, durations, parties, roles, codes, actions, statuses.
Rules:
- Each field: "name" (lowercase snake_case, max 40 chars), "type" (one of: text, number, integer, date, boolean),
  "description" (short, specific: what exactly the field holds), "value" (the value as WRITTEN in the document).
- Only facts literally stated in the document. Never compute, add up, convert or infer a value, and never answer the questions.
- number/integer values are JSON numbers; date values are YYYY-MM-DD and only when the document states that date; text values are copied verbatim (short).
- If a needed fact is not stated, leave it out.
Reply with JSON only: {"fields": [{"name": ..., "type": ..., "description": ..., "value": ...}]}

DOCUMENT:
<<<
%s
>>>

QUESTIONS:
%s"""


def questions_text(qs):
    lines = []
    for qid, q in qs.items():
        lines.append(f'- {qid} ({q["type"]}): {q["instructions"]}')
    return '\n'.join(lines)


def design(row, budget):
    """Cached per document in research/needle-ab/work/designs; None when the teacher gives no valid JSON in 3 tries."""
    cache = ROOT / 'research/needle-ab/work/designs' / MODEL.replace('/', '_') / (row['id'] + '.json')
    if cache.is_file():
        return json.loads(cache.read_text(encoding='utf-8'))
    import orclient
    for attempt in range(3):   # the reasoning model sometimes spends the whole budget on reasoning: retry
        out = orclient.chat(MODEL, [{'role': 'user', 'content': PROMPT % (row['state'], questions_text(row['questions']))}], budget,
                            temperature=0, max_tokens=2000)
        text = out['choices'][0]['message'].get('content') or ''
        try:
            data = json.loads(text[text.index('{'):text.rindex('}') + 1])
            break
        except ValueError:
            if attempt == 2:
                return None
    fields, oracle = [], {}
    for f in data.get('fields', [])[:needle.MAX_FIELDS]:
        try:
            spec = needle.validate_fields([{k: f.get(k) for k in ('name', 'type', 'description')}])[0]
        except ValueError:
            continue
        if spec['name'] in oracle:
            continue
        value = f.get('value')
        if spec['type'] == 'integer' and isinstance(value, float) and value.is_integer():
            value = int(value)
        ok = needle._typed(value, spec['type']) and needle.evidence(value, spec['type'], row['state']) in ('in_text', 'inferred')
        fields.append(spec)
        oracle[spec['name']] = value if ok else None
    result = {'fields': fields, 'oracle': oracle, 'teacher': out.get('model'), 'cost': (out.get('usage') or {}).get('cost')}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--questions', type=int, default=100)
    p.add_argument('--budget', type=float, default=2.0, help='USD cap for this step (local ledger)')
    p.add_argument('--seed', type=int, default=20261025)
    p.add_argument('--out', type=Path, default=ROOT / 'research/needle-ab/cases.json')
    args = p.parse_args()
    import os
    os.environ.setdefault('TYPELOGIC_DISTILL_WORK', str(ROOT / 'research/needle-ab/work'))
    rows = [json.loads(l) for l in (ROOT / 'research/data/typelogic-distill-v1-test2/test.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
    rows = [r for r in rows if len(r['state']) <= needle.MAX_TEXT_CHARS]
    random.Random(args.seed).shuffle(rows)
    # documents in a fixed order until the sample reaches --questions; a document without a valid design is replaced by the next
    picked, count, start = [], 0, 0
    with ThreadPoolExecutor(2) as pool:
        while count < args.questions and start < len(rows):
            batch = []
            for r in rows[start:]:
                if count + sum(len(b['questions']) for b in batch) >= args.questions:
                    break
                batch.append(r)
            start += len(batch)
            for r, d in zip(batch, pool.map(lambda r: design(r, args.budget), batch)):
                if d and len(d['fields']) >= 2:
                    picked.append((r, d))
                    count += len(r['questions'])
    cases = []
    for r, d in picked:
        cases.append({'id': r['id'], 'language': r['language'], 'domain': r['domain'], 'state': r['state'],
                      'questions': r['questions'], 'targets': r['targets'], **d})
    args.out.write_text(json.dumps({'source': 'typelogic-distill-v1-test2', 'seed': args.seed, 'teacher': MODEL,
                                    'documents': len(cases), 'questions': sum(len(c['questions']) for c in cases),
                                    'cases': cases}, ensure_ascii=False, indent=1), encoding='utf-8')
    kept = sum(v is not None for c in cases for v in c['oracle'].values())
    total = sum(len(c['oracle']) for c in cases)
    print(f'{len(cases)} documents, {sum(len(c["questions"]) for c in cases)} questions, {total} fields, {kept} oracle values written in the text, '
          f'cost ${sum(c["cost"] or 0 for c in cases):.4f}', file=sys.stderr)


if __name__ == '__main__':
    main()
