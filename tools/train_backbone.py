"""Train a NEW Qwen3.5-2B LoRA plus typed pointer head from local disjoint data.
The offline runtime is not modified until a compatible trained checkpoint exists.
"""
import argparse,json,math,random,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.learning import training_record,assert_disjoint,file_sha256,BASE_MODEL

def read(path):return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines() if s.strip()]

def validate_profile(cfg):
 from typelogic.model_profile import BASE_MODEL,BASE_REVISION,HIDDEN_SIZE
 if (cfg.get('base_model'),cfg.get('base_revision'),cfg.get('hidden_size'))!=(BASE_MODEL,BASE_REVISION,HIDDEN_SIZE):raise ValueError('Training profile identity mismatch: use the pinned Qwen3.5-2B model')
 if cfg.get('lora_alpha')!=2*cfg['lora_rank']:raise ValueError('This pointer implementation requires lora_alpha equal to twice the rank')
 for name in ['learning_rate','head_learning_rate','brier_weight','ordinal_weight','permutation_weight']:
  v=cfg[name]
  if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0:raise ValueError('Invalid training value: '+name)
 if cfg['min_gpu_memory_gib']!=12:raise ValueError('This reviewed training profile uses a 12 GiB admission limit')

def main():
 p=argparse.ArgumentParser();p.add_argument('--base-dir',type=Path,required=True);p.add_argument('--train',type=Path,required=True);p.add_argument('--validation',type=Path,required=True);p.add_argument('--calibration',type=Path,required=True);p.add_argument('--test',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--config',type=Path,default=Path(__file__).resolve().parents[1]/'research/training-2b.json');args=p.parse_args()
 cfg=json.loads(args.config.read_text());splits={k:read(getattr(args,k)) for k in ['train','validation','calibration','test']};assert_disjoint(splits)
 if args.out.exists():raise ValueError('Output already exists; no checkpoints are overwritten')
 validate_profile(cfg)
 for k in ['epochs','gradient_accumulation','lora_rank','max_state','max_branch']:
  if isinstance(cfg[k],bool) or not isinstance(cfg[k],int) or cfg[k]<1:raise ValueError('Invalid training configuration: '+k)
 for rows in splits.values():
  for r in rows:training_record(r)
 import torch
 if not torch.cuda.is_available():raise RuntimeError('2B training not started: CUDA GPU is required by this training profile. No weights were changed.')
 if not torch.cuda.is_bf16_supported():raise RuntimeError('This profile requires CUDA BF16 support. No weights were changed.')
 if torch.cuda.get_device_properties(0).total_memory < cfg['min_gpu_memory_gib']*1024**3:raise RuntimeError('This 2B BF16 LoRA profile has a conservative 12 GiB VRAM admission limit; 16 GiB or more is a planning estimate, not a measured minimum. No weights were changed.')
 from typelogic.offline import install
 install()
 from typelogic.vendor.pointer import DecisionModel,load_tokenizer
 from typelogic.objectives import decision_loss
 random.seed(cfg['seed']);torch.manual_seed(cfg['seed']);torch.cuda.manual_seed_all(cfg['seed'])
 tok=load_tokenizer(str(args.base_dir))
 base=json.loads((args.base_dir/'config.json').read_text());hidden=base.get('text_config',base)['hidden_size']
 if hidden!=cfg['hidden_size']:raise ValueError('Backbone hidden size is incompatible')
 # Verify all official files, including the manually supplied shard.
 from typelogic.model_profile import verify_backbone,asset_path
 verify_backbone(args.base_dir)
 provenance=json.loads((args.base_dir/'asset-provenance.json').read_text())
 if provenance.get('repo')!=cfg['base_model'] or provenance.get('revision')!=cfg['base_revision']:raise ValueError('Base model provenance mismatch')
 for name,digest in provenance['files'].items():
  if file_sha256(asset_path(args.base_dir,name))!=digest:raise ValueError('Modified base asset: '+name)
 model=DecisionModel(str(args.base_dir),tok,'cuda',lora=cfg['lora_rank'],head_dim=cfg['head_dim'],lora_targets='all',dtype=torch.bfloat16,direct_load=True)
 if hasattr(model.lm,'gradient_checkpointing_enable'):model.lm.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False});model.lm.enable_input_require_grads()
 optimizer=torch.optim.AdamW([{'params':[p for p in model.lm.parameters() if p.requires_grad],'lr':cfg['learning_rate']},{'params':model.head.parameters(),'lr':cfg['head_learning_rate']}],weight_decay=.01)
 # Preflight EVERY split to reject overflow instead of learning/evaluating truncated states.
 encoded={name:[model.encode(tok,training_record(r),max_state=cfg['max_state'],max_branch=cfg['max_branch'],strict=True) for r in rows] for name,rows in splits.items()}
 def losses(rows,encs,training=False):
  total=0;count=0;correct=0;model.train(training)
  with torch.set_grad_enabled(training):
   for row,enc in zip(rows,encs):
    rec=training_record(row);logits=model(enc)
    for q,z in zip(rec['questions'],logits):
     target=torch.tensor(q['target'],device='cuda');loss=decision_loss(z,target,ordinal=q['type']=='score',brier_weight=cfg['brier_weight'],ordinal_weight=cfg['ordinal_weight'])
     total+=float(loss.detach());count+=1;correct+=int(z.argmax()==target.argmax())
  return {'loss':total/count,'accuracy':correct/count,'questions':count}
 before=losses(splits['validation'],encoded['validation']);history=[];best=math.inf;steps=0;best_epoch=None
 args.out.mkdir(parents=True);adapter=args.out/'adapter';adapter.mkdir();accum=cfg['gradient_accumulation']
 for epoch in range(cfg['epochs']):
  model.train();optimizer.zero_grad(set_to_none=True);order=list(range(len(splits['train'])));random.shuffle(order);running=0
  for position,index in enumerate(order):
   row=splits['train'][index];rec=training_record(row);z=model(encoded['train'][index]);losses_row=[]
   # Cyclic permutation regularisation aligns the second vector back to original IDs.
   permuted={**rec,'questions':[]};orders=[]
   for q in rec['questions']:
    order_q=list(range(1,len(q['options'])))+[0] if q['type']=='choice' else list(range(len(q['options'])))
    orders.append(order_q);permuted['questions'].append({**q,'options':[q['options'][i] for i in order_q]})
   enc2=model.encode(tok,permuted,max_state=cfg['max_state'],max_branch=cfg['max_branch'],strict=True)
   z2=model(enc2)
   for q,logit,other,order_q in zip(rec['questions'],z,z2,orders):
    inverse=[order_q.index(i) for i in range(len(order_q))];aligned=other[inverse]
    losses_row.append(decision_loss(logit,torch.tensor(q['target'],device='cuda'),ordinal=q['type']=='score',brier_weight=cfg['brier_weight'],ordinal_weight=cfg['ordinal_weight'],permuted_logits=aligned if q['type']=='choice' else None,permutation_weight=cfg['permutation_weight']))
   loss=torch.stack(losses_row).mean();running+=float(loss.detach());group_start=(position//accum)*accum;group_size=min(accum,len(order)-group_start);(loss/group_size).backward()
   if (position+1)%accum==0 or position+1==len(order):
    torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad],1.);optimizer.step();optimizer.zero_grad(set_to_none=True);steps+=1
  validation=losses(splits['validation'],encoded['validation']);history.append({'epoch':epoch+1,'train_loss':running/len(order),'validation':validation});print(json.dumps(history[-1]),flush=True)
  if validation['loss']<best:
   best=validation['loss'];best_epoch=epoch+1;model.lm.save_pretrained(adapter);tok.save_pretrained(adapter);torch.save({'head':model.head.state_dict(),'head_dim':cfg['head_dim'],'temperature':1.},adapter/'head.pt')
 metadata={'base_model':BASE_MODEL,'base_revision':cfg['base_revision'],'hidden_size':hidden,'trained':True,'training_steps':steps,'best_epoch':best_epoch,'seed':cfg['seed'],'configuration':cfg,'validation_before':before,'history':history,'datasets':{k:file_sha256(getattr(args,k)) for k in splits},'final_test_evaluated':False,'calibration_fitted':False,'deployment_eligible':False,'warning':'Evaluate selected checkpoint, fit calibration on calibration split and run independent final test before release.'}
 (args.out/'checkpoint.json').write_text(json.dumps(metadata,indent=2))
 print(json.dumps({'checkpoint':str(args.out),'trained':True,'deployment_eligible':False}),flush=True)
if __name__=='__main__':main()
