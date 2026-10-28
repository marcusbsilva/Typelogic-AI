"""Assemble the TypeLogic distillation set (step 3): teacher consensus, soft targets, decontamination, document splits."""
import collections
import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = Path(os.environ.get('TYPELOGIC_DISTILL_WORK', ROOT / 'research/distill/work'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from typelogic.core import validate
from typelogic.learning import training_record

RAW = WORK / 'raw'
OUT = ROOT / 'research/data/typelogic-distill-v1'
TEST2 = len(sys.argv) > 1 and sys.argv[1] == 'test2'   # documents generated after v1 was frozen -> one extra test split
if TEST2:
    V1_IDS = {__import__('json').loads(l)['id'] for n in ('train', 'validation', 'calibration', 'test')
              for l in (OUT / f'{n}.jsonl').read_text(encoding='utf-8').splitlines()}
    V1_SEEN = {__import__('json').loads(l)['id'] for l in (WORK / 'raw_v1_snapshot/generated.jsonl').read_text(encoding='utf-8').splitlines()}
    OUT = ROOT / 'research/data/typelogic-distill-v1-test2'
AUTHOR_WEIGHT = 1.0
MIN_AGREE = 3          # votes (author + teachers) that must agree with the consensus answer
MIN_VOTERS = 3


def ngrams(text, n=8):
    words = re.findall(r'\w+', text.lower())
    return {' '.join(words[i:i + n]) for i in range(len(words) - n + 1)}


def bench_ngrams():
    grams = set()
    for p in (ROOT / 'research/data/jevbench-public').glob('*.jsonl'):
        for line in p.read_text(encoding='utf-8').splitlines():
            r = json.loads(line)
            state = r['state'] if isinstance(r['state'], str) else json.dumps(r['state'])
            grams |= ngrams(state + ' ' + r['question'].get('instructions', ''))
    return grams


def main():
    if not TEST2 and (OUT / 'train.jsonl').exists():
        raise SystemExit(f'{OUT} is frozen; use "assemble.py test2" for documents generated later')
    generated = {json.loads(l)['id']: json.loads(l) for l in (RAW / 'generated.jsonl').read_text(encoding='utf-8').splitlines()}
    labels = collections.defaultdict(dict)
    for line in (RAW / 'labels.jsonl').read_text(encoding='utf-8').splitlines():
        r = json.loads(line)
        if r['distribution']:
            labels[(r['id'], r['qid'])][r['teacher']] = r
    bench = bench_ngrams()
    stats = collections.Counter()
    rows = []
    for doc_id, g in generated.items():
        if TEST2 and (doc_id in V1_SEEN or doc_id in V1_IDS):
            continue
        if len(ngrams(g['state']) & bench) > 2:
            stats['dropped_contaminated_documents'] += 1
            continue
        questions, targets, provenance = {}, {}, {}
        for qid, spec in g['questions'].items():
            stats['questions_seen'] += 1
            votes = labels.get((doc_id, qid), {})
            _, qs, _ = validate({'state': g['state'], 'questions': {qid: spec}})
            keys = qs[0]['keys']
            if len(votes) + 1 < MIN_VOTERS:
                stats['dropped_few_teachers'] += 1
                continue
            mass = {k: 0.0 for k in keys}
            argmaxes = []
            author = g['author_answers'][qid]
            mass[author] += AUTHOR_WEIGHT
            argmaxes.append(author)
            for teacher, v in votes.items():
                ids = v['option_ids']
                for k, p in zip(ids, v['distribution']):
                    mass[k] += p
                argmaxes.append(ids[max(range(len(ids)), key=v['distribution'].__getitem__)])
            total = sum(mass.values())
            soft = {k: mass[k] / total for k in keys}
            consensus = max(soft, key=soft.get)
            agree = sum(a == consensus for a in argmaxes)
            if consensus != author or agree < MIN_AGREE:
                stats['dropped_disagreement'] += 1
                continue
            questions[qid] = spec
            targets[qid] = soft
            provenance[qid] = {'author': g['generator'], 'teachers': {t: v['returned'] for t, v in votes.items()},
                               'agreement': f'{agree}/{len(argmaxes)}', 'skill': g['meta'][qid].get('skill')}
            stats['questions_kept'] += 1
        if not questions:
            continue
        row = {'id': doc_id, 'group_id': doc_id, 'state': g['state'], 'questions': questions, 'targets': targets,
               'language': 'pt' if g['language'].startswith('pt') else 'en', 'source_id': 'typelogic-distill-v1',
               'domain': g.get('domain'), 'provenance': provenance,
               'license': 'Synthetic, generated and labelled with OpenRouter models (see provenance); check each provider\'s terms before redistribution.'}
        training_record(row)
        rows.append(row)
    random.Random(1701).shuffle(rows)
    n = len(rows)
    cut = [int(n * .85), int(n * .90), int(n * .95)]
    splits = {'test': rows} if TEST2 else {'train': rows[:cut[0]], 'validation': rows[cut[0]:cut[1]], 'calibration': rows[cut[1]:cut[2]], 'test': rows[cut[2]:]}
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {'name': OUT.name, 'note': 'Extra held-out test: documents generated after typelogic-distill-v1 was frozen; none of them is in v1.' if TEST2 else None, 'documents': n, 'stats': dict(stats), 'splits': {}}
    for name, part in splits.items():
        path = OUT / f'{name}.jsonl'
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in part), encoding='utf-8')
        manifest['splits'][name] = {'documents': len(part), 'questions': sum(len(r['questions']) for r in part),
                                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                    'by_type': dict(collections.Counter(q['type'] for r in part for q in r['questions'].values())),
                                    'by_language': dict(collections.Counter(r['language'] for r in part))}
    manifest['decontamination'] = 'Documents sharing more than two 8-word sequences with the public JevBench items are dropped.'
    manifest['consensus'] = f'Author answer + teacher votes; kept when the soft consensus equals the author answer and at least {MIN_AGREE} voters agree.'
    (OUT / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
