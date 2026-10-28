"""Explicit, bounded OpenRouter JEV collection on public TRAIN records only."""
import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from typelogic.jev_distill import decide, MODEL, TeacherPayloadError
from typelogic.learning import file_sha256, training_record


def digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    # Keep malformed non-finite values inspectable without emitting nonstandard JSON.
    def clean(value):
        if isinstance(value,float) and not math.isfinite(value):return str(value)
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items()}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    temporary.write_text(json.dumps(clean(data),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)


def select(path, per_label, max_calls):
    chosen = []
    counts = {}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('split') != 'train' or row.get('source_id') not in ('banking77', 'clinc150'):
                raise ValueError('This collector accepts only audited public training sources')
            training_record(row)
            key = (row['source_id'], row['gold_label'])
            if counts.get(key, 0) >= per_label or len(chosen) >= max_calls:
                continue
            counts[key] = counts.get(key, 0) + 1
            chosen.append(row)
    return chosen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--train-file', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=10)
    parser.add_argument('--per-label', type=int, default=1)
    parser.add_argument('--budget-usd', type=float, default=.25)
    parser.add_argument('--execute', action='store_true', help='Authorize actual paid API calls')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 500 or not 1 <= args.per_label <= 10:
        raise ValueError('Invalid pilot size')
    if not math.isfinite(args.budget_usd) or not 0 < args.budget_usd <= 25:
        raise ValueError('Invalid budget')
    if args.resume and not args.execute:
        raise ValueError('--resume requires --execute')
    selected = select(args.train_file, args.per_label, args.max_calls)
    identity = {'model': MODEL, 'train_sha256': file_sha256(args.train_file),
                'ids': [r['id'] for r in selected], 'row_sha256': [digest(r) for r in selected]}
    if not args.execute:
        print(json.dumps({'dry_run': True, 'selected': len(selected), 'model': MODEL,
                          'max_calls': args.max_calls, 'budget_usd': args.budget_usd}, indent=2))
        return
    key = os.environ.get('OPENROUTER_API_KEY')
    if not key:
        raise ValueError('Set OPENROUTER_API_KEY in the research environment')
    if args.out.exists():
        if not args.resume or json.loads((args.out/'identity.json').read_text(encoding='utf-8')) != identity:
            raise ValueError('Output exists or dataset identity changed')
    else:
        args.out.mkdir(parents=True)
        (args.out/'identity.json').write_text(json.dumps(identity, indent=2), encoding='utf-8')
    planned = spent = completed = 0.
    for row in selected:
        destination = args.out/(digest(row)+'.json')
        if destination.exists():
            saved = json.loads(destination.read_text(encoding='utf-8'))
            if saved.get('row_sha256') != digest(row) or saved.get('id') != row['id']:
                raise ValueError('Stored response does not match the training input')
            spent += saved.get('cost_usd') or 0
            continue
        # One UTF-8 byte per token plus 4K overhead: conservative admission estimate.
        bytes_in = len(json.dumps({'state': row['state'], 'questions': row['questions']}, ensure_ascii=False).encode())
        estimate = (bytes_in + 4096) * .042 / 1_000_000
        if planned + spent + estimate > args.budget_usd:
            raise ValueError('Research admission budget reached; existing results preserved')
        planned += estimate
        rejected=args.out/'rejected'/(digest(row)+'.json')
        cached=None
        if rejected.exists():
            cached=json.loads(rejected.read_text(encoding='utf-8'))
            if cached.get('row_sha256')!=digest(row):raise ValueError('Cached rejection identity mismatch')
        try:
            if cached is not None:
                # Revalidate the preserved response; never charge another call automatically.
                result=decide(row,key,transport=lambda *unused:cached['response'])
                result['recovered_without_api_call']=True
            else:
                result = decide(row, key)
        except TeacherPayloadError as exc:
            if cached is None:
                write_json(rejected,{'row_sha256':digest(row),'id':row['id'],
                    'error':str(exc),'estimate_reserved_usd':estimate,'response':exc.response})
            raise ValueError('Response rejected and preserved for offline inspection: '+str(rejected)+'; '+str(exc)) from None
        result['row_sha256'] = digest(row)
        write_json(destination,result)
        spent += result.get('cost_usd') or 0
        completed += 1
        print(json.dumps({'collected': int(completed), 'selected': len(selected), 'reported_usd': spent}), flush=True)
    print(json.dumps({'collected_this_execution': int(completed), 'reported_usd': spent,
                      'estimate_reserved_usd': planned, 'out': str(args.out)}))


if __name__ == '__main__':
    main()
