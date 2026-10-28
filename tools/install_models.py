"""Prepare manual Qwen3.5-2B assets and install a compatible trained adapter."""
import argparse,json,shutil,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from typelogic.model_profile import default_model_dir,prepare_backbone,verify_backbone
from typelogic.learning import select_checkpoint,validate_head,file_sha256


def install_trained(root,trained):
    root,trained=Path(root).resolve(),Path(trained).resolve();metadata=select_checkpoint(trained)
    for name in ('adapter','checkpoint.json','manifest.json'):
        if (root/name).exists():raise FileExistsError('Existing checkpoint will not be overwritten: '+name+'. Assemble a new output directory instead.')
    verify_backbone(root/'backbone');source=trained/'adapter'
    for name in ['head.pt','adapter_config.json','adapter_model.safetensors','tokenizer.json','tokenizer_config.json']:
        if not (source/name).is_file() or (source/name).is_symlink():raise FileNotFoundError('Missing regular trained adapter asset: '+name)
    import torch
    validate_head(torch.load(source/'head.pt',map_location='cpu',weights_only=True),metadata['hidden_size'])
    from safetensors import safe_open
    with safe_open(source/'adapter_model.safetensors',framework='pt',device='cpu') as tensors:
        if not list(tensors.keys()):raise ValueError('Adapter contains no tensors')
    lock=root/'.adapter-install.lock'
    with lock.open('x'):pass
    created=[]
    try:
        with tempfile.TemporaryDirectory(prefix='.adapter-staging-',dir=root) as stage:
            staged=Path(stage)/'adapter';staged.mkdir()
            for path in source.iterdir():
                if path.is_symlink() or not path.is_file():raise ValueError('Adapter must contain regular files only')
                shutil.copy2(path,staged/path.name)
            files={}
            for folder,prefix in [(root/'backbone','backbone'),(staged,'adapter')]:
                for path in folder.iterdir():
                    if path.is_file():files[prefix+'/'+path.name]={'bytes':path.stat().st_size,'sha256':file_sha256(path)}
            checkpoint=root/'checkpoint.json'
            with checkpoint.open('x',encoding='utf-8') as out:
                created.append(checkpoint);json.dump(metadata,out,indent=2)
            files['checkpoint.json']={'bytes':checkpoint.stat().st_size,'sha256':file_sha256(checkpoint)}
            manifest=root/'manifest.json'
            with manifest.open('x',encoding='utf-8') as out:
                created.append(manifest);json.dump({'files':files},out,indent=2)
            staged.rename(root/'adapter')
        return {'adapter_installed':True,'base_model':metadata['base_model'],'calibration_fitted':False,'final_test_evaluated':False,'deployment_eligible':False}
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        raise
    finally:lock.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir',type=Path,default=default_model_dir())
    parser.add_argument('--weights',type=Path,help='Optional local downloaded shard, copied after verification')
    parser.add_argument('--trained',type=Path,help='Completed 2B run containing checkpoint.json and adapter/')
    args=parser.parse_args()
    try:
        result=prepare_backbone(args.model_dir/'backbone',args.weights)
        if args.trained:result.update(install_trained(args.model_dir,args.trained))
        elif (args.model_dir/'adapter/head.pt').is_file():
            from typelogic.learning import checkpoint_preflight
            checkpoint_preflight(args.model_dir);result['adapter_trained']=True
        result['next_step']='Calibrate and evaluate the trained checkpoint.' if result.get('adapter_installed') or result.get('adapter_trained') else 'Backbone verified. Train the Qwen3.5-2B LoRA and pointer head, then rerun with --trained RUN_DIRECTORY.'
        print(json.dumps(result,indent=2));return 0
    except (ValueError,OSError) as exc:print(str(exc),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
