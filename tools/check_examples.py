"""Run curated fictional examples through the actual application pipeline, offline."""
import argparse,json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.server import Application
from typelogic import __version__

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,default=Path(__file__).resolve().parents[1]/'examples/business-validation.jsonl');p.add_argument('--output',type=Path,required=True);args=p.parse_args()
 if args.output.exists():p.error('Output already exists; refusing overwrite')
 root=Path(__file__).resolve().parents[1];rows=[]
 with tempfile.TemporaryDirectory(prefix='typelogic-example-check-') as folder:
  app=Application(root,Path(folder))
  try:
   app.engine.load()
   for line in args.input.read_text(encoding='utf-8').splitlines():
    if not line.strip():continue
    payload=json.loads(line);targets=payload.pop('targets');payload.pop('group_id',None);response=app.evaluate(payload);checks=[]
    for question,target in targets.items():
     distribution=response['answers'][question]['probabilities'];expected=max(target,key=target.get);predicted=max(distribution,key=distribution.get)
     checks.append({'question':question,'predicted':predicted,'expected':expected,'correct':predicted==expected,'probability':distribution[predicted]})
    rows.append({'title':payload.get('title','Decision'),'checks':checks,'response':response})
  finally:app.close()
 report={'version':__version__,'kind':'Fictional example sanity check using the application pipeline; curated examples, not a held-out accuracy benchmark','correct':sum(c['correct'] for r in rows for c in r['checks']),'total':sum(len(r['checks']) for r in rows),'cases':rows}
 with args.output.open('x',encoding='utf-8') as output:json.dump(report,output,indent=2,ensure_ascii=False,allow_nan=False);output.write('\n')
 print(f"{report['correct']}/{report['total']} curated example answers matched; this is not production accuracy.")
if __name__=='__main__':main()
