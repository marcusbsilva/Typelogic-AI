"""Local Qwen3.5-2B LoRA + pointer training. Accelerator use is explicit.
New command; does not replace the original trainer or pretrained backbone.
"""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from typelogic.colab_support import atomic_write, read_checked, groups
from typelogic.learning import training_record, assert_disjoint, file_sha256
from train_backbone import validate_profile, read
from typelogic.training_data_v2 import permute_record, check_groups
from typelogic.jev_distill import preflight_rows
from typelogic.training_backend import resolve_training_backend


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--train-file', type=Path, help='Optional independently prepared distilled TRAIN split')
    parser.add_argument('--config', type=Path, default=ROOT/'research/training-2b.json')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--base-dir', type=Path, default=ROOT/'models/decision-2b/backbone')
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--init-from', type=Path,
                        help='Completed run (checkpoint.json + adapter/) whose LoRA and pointer head start this run (warm start)')
    parser.add_argument('--device', choices=('cuda', 'xpu'), default='cuda',
                        help='Training accelerator. CUDA remains the default; XPU is experimental.')
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    validate_profile(cfg)
    paths = {name: args.data_dir/f'{name}.jsonl' for name in ('train','validation','calibration','test')}
    if args.train_file is not None:
        paths['train'] = args.train_file
    splits = {name: read(path) for name, path in paths.items()}
    check_groups(splits)
    assert_disjoint(splits)
    if any(not rows for rows in splits.values()):
        raise ValueError('Empty dataset split')
    for rows in splits.values():
        for row in rows:
            training_record(row)
    if args.validate_only:
        from collections import Counter
        print(json.dumps({k:dict(Counter(r.get('language','unspecified')+'/'+q['type'] for r in rows for q in r['questions'].values())) for k,rows in splits.items()},indent=2))
        return
    import torch
    min_memory_gib = (cfg.get('xpu_min_gpu_memory_gib', 10.5) if args.device == 'xpu'
                      else cfg['min_gpu_memory_gib'])
    resolved = resolve_training_backend(torch, args.device, min_memory_gib=min_memory_gib)
    backend = resolved['backend']
    device = resolved['device']
    mode = resolved['precision']
    identity = {'configuration': cfg, 'precision': mode, 'device': device,
                'accelerator_name': resolved['name'], 'accelerator_memory_bytes': resolved['memory_bytes'],
                'min_memory_gib': resolved['min_memory_gib'], 'smoke': args.smoke,
                'datasets': {k:file_sha256(p) for k,p in paths.items()},
                'init_from': None if args.init_from is None else {name: file_sha256(args.init_from/'adapter'/name) for name in ('adapter_model.safetensors', 'head.pt')},
                'torch': torch.__version__, 'trainer_version': 2, 'trainer_sha256': file_sha256(Path(__file__)), 'data_helper_sha256': file_sha256(ROOT/'typelogic/training_data_v2.py')}
    checkpoint = args.out/'resume.tlc'
    if args.resume:
        if not checkpoint.is_file():
            raise ValueError('Resume checkpoint missing')
    elif args.out.exists():
        raise ValueError('Output exists; use --resume or choose another output')
    from typelogic.offline import install
    install()
    from typelogic.model_profile import verify_backbone
    print('Verifying local backbone...', flush=True)
    verify_backbone(args.base_dir)
    from typelogic.vendor.pointer import DecisionModel, load_tokenizer
    from typelogic.objectives import decision_loss
    random.seed(cfg['seed']);torch.manual_seed(cfg['seed']);backend.manual_seed_all(cfg['seed'])
    tok = load_tokenizer(str(args.base_dir))
    print('Loading backbone; mixed precision:', mode, flush=True)
    # FP32 master weights avoid permanently rounding BF16 pretrained weights into FP16.
    # GPU memory requirements must be measured by --smoke, not inferred from parameter count.
    model = DecisionModel(str(args.base_dir), tok, device, lora=cfg['lora_rank'],
                          head_dim=cfg['head_dim'], lora_targets='all',
                          dtype=torch.float32, direct_load=True)
    if args.init_from is not None:
        # Warm start: fine-tuning from the released checkpoint keeps what it learned (kev: 0.83 vs 0.33 from the base).
        from safetensors.torch import load_file
        from peft import set_peft_model_state_dict
        from typelogic.learning import select_checkpoint
        previous = select_checkpoint(args.init_from)
        if previous['configuration']['lora_rank'] != cfg['lora_rank'] or previous['configuration']['head_dim'] != cfg['head_dim']:
            raise ValueError('Warm start needs the same LoRA rank and head dimension')
        weights = load_file(str(args.init_from/'adapter'/'adapter_model.safetensors'))
        report = set_peft_model_state_dict(model.lm, weights)
        missing = [k for k in getattr(report, 'unexpected_keys', []) if 'lora' in k]
        if missing:
            raise ValueError('Warm-start adapter keys do not match this model: ' + ', '.join(missing[:5]))
        head = torch.load(args.init_from/'adapter'/'head.pt', map_location='cpu', weights_only=True)
        model.head.load_state_dict(head['head'], strict=True)
        model.to(device)
        print('Warm start from', args.init_from, 'epoch', previous['best_epoch'], flush=True)
    model.lm.config.use_cache = False
    model.lm.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.lm.enable_input_require_grads()
    amp_dtype = torch.float16 if mode == 'fp16' else torch.bfloat16
    # Head computation remains FP32 even within the backbone's autocast context.
    original_head = model.head.forward
    def fp32_head(*values, **kwargs):
        with torch.autocast(device, enabled=False):
            return original_head(*(v.float() if isinstance(v,torch.Tensor) else v for v in values), **kwargs)
    model.head.forward = fp32_head
    params = {n:p for n,p in model.named_parameters() if p.requires_grad}
    for param in params.values():
        param.data = param.data.float()
    optimizer = torch.optim.AdamW([
        {'params':[p for p in model.lm.parameters() if p.requires_grad],'lr':cfg['learning_rate']},
        {'params':model.head.parameters(),'lr':cfg['head_learning_rate']}], weight_decay=.01)
    scaler = torch.amp.GradScaler(device, enabled=(mode=='fp16'), init_scale=256)
    print('Trainable parameters:', sum(p.numel() for p in params.values()), flush=True)
    def encode_row(row):
        return model.encode(tok,training_record(row),max_state=cfg['max_state'],
                            max_branch=cfg['max_branch'],strict=True)
    lengths = preflight_rows(splits, encode_row)
    print('Token preflight:', json.dumps(lengths), flush=True)
    def forward(enc):
        with torch.autocast(device, dtype=amp_dtype):
            values = model(enc)
        if any(not torch.isfinite(v).all() for v in values):
            raise FloatingPointError('Non-finite forward logits; FP16 profile not approved on this model')
        return [v.float() for v in values]
    def state_params():
        return {n:p.detach().cpu().clone() for n,p in params.items()}
    def load_params(values):
        if set(values) != set(params):
            raise ValueError('Trainable parameter names mismatch')
        with torch.no_grad():
            for n,p in params.items():
                if p.shape != values[n].shape or not torch.isfinite(values[n]).all():
                    raise ValueError('Invalid parameter in checkpoint: '+n)
                p.copy_(values[n])
    def dump(state):
        state.update(params=state_params(), optimizer=optimizer.state_dict(), scaler=scaler.state_dict(),
                     python_rng=random.getstate(), torch_rng=torch.get_rng_state(),
                     accelerator_rng=backend.get_rng_state_all())
        buffer=io.BytesIO();torch.save(state,buffer);atomic_write(checkpoint,buffer.getvalue())
        # Avoid retaining a second trainable-parameter snapshot between saves.
        for key in ('params','optimizer','scaler','python_rng','torch_rng','accelerator_rng'):
            state.pop(key,None)
    def restore():
        # Only load checkpoints produced by this command in the user's own run directory.
        s=torch.load(io.BytesIO(read_checked(checkpoint)),map_location='cpu',weights_only=False)
        if s['identity'] != identity:
            raise ValueError('Resume configuration/data/runtime mismatch')
        load_params(s.pop('params'));optimizer.load_state_dict(s.pop('optimizer'))
        scaler.load_state_dict(s.pop('scaler'));random.setstate(s.pop('python_rng'))
        torch.set_rng_state(s.pop('torch_rng'));backend.set_rng_state_all(s.pop('accelerator_rng'))
        return s
    def evaluate():
        model.eval();total=correct=count=0
        with torch.no_grad():
            for row in splits['validation']:
                for q,z in zip(training_record(row)['questions'],forward(encode_row(row))):
                    target=torch.tensor(q['target'],device=device)
                    loss=decision_loss(z,target,ordinal=q['type']=='score',
                        brier_weight=cfg['brier_weight'],ordinal_weight=cfg['ordinal_weight'])
                    total+=loss.item();correct+=int(z.argmax()==target.argmax());count+=1
        model.train()
        return {'loss':total/count,'accuracy':correct/count,'questions':count}
    args.out.mkdir(parents=True,exist_ok=True)
    state = restore() if args.resume else dict(identity=identity,epoch=0,cursor=0,order=None,
        steps=0,best=math.inf,best_params=None,best_epoch=None,history=[],running=0.,before=None,complete=False)
    if state['complete']:
        print('Run already complete. No new training performed.',flush=True);return
    if state['before'] is None:
        state['before'] = {'smoke_only':True} if args.smoke else evaluate()
        dump(state)
    start_time=time.monotonic();backend.reset_peak_memory_stats();model.train()
    epochs = 1 if args.smoke else cfg['epochs']
    accumulation = 1 if args.smoke else cfg['gradient_accumulation']
    while state['epoch'] < epochs:
        if state['order'] is None:
            state['order']=list(range(len(splits['train'])))
            if args.smoke:
                if len(splits['train']) < 2:
                    raise ValueError('Smoke requires at least two training rows')
                # Include the longest encoded training record to probe activation memory.
                # Full preflight found the longest row; smoke it alongside the first row.
                longest=next(i for i,r in enumerate(splits['train']) if r.get('id')==lengths['train']['longest_id'])
                state['order']=[longest]+([0] if longest!=0 else [1])
            else:random.shuffle(state['order'])
        for batch in groups(state['order'][state['cursor']:],accumulation):
            success=False
            for attempt in range(6):
                optimizer.zero_grad(set_to_none=True);batch_loss=0.
                for index in batch:
                    rec=permute_record(training_record(splits['train'][index]),random.Random(f"{cfg['seed']}:{state['epoch']}:{index}"))
                    z=forward(model.encode(tok,rec,max_state=cfg['max_state'],max_branch=cfg['max_branch'],strict=True))
                    permuted={**rec,'questions':[]};orders=[]
                    for q in rec['questions']:
                        order=list(range(1,len(q['options'])))+[0] if q['type']=='choice' else list(range(len(q['options'])))
                        orders.append(order);permuted['questions'].append({**q,'options':[q['options'][i] for i in order]})
                    z2=forward(model.encode(tok,permuted,max_state=cfg['max_state'],max_branch=cfg['max_branch'],strict=True))
                    losses=[]
                    for q,a,b,order in zip(rec['questions'],z,z2,orders):
                        losses.append(decision_loss(a,torch.tensor(q['target'],device=device),ordinal=q['type']=='score',
                          brier_weight=cfg['brier_weight'],ordinal_weight=cfg['ordinal_weight'],
                          permuted_logits=b[[order.index(i) for i in range(len(order))]] if q['type']=='choice' else None,
                          permutation_weight=cfg['permutation_weight']))
                    loss=torch.stack(losses).mean()
                    if not torch.isfinite(loss):raise FloatingPointError('Non-finite loss')
                    batch_loss+=loss.item();scaler.scale(loss/len(batch)).backward()
                    del loss,losses,z,z2
                scaler.unscale_(optimizer)
                finite=all(p.grad is None or torch.isfinite(p.grad).all() for p in params.values())
                if finite:
                    norm=torch.nn.utils.clip_grad_norm_(list(params.values()),1.,error_if_nonfinite=True)
                    scaler.step(optimizer);scaler.update();success=True;break
                if not scaler.is_enabled():raise FloatingPointError('Non-finite BF16 gradient')
                # step skips non-finite updates; retry the same examples after reducing the scale.
                scaler.step(optimizer);scaler.update()
            if not success:raise FloatingPointError('FP16 gradients failed after 6 attempts; resume last good checkpoint')
            optimizer.zero_grad(set_to_none=True)
            if any(not torch.isfinite(p).all() for p in params.values()):raise FloatingPointError('Non-finite updated parameter')
            state['cursor']+=len(batch);state['steps']+=1;state['running']+=batch_loss
            print(json.dumps({'step':state['steps'],'epoch':state['epoch']+1,'examples':state['cursor'],
                'loss':batch_loss/len(batch),'gradient_norm':float(norm),'scale':scaler.get_scale(),
                'peak_vram_gib':backend.max_memory_allocated()/1024**3}),flush=True)
            if args.smoke or state['steps']%10==0:
                dump(state)
            if args.smoke and state['steps']==1:
                # Exercise parameter/optimizer/scaler/RNG restore in the actual Colab runtime.
                reference=state_params()
                with torch.no_grad():
                    next(iter(params.values())).add_(1.)
                state=restore()
                if any(not torch.equal(reference[n],p.detach().cpu()) for n,p in params.items()):
                    raise ValueError('Resume parameter roundtrip failed')
                del reference
                print('Checkpoint restore: OK',flush=True)
        if args.smoke:
            validation={'smoke_only':True}
        else:
            validation=evaluate()
            if validation['loss'] < state['best']:
                state['best']=validation['loss'];state['best_params']=state_params();state['best_epoch']=state['epoch']+1
        state['history'].append({'epoch':state['epoch']+1,'train_loss':state['running']/len(state['order']),'validation':validation})
        state.update(epoch=state['epoch']+1,cursor=0,order=None,running=0.)
        dump(state)
    if args.smoke:
        result={'smoke_passed':True,'trained':False,'deployment_eligible':False,'steps':state['steps'],
                'peak_vram_gib':backend.max_memory_allocated()/1024**3,'seconds':time.monotonic()-start_time,
                'device':device,'accelerator_name':resolved['name'],'precision':mode,
                'warning':'Two training records only; full-run stability and accuracy not established. XPU smoke does not establish CPU deployment parity.'}
        (args.out/'smoke.json').write_text(json.dumps(result,indent=2))
    else:
        if not state['best_params'] or not state['steps']:raise RuntimeError('No valid trained checkpoint')
        load_params(state['best_params']);adapter=args.out/'adapter'
        model.lm.save_pretrained(adapter);tok.save_pretrained(adapter)
        torch.save({'head':model.head.state_dict(),'head_dim':cfg['head_dim'],'temperature':1.},adapter/'head.pt')
        result={'base_model':cfg['base_model'],'base_revision':cfg['base_revision'],'hidden_size':cfg['hidden_size'],
            'trained':True,'training_steps':state['steps'],'best_epoch':state['best_epoch'],'seed':cfg['seed'],
            'configuration':cfg,'precision':mode,'device':device,'accelerator_name':resolved['name'],
            'validation_before':state['before'],'history':state['history'],
            'datasets':identity['datasets'],'final_test_evaluated':False,'calibration_fitted':False,'deployment_eligible':False}
        (args.out/'checkpoint.json').write_text(json.dumps(result,indent=2))
    state['complete']=True;dump(state);print(json.dumps(result),flush=True)

if __name__=='__main__':main()
