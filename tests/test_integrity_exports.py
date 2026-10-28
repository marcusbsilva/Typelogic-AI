import json,sqlite3,subprocess,sys,threading,http.client
from pathlib import Path
import pytest
from typelogic.store import Store
from typelogic.schema import validate,answers
from typelogic.server import Application
ROOT=Path(__file__).resolve().parents[1]

def payload():
    return {'state':'Invoice charged twice; refund requested.', 'questions':{
        'q1':{'type':'choice','instructions':'Who should review?','criteria':{'A':'Billing','X':'Insufficient evidence'}},
        'q2':{'type':'noul','instructions':'Was a refund requested?'},
        'q3':{'type':'score','instructions':'What is the priority?','criteria':['Low','Normal','High']}}}
def response():
    _,qs,t=validate(payload());return {'answers':answers(qs,[[.9,.1],[.1,.9],[.1,.6,.3]],t,'unvalidated')}
def call(data,*args):
    return subprocess.run([sys.executable,'-I',str(ROOT/'tools/run_app.py'),*args,'--data',str(data)],cwd=data.parent,capture_output=True,text=True,encoding='utf-8')

def test_fingerprints_unique_and_corrections_keep_snapshot(tmp_path):
    db=Store(tmp_path/'db');key=db.add_decision(payload(),response(),'Invoice','default');other=db.add_decision(payload(),response(),'Invoice','default')
    first=db.get_decision(key);hashes=first['integrity'];assert hashes['verified'] and len(hashes['sha512'])==128 and len(hashes['md5'])==32
    assert key!=other and hashes['sha512']!=db.get_decision(other)['integrity']['sha512']
    db.feedback(key,'q1',{'A':1,'X':0})
    assert db.get_decision(key)['integrity']==hashes
    lite=db.export_decision(key,'lite');assert lite['title']=='Invoice' and lite['tags']==[] and set(lite)=={'title','tags','questions'}
    assert lite['questions']==[{'question':'Who should review?','answer':'A'},{'question':'Was a refund requested?','answer':True},{'question':'What is the priority?','answer':'Normal'}]
    assert all(set(q)=={'question','answer'} for q in lite['questions']);db.close()

@pytest.mark.parametrize('column,value',[('title','Modified'),('request',json.dumps({**payload(),'state':'Altered facts'})),('response',json.dumps({'answers':{}})),('sha512',None)])
def test_modified_records_are_flagged_not_resealed_and_not_corrected_or_exported(tmp_path,column,value):
    path=tmp_path/'db';db=Store(path);key=db.add_decision(payload(),response(),'Original','default')
    with db.db:db.db.execute(f'UPDATE decisions SET {column}=? WHERE id=?',(value,key))
    db.close();db=Store(path)
    assert not db.get_decision(key)['integrity']['verified']
    for operation in [lambda:db.export_decision(key,'full'),lambda:db.export_decision(key,'lite'),lambda:db.feedback(key,'q1',{'A':1,'X':0})]:
        with pytest.raises(ValueError,match='integrity'):operation()
    assert db.delete(key)['deleted'] and db.export()['audit']==[];db.close()

def test_old_database_migration_preserves_identity_and_establishes_explicit_baseline(tmp_path):
    path=tmp_path/'old';db=sqlite3.connect(path);db.execute('CREATE TABLE decisions(id TEXT PRIMARY KEY,space TEXT,title TEXT,request TEXT,response TEXT,status TEXT,created TEXT)')
    db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?)',('existing-id','default','Existing',json.dumps(payload()),json.dumps(response()),'approved','2026-10-05T00:00:00+00:00'));db.commit();db.close()
    db=Store(path);r=db.get_decision('existing-id');assert 'status' not in r and r['integrity']['verified'] and r['integrity']['baseline_origin']=='migrated_existing'
    assert 'status' not in {c[1] for c in db.db.execute('PRAGMA table_info(decisions)')}   # the review status column is dropped
    baseline=r['integrity'];db.close();db=Store(path);assert db.get_decision('existing-id')['integrity']==baseline;db.close()

def test_cli_exports_title_and_tags_and_verifies_tampering(tmp_path):
    data=tmp_path/'data';db=Store(data/'typelogic.sqlite3');key=db.add_decision({**payload(),'tags':['billing']},response(),'Invoice','default');db.close()
    result=call(data,'export','--id',key,'--format','lite');assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['title']=='Invoice' and json.loads(result.stdout)['tags']==['billing'] and set(json.loads(result.stdout))=={'title','tags','questions'}
    result=call(data,'verify','--id',key);assert result.returncode==0 and json.loads(result.stdout)['verified']
    db=Store(data/'typelogic.sqlite3')
    with db.db:db.db.execute('UPDATE decisions SET title=? WHERE id=?',('Modified',key))
    db.close();result=call(data,'verify','--id',key);assert result.returncode==1 and not json.loads(result.stdout)['verified']
    assert call(data,'export','--id',key,'--format','lite').returncode==1

def test_session_protected_full_and_lite_exports(tmp_path):
    class Engine:
        lock=threading.RLock()
        def health(self):return {'status':'ready'}
    app=Application(ROOT,tmp_path,Engine());key=app.store.add_decision(payload(),response(),'Invoice','default')
    from http.server import ThreadingHTTPServer
    server=ThreadingHTTPServer(('127.0.0.1',0),app.handler());thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def get(fmt,authorized=True):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port);con.request('GET',f'/api/decision?id={key}&format={fmt}',headers={'X-TypeLogic-Token':app.token} if authorized else {});r=con.getresponse();data=json.loads(r.read());con.close();return r.status,data
    try:
        assert get('lite',False)[0]==401
        status,lite=get('lite');assert status==200 and lite['title']=='Invoice' and set(lite)=={'title','tags','questions'}
        status,full=get('full');assert status==200 and full['integrity']['verified'] and 'status' not in full and full['id']==key
    finally:server.shutdown();server.server_close();app.store.close()
