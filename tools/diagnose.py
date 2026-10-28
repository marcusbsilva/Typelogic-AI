"""Check local dependencies and the selected model without downloading anything."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from typelogic.offline import install
from typelogic.model_profile import BASE_MODEL
from typelogic.profiles import active_model_dir as default_model_dir
from typelogic.learning import checkpoint_preflight


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--model-dir',type=Path,default=default_model_dir());args=parser.parse_args();install()
    report={'offline':True,'python':sys.version.split()[0],'model_dir':str(args.model_dir),'configured_default_base':BASE_MODEL,'dependencies':{},'errors':[]}
    for name in ('numpy','torch','onnx','onnxruntime','transformers','peft','safetensors'):
        try:
            module=__import__(name)
            if name=='onnxruntime':module.disable_telemetry_events()
            from importlib.metadata import version
            report['dependencies'][name]=version(name)
        except Exception as exc:report['errors'].append({'module':name,'error':str(exc)})
    try:report['checkpoint']=checkpoint_preflight(args.model_dir)
    except (ValueError,OSError) as exc:report['errors'].append({'checkpoint':str(args.model_dir),'error':str(exc)})
    # Optional field extraction (Needle 3); its absence is not an error.
    from typelogic import needle
    status={k:v for k,v in needle.status().items() if k in ('available','reason','folder','network')}
    if status['available']:
        try:needle.runtime()._verify();status['hashes']='verified'
        except Exception as exc:status.update(available=False,reason=str(exc))
    report['needle']=status
    # Evidence extraction through the local Ollama (optional; reported, never an error).
    from typelogic import evidence,modes
    from pathlib import Path as _P
    settings=modes.load_settings(_P.home()/'.typelogic-ai')
    report['evidence_extraction']={'backend':settings['extract_backend'],**evidence.available(settings['extract_model'])}
    report['ready']=not report['errors'];print(json.dumps(report,indent=2));return 0 if report['ready'] else 1
if __name__=='__main__':raise SystemExit(main())
