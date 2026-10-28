import json,math,threading,http.client
from pathlib import Path
import pytest
from typelogic.schema import validate,answers
from typelogic.store import Store
from typelogic.server import Application
from typelogic.metrics import summary
ROOT=Path(__file__).resolve().parents[1]

def request():return {'state':{'customer':'charged twice'},'questions':{'route':{'type':'choice','instructions':'Route this case','criteria':{'A':'billing','B':'technical','X':'insufficient evidence'},'abstain_option':'X'}}}

def test_typed_abstention_is_always_reviewed():
    _,qs,t=validate(request());a=answers(qs,[[.01,.01,.98]],t,'unvalidated')['route']
    assert a['choice']=='X' and a['abstained'] and a['recommendation']=='review'
    assert a['requires_review'] and not a['eligible_for_automation'] and not a['concentration_is_correctness_probability']

@pytest.mark.parametrize('mutation',[lambda p:p.update(threshold=float('nan')),lambda p:p.update(threshold=True),lambda p:p.update(permutation_check='true'),lambda p:p['questions']['route'].update(abstain_option='undeclared'),lambda p:p['questions']['route'].update(criteria=['same','same']),lambda p:p.update(state=''),lambda p:p['questions']['route'].update(type='free_text')])
def test_invalid_contracts_rejected(mutation):
    p=request();mutation(p)
    with pytest.raises(ValueError):validate(p)

@pytest.mark.parametrize('values',[[float('nan'),.1,.9],[-.1,.2,.9],[0,0,0],[.5,.5]])
def test_invalid_distributions_rejected(values):
    _,qs,t=validate(request())
    with pytest.raises(RuntimeError):answers(qs,[values],t,'unvalidated')

def test_binary_and_ordinal_are_normalized():
    p={'state':'test','questions':{'bool':{'type':'noul','instructions':'Can it refund?'},'level':{'type':'score','instructions':'severity','criteria':['low','medium','high']}}}
    _,qs,t=validate(p);out=answers(qs,[[2,8],[1,2,7]],t,'unvalidated')
    assert out['bool']['noul']==.8 and out['level']['score']==pytest.approx(1.6)
    assert sum(out['level']['probabilities'].values())==pytest.approx(1)

@pytest.fixture
def store(tmp_path):
    db=Store(tmp_path/'workspace.sqlite3');yield db;db.close()

def test_feedback_is_checked_and_delete_never_executes_actions(store):
    _,qs,t=validate(request());resp={'answers':answers(qs,[[.9,.09,.01]],t,'unvalidated')}
    key=store.add_decision(request(),resp,'Review','default')
    assert len(store.export()['audit'])==1
    with pytest.raises(ValueError):store.feedback(key,'route',{'A':True,'B':0,'X':0})
    assert store.feedback(key,'route',{'A':1,'B':0,'X':0})['retrained'] is False
    assert not store.delete(key)['external_action_executed'] and store.export()=={'decisions':[],'feedback':[],'audit':[]}

class FakeEngine:
    def __init__(self):self.lock=threading.RLock();self.payload=None
    def evaluate(self,p):
        self.payload=p;_,qs,t=validate(p);return {'answers':answers(qs,[[.95,.04,.01]],t,'unvalidated'),'external_action_executed':False}
    def health(self):return {'status':'ready'}

def test_memory_and_exact_check_keys_are_not_read(tmp_path):
    """Local memory and exact checks were removed: the old keys never reach the engine or the response."""
    engine=FakeEngine();app=Application(ROOT,tmp_path,engine)
    try:
        p=request();p.update(use_memory=True,memory_query='refund billing',exact_checks=[{'op':'money_sum','values':['0.10','0.20'],'expected':'0.40'}])
        out=app.evaluate(p)
        assert 'reference_documents' not in json.dumps(engine.payload) and 'exact_checks' not in out and 'sources' not in out
        assert 'exact_rule_mismatch' not in out['answers']['route']['review_reasons']
    finally:app.store.close()

def test_loopback_api_requires_session_host_and_origin(tmp_path):
    from http.server import ThreadingHTTPServer
    app=Application(ROOT,tmp_path,FakeEngine());server=ThreadingHTTPServer(('127.0.0.1',0),app.handler());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(path,headers=None):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port);con.request('GET',path,headers=headers or {});r=con.getresponse();status=r.status;body=r.read();con.close();return status,body
    try:
        assert get('/api/status')[0]==401
        assert get('/api/status',{'X-TypeLogic-Token':app.token})[0]==200
        assert get('/api/status',{'X-TypeLogic-Token':app.token,'Origin':'https://attacker.invalid'})[0]==403
        assert get('/api/status',{'X-TypeLogic-Token':app.token,'Host':'attacker.invalid'})[0]==403
        assert get('/../../README.md')[0]==404
        assert b'TypeLogic AI' in get('/')[1]
    finally:server.shutdown();server.server_close();thread.join();app.store.close()

def test_metrics_count_failures_and_report_answered_only_losses():
    rows=[{'keys':['a','b'],'target':[1,0],'probabilities':[.9,.1],'candidate':True},{'keys':['a','b'],'target':[0,1],'probabilities':None}]
    out=summary(rows)
    assert out['accuracy_including_failures']==.5 and out['failures']==1 and out['candidate_coverage']==.5
    assert out['nll_responded_only']==pytest.approx(-math.log(.9)) and out['brier_responded_only']==pytest.approx(.02)

def test_offline_connections_are_blocked_in_subprocess():
    import subprocess,sys
    code=f"import sys; sys.path.insert(0, {str(ROOT)!r}); from typelogic.offline import install; install(); import socket; socket.socket().connect(('8.8.8.8',443))"
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode!=0 and 'PermissionError' in result.stderr and 'blocked' in result.stderr


def test_bootstrap_works_with_isolated_python_from_another_folder(tmp_path):
    import subprocess,sys
    result=subprocess.run([sys.executable,'-I',str(ROOT/'tools/run_app.py'),'--help'],cwd=tmp_path,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
    assert '--model-dir' in result.stdout and '--specialist' in result.stdout

def test_isolated_bootstrap_serves_local_ui(tmp_path):
    import subprocess,sys,queue,urllib.request
    events=queue.Queue()
    process=subprocess.Popen([sys.executable,'-I',str(ROOT/'tools/run_app.py'),'--no-browser','--port','0','--data',str(tmp_path/'data')],cwd=tmp_path,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
    def read_lines():
        for line in process.stdout:events.put(line)
    reader=threading.Thread(target=read_lines,daemon=True);reader.start()
    try:
        first=events.get(timeout=15);assert first.startswith('TypeLogic AI: '),first
        url=first.split(': ',1)[1].strip()
        with urllib.request.urlopen(url,timeout=5) as response:assert response.status==200 and b'TypeLogic AI' in response.read()
    finally:process.terminate();process.wait(timeout=10);reader.join(timeout=5)

class LoadingEngine:
    def __init__(self):
        self.lock=threading.RLock();self.status='not_loaded';self.error=None;self.calls=0;self.entered=threading.Event();self.release=threading.Event()
    def health(self):return {'status':self.status,'error':self.error}
    def load(self):
        self.calls+=1;self.entered.set();assert self.release.wait(5);self.status='ready'

def test_background_load_is_nonblocking_and_coalesces_requests(tmp_path):
    engine=LoadingEngine();app=Application(ROOT,tmp_path,engine)
    try:
        assert app.start_engine();assert engine.entered.wait(3)
        assert engine.health()['status']=='loading'
        for _ in range(5):assert app.start_engine()
        assert engine.calls==1
        engine.release.set();app.load_thread.join(5)
        assert engine.status=='ready' and app.start_engine() is False and engine.calls==1
    finally:engine.release.set();app.load_thread.join(5);app.store.close()

def test_background_load_failure_remains_visible_and_can_retry(tmp_path):
    class FailureEngine(LoadingEngine):
        def load(self):
            self.calls+=1
            if self.calls==1:raise FileNotFoundError('local weights missing')
            self.status='ready'
    engine=FailureEngine();app=Application(ROOT,tmp_path,engine)
    try:
        app.start_engine();app.load_thread.join(5)
        assert engine.status=='error' and 'local weights missing' in engine.error
        app.start_engine();app.load_thread.join(5)
        assert engine.status=='ready' and engine.error is None and engine.calls==2
    finally:app.store.close()

def test_http_remains_responsive_while_engine_loads_and_blocks_evaluation(tmp_path):
    from http.server import ThreadingHTTPServer
    engine=LoadingEngine();app=Application(ROOT,tmp_path,engine);server=ThreadingHTTPServer(('127.0.0.1',0),app.handler());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();app.start_engine()
    try:
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
        con.request('GET','/api/status',headers={'X-TypeLogic-Token':app.token});response=con.getresponse();assert response.status==200 and json.loads(response.read())['engine']['status']=='loading'
        con.request('POST','/api/evaluate',body=json.dumps(request()),headers={'X-TypeLogic-Token':app.token,'Content-Type':'application/json'});response=con.getresponse();assert response.status==503 and json.loads(response.read())['code']=='engine_not_ready';con.close()
        assert app.store.stats()['decisions']==0
    finally:engine.release.set();app.load_thread.join(5);server.shutdown();server.server_close();thread.join();app.store.close()
