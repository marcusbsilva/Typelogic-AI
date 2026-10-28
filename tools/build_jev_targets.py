"""Build a separate 2B training JSONL from audited JEV TRAIN outputs."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from typelogic.jev_distill import blend_training_record, MODEL
from typelogic.learning import file_sha256
from collect_jev_intents import digest


def build(train, teacher_dir, out, weight):
    identity = json.loads((teacher_dir/'identity.json').read_text(encoding='utf-8'))
    if identity['model'] != MODEL or identity['train_sha256'] != file_sha256(train):
        raise ValueError('JEV response source does not match training data')
    if out.exists():
        raise ValueError('Output already exists')
    entries = {}
    for filename in teacher_dir.glob('*.json'):
        if filename.name == 'identity.json':
            continue
        entry = json.loads(filename.read_text(encoding='utf-8'))
        if (entry.get('row_sha256') != filename.stem or
                not (entry.get('model') == MODEL or entry.get('model', '').startswith(MODEL + '-2026'))):
            raise ValueError('Unexpected teacher response: ' + filename.name)
        if filename.stem not in identity['row_sha256']:
            raise ValueError('Teacher response is outside selected pilot')
        if filename.stem in entries:
            raise ValueError('Duplicate teacher response')
        entries[filename.stem] = entry
    if not entries:
        raise ValueError('No verified JEV responses')
    used = set();review = [];count = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.with_suffix(out.suffix+'.tmp')
    if stage.exists():
        raise ValueError('Temporary output exists')
    try:
        with train.open(encoding='utf-8') as source, stage.open('w', encoding='utf-8') as target:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line); key = digest(row)
                if key in entries:
                    item = entries[key]
                    if item['id'] != row['id'] or item['split'] != 'train' or item['source_id'] != row['source_id']:
                        raise ValueError('Teacher response provenance mismatch')
                    new = blend_training_record(row, item['probabilities'], weight)
                    if new['distillation']['needs_review']:
                        review.append(row['id'])
                    else:
                        count += 1
                    used.add(key)
                else:
                    new = row
                target.write(json.dumps(new, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n')
        if used != set(entries):
            raise ValueError('A response does not correspond to a training row')
        stage.replace(out)
    finally:
        stage.unlink(missing_ok=True)
    report = {'train_sha256': identity['train_sha256'], 'teacher_model': MODEL,
              'teacher_records': len(entries), 'blended_agreeing': count,
              'disagreement_gold_retained': len(review), 'review_ids': review,
              'new_train_sha256': file_sha256(out), 'teacher_weight': weight}
    out.with_suffix(out.suffix+'.report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--train-file', type=Path, required=True)
    p.add_argument('--teacher-dir', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--teacher-weight', type=float, default=.3)
    a = p.parse_args()
    print(json.dumps(build(a.train_file, a.teacher_dir, a.out, a.teacher_weight), indent=2))


if __name__ == '__main__':
    main()
