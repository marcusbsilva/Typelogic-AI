"""Local SQLite workspace: decisions (with title and tags), corrections, provenance and the audit trail."""
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid
from .core import canonical,digests,verification,lite

def now():return datetime.now(timezone.utc).isoformat()

class Store:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,space TEXT NOT NULL,title TEXT NOT NULL,request TEXT NOT NULL,response TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,decision_id TEXT NOT NULL REFERENCES decisions(id),question_id TEXT NOT NULL,target TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,decision_id TEXT,action TEXT NOT NULL,details TEXT NOT NULL,created TEXT NOT NULL);
        ''')
        columns={r['name'] for r in self.db.execute('PRAGMA table_info(decisions)')}
        self.legacy_status='status' in columns
        if self.legacy_status:
            # Databases from versions with approve / deny review: drop rows discarded there (with their related
            # records), then the status column itself. Hashes never covered the status, so records stay verified.
            with self.db:
                for table in ('feedback','audit'):
                    self.db.execute(f"DELETE FROM {table} WHERE decision_id IN (SELECT id FROM decisions WHERE status='dismissed')")
                self.db.execute("DELETE FROM decisions WHERE status='dismissed'")
            try:
                with self.db:self.db.execute('ALTER TABLE decisions DROP COLUMN status')
                self.legacy_status=False
            except sqlite3.OperationalError:pass   # SQLite older than 3.35: keep the column and fill it with a constant
        migrating='sha512' not in columns or 'md5' not in columns
        with self.db:
            for name in ('sha512','md5','baseline_origin'):
                if name not in columns:self.db.execute(f'ALTER TABLE decisions ADD COLUMN {name} TEXT')
            if migrating:
                for row in self.db.execute('SELECT * FROM decisions WHERE sha512 IS NULL AND md5 IS NULL').fetchall():
                    record=dict(row);record['request']=json.loads(record['request']);record['response']=json.loads(record['response']);values=digests(record)
                    self.db.execute('UPDATE decisions SET sha512=?,md5=?,baseline_origin=? WHERE id=?',(values['sha512'],values['md5'],'migrated_existing',record['id']))
    def close(self):
        with self.lock:self.db.close()
    def add_decision(self,request,response,title,space):
        key=uuid.uuid4().hex;created=now()
        record={'id':key,'space':space,'title':title,'created':created,'request':request,'response':response}
        values=digests(record)
        with self.lock,self.db:
            columns,extra=('id,space,title,request,response,created,sha512,md5,baseline_origin',()) if not self.legacy_status else ('id,space,title,request,response,created,sha512,md5,baseline_origin,status',('recorded',))
            self.db.execute(f'INSERT INTO decisions({columns}) VALUES({",".join("?"*(9+len(extra)))})',(key,space,title,canonical(request),canonical(response),created,values['sha512'],values['md5'],'created',*extra))
            self.db.execute('INSERT INTO audit(decision_id,action,details,created) VALUES(?,?,?,?)',(key,'created','{}',now()))
        return key
    def count(self):
        with self.lock:return self.db.execute('SELECT count(*) FROM decisions').fetchone()[0]
    def decisions(self,space=None,all_rows=False):
        with self.lock:
            sql='SELECT * FROM decisions'+(' WHERE space=?' if space is not None else '')+' ORDER BY created DESC,rowid DESC'+('' if all_rows else ' LIMIT 500')   # rowid breaks ties of equal timestamps (coarse Windows clock)
            return [self.decode_record(r) for r in self.db.execute(sql,() if space is None else (space,))]
    def decode_record(self,row):
        # A row altered outside the application (invalid JSON, NaN, wrong types) is reported as unverified instead of
        # breaking every listing, so it can still be inspected and deleted.
        record=dict(row);error=None
        try:
            record['request']=json.loads(record['request']);record['response']=json.loads(record['response'])
            if not isinstance(record['request'],dict) or not isinstance(record['response'],dict):raise ValueError('request and response must be JSON objects')
            record['integrity']=verification(record)
        except Exception as exc:
            error=f'{type(exc).__name__}: {str(exc)[:200]}'
        if error is not None:
            for name in ('request','response'):
                if not isinstance(record[name],dict):record[name]={'unreadable':str(record[name])[:2000]}
            record['response'].setdefault('answers',{})
            record['integrity']={'verified':False,'error':'Stored record is unreadable: '+error}
        for name in ('sha512','md5','baseline_origin','status'):record.pop(name,None)
        return record
    def get_decision(self,key,require_verified=False):
        with self.lock:
            row=self.db.execute('SELECT * FROM decisions WHERE id=?',(key,)).fetchone()
            if not row:raise ValueError('Decision not found')
            record=self.decode_record(row)
            if require_verified and not record['integrity']['verified']:raise ValueError('Decision integrity verification failed')
            return record
    def export_decision(self,key,format='full'):
        if format not in ('full','lite'):raise ValueError('Unknown export format')
        record=self.get_decision(key,require_verified=True)
        return lite(record) if format=='lite' else record
    def delete(self,key):
        """Delete one decision with its corrections and audit records (works for records that fail verification)."""
        if not isinstance(key,str) or not 1<=len(key)<=128:raise ValueError('Invalid decision id')
        with self.lock,self.db:
            found=self.db.execute('SELECT 1 FROM decisions WHERE id=?',(key,)).fetchone()
            for table,column in (('feedback','decision_id'),('audit','decision_id'),('decisions','id')):
                self.db.execute(f'DELETE FROM {table} WHERE {column}=?',(key,))
        return {'id':key,'deleted':bool(found),'external_action_executed':False}
    def delete_many(self,ids):
        """Delete several decisions; each one succeeds or reports its own error."""
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or len(set(map(str,ids)))!=len(ids):raise ValueError('ids: a list of 1-500 distinct decision ids')
        results=[]
        for key in ids:
            try:results.append(self.delete(key))
            except ValueError as exc:results.append({'id':key,'error':str(exc)})
        return {'results':results,'deleted':sum(bool(r.get('deleted')) for r in results),'external_action_executed':False}
    def audit_trail(self,key):
        with self.lock:return [dict(r) for r in self.db.execute('SELECT action,details,created FROM audit WHERE decision_id=? ORDER BY id',(key,))]
    def feedback(self,key,question_id,target):
        if not isinstance(key,str) or not 1<=len(key)<=128 or not isinstance(question_id,str):raise ValueError('Invalid decision or question id')
        with self.lock,self.db:
            self.get_decision(key,require_verified=True)
            row=self.db.execute('SELECT response FROM decisions WHERE id=?',(key,)).fetchone()
            if not row:raise ValueError('Decision not found')
            answer=json.loads(row['response'])['answers'].get(question_id)
            if not answer or not isinstance(target,dict) or set(target)!=set(answer['probabilities']) or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in target.values()) or abs(sum(target.values())-1)>1e-6:raise ValueError('Invalid target distribution')
            self.db.execute('INSERT INTO feedback VALUES(?,?,?,?,?)',(uuid.uuid4().hex,key,question_id,canonical(target),now()))
            self.db.execute('INSERT INTO audit(decision_id,action,details,created) VALUES(?,?,?,?)',(key,'feedback',canonical({'question_id':question_id}),now()))
        return {'saved':True,'retrained':False}
    def training_records(self,space=None):
        """Corrected decisions as training rows (state, questions, targets) for tools/build_training_mix.py --feedback.

        A recorded correction is the target of its question (the latest one wins). Decisions without corrections, and
        records whose integrity check fails, are left out. Decisions about the same state (the
        same scenario evaluated again) are merged into one row whose group_id is a hash of that state, so a scenario
        never ends up in both training and validation. Nothing is retrained here."""
        merged,skipped=OrderedDict(),{'integrity_failed':0,'uncorrected':0}
        with self.lock:
            corrections={}
            for f in self.db.execute('SELECT decision_id,question_id,target FROM feedback ORDER BY created,rowid'):
                corrections.setdefault(f['decision_id'],{})[f['question_id']]=json.loads(f['target'])
            for r in reversed(self.decisions(space,all_rows=True)):       # oldest first: later reviews win
                if not r['integrity']['verified']:skipped['integrity_failed']+=1;continue
                fixed=corrections.get(r['id'],{});found=[]
                for qid,a in (r['response'].get('answers') or {}).items():
                    if qid not in r['request'].get('questions',{}) or not a.get('probabilities'):continue
                    if qid in fixed:found.append((qid,fixed[qid],'correction'))
                if not found:skipped['uncorrected']+=1;continue
                state=r['request']['state']
                group='state-'+hashlib.sha256(canonical(state).encode()).hexdigest()[:24]
                row=merged.setdefault(group,{'id':group,'group_id':group,'state':state,'questions':{},'targets':{},
                                             'source_id':'typelogic-feedback','domain':r['space'],'provenance':{},'decisions':[]})
                row['decisions'].append(r['id'])
                for qid,target,origin in found:
                    spec=r['request']['questions'][qid];key=qid;n=2
                    while key in row['questions'] and row['questions'][key]!=spec:key=f'{qid}_{n}';n+=1   # a different question under the same id
                    row['questions'][key]=spec;row['targets'][key]=target;row['provenance'][key]=origin
        rows=list(merged.values())
        return {'records':rows,'skipped':skipped,'corrections':sum(v=='correction' for r in rows for v in r['provenance'].values())}
    def stats(self):
        with self.lock:return {'decisions':self.count(),'feedback':self.db.execute('SELECT count(*) FROM feedback').fetchone()[0]}
    def export(self):
        with self.lock:return {'decisions':self.decisions(all_rows=True),'feedback':[dict(r) for r in self.db.execute('SELECT * FROM feedback')],'audit':[dict(r) for r in self.db.execute('SELECT * FROM audit')]}
