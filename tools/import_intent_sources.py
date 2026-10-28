"""Import immutable intent sources into an auditable intermediate corpus (stdlib only)."""
import argparse
import copy
import csv
import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ALLOWED = {'banking77': 'CC-BY-4.0', 'clinc150': 'CC-BY-3.0'}
CLINC_SPLITS = ('train', 'oos_train', 'val', 'oos_val', 'test', 'oos_test')


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_file(path, entry):
    data = path.read_bytes()
    if len(data) != entry['bytes'] or digest(data) != entry['sha256']:
        raise ValueError('Source integrity mismatch: ' + path.name)
    return data


def partition(split):
    return split.removeprefix('oos_')


def make_record(sid, source, split, index, text, label):
    if source['license'] != ALLOWED.get(sid):
        raise ValueError('Unapproved source license: ' + sid)
    if not isinstance(text, str) or not normalize(text) or not isinstance(label, str) or not label:
        raise ValueError('Empty or invalid text/label')
    key = digest(normalize(text).encode('utf-8'))
    return {'id': f'{sid}:{split}:{index}', 'source_id': sid,
            'source_revision': source['revision'], 'source_record_id': f'{split}:{index}',
            'source_split': split, 'language': source['language'],
            'origin_kind': source['origin_kind'], 'license': source['license'],
            'task': 'intent_classification', 'state': text, 'gold_label': label,
            'group_id': key, 'is_oos': sid == 'clinc150' and label == 'oos'}


def parse_clinc(data, source):
    if set(data) != set(CLINC_SPLITS):
        raise ValueError('Unexpected CLINC partitions')
    for split in CLINC_SPLITS:
        if not isinstance(data[split], list):
            raise ValueError('Invalid CLINC split: ' + split)
        for item in data[split]:
            if not isinstance(item, list) or len(item) != 2 or not all(isinstance(v, str) and v.strip() for v in item):
                raise ValueError('Invalid CLINC record in ' + split)
    labels = {label for _, label in data['train']}
    if 'oos' in labels:
        raise ValueError('OOS in in-scope train')
    rows = []
    for split in CLINC_SPLITS:
        for i, (text, label) in enumerate(data[split]):
            if (split.startswith('oos_') and label != 'oos') or (not split.startswith('oos_') and label not in labels):
                raise ValueError('Unexpected CLINC label: ' + label)
            rows.append(make_record('clinc150', source, split, i, text, label))
    return rows


def load_exposures(directories):
    exposures = defaultdict(list)
    inventory = []
    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError('Missing exposure directory: ' + str(directory))
        for path in sorted(directory.rglob('*.jsonl')):
            name = path.relative_to(directory).as_posix()
            # Unknown previous roles are conservatively blocked for training.
            role = 'heldout' if any(s in name.lower() for s in ('test', 'validation', 'calibration', 'regression')) else ('train' if 'train' in name.lower() else 'unknown')
            data = path.read_bytes()
            count = 0
            for line in data.decode('utf-8-sig').splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                state = record.get('state')
                if not isinstance(state, str):
                    raise ValueError('Exposure record requires text state: ' + str(path))
                exposures[normalize(state)].append({'file': directory.name + '/' + name, 'role': role})
                count += 1
            inventory.append({'file': directory.name + '/' + name, 'sha256': digest(data), 'records': count, 'role': role})
    return dict(exposures), inventory


def audit(records, exposures):
    rows = copy.deepcopy(records)
    groups = defaultdict(list)
    for r in rows:
        r['exclusion_reasons'] = []
        r['prior_exposure'] = exposures.get(normalize(r['state']), [])
        # Import-only: neither fresh evaluation nor deployment has been certified.
        r['fresh_evaluation_eligible'] = False
        groups[r['group_id']].append(r)
    cross = conflicts = duplicates = 0
    for group in groups.values():
        split_set = {partition(r['source_split']) for r in group}
        is_cross = len(split_set) > 1
        if is_cross:
            cross += 1
        by_source = defaultdict(set)
        for r in group:
            by_source[r['source_id']].add(r['gold_label'])
        conflicting = {s for s, labels in by_source.items() if len(labels) > 1}
        conflicts += bool(conflicting)
        seen = set()
        for r in group:
            reasons = r['exclusion_reasons']
            if is_cross:
                reasons.append('cross_split_exact')
            if r['source_id'] in conflicting:
                reasons.append('conflicting_gold')
            identity = (r['source_id'], r['source_split'], r['gold_label'])
            if identity in seen:
                reasons.append('same_split_duplicate')
                duplicates += 1
            seen.add(identity)
            if any(e['role'] != 'train' for e in r['prior_exposure']):
                reasons.append('prior_heldout')
    # Bounded, explicitly non-exhaustive lexical review heuristic.
    # Inspect neighboring normalized texts; never claim semantic decontamination.
    ordered = sorted(groups.values(), key=lambda g: normalize(g[0]['state']))
    near_pairs = []
    for left, right in zip(ordered, ordered[1:]):
        a, b = normalize(left[0]['state']), normalize(right[0]['state'])
        if min(len(a.split()), len(b.split())) < 5:
            continue
        if len({partition(r['source_split']) for r in left + right}) < 2:
            continue
        score = SequenceMatcher(None, a, b, autojunk=False).ratio()
        if score >= 0.92:
            near_pairs.append({'left': left[0]['id'], 'right': right[0]['id'], 'similarity': score})
            for r in left + right:
                if 'near_duplicate_review' not in r['exclusion_reasons']:
                    r['exclusion_reasons'].append('near_duplicate_review')
    for r in rows:
        r['training_candidate'] = partition(r['source_split']) == 'train' and not r['exclusion_reasons']
    report = {'records': len(rows), 'cross_split_exact_groups': cross,
              'conflicting_gold_groups': conflicts, 'same_split_duplicate_records': duplicates,
              'near_duplicate_review_pairs': len(near_pairs), 'near_duplicate_pairs': near_pairs,
              'near_duplicate_method': 'Adjacent lexicographic normalized texts, >=5 words, SequenceMatcher >=0.92; heuristic, not exhaustive or semantic',
              'records_with_prior_exposure': sum(bool(r['prior_exposure']) for r in rows),
              'exclusion_reasons': dict(Counter(reason for r in rows for reason in r['exclusion_reasons'])),
              'training_candidates': sum(r['training_candidate'] for r in rows)}
    return rows, report


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def run(raw, out, lock_path, exposure_dirs, download=False):
    raw, out, lock_path = Path(raw), Path(out), Path(lock_path)
    if out.exists():
        raise ValueError('Output already exists; select a new version directory: ' + str(out))
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    if lock.get('schema_version') != 1 or set(lock['sources']) != set(ALLOWED):
        raise ValueError('Unsupported source lock')
    raw.mkdir(parents=True, exist_ok=True)
    for sid, source in lock['sources'].items():
        if source['license'] != ALLOWED[sid] or not re.fullmatch('[0-9a-f]{40}', source['revision']):
            raise ValueError('Invalid source identity')
        for name, entry in source['files'].items():
            if Path(name).name != name or '/' in name or '\\' in name:
                raise ValueError('Invalid source filename')
            path = raw / name
            if not path.exists() and download:
                if not entry['url'].startswith('https://raw.githubusercontent.com/') or '/' + source['revision'] + '/' not in entry['url']:
                    raise ValueError('Unpinned source URL')
                with urllib.request.urlopen(entry['url'], timeout=60) as response:
                    data = response.read()
                if digest(data) != entry['sha256'] or len(data) != entry['bytes']:
                    raise ValueError('Download hash mismatch: ' + name)
                with path.open('xb') as handle:
                    handle.write(data)
            verify_file(path, entry)
    rows = []
    banking_labels = None
    for split in ('train', 'test'):
        with (raw / f'banking77-{split}.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != ['text', 'category']:
                raise ValueError('Unexpected BANKING77 columns')
            items = list(reader)
        labels = {r['category'] for r in items}
        if split == 'train':
            banking_labels = labels
        elif labels != banking_labels:
            raise ValueError('BANKING77 label mismatch')
        for i, item in enumerate(items):
            if set(item) != {'text', 'category'}:
                raise ValueError('Malformed BANKING77 row')
            rows.append(make_record('banking77', lock['sources']['banking77'], split, i, item['text'], item['category']))
    rows += parse_clinc(json.loads((raw / 'clinc150.json').read_text(encoding='utf-8')), lock['sources']['clinc150'])
    counts = Counter(f"{r['source_id']}/{r['source_split']}" for r in rows)
    expected = {'banking77/train':10003,'banking77/test':3080,'clinc150/train':15000,'clinc150/val':3000,'clinc150/test':4500,'clinc150/oos_train':100,'clinc150/oos_val':100,'clinc150/oos_test':1000}
    if counts != expected or len(banking_labels) != 77 or len({r['gold_label'] for r in rows if r['source_id']=='clinc150' and not r['is_oos']}) != 150:
        raise ValueError('Official corpus counts do not match')
    exposures, inventory = load_exposures(exposure_dirs)
    rows, report = audit(rows, exposures)
    report.update({'schema_version':1, 'source_counts':dict(counts), 'languages':dict(Counter(r['language'] for r in rows)),
                   'training_candidates_by_source':dict(Counter(r['source_id'] for r in rows if r['training_candidate'])),
                   'exposure_inventory':inventory, 'source_lock_sha256':digest(lock_bytes),
                   'importer_sha256':digest(Path(__file__).read_bytes()), 'ready_for_training':False,
                   'limitations':['English only', 'Intermediate schema, not trainer input', 'No new validation/calibration allocation', 'Prior exposure scan limited to supplied directories', 'No semantic duplicate certification', 'Public pretraining contamination unknown', 'Existing official test is not certified as unseen by prior work']})
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=out.name + '-', dir=out.parent))
    try:
        for name, selected in [('records.jsonl',rows),('training-candidates.jsonl',[r for r in rows if r['training_candidate']]),('quarantine.jsonl',[r for r in rows if r['exclusion_reasons']])]:
            with (staging / name).open('w',encoding='utf-8',newline='\n') as handle:
                for r in selected:
                    handle.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
        for name in ('banking77-LICENSE','clinc150-LICENSE'):
            shutil.copyfile(raw/name,staging/name)
        shutil.copyfile(lock_path,staging/'intent-sources.lock.json')
        report['output_sha256']={p.name:digest(p.read_bytes()) for p in sorted(staging.iterdir())}
        write_json(staging/'report.json',report)
        if out.exists():
            raise ValueError('Output was created concurrently')
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging,ignore_errors=True)
        raise
    return report


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir',type=Path,default=root/'research/data/raw')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--source-lock',type=Path,default=root/'research/sources/intent-sources.lock.json')
    parser.add_argument('--exposure-dir',type=Path,action='append',required=True,help='Previous JSONL material; repeat for multiple directories')
    parser.add_argument('--download',action='store_true',help='Download missing pinned source files; never replace existing files')
    args = parser.parse_args()
    report=run(args.raw_dir,args.out,args.source_lock,args.exposure_dir,args.download)
    print(json.dumps({k:v for k,v in report.items() if k not in ('near_duplicate_pairs','exposure_inventory','output_sha256')},indent=2))

if __name__ == '__main__':
    main()
