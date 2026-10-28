import json
import math

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)

def validate(payload):
    if not isinstance(payload,dict) or 'state' not in payload: raise ValueError('Provide an object with state and questions')
    state = payload['state'] if isinstance(payload['state'],str) else canonical(payload['state'])
    if not state.strip() or len(state)>120000: raise ValueError('Scenario: 1–120,000 characters')
    source = payload.get('questions')
    if not isinstance(source,dict) or not 1<=len(source)<=32: raise ValueError('Provide 1–32 questions')
    questions=[]
    for key,q in source.items():
        if not isinstance(key,str) or not 1<=len(key)<=120 or not isinstance(q,dict): raise ValueError('Invalid question')
        kind,instr,criteria=q.get('type'),q.get('instructions'),q.get('criteria')
        if kind not in ('noul','choice','score') or not isinstance(instr,str) or not instr.strip() or len(instr)>6000: raise ValueError('Invalid type or instruction')
        if kind=='noul':
            criteria=criteria or {}
            if not isinstance(criteria,dict) or set(criteria)-{'false','true'} or any(not isinstance(v,str) or len(v)>3000 for v in criteria.values()): raise ValueError('Invalid binary criteria')
            keys=['false','true']; options=['no'+(': '+criteria['false'] if criteria.get('false') else ''),'yes'+(': '+criteria['true'] if criteria.get('true') else '')]
        elif kind=='choice':
            if isinstance(criteria,list) and all(isinstance(v,str) for v in criteria):
                if len(set(criteria))!=len(criteria): raise ValueError('Duplicate alternatives')
                criteria={v:'' for v in criteria}
            if not isinstance(criteria,dict) or not 2<=len(criteria)<=255: raise ValueError('Choice: 2–255 alternatives')
            if any(not isinstance(k,str) or not k.strip() or len(k)>200 or not isinstance(v,(str,type(None))) or (v and len(v)>3000) for k,v in criteria.items()): raise ValueError('Invalid alternative')
            criteria={k:v or '' for k,v in criteria.items()};keys=list(criteria);options=[k+(': '+criteria[k] if criteria[k] else '') for k in keys]
        else:
            if not isinstance(criteria,list) or not 2<=len(criteria)<=32 or any(not isinstance(v,str) or not v.strip() or len(v)>3000 for v in criteria) or len(set(criteria))!=len(criteria): raise ValueError('Score: 2–32 unique ordered levels')
            keys=[str(i) for i in range(len(criteria))];options=criteria
        abstain=q.get('abstain_option')
        if abstain is not None and (kind!='choice' or abstain not in keys): raise ValueError('abstain_option must identify a declared Choice alternative')
        questions.append({'id':key,'type':kind,'instr':instr,'keys':keys,'options':options,'criteria':criteria,'abstain_option':abstain})
    threshold=payload.get('threshold',.85)
    if isinstance(threshold,bool) or not isinstance(threshold,(int,float)) or not math.isfinite(threshold) or not .5<=threshold<=1: raise ValueError('Threshold: 0.5–1')
    if not isinstance(payload.get('permutation_check',True),bool): raise ValueError('permutation_check must be a boolean')
    return state,questions,float(threshold)

def answers(questions,rows,threshold,calibration):
    if len(questions)!=len(rows): raise RuntimeError('Inconsistent answer count')
    output={}
    for q,row in zip(questions,rows):
        p=[float(v) for v in row]
        if len(p)!=len(q['keys']) or any(not math.isfinite(v) or v<0 for v in p) or sum(p)<=0: raise RuntimeError('Invalid distribution')
        total=sum(p)
        if not math.isfinite(total):raise RuntimeError('Invalid distribution total')
        p=[v/total for v in p];best=max(range(len(p)),key=p.__getitem__)
        reason=['human_review_required']
        if max(p)<threshold: reason.append('below_threshold')
        if q['type']=='choice' and q['abstain_option'] is None: reason.append('abstention_not_declared')
        abstained=q['abstain_option'] is not None and q['keys'][best]==q['abstain_option']
        if abstained: reason.append('explicit_abstention')
        a={'type':q['type'],'instructions':q['instr'],'criteria':q['criteria'],'probabilities':dict(zip(q['keys'],p)),
           'concentration':(max(p)-1/len(p))/(1-1/len(p)),
           'concentration_is_correctness_probability':False,'calibration':calibration,
           'abstained':abstained,'requires_review':True,'eligible_for_automation':False,
           'review_reasons':reason,'recommendation':'review' if abstained or max(p)<threshold else 'candidate'}
        if q['type']=='choice':a['choice']=q['keys'][best]
        elif q['type']=='noul':a['noul']=p[1]
        else:a['score']=sum(i*v for i,v in enumerate(p));a['legend']=dict(zip(q['keys'],q['options']))
        output[q['id']]=a
    return output
