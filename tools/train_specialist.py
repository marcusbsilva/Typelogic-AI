"""Supervised CPU training from local labels; no teachers or external API calls."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.offline import install
install()
import numpy as np
import torch
from typelogic.schema import validate,canonical
from typelogic.specialist import state_inputs
from typelogic.vendor.structured import Jev,JevConfig,Noul,Choice,Score,StateCache,RLCDLoss

def read(path,profile=None):
    rows=[json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if len(rows)<2:raise ValueError('At least two labeled states per dataset are required')
    for row in rows:
        _,questions,_=validate(row);row['_normalized']=questions
        if profile is None:profile=row['questions']
        if canonical(row['questions'])!=canonical(profile) or list(row['questions'])!=list(profile):raise ValueError('Use o mesmo perfil e a mesma ordem em todas as linhas')
        for q in questions:
            target=row.get('targets',{}).get(q['id'])
            if isinstance(target,dict):
                if set(target)!=set(q['keys']):raise ValueError('Target alternatives do not match')
                target=[target[key] for key in q['keys']]
            if not isinstance(target,list) or len(target)!=len(q['keys']) or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not np.isfinite(v) or v<0 for v in target) or abs(sum(target)-1)>1e-6:raise ValueError('Target must be a finite distribution for each question')
            row['targets'][q['id']]=target
    return rows,profile

def typed(questions):
    return [Noul(q['instr']+(' Binary criteria: '+canonical(q['criteria']) if q['criteria'] else ''),key=q['id']) if q['type']=='noul' else Choice(q['instr'],q['options'],key=q['id']) if q['type']=='choice' else Score(q['instr'],q['options'],key=q['id']) for q in questions]

def metrics(core,rows,questions):
    core.eval();nll=brier=accuracy=total=0.
    with torch.no_grad():
        for row in rows:
            predictions=core.logits([row['state']],questions)
            for q,(probability,_) in zip(questions,predictions):
                target=torch.tensor(row['targets'][q.key]);p=probability[0]
                nll-=float((target*p.clamp(min=1e-8).log()).sum());brier+=float(((p-target)**2).sum());accuracy+=int(p.argmax()==target.argmax());total+=1
    return {'questions':int(total),'nll':nll/total,'brier':brier/total,'argmax_accuracy':accuracy/total}

class StateGraph(torch.nn.Module):
    def __init__(self,core):super().__init__();self.encoder=core.state_encoder
    def forward(self,ids,paths,padding):return self.encoder(ids,paths,padding)

class ReadoutGraph(torch.nn.Module):
    def __init__(self,core,questions):super().__init__();self.core=core;self.questions=questions
    def forward(self,hidden,padding):
        vectors=self.core._readout(StateCache(hidden,padding),self.questions);outputs=[];confidences=[]
        for index,q in enumerate(self.questions):
            vector=vectors[index:index+1]
            if isinstance(q,Noul):
                true=torch.sigmoid(self.core.noul_head(vector)).unsqueeze(-1);p=torch.cat([1-true,true],-1)
            elif isinstance(q,Choice):
                options=self.core._encode_options(q.options).unsqueeze(0);mask=torch.ones((1,len(q.options)),dtype=torch.bool)
                p=torch.softmax(self.core.choice_head(vector,options,mask),-1)
            else:p=self.core.score_head(vector,len(q.labels))
            evidence,_=self.core.confidence_head(vector,p.shape[-1]);outputs.append(p);confidences.append(evidence)
        return torch.cat(outputs,-1),torch.stack(confidences,-1)

def export(core,questions,out,validation):
    import onnxruntime as ort
    ort.disable_telemetry_events();core.eval();torch.backends.mha.set_fastpath_enabled(False)
    items,_=state_inputs(validation[0]['state'],asdict(core.cfg));inputs=tuple(torch.from_numpy(items[k]) for k in ('ids','paths','padding'))
    encoder=StateGraph(core).eval();readout=ReadoutGraph(core,questions).eval()
    with torch.no_grad():
        hidden=encoder(*inputs)
        torch.onnx.export(encoder,inputs,str(out/'state.onnx'),input_names=['ids','paths','padding'],output_names=['hidden'],opset_version=17,dynamo=False)
        torch.onnx.export(readout,(hidden,inputs[2]),str(out/'readout.onnx'),input_names=['hidden','padding'],output_names=['probabilities','evidence_confidence'],opset_version=17,dynamo=False)
    options=ort.SessionOptions();options.intra_op_num_threads=1
    state=ort.InferenceSession(str(out/'state.onnx'),options,providers=['CPUExecutionProvider']);head=ort.InferenceSession(str(out/'readout.onnx'),options,providers=['CPUExecutionProvider']);worst=0.
    for row in validation:
        inputs,_=state_inputs(row['state'],asdict(core.cfg));h=state.run(None,inputs)[0];actual=head.run(None,{'hidden':h,'padding':inputs['padding']})[0]
        with torch.no_grad():expected=np.concatenate([p.numpy() for p,_ in core.logits([row['state']],questions)],-1)
        np.testing.assert_allclose(actual,expected,rtol=1e-4,atol=2e-5);worst=max(worst,float(np.max(np.abs(actual-expected))))
    return {'max_abs_probability_difference':worst,'validation_states':len(validation)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--train',required=True,type=Path);parser.add_argument('--validation',required=True,type=Path);parser.add_argument('--out',required=True,type=Path);parser.add_argument('--steps',type=int,default=100);parser.add_argument('--max-state',type=int,default=256);parser.add_argument('--seed',type=int,default=17)
    args=parser.parse_args()
    if args.out.exists() or not 1<=args.steps<=100000 or not 32<=args.max_state<=2048:raise ValueError('Output must be new and training limits valid')
    train,profile=read(args.train);validation,_=read(args.validation,profile)
    if {canonical(r['state']) for r in train}&{canonical(r['state']) for r in validation}:raise ValueError('Training and validation share states')
    train_groups={r['group_id'] for r in train if 'group_id' in r};validation_groups={r['group_id'] for r in validation if 'group_id' in r}
    if train_groups&validation_groups:raise ValueError('Separate training and validation groups')
    torch.manual_seed(args.seed);torch.set_num_threads(2)
    cfg=JevConfig(vocab_size=4096,d_model=64,n_heads=4,d_ff=128,dropout=0.,n_state_layers=2,n_question_layers=1,n_readout_layers=2,max_state_len=args.max_state,n_slots=4)
    for row in train+validation:state_inputs(row['state'],asdict(cfg))
    questions=typed(train[0]['_normalized'])
    if any(len(getattr(q,'labels',[]))>16 for q in questions):raise ValueError('Specialist: up to 16 ordinal levels')
    core=Jev(cfg);before=metrics(core,validation,questions);optimizer=torch.optim.AdamW(core.parameters(),lr=.001,weight_decay=.01);objective=RLCDLoss(w_consistency=.1,w_ece=0.)
    core.train()
    for step in range(args.steps):
        row=train[step%len(train)];targets=[torch.tensor([row['targets'][q.key]]) for q in questions]
        optimizer.zero_grad(set_to_none=True);loss=objective(core,[row['state']],questions,targets);loss.backward();torch.nn.utils.clip_grad_norm_(core.parameters(),1.);optimizer.step()
        if step%20==0:print(json.dumps({'step':step,'loss':float(loss.detach())}),flush=True)
    after=metrics(core,validation,questions);args.out.mkdir(parents=True)
    torch.save({'config':asdict(cfg),'state_dict':core.state_dict()},args.out/'training.pt');parity=export(core,questions,args.out,validation)
    meta={'name':args.out.name,'config':asdict(cfg),'questions':profile,'trained_steps':args.steps,'seed':args.seed,
          'training_sha256':hashlib.sha256(args.train.read_bytes()).hexdigest(),'validation_sha256':hashlib.sha256(args.validation.read_bytes()).hexdigest(),
          'validation':{'before':before,'after':after,'onnx_parity':parity},'graph_sha256':{name:hashlib.sha256((args.out/name).read_bytes()).hexdigest() for name in ('state.onnx','readout.onnx')},
          'general_domain_claim':False,'warning':'Supervised research profile; not proprietary RLCD reproduction. Validation is not a final independent test.'}
    (args.out/'profile.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False));print(json.dumps(meta['validation'],indent=2))
if __name__=='__main__':main()
