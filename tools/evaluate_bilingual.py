"""Small known regression suite; not training data or a general benchmark."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def prediction(answer):
    p=answer['probabilities']
    return max(p,key=p.__getitem__)

def summarize(rows):
    result={}
    for row in rows:
        key=row['language']+'/'+row['type']
        item=result.setdefault(key,{'correct':0,'questions':0})
        item['questions']+=1;item['correct']+=int(row['correct'])
    for item in result.values():item['accuracy']=item['correct']/item['questions']
    return result

def read_cases(path):
    rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not rows or len({r['id'] for r in rows})!=len(rows):raise ValueError('Empty suite or duplicate IDs')
    for r in rows:
        q=r['questions']['q'];kind=q['type']
        keys=['false','true'] if kind=='noul' else list(q['criteria']) if kind=='choice' else [str(i) for i in range(len(q['criteria']))]
        if r['expected'] not in keys:raise ValueError('Invalid expected label')
    return rows

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-dir',type=Path,default=None,help='Profile folder (default: the active profile)')
    parser.add_argument('--cases',type=Path,default=ROOT/'research/data/regression/bilingual-v1.jsonl')
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--validate-only',action='store_true')
    args=parser.parse_args();cases=read_cases(args.cases)
    from typelogic.schema import validate
    for r in cases:validate(r)
    if args.validate_only:
        print('Dataset contracts: OK; 18 authored regression cases expected, loaded:',len(cases));return
    if args.out.exists():raise ValueError('Output exists; choose a new filename')
    from typelogic.offline import install
    install()
    from typelogic.engine import DecisionEngine
    if args.model_dir is None:
        from typelogic.profiles import active_model_dir
        args.model_dir=active_model_dir(ROOT)
    engine=DecisionEngine(args.model_dir,use_onnx=False);engine.load()
    rows=[]
    for index,case in enumerate(cases,1):
        response=engine.evaluate(case);answer=response['answers']['q'];chosen=prediction(answer)
        rows.append({'id':case['id'],'group_id':case['group_id'],'language':case['language'],'type':case['questions']['q']['type'],'expected':case['expected'],'predicted':chosen,'correct':chosen==case['expected'],'request':case,'response':response})
        print(f"{index}/{len(cases)} {case['id']}: expected={case['expected']} predicted={chosen}",flush=True)
    report={'scope':'18 manually authored known regression cases; not a held-out general benchmark. Do not train on these cases or translations.','dataset_sha256':hashlib.sha256(args.cases.read_bytes()).hexdigest(),'metrics':summarize(rows),'records':rows}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(report['metrics'],ensure_ascii=False,indent=2));print('Report:',args.out)
if __name__=='__main__':main()
