import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.server import Application
from typelogic.profiles import active_model_dir as default_model_dir
from typelogic.engine import DecisionEngine
from typelogic.specialist import SpecialistEngine
from typelogic.offline import install

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--model-dir',type=Path);parser.add_argument('--specialist',type=Path);parser.add_argument('--torch-only',action='store_true');parser.add_argument('--data',type=Path,default=Path.home()/'.typelogic-ai')
    args=parser.parse_args();install();root=Path(__file__).resolve().parents[1]
    if args.output.exists():raise ValueError('Output already exists; choose another file')
    engine=SpecialistEngine(args.specialist) if args.specialist else DecisionEngine(args.model_dir or default_model_dir(root),not args.torch_only)
    app=Application(root,args.data,engine);text=args.input.read_text();requests=[json.loads(line) for line in text.splitlines() if line.strip()] if args.input.suffix.lower()=='.jsonl' else [json.loads(text)]
    errors=0;args.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        with args.output.open('x',encoding='utf-8') as output:
            for i,payload in enumerate(requests):
                try:response=app.evaluate(payload)
                except Exception as exc:errors+=1;response={'error':str(exc)}
                output.write(json.dumps({'record_index':i,**response},ensure_ascii=False,allow_nan=False)+'\n');output.flush()
    finally:app.close()
    print(json.dumps({'records':len(requests),'errors':errors}));sys.exit(1 if errors else 0)
if __name__=='__main__':main()
