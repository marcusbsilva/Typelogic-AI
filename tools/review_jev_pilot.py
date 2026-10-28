"""Apply an explicit editorial pilot review to a NEW training directory."""
import argparse
import json
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tools'))
from collect_jev_intents import digest
from typelogic.jev_distill import MODEL,blend_training_record
from typelogic.learning import file_sha256,training_record


def review_dataset(train,teacher_dir,policy,out):
    if out.exists():
        raise ValueError('Output already exists; use a new directory')
    identity=json.loads((teacher_dir/'identity.json').read_text(encoding='utf-8'))
    if identity['model']!=MODEL or identity['train_sha256']!=file_sha256(train):
        raise ValueError('Pilot training identity mismatch')
    decisions=policy.get('decisions',[])
    indexed={d['id']:d for d in decisions}
    if len(indexed)!=len(decisions) or set(indexed)!=set(identity['ids']):
        raise ValueError('Review must cover each selected pilot ID exactly once')
    if any(d.get('action') not in ('blend','gold_only','quarantine') or
           not isinstance(d.get('reason'),str) or not d['reason'].strip() for d in decisions):
        raise ValueError('Every review needs an action and a reason')
    weight=policy['teacher_weight']
    if isinstance(weight,bool) or not isinstance(weight,(int,float)) or not 0<=weight<=1:
        raise ValueError('Invalid teacher weight')
    responses={}
    teacher_hashes={}
    for name in teacher_dir.glob('*.json'):
        if name.name=='identity.json':continue
        response=json.loads(name.read_text(encoding='utf-8'))
        rid=response.get('id');model=response.get('model','')
        if (rid not in indexed or rid in responses or response.get('row_sha256')!=name.stem or
            response.get('split')!='train' or name.stem not in identity['row_sha256'] or
            not (model==MODEL or model.startswith(MODEL+'-2026'))):
            raise ValueError('Unexpected teacher response')
        responses[rid]=response;teacher_hashes[name.name]=file_sha256(name)
    if set(responses)!=set(indexed):raise ValueError('Missing pilot response')
    out.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix=out.name+'-',dir=out.parent))
    counts=Counter();seen=set()
    try:
        with train.open(encoding='utf-8') as source, (stage/'train.jsonl').open('w',encoding='utf-8') as target, (stage/'quarantine.jsonl').open('w',encoding='utf-8') as quarantine:
            for line in source:
                if not line.strip():continue
                row=json.loads(line);training_record(row)
                if row.get('split')!='train':raise ValueError('Input includes held-out records')
                rid=row['id']
                if rid not in indexed:
                    target.write(line if line.endswith('\n') else line+'\n');counts['unchanged']+=1
                    continue
                if rid in seen:raise ValueError('Duplicate reviewed training ID')
                seen.add(rid);decision=indexed[rid];response=responses[rid]
                if digest(row)!=response['row_sha256'] or response['source_id']!=row['source_id']:
                    raise ValueError('Teacher response row mismatch')
                # Validate the complete distribution even for gold-only and quarantined cases.
                blended=blend_training_record(row,response['probabilities'],weight)
                action=decision['action'];counts[action]+=1
                if action=='quarantine':
                    entry={'record':row,'teacher':response,'review':decision}
                    quarantine.write(json.dumps(entry,ensure_ascii=False,allow_nan=False)+'\n')
                    continue
                if action=='blend':
                    if blended['distillation']['needs_review']:
                        raise ValueError('Blend requested for teacher/gold disagreement')
                    result=blended
                else:
                    result=dict(row)
                result['editorial_review']=decision
                target.write(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
        if seen!=set(indexed):raise ValueError('Reviewed ID absent from training data')
        report={'counts':dict(counts),'remaining_training_records':counts['unchanged']+counts['blend']+counts['gold_only'],
                'teacher_model':MODEL,'teacher_weight':weight,'original_train_sha256':identity['train_sha256'],
                'teacher_identity_sha256':file_sha256(teacher_dir/'identity.json'),
                'teacher_response_sha256':teacher_hashes,
                'policy':policy,'output_sha256':{n:file_sha256(stage/n) for n in ('train.jsonl','quarantine.jsonl')},
                'review_scope':f'Editorial reading of {len(indexed)} English training cases; not a new gold annotation or model accuracy benchmark',
                'heldout_modified':False,'deployment_eligible':False}
        (stage/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        if out.exists():raise ValueError('Output appeared during review')
        stage.rename(out)
    except BaseException:
        shutil.rmtree(stage,ignore_errors=True)
        raise
    return {k:report[k] for k in ('counts','remaining_training_records','heldout_modified')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--train-file',type=Path,required=True)
    p.add_argument('--teacher-dir',type=Path,required=True)
    p.add_argument('--policy',type=Path,default=ROOT/'research/pilot-jev-review-v1.json')
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(review_dataset(a.train_file,a.teacher_dir,json.loads(a.policy.read_text(encoding='utf-8')),a.out),indent=2))


if __name__=='__main__':main()
