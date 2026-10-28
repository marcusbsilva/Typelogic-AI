"""Create a versioned intent corpus with TRAIN-derived criteria and original gold."""
import argparse
import copy
import hashlib
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from typelogic.learning import training_record,file_sha256,assert_disjoint
from typelogic.training_data_v2 import check_groups

SPLITS=('train','validation','calibration','test')


def canonical_hash(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def revise(source,catalog,policy,out):
    source,out=Path(source),Path(out)
    if out.exists():raise ValueError('Output exists; choose a new version directory')
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    files={s:source/(s+'.jsonl') for s in SPLITS}
    for s,p in files.items():
        if file_sha256(p)!=manifest['files'][p.name]:raise ValueError('Source dataset hash mismatch: '+s)
    if catalog.get('source_id')!='banking77' or catalog.get('language')!='en':
        raise ValueError('This revision requires the English BANKING77 catalog')
    if catalog['source_train_sha256']!=manifest['files']['train.jsonl']:
        raise ValueError('Catalog evidence training identity mismatch')
    descriptions=catalog['descriptions'];evidence=catalog['evidence_train_ids']
    if (not isinstance(descriptions,dict) or set(descriptions)!=set(evidence) or
        any(not isinstance(v,str) or not v.strip() or len(v)>3000 for v in descriptions.values())):
        raise ValueError('Invalid catalog definitions or evidence coverage')
    decisions=policy['decisions'];indexed={d['id']:d for d in decisions}
    if len(indexed)!=len(decisions):raise ValueError('Duplicate review ID')
    if any(d.get('action') not in ('blend','gold_only','quarantine') for d in decisions):
        raise ValueError('Unknown review action')
    excluded={k for k,d in indexed.items() if d['action']=='quarantine'}
    train_ids={};source_labels=set()
    with files['train'].open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():continue
            r=json.loads(line)
            if r['id'] in train_ids:raise ValueError('Duplicate training ID')
            train_ids[r['id']]=(r['source_id'],r['gold_label'])
            if r['source_id']=='banking77':source_labels.add(r['gold_label'])
    if set(descriptions)!=source_labels:raise ValueError('Catalog IDs differ from source training labels')
    if not set(indexed)<=set(train_ids):raise ValueError('Reviewed ID not in TRAIN')
    for label,ids in evidence.items():
        if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):
            raise ValueError('Catalog evidence IDs missing or duplicated')
        if any(train_ids.get(i)!=('banking77',label) or i in excluded for i in ids):
            raise ValueError('Catalog evidence must be matching, non-quarantined TRAIN records')
    fingerprint=canonical_hash(catalog)
    out.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=out.name+'-',dir=out.parent))
    counts={};changed={};by_source={};compact={s:[] for s in SPLITS};quarantined=0
    try:
        with (stage/'quarantine.jsonl').open('w',encoding='utf-8') as quarantine:
            for split,path in files.items():
                count=updates=0;sources=Counter()
                with path.open(encoding='utf-8') as stream,(stage/(split+'.jsonl')).open('w',encoding='utf-8') as target:
                    for line in stream:
                        if not line.strip():continue
                        row=json.loads(line);training_record(row)
                        if row.get('split')!=split:raise ValueError('Wrong declared split')
                        q=row['questions'].get('intent')
                        if q is None or q['type']!='choice' or len(row['questions'])!=1:
                            raise ValueError('One intent Choice question required')
                        expected={k:float(k==row['gold_label']) for k in q['criteria']}
                        if row['targets'].get('intent')!=expected or row.get('teacher_used',False):
                            raise ValueError('Use original gold inputs, not an earlier distilled corpus')
                        if split=='train' and row['id'] in excluded:
                            quarantine.write(json.dumps({'record':row,'review':indexed[row['id']]},ensure_ascii=False)+'\n')
                            quarantined+=1;continue
                        if row['source_id']=='banking77':
                            if set(q['criteria'])!=set(descriptions):raise ValueError('Alternative IDs mismatch')
                            row=copy.deepcopy(row)
                            row['questions']['intent']['criteria']={k:descriptions[k] for k in q['criteria']}
                            row['catalog_revision']=catalog['revision'];row['catalog_sha256']=fingerprint
                            training_record(row);updates+=1
                            target.write(json.dumps(row,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
                        else:
                            target.write(line if line.endswith('\n') else line+'\n')
                        count+=1;sources[row['source_id']]+=1
                        compact[split].append({'state':row['state'],'group_id':row['group_id']})
                counts[split]=count;changed[split]=updates;by_source[split]=dict(sources)
        if quarantined!=len(excluded):raise ValueError('Quarantine IDs not all found in TRAIN')
        check_groups(compact);assert_disjoint(compact)
        for name in ('banking77-LICENSE','clinc150-LICENSE'):
            if (source/name).exists():shutil.copyfile(source/name,stage/name)
        (stage/'banking77-catalog.json').write_text(json.dumps(catalog,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        report={'schema_version':2,'base_model':'Qwen/Qwen3.5-2B','catalog_revision':catalog['revision'],
            'catalog_sha256':fingerprint,'source_manifest_sha256':file_sha256(source/'manifest.json'),
            'source_files_sha256':{p.name:manifest['files'][p.name] for p in files.values()},
            'counts':counts,'by_source':by_source,'banking_criteria_updated':changed,
            'quarantined':quarantined,'review_policy_sha256':canonical_hash(policy),
            'contexts_labels_groups_and_option_order_preserved':True,
            'heldout_membership_and_gold_preserved':True,'heldout_criteria_updated':True,
            'teacher_targets_reused':False,'teacher_calls':0,'languages':['en'],
            'files':{s+'.jsonl':file_sha256(stage/(s+'.jsonl')) for s in SPLITS},
            'deployment_eligible':False,'bilingual_release_ready':False,
            'scope':'TRAIN-derived editorial criteria; historical test gold untouched; token preflight and native Portuguese coverage pending'}
        (stage/'manifest.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        if out.exists():raise ValueError('Output appeared during revision')
        stage.rename(out)
    except BaseException:
        shutil.rmtree(stage,ignore_errors=True);raise
    return {k:report[k] for k in ('counts','banking_criteria_updated','quarantined',
        'heldout_membership_and_gold_preserved','heldout_criteria_updated','teacher_targets_reused')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--catalog',type=Path,default=ROOT/'research/banking77-catalog-en-v2.json')
    p.add_argument('--review-policy',type=Path,default=ROOT/'research/pilot-jev-banking77-review-v2.json')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(revise(a.data_dir,json.loads(a.catalog.read_text(encoding='utf-8')),
        json.loads(a.review_policy.read_text(encoding='utf-8')),a.out),indent=2))


if __name__=='__main__':main()
