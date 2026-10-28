"""Session-authenticated loopback UI; external actions are never executed."""
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hmac
import json
import mimetypes
from pathlib import Path
import secrets
import subprocess
import os
import sys
import threading
from urllib.parse import urlparse,parse_qs
import webbrowser
from .offline import install
from . import modes
from .store import Store
from .profiles import active_model_dir as default_model_dir
from .core import validate
from .examples import catalogue
from . import archive as library_archive

def tag_list():
    from .casefmt import TAGS,MAX_TAGS,MAX_TAG_CHARS
    return {'suggested':list(TAGS),'max_tags':MAX_TAGS,'max_chars':MAX_TAG_CHARS}

def _release(engine):
    """Drop a replaced engine and give its memory back (RAM and GPU)."""
    import gc
    for part in (engine,getattr(engine,'specialist',None),getattr(engine,'generalist',None)):
        if part is not None and hasattr(part,'model'):
            try:part.model=None
            except Exception:pass
    del engine;gc.collect()
    try:
        import torch
        if getattr(torch,'xpu',None) and torch.xpu.is_available():torch.xpu.empty_cache()
    except Exception:pass

class LoopbackServer(ThreadingHTTPServer):
    """HTTP server that owns its port. ThreadingHTTPServer sets SO_REUSEADDR, which on Windows lets a second process bind a
    port that another program is already serving, so the browser could reach that other program instead."""
    allow_reuse_address=False
    daemon_threads=True
    def server_bind(self):
        import socket,sys
        if sys.platform=='win32' and hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()
    def handle_error(self,request,client_address):
        import sys
        if isinstance(sys.exc_info()[1],ConnectionError):return   # the browser closed the connection (or the app is stopping)
        super().handle_error(request,client_address)

def bind_server(handler,port,attempts=20):
    """(server, note): the requested port, or the next free one when another program uses it."""
    last=None
    for candidate in ([port] if port==0 else range(port,port+attempts)):
        try:server=LoopbackServer(('127.0.0.1',candidate),handler)
        except OSError as exc:last=exc;continue
        note=None if candidate==port else f'Port {port} is used by another program; using {candidate} instead.'
        return server,note
    raise OSError(f'No free port between {port} and {port+attempts-1}: {last}')

class Application:
    def __init__(self,root,data,engine=None,mode=None,model_dir=None,readout=None,use_onnx=True):
        self.root=Path(root);self.data=Path(data);self.store=Store(self.data/'typelogic.sqlite3');self.token=secrets.token_urlsafe(32)
        self.model_dir=Path(model_dir) if model_dir else default_model_dir(self.root);self.readout=readout;self.use_onnx=use_onnx
        self.settings=modes.load_settings(self.data);self.mode=modes.resolve(mode,self.data) if engine is None else (mode or 'custom')
        self.engine=engine or modes.build(self.mode,self.model_dir,self.settings,use_onnx,readout,self.root)
        self.load_guard=threading.Lock();self.load_thread=None;self.restart=None;self.restarting=False
    def close(self,timeout=None):
        """Close the database once the engine is idle; with a timeout, do not wait for a model that is still loading."""
        acquired=self.engine.lock.acquire(timeout=timeout) if timeout else self.engine.lock.acquire()
        try:self.store.close()
        finally:
            if acquired:self.engine.lock.release()
    SETTING_KEYS=('extract_backend','extract_model')
    def settings_info(self):
        return {k:self.settings[k] for k in self.SETTING_KEYS}
    def update_settings(self,request):
        changes={k:request[k] for k in self.SETTING_KEYS if k in request}
        if not changes:raise ValueError('Nothing to change (supported: '+', '.join(self.SETTING_KEYS)+')')
        self.settings=modes.save_settings(self.data,changes)
        return self.settings_info()
    def mode_info(self):
        """Current engine mode, its options and what this computer offers (GET /api/modes)."""
        public={k:self.settings[k] for k in ('hybrid_device','hybrid_combine','ollama_model')}
        return {'mode':self.mode,'label':modes.LABELS.get(self.mode,self.mode),'settings':public,'restarting':self.restarting,
                'availability':modes.availability(self.settings,self.root),'engine_status':self.engine.health()['status']}
    def switch_mode(self,request):
        """Change the engine mode (POST /api/mode). Builds the new engine and frees the old one; when the GPU needs another
        Python environment and the launcher can provide it, the choice is saved and the launcher restarts the app."""
        mode=modes.normalize(request.get('mode'))
        changes={'mode':mode,**{k:request[k] for k in ('hybrid_device','hybrid_combine','ollama_model') if request.get(k) is not None}}
        target=modes.validate_settings({**self.settings,**changes})
        with self.load_guard:
            if self.load_thread is not None and self.load_thread.is_alive():
                raise RuntimeError('The engine is still loading; wait until it is ready or has failed, then change the mode')
            if modes.needs_gpu(mode,target) and not modes.gpu_info()['available']:
                if os.environ.get('TYPELOGIC_GPU_RESTART','')=='1' and self.restart is not None:
                    self.settings=modes.save_settings(self.data,changes);self.restarting=True
                    threading.Timer(.5,self.restart).start()
                    return {'mode':mode,'restarting':True}
                raise RuntimeError(modes.gpu_info()['reason'] or 'No usable GPU in this Python environment')
            note=None
            from . import ollama
            if mode=='hybrid':note='Ollama: '+ollama.ensure_running(target['ollama_model'],models_dir=target['ollama_models'])
            elif mode=='gpu':ollama.unload_all()   # free GPU memory used by the generalist
            engine=modes.build(mode,self.model_dir,target,self.use_onnx,self.readout,self.root)
            old,self.engine=self.engine,engine
            self.settings=modes.save_settings(self.data,changes);self.mode=mode
        _release(old)
        self.start_engine()
        return {'mode':mode,'restarting':False,'loading':True,'note':note}
    def evidence_backends(self):
        from . import evidence
        return {**evidence.available(self.settings['extract_model']),'backend':self.settings['extract_backend']}
    def extract_evidence(self,request):
        from . import evidence
        return evidence.extract(request.get('text'),request.get('schema'),request.get('backend') or self.settings['extract_backend'],
                                request.get('model') or self.settings['extract_model'])
    def template_list(self):
        from . import templates
        return templates.load_all(self.root,self.data)
    def needle_status(self):
        """Field extraction with Needle 3 (GET /api/needle): installed or not, limits and field presets."""
        from . import needle
        return needle.status()
    def evaluate_when_ready(self,payload):
        if self.engine.health()['status']!='ready':raise RuntimeError('Engine is not ready')
        return self.evaluate(payload)
    def start_engine(self):
        """One background load at a time; ready engines are never loaded twice."""
        with self.load_guard:
            if self.engine.health()['status']=='ready':return False
            if self.load_thread is not None and self.load_thread.is_alive():return True
            self.engine.status='loading';self.engine.error=None
            def load():
                with self.engine.lock:
                    try:self.engine.load()
                    except Exception as exc:
                        self.engine.status='error';self.engine.error=str(exc)
            self.load_thread=threading.Thread(target=load,daemon=True,name='typelogic-model-loader')
            self.load_thread.start();return True
    def title_models(self):
        """Ollama models that may write a title: the generalist already loaded in Hybrid mode, then the extraction model."""
        models=[]
        if self.mode=='hybrid':models.append(self.settings.get('ollama_model'))
        models.append(self.settings.get('extract_model'))
        return [m for m in dict.fromkeys(models) if m]
    def archive_info(self):
        from . import archive
        return archive.info(self.store,self.root)
    def archive(self):
        from . import archive
        return archive.archive(self.store,self.root)
    def evaluate(self,payload):
        from . import archive,casefmt,titles
        if not isinstance(payload,dict):raise ValueError('The request must be a JSON object')
        title=casefmt.validate_title(payload.get('title'));tags=casefmt.validate_tags(payload.get('tags'))
        case_info=None
        if casefmt.is_v2(payload):payload,case_info=casefmt.normalize(payload)
        payload={k:v for k,v in payload.items() if k not in ('review_mode','title','tags')}   # review_mode: accepted from earlier versions, ignored
        validate(payload);payload=dict(payload)
        space=payload.get('space','default')
        if not isinstance(space,str) or not 1<=len(space)<=100:raise ValueError('Invalid workspace')
        archive.check_room(self.store,self.root)
        extracted=None
        if payload.get('extracted_fields') is not None:
            from .needle import check_attached
            extracted=payload['extracted_fields']=check_attached(payload['extracted_fields'])
            payload['state']={'provided_context':payload['state'],'model_extracted_fields':extracted}
        response=self.engine.evaluate(payload)
        for answer in response['answers'].values():
            # As in Jev: confidence is the probability of the most likely answer (a model estimate, not accuracy).
            answer['confidence']=max(answer['probabilities'].values())
        if extracted:response['extracted_fields']=extracted
        if case_info is not None:casefmt.apply_to_response(response,case_info)
        if extracted:
            for answer in response['answers'].values():
                answer['review_reasons'].append('model_extracted_fields');answer['recommendation']='review'
        # Without a title from the user, a short one is generated from the case now that the evaluation is done.
        if title:source='user'
        else:title,source=titles.generate(case_info['case'] if case_info is not None else payload['state'],self.title_models())
        payload['title']=title
        if tags:payload['tags']=tags
        response['title_source']=source
        response['decision_id']=self.store.add_decision(payload,response,title,space)
        record=self.store.get_decision(response['decision_id'],require_verified=True)
        response.update(title=title,tags=tags,created=record['created'],integrity=record['integrity'])
        return response
    def handler(self):
        app=self
        class Handler(BaseHTTPRequestHandler):
            server_version='TypeLogicAI/1.0'
            def log_message(self,*args):pass
            def reply(self,status,value,kind='application/json; charset=utf-8'):
                content=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
                self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(content)))
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                self.end_headers();self.wfile.write(content)
            def local(self):
                expected=f'127.0.0.1:{self.server.server_port}'
                if self.headers.get('Host')!=expected or (self.headers.get('Origin') and self.headers['Origin']!='http://'+expected):self.reply(403,{'error':'Origin or Host not allowed'});return False
                return True
            def auth(self):
                token=self.headers.get('X-TypeLogic-Token','')
                if self.headers.get('Authorization','').startswith('Bearer '):token=self.headers['Authorization'][7:]
                if not hmac.compare_digest(token.encode('utf-8','surrogateescape'),app.token.encode()):self.reply(401,{'error':'Unauthorized session'});return False
                return True
            def do_GET(self):
                if not self.local():return
                parsed=urlparse(self.path);path=parsed.path;query=parse_qs(parsed.query);space=query.get('space',['default'])[0]
                try:
                    if path.startswith('/api/'):
                        if not self.auth():return
                        routes={'/api/decision':lambda:app.store.export_decision(query.get('id',[''])[0],query.get('format',['full'])[0]),'/api/examples':catalogue,'/api/status':lambda:{'engine':app.engine.health(),'mode':app.mode,'restarting':app.restarting,'stats':app.store.stats(),'library_limit':library_archive.LIMIT},'/api/modes':app.mode_info,'/api/needle':app.needle_status,'/api/settings':app.settings_info,'/api/templates':app.template_list,'/api/evidence-backends':app.evidence_backends,'/api/decisions':lambda:{'decisions':app.store.decisions(space,all_rows=True)},'/api/export':app.store.export,'/api/archive':app.archive_info,'/api/tags':tag_list,'/api/training-export':lambda:app.store.training_records(None if query.get('all',['0'])[0]=='1' else space)}
                        if path not in routes:self.reply(404,{'error':'Route not found'});return
                        self.reply(200,routes[path]());return
                    name={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/theme.js':'theme.js'}.get(path)
                    if not name:self.reply(404,{'error':'File not found'});return
                    data=(app.root/'typelogic/web'/name).read_bytes()
                    if name=='index.html':data=data.replace(b'__SESSION_TOKEN__',app.token.encode())
                    self.reply(200,data,(mimetypes.guess_type(name)[0] or 'application/octet-stream')+'; charset=utf-8')
                except ValueError as exc:self.reply(422,{'error':str(exc)})
                except Exception as exc:self.reply(500,{'error':str(exc)})
            def do_POST(self):
                if not self.local() or not self.auth():return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=8*1024*1024:raise ValueError('Invalid body or body exceeds 8 MB')
                    payload=json.loads(self.rfile.read(length),parse_constant=lambda x:(_ for _ in ()).throw(ValueError('JSON must contain finite values')))
                    if not isinstance(payload,dict):raise ValueError('The body must be a JSON object')
                    route=urlparse(self.path).path
                    if route=='/api/evaluate':
                        if app.engine.health()['status']!='ready':
                            self.reply(503,{'error':'Engine is not ready. Wait for loading or open Engine & privacy.','code':'engine_not_ready'});return
                        from .archive import LibraryFull
                        try:self.reply(200,app.evaluate(payload))
                        except LibraryFull as exc:self.reply(409,{'error':str(exc),'code':'library_full','archive':app.archive_info()})
                    elif route=='/api/delete':
                        if 'ids' in payload:self.reply(200,app.store.delete_many(payload['ids']))
                        else:self.reply(200,app.store.delete(payload.get('id')))
                    elif route=='/api/archive':
                        if payload.get('confirm') is not True:raise ValueError('Send {"confirm": true} to archive every decision of the library')
                        try:self.reply(200,app.archive())
                        except (OSError,RuntimeError,subprocess.SubprocessError) as exc:self.reply(500,{'error':'Archive failed; nothing was removed: '+str(exc),'code':'archive_failed'})
                    elif route=='/api/settings':self.reply(200,app.update_settings(payload))
                    elif route=='/api/extract-evidence':
                        try:self.reply(200,app.extract_evidence(payload))
                        except (RuntimeError,OSError) as exc:self.reply(503,{'error':str(exc),'code':'extractor_unavailable'})
                    elif route=='/api/templates':
                        from . import templates
                        self.reply(200,templates.save(payload.get('template'),app.root,app.data,overwrite=payload.get('overwrite') is True))
                    elif route=='/api/feedback':self.reply(200,app.store.feedback(payload.get('id'),payload.get('question_id'),payload.get('target')))
                    elif route=='/api/mode':
                        try:result=app.switch_mode(payload)
                        except RuntimeError as exc:self.reply(409,{'error':str(exc),'code':'mode_unavailable'});return
                        self.reply(202,result)
                    elif route=='/api/extract':
                        from . import needle
                        if not app.needle_status()['available']:
                            self.reply(503,{'error':app.needle_status()['reason'],'code':'needle_unavailable'});return
                        try:self.reply(200,needle.extract(payload.get('text'),payload.get('fields')))
                        except RuntimeError as exc:self.reply(503,{'error':str(exc),'code':'needle_unavailable'})
                    elif route=='/api/load':self.reply(202,{'loading':app.start_engine(),'status':app.engine.health()['status']})
                    else:self.reply(404,{'error':'Route not found'})
                except (ValueError,TypeError,RecursionError) as exc:self.reply(422,{'error':str(exc)})
                except FileNotFoundError as exc:self.reply(503,{'error':str(exc)})
                except Exception as exc:self.reply(500,{'error':'Processing failed: '+str(exc)})
        return Handler

def main():
    parser=argparse.ArgumentParser(description='TypeLogic AI — structured offline decisions',epilog='CLI: tools/run_app.py decide --help; examples: tools/run_app.py examples')
    parser.add_argument('--port',type=int,default=8765);parser.add_argument('--data',type=Path,default=Path.home()/'.typelogic-ai')
    parser.add_argument('--mode',choices=modes.MODES,help='Engine mode: cpu (default), gpu or hybrid; without it the choice saved in the web app is used')
    parser.add_argument('--model-dir',type=Path);parser.add_argument('--specialist',type=Path);parser.add_argument('--no-browser',action='store_true');parser.add_argument('--torch-only',action='store_true');parser.add_argument('--readout',choices=['onnx','rust','torch'],help='Decision-head backend (default onnx, or TYPELOGIC_READOUT)')
    args=parser.parse_args();install();root=Path(__file__).resolve().parents[1]
    engine=None
    if args.specialist:
        from .specialist import SpecialistEngine
        engine=SpecialistEngine(args.specialist)
    app=Application(root,args.data,engine,mode=args.mode,model_dir=args.model_dir,readout=None if args.torch_only else args.readout,use_onnx=not args.torch_only)
    if app.mode=='hybrid':
        from . import ollama
        print('Ollama: '+ollama.ensure_running(app.settings['ollama_model'],models_dir=app.settings['ollama_models']),flush=True)
    server,port_note=bind_server(app.handler(),args.port)
    if port_note:print(port_note,flush=True)
    app.restart=lambda:threading.Thread(target=server.shutdown,daemon=True).start()
    url=f'http://127.0.0.1:{server.server_port}';print('TypeLogic AI: '+url,flush=True);print('Engine mode: '+modes.LABELS.get(app.mode,app.mode),flush=True);print('Offline active. Press Ctrl+C to stop.',flush=True)
    app.start_engine()
    if not args.no_browser:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    stopped=False
    try:server.serve_forever()
    except KeyboardInterrupt:stopped=True
    finally:
        server.server_close()
        app.close(timeout=3 if stopped else None)
    if stopped:
        # Stopped by the user (Ctrl+C in the launcher): do not wait for a model that is still loading in the background.
        sys.stdout.flush();sys.stderr.flush();os._exit(0)
    if app.restarting:
        print('Restarting in the GPU environment (mode: '+app.settings['mode']+')...',flush=True)
        raise SystemExit(modes.RESTART_EXIT_CODE)
