"""Explicit online research only. Never imported by the offline app."""
import json,math,os,sqlite3,time,urllib.request,urllib.error
from pathlib import Path
from typelogic.learning import distribution
from typelogic.schema import validate

class BudgetLedger:
    def __init__(self,path,limit):
        if isinstance(limit,bool) or not isinstance(limit,(int,float)) or not math.isfinite(limit) or limit<=0:raise ValueError('Invalid budget')
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.limit=limit
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, amount REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS costs(id INTEGER PRIMARY KEY,model TEXT NOT NULL,cost REAL NOT NULL,generation TEXT,created REAL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS cost_generation ON costs(generation)')
    @property
    def reserved(self):
        with sqlite3.connect(self.path) as db:return db.execute('SELECT COALESCE(SUM(amount),0) FROM reservations').fetchone()[0]
    def reserve(self,amount):
        if isinstance(amount,bool) or not isinstance(amount,(int,float)) or not math.isfinite(amount) or amount<0:raise ValueError('Invalid reservation')
        with sqlite3.connect(self.path,timeout=30,isolation_level=None) as db:
            db.execute('BEGIN IMMEDIATE')
            total=db.execute('SELECT COALESCE(SUM(amount),0) FROM reservations').fetchone()[0]
            if total+amount>self.limit:db.rollback();raise ValueError('Research budget reached')
            db.execute('INSERT INTO reservations(amount) VALUES(?)',(amount,));db.commit()
    def record(self,model,cost,generation):
        if not isinstance(cost,(int,float)) or not math.isfinite(cost) or cost<0:raise ValueError('Invalid reported cost')
        with sqlite3.connect(self.path) as db:db.execute('INSERT OR IGNORE INTO costs(model,cost,generation,created) VALUES(?,?,?,?)',(model,cost,generation,time.time()))
    def report(self):
        with sqlite3.connect(self.path) as db:
            rows=db.execute('SELECT model,COUNT(*),SUM(cost) FROM costs GROUP BY model').fetchall()
        return {'limit_usd':self.limit,'reserved_usd':self.reserved,'reported_cost_usd':sum(r[2] for r in rows),'models':[{'model':r[0],'requests':r[1],'cost_usd':r[2]} for r in rows],'unknown_requests_remain_reserved':True}

def parse_decision(payload,data):
    _,qs,_=validate(payload);source=data.get('answers')
    if not isinstance(source,dict) or set(source)!={q['id'] for q in qs}:raise ValueError('Teacher question IDs mismatch')
    result={}
    for q in qs:
        a=source[q['id']]
        if 'probabilities' in a:
            raw=a['probabilities']
            if not isinstance(raw,dict) or set(raw)!=set(q['keys']):raise ValueError('Teacher alternative IDs mismatch')
            values=[raw[k] for k in q['keys']]
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in values):raise ValueError('Invalid teacher probabilities')
            total=sum(values)
            if not math.isfinite(total) or abs(total-1)>.005:raise ValueError('Teacher distribution sum exceeds wire rounding tolerance')
            result[q['id']]=distribution([v/total for v in values],q['keys'])
        elif q['type']=='noul' and isinstance(a.get('noul'),(float,int)):
            result[q['id']]=distribution([1-a['noul'],a['noul']],q['keys'])
        else:raise ValueError('Teacher did not return a complete distribution')
    return result

class TeacherResponseError(ValueError):
    def __init__(self,message,raw):
        super().__init__(message);self.raw=raw

class ResearchClient:
    def __init__(self,ledger,catalog,key=None):
        self.ledger=ledger;self.catalog={m['id']:m for m in catalog};self.key=key or os.environ.get('OPENROUTER_API_KEY')
        if not self.key:raise ValueError('Set OPENROUTER_API_KEY for explicit research')
    def decide(self,model,payload):
        if model not in self.catalog:raise ValueError('Model missing from live decisions catalog: '+model)
        body={'model':model,'state':payload['state'],'questions':payload['questions']}
        encoded=json.dumps(body,ensure_ascii=False).encode()
        price=float(self.catalog[model]['pricing']['prompt'])
        # One UTF8 byte per input token is conservative; include schema/provider overhead.
        reserve=(len(encoded)+4096)*price
        self.ledger.reserve(reserve)
        request=urllib.request.Request('https://openrouter.ai/api/v1/systemone',data=encoded,headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json','X-Title':'TypeLogic Training Research'})
        began=time.perf_counter()
        try:
            with urllib.request.urlopen(request,timeout=60) as response:data=json.load(response)
        except urllib.error.HTTPError as exc:raise RuntimeError('OpenRouter HTTP '+str(exc.code)) from None
        cost=data.get('usage',{}).get('cost')
        if cost is not None:self.ledger.record(model,cost,data.get('id'))
        try:rows=parse_decision(payload,data)
        except ValueError as exc:raise TeacherResponseError(str(exc),data) from None
        return {'requested_model':model,'actual_model':data.get('model'),'provider':data.get('provider'),'generation_id':data.get('id'),'probabilities':rows,'usage':data.get('usage',{}),'latency_ms':(time.perf_counter()-began)*1000,'raw':data}
