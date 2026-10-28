"""Evaluate a trained TypeLogic Qwen2B adapter on Colab, without changing it.
Temperature is selected on calibration only; test is reported once afterwards.
No network/API calls. Run this file in a fresh Python process.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time


def probabilities(logits, temperature):
    if not math.isfinite(temperature) or temperature <= 0 or len(logits)<2 or any(not math.isfinite(v) for v in logits):
        raise ValueError('Invalid logits or temperature')
    values=[v/temperature for v in logits];peak=max(values)
    exp=[math.exp(v-peak) for v in values];total=sum(exp)
    return [v/total for v in exp]


def metrics(rows, temperature):
    if not rows:raise ValueError('No evaluation rows')
    nll=brier=correct=0;bins=[[] for _ in range(10)]
    for row in rows:
        z=row['logits'];target=row['target'];p=probabilities(z,temperature)
        if len(target)!=len(z) or any(not math.isfinite(v) or v<0 for v in target) or abs(sum(target)-1)>1e-6:
            raise ValueError('Invalid target distribution')
        pred=max(range(len(p)),key=p.__getitem__);gold=max(range(len(target)),key=target.__getitem__)
        hit=int(pred==gold);correct+=hit
        scaled=[v/temperature for v in z];peak=max(scaled)
        logsum=peak+math.log(sum(math.exp(v-peak) for v in scaled))
        nll+=sum(t*(logsum-v) for t,v in zip(target,scaled))
        brier+=sum((a-b)**2 for a,b in zip(p,target))
        bins[min(9,int(max(p)*10))].append((max(p),hit))
    count=len(rows);accuracy=correct/count
    ece=sum(abs(sum(c for c,_ in b)-sum(h for _,h in b)) for b in bins if b)/count
    # Wilson interval for accuracy: small test sets have wide uncertainty.
    k=1.96;den=1+k*k/count;center=(accuracy+k*k/(2*count))/den
    radius=k*math.sqrt(accuracy*(1-accuracy)/count+k*k/(4*count*count))/den
    return {'questions':count,'correct':correct,'accuracy':accuracy,'nll':nll/count,
            'brier':brier/count,'ece_10_bins':ece,'accuracy_wilson_95':[center-radius,center+radius]}


def fit_temperature(rows):
    # Include T=1 exactly, so empirical calibration NLL cannot worsen versus baseline.
    candidates=sorted(set([1.]+[math.exp(math.log(.1)+i/249*math.log(200)) for i in range(250)]))
    return min(candidates,key=lambda t:metrics(rows,t)['nll'])


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


def write_json(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    temporary.replace(path)


def check_dataset_identity(evaluation_hashes, checkpoint_hashes, allow_mismatch=False):
    matches=evaluation_hashes==checkpoint_hashes
    if not matches and not allow_mismatch:
        raise ValueError('Dataset hashes differ from the trained checkpoint')
    return matches


def validation_reproduction(expected_accuracy, loaded_accuracy, dataset_matches):
    if not dataset_matches:
        return {'performed':False,
                'reason':'Evaluation dataset differs from the checkpoint training dataset; accuracy reproduction is not applicable.',
                'loaded_accuracy_on_evaluation_dataset':loaded_accuracy}
    return {'performed':True,'expected_accuracy':expected_accuracy,'loaded_accuracy':loaded_accuracy,
            'absolute_difference':abs(expected_accuracy-loaded_accuracy)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',type=Path,default=Path('/content/typelogic_work/TypeLogic-AI'))
    p.add_argument('--run',type=Path,default=Path('/content/drive/MyDrive/TypeLogic/runs/qwen2b-t4-v1'))
    p.add_argument('--out',type=Path)
    p.add_argument('--data-dir',type=Path,required=True)
    p.add_argument('--train-file',type=Path,help='Optional audited training JSONL; defaults to data-dir/train.jsonl')
    p.add_argument('--resume',action='store_true',help='Reuse verified per-split logits from this evaluation only')
    p.add_argument('--allow-dataset-mismatch',action='store_true',
                   help='Evaluate this checkpoint on another validated dataset; skips only training-validation reproduction')
    args=p.parse_args();root=args.project.resolve();out=args.out or args.run/'evaluation-t4-v1'
    sys.path.insert(0,str(root))
    from typelogic.offline import install
    install()
    from typelogic.learning import select_checkpoint,assert_disjoint,training_record
    from typelogic.model_profile import verify_backbone
    meta=select_checkpoint(args.run);cfg=meta['configuration'];adapter=args.run/'adapter'
    paths={name:args.data_dir/f'{name}.jsonl' for name in ['train','validation','calibration','test']}
    if args.train_file:paths['train']=args.train_file
    hashes={name:sha(path) for name,path in paths.items()}
    dataset_matches=check_dataset_identity(hashes,meta['datasets'],args.allow_dataset_mismatch)
    splits={name:[json.loads(s) for s in path.read_text().splitlines() if s.strip()] for name,path in paths.items()}
    assert_disjoint(splits)
    for rows in splits.values():
        for row in rows:training_record(row)
    import torch,transformers,peft
    if not torch.cuda.is_available():raise RuntimeError('Connect a CUDA GPU before evaluating')
    amp_dtype=torch.float16 if meta['precision']=='fp16' else torch.bfloat16
    if amp_dtype==torch.bfloat16 and not torch.cuda.is_bf16_supported(including_emulation=False):
        raise RuntimeError('This checkpoint evaluation requires native BF16')
    identity={'script_sha256':sha(__file__),'datasets':hashes,'checkpoint_datasets':meta['datasets'],
              'dataset_matches_checkpoint':dataset_matches,'checkpoint_sha256':sha(args.run/'checkpoint.json'),
              'adapter_sha256':{name:sha(adapter/name) for name in ['adapter_config.json','adapter_model.safetensors','head.pt']},
              'precision':meta['precision'],'torch':torch.__version__,'transformers':transformers.__version__,
              'peft':peft.__version__,'gpu':torch.cuda.get_device_name(0)}
    if out.exists():
        if not args.resume:raise ValueError('Output exists; add --resume or select a new --out directory')
        if json.loads((out/'identity.json').read_text())!=identity:raise ValueError('Evaluation identity mismatch')
        if (out/'report.json').is_file():
            print((out/'report.json').read_text());return
    else:
        out.mkdir(parents=True);write_json(out/'identity.json',identity)
    print('Verifying local backbone...',flush=True)
    base=root/'models/decision-2b/backbone';verify_backbone(base)
    from typelogic.vendor.pointer import DecisionModel,load_tokenizer
    from peft import PeftModel
    head=torch.load(adapter/'head.pt',map_location='cpu',weights_only=True)
    if head['head_dim']!=cfg['head_dim']:raise ValueError('Head dimension mismatch')
    print('Loading epoch',meta['best_epoch'],'adapter for evaluation...',flush=True)
    tok=load_tokenizer(str(base))
    model=DecisionModel(str(base),tok,'cuda',head_dim=head['head_dim'],dtype=torch.float32,direct_load=True)
    model.lm=PeftModel.from_pretrained(model.lm,str(adapter),is_trainable=False,local_files_only=True)
    model.head.load_state_dict(head['head'],strict=True);model.head.temperature=1.
    if any(not torch.isfinite(v).all() for v in model.head.parameters()):raise ValueError('Invalid head values')
    model.lm.config.use_cache=False
    original_head=model.head.forward
    def fp32_head(*values,**kwargs):
        with torch.autocast('cuda',enabled=False):
            return original_head(*(v.float() if isinstance(v,torch.Tensor) else v for v in values),**kwargs)
    model.head.forward=fp32_head;model.eval();torch.cuda.reset_peak_memory_stats()
    start=time.monotonic()
    def infer(name):
        destination=out/(name+'-logits.json')
        checksum=out/(name+'-logits.sha256')
        if destination.is_file():
            if not checksum.is_file() or checksum.read_text()!=sha(destination):raise ValueError('Cached logits checksum mismatch')
            cached=json.loads(destination.read_text())
            if len(cached)!=sum(len(training_record(r)['questions']) for r in splits[name]):raise ValueError('Incomplete cached split')
            print('Reusing verified',name,'logits',flush=True);return cached
        result=[]
        with torch.no_grad():
            for index,row in enumerate(splits[name]):
                rec=training_record(row)
                enc=model.encode(tok,rec,max_state=cfg['max_state'],max_branch=cfg['max_branch'],strict=True)
                with torch.autocast('cuda',dtype=amp_dtype):values=model(enc)
                if len(values)!=len(rec['questions']):raise ValueError('Wrong number of question outputs')
                for question,z in zip(rec['questions'],values):
                    if not torch.isfinite(z).all():raise FloatingPointError('Non-finite evaluation logits')
                    result.append({'record_index':index,'question_id':question['id'],'options':question['options'],
                                   'logits':z.float().cpu().tolist(),'target':question['target'],
                                   'language':row.get('language','en'),'type':question['type'],
                                   'source_id':row.get('source_id','unknown')})
                if (index+1)%10==0 or index+1==len(splits[name]):
                    print(f'{name}: {index+1}/{len(splits[name])}',flush=True)
        write_json(destination,result);checksum.write_text(sha(destination));return result
    validation=infer('validation')
    baseline_val=metrics(validation,1.)
    expected=meta['history'][meta['best_epoch']-1]['validation']['accuracy']
    reproduction=validation_reproduction(expected,baseline_val['accuracy'],dataset_matches)
    # Require exact reproduction only when evaluating the original training dataset.
    if dataset_matches and reproduction['absolute_difference']>1e-8:
        write_json(out/'validation-mismatch.json',reproduction)
        raise RuntimeError('Saved adapter validation accuracy differs from training. Inspect validation-mismatch.json before testing.')
    calibration=infer('calibration');temperature=fit_temperature(calibration)
    # Freeze calibration choice BEFORE obtaining or looking at final-test scores.
    calibration_report={'temperature':temperature,'fit_split':'calibration',
        'baseline':metrics(calibration,1.),'fitted':metrics(calibration,temperature),
        'grid_boundary':temperature<=.100000001 or temperature>=19.999999,
        'applied_to_original_checkpoint':False}
    write_json(out/'temperature-fit.json',calibration_report)
    test=infer('test')
    report={'base_model':meta['base_model'],'best_epoch':meta['best_epoch'],'training_steps':meta['training_steps'],
        'identity':identity,'validation_reproduction':reproduction,
        'validation':{'raw':baseline_val,'calibrated':metrics(validation,temperature)},
        'calibration':calibration_report,'test':{'raw':metrics(test,1.),'calibrated':metrics(test,temperature)},
        'seconds_this_execution':time.monotonic()-start,'peak_vram_gib':torch.cuda.max_memory_allocated()/1024**3,
        'deployment_eligible':False,'checkpoint_modified':False,
        'scope':('Checkpoint evaluated on the supplied dataset without updating weights. '
                 'The historical filtered test split was previously inspected, so it is a diagnostic comparison, not an independent final benchmark. '
                 'GPU mixed-precision evaluation; CPU deployment parity not tested.')}
    report['by_language_type']={}
    for split_name,records in [('validation',validation),('calibration',calibration),('test',test)]:
        report['by_language_type'][split_name]={}
        for language,kind in sorted({(r['language'],r['type']) for r in records}):
            subset=[r for r in records if (r['language'],r['type'])==(language,kind)]
            report['by_language_type'][split_name][language+'/'+kind]={'raw':metrics(subset,1.),'calibrated':metrics(subset,temperature)}
    report['by_source']={}
    for split_name,records in [('validation',validation),('calibration',calibration),('test',test)]:
        report['by_source'][split_name]={}
        for source in sorted({r['source_id'] for r in records}):
            subset=[r for r in records if r['source_id']==source]
            report['by_source'][split_name][source]={'raw':metrics(subset,1.),'calibrated':metrics(subset,temperature)}
    write_json(out/'report.json',report)
    print(json.dumps(report,indent=2),flush=True)
    print('Report saved:',out/'report.json',flush=True)

if __name__=='__main__':main()
