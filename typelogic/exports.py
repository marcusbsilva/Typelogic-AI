"""Stable record fingerprints and the intentionally small Lite export contract."""
import hashlib
import hmac
import json

FIELDS=('id','space','title','created','request','response')
def canonical_bytes(record):
    payload={key:record[key] for key in FIELDS}
    return json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
def digests(record):
    data=canonical_bytes(record)
    return {'sha512':hashlib.sha512(data).hexdigest(),'md5':hashlib.md5(data,usedforsecurity=False).hexdigest()}
def verification(record):
    actual=digests(record)
    matches={key:hmac.compare_digest(str(record.get(key) or ''),value) for key,value in actual.items()}
    return {'format':'typelogic-record-v1','sha512':record.get('sha512'),'md5':record.get('md5'),
            'sha512_matches':matches['sha512'],'md5_matches':matches['md5'],'verified':all(matches.values()),
            'baseline_origin':record.get('baseline_origin','unknown'),
            'scope':'Immutable id, space, title, created, request and response. Checksums are not signatures.'}
def lite(record):
    questions=record['request'].get('questions',{})
    rows=[]
    for key,answer in record['response']['answers'].items():
        kind=answer['type']
        if kind=='choice':value=answer['choice']
        elif kind=='noul':value=bool(answer['noul']>.5)
        else:
            winner=max(answer['probabilities'],key=lambda key:(answer['probabilities'][key],-int(key)))
            value=answer.get('legend',{}).get(winner,winner)
        rows.append({'question':answer.get('instructions') or questions.get(key,{}).get('instructions',key),'answer':value})
    return {'title':record['title'],'tags':record['request'].get('tags') or [],'questions':rows}


def export_filename(decision_id,format='full'):
    """Eight case-insensitive Base32 characters; the full ID remains authoritative."""
    import base64
    if not isinstance(decision_id,str) or not 1<=len(decision_id)<=128 or format not in ('full','lite'):raise ValueError('Invalid decision ID or format')
    digest=hashlib.sha256(f'{decision_id}:{format}'.encode('utf-8')).digest()[:5]
    return base64.b32encode(digest).decode('ascii').lower()+'.json'
