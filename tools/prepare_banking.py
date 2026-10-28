"""Create reproducible, stratified natural-language samples from official raw files."""
import csv,hashlib,json,random,sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.learning import assert_disjoint,normalized_state,file_sha256
ROOT=Path(__file__).resolve().parents[1]/'research/data'

def main():
    raw=ROOT/'raw';out=ROOT/'prepared';out.mkdir(parents=True,exist_ok=True)
    train=list(csv.DictReader((raw/'banking77-train.csv').open()));test=list(csv.DictReader((raw/'banking77-test.csv').open()))
    labels=sorted({r['category'] for r in train});criteria={k:k.replace('_',' ').replace('?','') for k in labels}
    rng=random.Random(1701);seen=set();splits={k:[] for k in ['train','validation','calibration','test']}
    for original,alloc in [(train,[('train',8),('validation',1),('calibration',1)]),(test,[('test',1)])]:
        groups=defaultdict(list)
        for r in original:groups[r['category']].append(r)
        for label in labels:
            choices=groups[label][:];rng.shuffle(choices);cursor=0
            for split,count in alloc:
                accepted=0
                while accepted<count:
                    r=choices[cursor];cursor+=1;n=normalized_state(r['text'])
                    if n in seen:continue
                    seen.add(n);i=labels.index(label);key=hashlib.sha256(n.encode()).hexdigest()
                    splits[split].append({'id':'banking77-'+key[:16],'group_id':key,'source':'BANKING77','split':split,'state':r['text'],'questions':{'intent':{'type':'choice','instructions':'Identify the banking customer intent. Select the single best matching intent.','criteria':criteria}},'targets':{'intent':[float(j==i) for j in range(len(labels))]},'label':label,'permutation_check':False})
                    accepted+=1
    # CLINC150 has broader, independently named banking intents; keep a separate ontology.
    clinc=json.loads((raw/'clinc150.json').read_text())
    bank={'balance','freeze_account','card_declined','pin_change','report_lost_card','damaged_card','transactions','bill_balance','bill_due','pay_bill','transfer','international_fees','replacement_card_duration','expiration_date','account_blocked','interest_rate','minimum_payment','routing','direct_deposit','order_checks'}
    bank &= {label for _,label in clinc['train']};bank=sorted(bank);c={k:k.replace('_',' ') for k in bank};cs={k:[] for k in ['train','validation','calibration','test']}
    for split,orig in [('train','train'),('validation','val'),('test','test')]:
        by=defaultdict(list)
        for text,label in clinc[orig]:
            if label in bank:by[label].append(text)
        for label in bank:
            rng.shuffle(by[label]);count=4 if split=='train' else 2
            for text in by[label][:count]:
                n=normalized_state(text)
                if n in seen:continue
                seen.add(n);key=hashlib.sha256(n.encode()).hexdigest();dest='calibration' if split=='validation' and text==by[label][1] else split
                cs[dest].append({'id':'clinc150-'+key[:16],'group_id':key,'source':'CLINC150 banking subset','split':dest,'state':text,'questions':{'intent':{'type':'choice','instructions':'Identify the banking customer intent.','criteria':c}},'targets':{'intent':[float(l==label) for l in bank]},'label':label,'permutation_check':False})
    assert_disjoint(splits);assert_disjoint(cs)
    for name,rows in splits.items():(out/('banking77-'+name+'.jsonl')).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    for name,rows in cs.items():(out/('clinc150-banking-'+name+'.jsonl')).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    manifest={'seed':1701,'banking77':{k:len(v) for k,v in splits.items()},'clinc150_banking':{k:len(v) for k,v in cs.items()},'banking77_labels':labels,'clinc_labels':bank,'disjoint':True,'selection':'Stratified deterministic sample, original official test only for final test; normalized duplicates removed globally','files':{p.name:file_sha256(p) for p in sorted(raw.iterdir())},'prepared':{p.name:file_sha256(p) for p in sorted(out.iterdir())},'licenses':{'BANKING77':'CC BY 4.0; PolyAI-LDN/task-specific-datasets','CLINC150':'CC BY 3.0; clinc/oos-eval'},'warning':'These public datasets may have appeared in upstream pretraining. Split separation here does not prove foundation-model decontamination.'}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2));print(json.dumps({k:manifest[k] for k in ['banking77','clinc150_banking']}))
if __name__=='__main__':main()
