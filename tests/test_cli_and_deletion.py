import json
from pathlib import Path
import subprocess
import sys
from typelogic.store import Store
from typelogic.examples import catalogue
from typelogic.schema import validate

ROOT=Path(__file__).resolve().parents[1]
def cli(tmp_path,*args,input=None):
    return subprocess.run([sys.executable,'-I',str(ROOT/'tools/run_app.py'),*args],cwd=tmp_path,input=input,text=True,encoding='utf-8',capture_output=True)

def test_discard_removes_related_data_and_is_retryable(tmp_path):
    store=Store(tmp_path/'db.sqlite3')
    try:
        first=store.add_decision({'state':'sensitive content'},{'answers':{'q':{'probabilities':{'false':.1,'true':.9}}}},'to delete','default')
        second=store.add_decision({}, {'answers':{}},'keep','default')
        store.feedback(first,'q',{'false':0,'true':1})
        result=store.delete(first)
        assert result['deleted'] and not result['external_action_executed']
        for table in ['feedback','audit']:
            assert store.db.execute(f'SELECT count(*) FROM {table} WHERE decision_id=?',(first,)).fetchone()[0]==0
        assert [r['id'] for r in store.decisions()]==[second]
        assert first not in json.dumps(store.export()) and 'sensitive content' not in json.dumps(store.export())
        assert store.stats()=={'decisions':1,'feedback':0}
        assert store.delete(first)['deleted'] is False
    finally:store.close()

def test_legacy_discard_is_removed_on_open(tmp_path):
    path=tmp_path/'db.sqlite3';store=Store(path)
    key=store.add_decision({}, {'answers':{'q':{'probabilities':{'a':1,'b':0}}}},'old','default');store.feedback(key,'q',{'a':1,'b':0})
    with store.db:   # as left by a version with review statuses
        store.db.execute("ALTER TABLE decisions ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        store.db.execute("UPDATE decisions SET status='dismissed' WHERE id=?",(key,))
    store.close();store=Store(path)
    try:assert store.export()=={'decisions':[],'feedback':[],'audit':[]}
    finally:store.close()

def test_examples_are_valid_and_available_without_model(tmp_path):
    rows=catalogue()['examples'];assert len(rows)>=8
    for row in rows:validate(row['payload'])
    result=cli(tmp_path,'examples','--id','purchasing')
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout);assert payload['questions'] and 'exact_checks' not in payload
    assert not (Path.home()/'.typelogic-ai/unused-test').exists()

def test_cli_export_never_overwrites_and_memory_commands_are_gone(tmp_path):
    data=tmp_path/'dados com espaços';args=['--data',str(data)]
    output=tmp_path/'result.json';output.write_text('preserve')
    result=cli(tmp_path,'export',*args,'--output',str(output))
    assert result.returncode==1 and output.read_text()=='preserve'
    result=cli(tmp_path,'status',*args);assert json.loads(result.stdout)['stats']['decisions']==0
    for removed in ('import','search'):
        assert cli(tmp_path,removed,*args).returncode!=0

def test_cli_invalid_input_does_not_create_decision(tmp_path):
    result=cli(tmp_path,'decide','--data',str(tmp_path/'data'),'--input','-',input='{"state":"", "questions":{}}')
    assert result.returncode==1 and not result.stdout
    assert json.loads(result.stderr)['error']
    assert not (tmp_path/'data/typelogic.sqlite3').exists()

def test_cli_delete_tags_and_list_by_tag(tmp_path):
    data=tmp_path/'data';store=Store(data/'typelogic.sqlite3')
    key=store.add_decision({}, {'answers':{}},'discard','default')
    tagged=store.add_decision({'tags':['billing','my tag']}, {'answers':{}},'tagged','default');store.close()
    result=cli(tmp_path,'list','--data',str(data),'--tag','My  Tag');assert [r['id'] for r in json.loads(result.stdout)['decisions']]==[tagged]
    result=cli(tmp_path,'tags','--data',str(data));tags=json.loads(result.stdout)
    assert tags['used']=={'billing':1,'my tag':1} and 'billing' in tags['suggested']
    result=cli(tmp_path,'delete','--data',str(data),'--id',key)
    assert result.returncode==0 and json.loads(result.stdout)['deleted']
    result=cli(tmp_path,'list','--data',str(data));assert [r['id'] for r in json.loads(result.stdout)['decisions']]==[tagged]
    for removed in ('review','queue'):
        assert cli(tmp_path,removed,'--data',str(data)).returncode!=0
    result=cli(tmp_path,'archive','--data',str(data))       # without --yes: only what would happen
    assert result.returncode==0 and json.loads(result.stdout)['count']==1 and '--yes' in result.stderr


def test_export_and_cli_list_include_more_than_history_window(tmp_path):
    store=Store(tmp_path/'data/typelogic.sqlite3')
    try:
        for i in range(501):store.add_decision({}, {'answers':{}},str(i),'default')
        assert len(store.decisions())==500
        assert len(store.export()['decisions'])==501
        result=cli(tmp_path,'list','--data',str(tmp_path/'data'))
        assert result.returncode==0 and len(json.loads(result.stdout)['decisions'])==501
    finally:store.close()
