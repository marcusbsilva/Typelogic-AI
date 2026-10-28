"""Evaluate locally with observed labels and simple independently fitted baselines."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.offline import install
install()
import numpy as np
from typelogic.profiles import active_model_dir as default_model_dir
from typelogic.engine import DecisionEngine
from typelogic.specialist import SpecialistEngine
from typelogic.schema import validate,canonical
from typelogic.metrics import summary
from typelogic import __version__

def read_rows(path):
    rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not rows:raise ValueError('Empty dataset')
    for row in rows:
        _,questions,_=validate(row);row['_questions']=questions
        for q in questions:
            target=row.get('targets',{}).get(q['id'])
            if isinstance(target,dict):
                if set(target)!=set(q['keys']):raise ValueError('Incompatible target')
                target=[target[key] for key in q['keys']]
            if not isinstance(target,list) or len(target)!=len(q['keys']) or any(not isinstance(v,(int,float)) or not np.isfinite(v) or v<0 for v in target) or abs(sum(target)-1)>1e-6:raise ValueError('targets must contain a valid distribution for each question')
            row['targets'][q['id']]=target
    return rows

def disjoint(a,b):
    if {canonical(r['state']) for r in a}&{canonical(r['state']) for r in b}:raise ValueError('Datasets share states')
    if {r['group_id'] for r in a if 'group_id' in r}&{r['group_id'] for r in b if 'group_id' in r}:raise ValueError('Datasets share groups')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--baseline-train',type=Path);parser.add_argument('--model-dir',type=Path);parser.add_argument('--specialist',type=Path)
    args=parser.parse_args()
    if args.out.exists():raise ValueError('Output already exists')
    data=read_rows(args.data);baseline_data=read_rows(args.baseline_train) if args.baseline_train else [];disjoint(data,baseline_data)
    template=lambda q:(q['type'],q['instr'],tuple(sorted(q['keys'])))
    priors=defaultdict(lambda:defaultdict(float));counts=defaultdict(int)
    for row in baseline_data:
        for q in row['_questions']:
            key=template(q);counts[key]+=1
            for option,value in zip(q['keys'],row['targets'][q['id']]):priors[key][option]+=value
    root=Path(__file__).resolve().parents[1];engine=SpecialistEngine(args.specialist) if args.specialist else DecisionEngine(args.model_dir or default_model_dir(root))
    scored=[];uniform=[];frequency=[];latencies=[];failures=[];order_flags=0
    for index,row in enumerate(data):
        began=time.perf_counter()
        try:response=engine.evaluate({k:v for k,v in row.items() if not k.startswith('_')})
        except Exception as exc:response=None;failures.append({'record_index':index,'error':str(exc)})
        latencies.append((time.perf_counter()-began)*1000)
        for q in row['_questions']:
            common={'question_id':q['id'],'keys':q['keys'],'target':row['targets'][q['id']]};a=response['answers'][q['id']] if response else None
            scored.append({**common,'probabilities':[a['probabilities'][key] for key in q['keys']] if a else None,'candidate':bool(a and a['recommendation']=='candidate'),'abstained':bool(a and a['abstained'])})
            order_flags+=bool(a and a.get('order_diagnostic',{}).get('unstable'))
            uniform.append({**common,'probabilities':[1/len(q['keys'])]*len(q['keys']),'candidate':False})
            if args.baseline_train:
                key=template(q)
                if not counts[key]:raise ValueError('Frequency baseline lacks training data for an evaluated profile')
                frequency.append({**common,'probabilities':[(priors[key][option]+1)/(counts[key]+len(q['keys'])) for option in q['keys']],'candidate':False})
    report={'application':'TypeLogic AI','version':__version__,'dataset_sha256':hashlib.sha256(args.data.read_bytes()).hexdigest(),'baseline_training_sha256':hashlib.sha256(args.baseline_train.read_bytes()).hexdigest() if args.baseline_train else None,
            'model':engine.health(),'metrics':summary(scored),'uniform_baseline':summary(uniform),'frequency_baseline':summary(frequency) if frequency else None,
            'latency_ms':{'cold_first':latencies[0],'p50_all':statistics.median(latencies),'p95_all':float(np.quantile(latencies,.95)),'scope':'CLI engine wall time; includes model load in first record and configured order tests; not HTTP/memory overhead'},
            'order_sensitive_questions':order_flags,'failed_records':failures,'selection_policy':'candidates are not authorized automation; all require human review','labels':'accuracy/ECE use target argmax; NLL/Brier also accept soft distributions','evaluated_records':len(data)}
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False));print(json.dumps(report['metrics'],indent=2))
if __name__=='__main__':main()
