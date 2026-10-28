"""Local temperature fitting with group-disjoint validation and explicit opt-in apply."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.offline import install
install()
import numpy as np
from typelogic.profiles import active_model_dir as default_model_dir
from typelogic.engine import DecisionEngine
from benchmark import read_rows,disjoint

def infer(engine,rows):
    output=[]
    for row in rows:
        result=engine.evaluate({**row,'permutation_check':False})
        for q in row['_questions']:
            p=[result['answers'][q['id']]['probabilities'][k] for k in q['keys']]
            output.append((np.log(np.clip(p,1e-30,1)),np.array(row['targets'][q['id']],dtype=float)))
    return output
def metrics(rows,t):
    nll=brier=0.
    for logits,target in rows:
        z=logits/t;p=np.exp(z-z.max());p/=p.sum();nll-=float((target*np.log(np.clip(p,1e-30,1))).sum());brier+=float(((p-target)**2).sum())
    return {'nll':nll/len(rows),'brier':brier/len(rows),'questions':len(rows)}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--calibration',type=Path,required=True);parser.add_argument('--validation',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--model-dir',type=Path,default=default_model_dir());parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.out.exists():raise ValueError('Report already exists')
    fit=read_rows(args.calibration);check=read_rows(args.validation);disjoint(fit,check)
    if args.apply and (len(fit)<50 or len(check)<20):raise ValueError('Application requires at least 50 calibration and 20 validation records; metrics do not guarantee quality')
    engine=DecisionEngine(args.model_dir,use_onnx=False);engine.load();original=engine.temperature;engine.model.head.temperature=1.
    fit_rows,check_rows=infer(engine,fit),infer(engine,check)
    temperature=float(min(np.geomspace(.1,20,250),key=lambda t:metrics(fit_rows,t)['nll']))
    before,after=metrics(check_rows,original),metrics(check_rows,temperature);improved=after['nll']<before['nll'] and after['brier']<=before['brier']
    report={'temperature':temperature,'original_temperature':original,'checkpoint_sha256':engine.fingerprint,'dataset_sha256':hashlib.sha256(args.calibration.read_bytes()).hexdigest(),'validation_sha256':hashlib.sha256(args.validation.read_bytes()).hexdigest(),'fit':metrics(fit_rows,temperature),'validation':{'baseline':before,'fitted':after},'improved_on_validation':improved,'applied':bool(args.apply and improved),'scope':'local workload validation; no independent final test or general calibration guarantee'}
    args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,indent=2))
    if args.apply:
        if not improved:raise ValueError('Validation did not improve; configuration unchanged')
        destination=args.model_dir/'calibration.json';destination.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
