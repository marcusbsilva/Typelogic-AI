"""Offline contracts for supervised data, distillation and checkpoint compatibility."""
import hashlib
import json
import math
from pathlib import Path
from .schema import validate

from .model_profile import BASE_MODEL,BASE_REVISION,HIDDEN_SIZE

def normalized_state(value):
    text=value if isinstance(value,str) else json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False)
    return ' '.join(text.casefold().split())

def distribution(values, keys):
    n=keys if isinstance(keys,int) else len(keys)
    if isinstance(values,dict):
        if isinstance(keys,int) or set(values)!=set(keys):raise ValueError('Distribution IDs must match declared alternatives')
        values=[values[k] for k in keys]
    if not isinstance(values,(list,tuple)) or len(values)!=n or n<2:raise ValueError('Distribution size mismatch')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in values):raise ValueError('Distribution must be finite and nonnegative')
    total=math.fsum(values)
    if not math.isfinite(total) or abs(total-1)>1e-6:raise ValueError('Distribution must sum to one')
    return [float(v)/total for v in values]

def softmax(logits,temperature=1.):
    if isinstance(temperature,bool) or not math.isfinite(temperature) or temperature<=0:raise ValueError('Invalid temperature')
    if len(logits)<2 or any(not math.isfinite(v) for v in logits):raise ValueError('Invalid logits')
    peak=max(logits);values=[math.exp((v-peak)/temperature) for v in logits];s=math.fsum(values)
    return [v/s for v in values]

def assert_disjoint(splits):
    seen_states={};seen_groups={}
    for name,rows in splits.items():
        if not rows:raise ValueError('Empty split: '+name)
        local_states=set()
        for row in rows:
            state=normalized_state(row['state']);group=row.get('group_id')
            if not state:raise ValueError('Empty state')
            if state in seen_states and seen_states[state]!=name:raise ValueError('State overlap between '+name+' and '+seen_states[state])
            if state in local_states:raise ValueError('Duplicate state in '+name)
            if group is not None and group in seen_groups and seen_groups[group]!=name:raise ValueError('Group overlap between '+name+' and '+seen_groups[group])
            seen_states[state]=name;local_states.add(state)
            if group is not None:seen_groups[group]=name
    return True

def teacher_consensus(rows,gold=None,weights=None,gold_weight=.7):
    if not rows:raise ValueError('No teacher predictions')
    n=len(rows[0]);rows=[distribution(r,n) for r in rows]
    weights=weights or [1/len(rows)]*len(rows)
    if len(weights)!=len(rows) or any(not math.isfinite(w) or w<0 for w in weights) or sum(weights)<=0:raise ValueError('Invalid teacher weights')
    weights=[w/sum(weights) for w in weights]
    mean=[sum(w*r[i] for w,r in zip(weights,rows)) for i in range(n)]
    # Jensen Shannon divergence, in nats, is bounded by log(number of teachers).
    js=sum(w*sum(p*math.log(p/max(m,1e-30)) for p,m in zip(r,mean) if p>0) for w,r in zip(weights,rows))
    winners={max(range(n),key=r.__getitem__) for r in rows}
    if gold is not None:
        if not 0<=gold_weight<=1:raise ValueError('Invalid gold weight')
        gold=distribution(gold,n);target=[gold_weight*g+(1-gold_weight)*m for g,m in zip(gold,mean)]
        review=len(winners)>1 or max(range(n),key=mean.__getitem__)!=max(range(n),key=gold.__getitem__)
    else:target=mean;review=len(winners)>1
    return {'target':distribution(target,n),'teacher_mean':mean,'disagreement':js,'review':review,'gold_retained':gold is not None}

def training_record(payload):
    state,questions,_=validate(payload);targets=payload.get('targets',{})
    if set(targets)!={q['id'] for q in questions}:raise ValueError('Missing or extra training targets')
    return {'state':state,'questions':[{'id':q['id'],'instr':q['instr'],'options':q['options'],'label':0,'target':distribution(targets[q['id']],q['keys']),'type':q['type']} for q in questions]}

def select_checkpoint(path,base_model=BASE_MODEL,hidden_size=HIDDEN_SIZE):
    meta=json.loads((Path(path)/'checkpoint.json').read_text())
    if meta.get('base_model')!=base_model:raise ValueError('Checkpoint base model mismatch')
    if meta.get('hidden_size')!=hidden_size:raise ValueError('Checkpoint hidden dimension mismatch')
    if meta.get('trained') is not True:raise ValueError('Checkpoint has not been trained')
    if base_model==BASE_MODEL and meta.get('base_revision')!=BASE_REVISION:raise ValueError('Checkpoint base revision mismatch')
    return meta

def file_sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def checkpoint_preflight(root):
    root=Path(root).resolve();base=root/'backbone';adapter=root/'adapter'
    for path in [base/'config.json']:
        if not path.is_file():raise FileNotFoundError('Missing local checkpoint asset: '+path.name)
    config=json.loads((base/'config.json').read_text());text=config.get('text_config',config)
    index=base/'model.safetensors.index.json'
    shards=set(json.loads(index.read_text())['weight_map'].values()) if index.exists() else {'model.safetensors'}
    for name in shards:
        path=(base/name).resolve()
        if not path.is_relative_to(base.resolve()):raise ValueError('Invalid shard path')
        if not path.is_file():raise FileNotFoundError('Missing backbone shard: '+name)
    checkpoint=root/'checkpoint.json'
    profile_path=root/'model-profile.json'
    if profile_path.exists():
        profile=json.loads(profile_path.read_text())
        if (profile.get('base_model'),profile.get('base_revision'),profile.get('hidden_size'))!=(BASE_MODEL,BASE_REVISION,HIDDEN_SIZE):raise ValueError('Prepared model profile mismatch')
    if profile_path.exists() or text['hidden_size']==HIDDEN_SIZE:
        if not checkpoint.is_file() or not (adapter/'head.pt').is_file():raise FileNotFoundError('Qwen3.5-2B backbone is not a trained TypeLogic checkpoint. Train the 2B LoRA and pointer head, then run tools/install_models.py --trained RUN_DIRECTORY.')
        select_checkpoint(root)
        from .model_profile import verify_backbone
        verify_backbone(base)
    elif checkpoint.exists():raise ValueError('Checkpoint base model mismatch: this release targets Qwen3.5-2B')
    if not (adapter/'head.pt').is_file():raise FileNotFoundError('Missing local checkpoint asset: head.pt')
    manifest=root/'manifest.json'
    if manifest.exists():
        expected=json.loads(manifest.read_text())['files']
        for name,record in expected.items():
            path=(root/name).resolve()
            if not path.is_relative_to(root):raise ValueError('Invalid manifest path')
            if not path.is_file():raise FileNotFoundError('Missing manifest asset: '+name)
            if path.stat().st_size!=record['bytes'] or file_sha256(path)!=record['sha256']:raise ValueError('Modified checkpoint asset: '+name)
    return {'hidden_size':text['hidden_size'],'shards':len(shards),'complete':True}


def validate_head(meta,hidden_size):
    """Reject incompatible readouts before allocating the full backbone."""
    import torch
    dimension=meta.get('head_dim',256)
    if isinstance(dimension,bool) or not isinstance(dimension,int) or dimension<1:raise ValueError('Invalid head dimension')
    expected={'q.weight':(dimension,hidden_size),'k.weight':(dimension,hidden_size),'q.bias':(dimension,),'k.bias':(dimension,)}
    state=meta.get('head',{})
    if set(state)!=set(expected):raise ValueError('Invalid pointer head parameters')
    for name,shape in expected.items():
        if not isinstance(state[name],torch.Tensor) or tuple(state[name].shape)!=shape:raise ValueError('Pointer head dimension mismatch: '+name)
        if not torch.isfinite(state[name]).all():raise ValueError('Pointer head contains nonfinite weights')
    return dimension
