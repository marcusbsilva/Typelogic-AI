"""Offline CLI. Machine results go to stdout; diagnostics go to stderr."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
from . import __version__
from .offline import install
from .store import Store
from .profiles import active_model_dir as default_model_dir
from .core import validate
from .examples import catalogue

COMMANDS={'decide','extract','templates','list','delete','archive','feedback','export','status','examples','verify','serve','profiles','route','training-export','tags'}
def read_json(text):
    return json.loads(text,parse_constant=lambda x:(_ for _ in ()).throw(ValueError('JSON must contain finite values')))
def read_input(path):
    # Refuse oversized files before reading them into memory.
    if path!='-' and Path(path).stat().st_size>4*(8*1024*1024)+3:raise ValueError('Input exceeds 8 MB')
    text=sys.stdin.read(8*1024*1024+1) if path=='-' else Path(path).read_text(encoding='utf-8-sig')
    if len(text)>8*1024*1024:raise ValueError('Input exceeds 8 MB')
    return text

def parser():
    p=argparse.ArgumentParser(description='TypeLogic AI — offline CLI; JSON on stdout')
    sub=p.add_subparsers(dest='command',required=True)
    for name in sorted(COMMANDS-{'serve'}):
        q=sub.add_parser(name)
        q.add_argument('--data',type=Path,default=Path.home()/'.typelogic-ai',help='SQLite directory shared with the interface')
        q.add_argument('--space',default='default');q.add_argument('--output',type=Path,help='New output file (no overwrite)')
        if name in ('decide','list','export'):q.add_argument('--format',choices=['full','lite'],default='full',help='Full technical JSON or Lite (title, tags, questions and answers)')
        if name=='export':
            q.add_argument('--id',help='Export a single stored decision')
            q.add_argument('--output-dir',type=Path,help='Write an eight-character ID-derived filename; requires --id and excludes --output')
        if name=='verify':q.add_argument('--id',required=True,help='Recompute SHA-512 and MD5 for a stored decision')
        if name=='decide':
            q.add_argument('--input',required=True,help='JSON; use - for stdin');q.add_argument('--jsonl',action='store_true',help='One input and output per line')
            q.add_argument('--mode',choices=['cpu','gpu','hybrid'],help='Engine mode (default: the choice saved in the web app, else cpu)');q.add_argument('--title',help='Title of the case (default: the request title, else a short generated one)');q.add_argument('--tag',action='append',default=[],help='Tag of the case; repeat for several (added to the request tags)');q.add_argument('--model-dir',type=Path);q.add_argument('--specialist',type=Path);q.add_argument('--torch-only',action='store_true');q.add_argument('--readout',choices=['onnx','rust','torch'],help='Decision-head backend (default onnx, or TYPELOGIC_READOUT)')
        elif name=='delete':q.add_argument('--id',required=True,help='Delete this decision with its corrections and audit records')
        elif name=='archive':q.add_argument('--yes',action='store_true',help='Compress every decision into <project>/data/DD-MM-YYYY.rar (or .zip) and remove them from the library')
        elif name=='feedback':q.add_argument('--id',required=True);q.add_argument('--question',required=True);q.add_argument('--target',required=True,help='Observed JSON target distribution')
        elif name=='extract':
            q.add_argument('--input',required=True,help='Case text file, or a v2/v1 request JSON (its case or state is read); - for stdin')
            group=q.add_mutually_exclusive_group(required=True)
            group.add_argument('--template',help='Use the evidence schema of this template')
            group.add_argument('--schema',help='Evidence schema: JSON list [{"name","type","description","options"}], a file path or inline JSON')
            q.add_argument('--backend',choices=['auto','ollama','needle'],help='Extractor (default: the saved setting, else auto)')
            q.add_argument('--model',help='Ollama model for extraction (default: the saved setting, else qwen3.5:4b)')
        elif name=='list':q.add_argument('--tag',help='Only decisions with this tag')
        elif name=='templates':q.add_argument('--id',help='Print the v2 request of one template (edit it, then run decide --input)');q.add_argument('--no-values',action='store_true',help='Leave the example case values out')
        elif name=='examples':q.add_argument('--id',help='Return the selected example input')
        elif name=='profiles':q.add_argument('--use',metavar='NAME',help='Make an installed profile under models/ active (writes models/active.json)')
        elif name=='training-export':
            q.add_argument('--all-spaces',action='store_true',help='Export every workspace instead of --space')
        elif name=='route':q.add_argument('--input',required=True,help='Router request JSON {"task","language","latency","text"}; - for stdin');q.add_argument('--specialist',type=Path)
    return p

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]=='serve':
        from .server import main as serve
        original=sys.argv;sys.argv=[original[0],*argv[1:]]
        try:serve()
        finally:sys.argv=original
        return 0
    args=parser().parse_args(argv);install();db=None;app=None;stream=None;created=None;failed=False
    try:
        # Validate input before loading weights or creating decisions.
        payloads=None
        if args.command=='decide':
            text=read_input(args.input)
            payloads=[read_json(line) for line in text.splitlines() if line.strip()] if args.jsonl else [read_json(text)]
            if not payloads:raise ValueError('No input')
            from . import casefmt
            for item in payloads:
                if not isinstance(item,dict):raise ValueError('Each input must be a JSON object')
                validate(casefmt.normalize(item)[0] if casefmt.is_v2(item) else item)
        if args.command=='export' and args.output_dir and (not args.id or args.output):raise ValueError('--output-dir requires --id and cannot be combined with --output')
        stream=args.output.open('x',encoding='utf-8') if args.output else sys.stdout
        if args.output:created=args.output
        def emit(value):
            stream.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n');stream.flush()
        if args.command=='examples':
            rows=catalogue()['examples']
            if args.id:
                row=next((r for r in rows if r['id']==args.id),None)
                if row is None:raise ValueError('Example not found')
                emit(row['payload'])
            else:emit({'examples':[{k:v for k,v in r.items() if k!='payload'} for r in rows],'fictitious':True})
            return 0
        if args.command=='decide':
            from .server import Application
            from . import modes
            root=Path(__file__).resolve().parents[1]
            mode=None
            if args.specialist:
                from .specialist import SpecialistEngine
                engine=SpecialistEngine(args.specialist)
            else:
                mode=modes.resolve(args.mode,args.data);settings=modes.load_settings(args.data)
                if mode=='hybrid':
                    from . import ollama
                    print('Ollama: '+ollama.ensure_running(settings['ollama_model'],models_dir=settings['ollama_models']),file=sys.stderr)
                engine=modes.build(mode,args.model_dir or default_model_dir(root),settings,not args.torch_only,None if args.torch_only else args.readout,root)
            app=Application(root,args.data,engine,mode=mode);db=app.store   # the mode decides which Ollama model may write titles
            with redirect_stdout(sys.stderr):engine.load()
            for i,payload in enumerate(payloads,1):
                payload=dict(payload);payload['space']=args.space
                if args.title:payload['title']=args.title
                if args.tag:payload['tags']=[*(payload.get('tags') or []),*args.tag]
                try:
                    with redirect_stdout(sys.stderr):result=app.evaluate(payload)
                    emit(db.export_decision(result['decision_id'],'lite') if args.format=='lite' else result)
                except Exception as exc:raise RuntimeError(f'Input {i}: {exc}. Earlier batch results remain saved.') from exc
        elif args.command=='extract':
            # Evidence extraction on this computer (local Ollama or Needle); every value is unverified.
            from . import evidence,modes,templates
            from .core import canonical
            text=read_input(args.input)
            try:
                value=read_json(text)
                if isinstance(value,dict) and isinstance(value.get('case'),(str,dict)):
                    from .casefmt import case_text
                    text=case_text(value['case'])
                elif isinstance(value,dict) and 'state' in value:
                    text=value['state'] if isinstance(value['state'],str) else canonical(value['state'])
            except ValueError:pass
            if args.template:schema=templates.get(args.template,data_dir=args.data).get('evidence_schema',[])
            else:schema=read_json(Path(args.schema).read_text(encoding='utf-8-sig') if not args.schema.lstrip().startswith('[') else args.schema)
            settings=modes.load_settings(args.data)
            emit(evidence.extract(text,schema,args.backend or settings['extract_backend'],args.model or settings['extract_model']))
        elif args.command=='templates':
            from . import templates
            if args.id:emit(templates.to_request(templates.get(args.id,data_dir=args.data),include_values=not args.no_values))
            else:
                found=templates.load_all(data_dir=args.data)
                emit({'templates':[{k:r[k] for k in ('id','title','description','origin','path')} for r in found['templates']],'errors':found['errors']})
        elif args.command=='profiles':
            from . import profiles
            if args.use:profiles.select(args.use)
            emit({'active':str(profiles.active_model_dir()),'profiles':profiles.installed()})
        elif args.command=='route':
            from . import core,profiles
            emit(core.route(read_json(read_input(args.input)),profiles.router_profiles(specialist=args.specialist)))
        else:
            db=Store(args.data/'typelogic.sqlite3')
            if args.command=='list':
                tag=' '.join(args.tag.split()).lower() if args.tag else None
                rows=[r for r in db.decisions(args.space,all_rows=True) if tag is None or tag in (r['request'].get('tags') or [])]
                if args.format=='lite':
                    # Lite needs a verified record; records that fail the check are listed by id instead of aborting.
                    emit({'decisions':[db.export_decision(r['id'],'lite') for r in rows if r['integrity']['verified']],
                          'integrity_failed':[r['id'] for r in rows if not r['integrity']['verified']]})
                else:emit({'decisions':rows})
            elif args.command=='delete':emit(db.delete(args.id))
            elif args.command=='archive':
                from . import archive
                root=Path(__file__).resolve().parents[1]
                if not args.yes:emit(archive.info(db,root));print('Add --yes to compress every decision and remove them from the library.',file=sys.stderr)
                else:emit(archive.archive(db,root))
            elif args.command=='tags':
                from .casefmt import TAGS
                used={}
                for r in db.decisions(args.space,all_rows=True):
                    for t in r['request'].get('tags') or []:used[t]=used.get(t,0)+1
                emit({'suggested':list(TAGS),'used':dict(sorted(used.items()))})
            elif args.command=='feedback':emit(db.feedback(args.id,args.question,read_json(args.target)))
            elif args.command=='export':
                if args.output_dir and (not args.id or args.output):raise ValueError('--output-dir requires --id and cannot be combined with --output')
                if args.id:value=db.export_decision(args.id,args.format)
                elif args.format=='lite':
                    rows=db.decisions(all_rows=True)
                    value={'decisions':[db.export_decision(r['id'],'lite') for r in rows if r['integrity']['verified']],
                           'integrity_failed':[r['id'] for r in rows if not r['integrity']['verified']]}
                else:value=db.export()
                if args.output_dir:
                    from .core import export_filename
                    args.output_dir.mkdir(parents=True,exist_ok=True)
                    target=args.output_dir/export_filename(args.id,args.format)
                    stream=target.open('x',encoding='utf-8');created=target
                emit(value)
            elif args.command=='verify':
                integrity=db.get_decision(args.id)['integrity'];emit({'id':args.id,**integrity})
                if not integrity['verified']:return 1
            elif args.command=='status':emit({'version':__version__,'offline':True,'data':str(args.data),'stats':db.stats()})
            elif args.command=='training-export':
                # JSON Lines (one training row per line) for tools/build_training_mix.py --feedback; the summary goes to stderr.
                result=db.training_records(None if args.all_spaces else args.space)
                for row in result['records']:emit(row)
                print(json.dumps({'records':len(result['records']),'corrections':result['corrections'],'skipped':result['skipped']}),file=sys.stderr)
        return 0
    except Exception as exc:
        failed=True
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 1
    finally:
        if db:db.close()
        if stream is not None and stream is not sys.stdout:stream.close()
        # A failed command must not leave an empty file that blocks the no-overwrite retry.
        # Files that already hold results (for example earlier batch lines) are kept.
        if failed and created is not None:
            try:
                if created.stat().st_size==0:created.unlink()
            except OSError:pass
