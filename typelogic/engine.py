"""Local pretrained decisions, bounded state cache and option-order diagnostics."""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from .offline import install
from .core import validate,answers,canonical,backend as core_backend
from .model_profile import BASE_MODEL,BASE_REVISION
from . import __version__

READOUTS=('onnx','rust','torch')
DEVICES=('cpu','xpu','auto')

def default_device(device=None):
    """TYPELOGIC_DEVICE: cpu (default), xpu (Intel Arc through a PyTorch XPU build) or auto (xpu when available).
    The backbone runs on the device in float32; ONNX/Rust readouts always run on the CPU."""
    value=(device or os.environ.get('TYPELOGIC_DEVICE','')).strip().lower() or 'cpu'
    if value=='gpu':value='xpu'   # the specialist's GPU path is Intel Arc (PyTorch XPU build)
    if value not in DEVICES:raise ValueError('TYPELOGIC_DEVICE must be cpu, xpu or auto')
    return value

def resolve_device(value):
    import torch
    available=bool(getattr(torch,'xpu',None) and torch.xpu.is_available())
    if value=='auto':return 'xpu' if available else 'cpu'
    if value=='xpu' and not available:raise RuntimeError('GPU mode needs an Intel Arc GPU and the PyTorch XPU environment (run: TypeLogic.bat setup --gpu)')
    return value

def checkpoint_fingerprint(root):
    """(per-file SHA-256, fingerprint) of the effective weights and configuration; calibration must match it."""
    root=Path(root).resolve();files={}
    for folder in (root/'backbone',root/'adapter'):
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix in ('.json','.safetensors','.pt','.txt') and path.name not in ('README.md','LICENSE'):
                digest=hashlib.sha256()
                with path.open('rb') as source:
                    for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
                files[str(path.relative_to(root)).replace('\\','/')]=digest.hexdigest()
    if (root/'checkpoint.json').is_file():
        from .learning import file_sha256
        files['checkpoint.json']=file_sha256(root/'checkpoint.json')
    return files,hashlib.sha256(canonical(files).encode()).hexdigest()

def default_readout(use_onnx=True):
    """TYPELOGIC_READOUT selects the decision-head backend; every option is parity-checked against PyTorch.
    use_onnx=False (tools that need the PyTorch head, e.g. calibration) always means the PyTorch readout."""
    if not use_onnx:return 'torch'
    value=os.environ.get('TYPELOGIC_READOUT','').strip().lower() or 'onnx'
    if value not in READOUTS:raise ValueError('TYPELOGIC_READOUT must be onnx, rust or torch')
    return value

class DecisionEngine:
    def __init__(self,model_dir,use_onnx=True,max_state=2048,readout=None,device=None,cpu_precision=None):
        self.root=Path(model_dir).resolve();self.max_state=max_state;self.cpu_precision=cpu_precision
        self.requested_device=default_device(device);self.device='cpu'
        self.readout=readout or default_readout(use_onnx)
        if self.readout not in READOUTS:raise ValueError('readout must be onnx, rust or torch')
        self.use_onnx=self.readout=='onnx'
        self.lock=threading.RLock();self.cache=OrderedDict();self.model=None;self.status='not_loaded';self.error=None
        self.base_model=BASE_MODEL if (self.root/'model-profile.json').exists() or self.root.name=='decision-2b' or self._checkpoint_base()==BASE_MODEL else 'unknown'
        self.calibration='not_validated_for_your_domain';self.runtime={};self.fingerprint=None;self.temperature=None
    def _checkpoint_base(self):
        try:return json.loads((self.root/'checkpoint.json').read_text()).get('base_model')
        except (OSError,ValueError,AttributeError):return None
    def load(self):
        with self.lock:
            return self._load()
    def _load(self):
        if self.model is not None:return
        install();self.status='loading'
        try:
            from .learning import checkpoint_preflight,validate_head
            self.asset_status=checkpoint_preflight(self.root)
            import torch
            from peft import PeftModel
            from .vendor.pointer import DecisionModel,load_tokenizer
            base,adapter=self.root/'backbone',self.root/'adapter'
            if not (base/'config.json').is_file() or not (adapter/'head.pt').is_file():raise FileNotFoundError('Model weights missing: run TypeLogic.bat models (python tools/fetch_models.py) once; the application itself never downloads anything')
            torch.set_num_threads(int(os.environ.get('TYPELOGIC_THREADS','0')) or min(4,os.cpu_count() or 1));torch.manual_seed(17)
            meta=torch.load(adapter/'head.pt',map_location='cpu',weights_only=True)
            head_dim=validate_head(meta,self.asset_status['hidden_size'])
            tokenizer=load_tokenizer(str(adapter))
            model=DecisionModel(str(base),tokenizer,'cpu',head_dim=head_dim,dtype=torch.float32)
            model.lm=PeftModel.from_pretrained(model.lm,str(adapter),local_files_only=True,torch_device='cpu').merge_and_unload()
            model.head.load_state_dict(meta['head'],strict=True)
            self.device=resolve_device(self.requested_device)
            if self.device!='cpu':
                model.to(self.device);model.device=self.device
                # XPU: float16 autocast for the backbone (the protocol used by evaluate_local.py and the calibration fit);
                # the decision head always computes in float32. TYPELOGIC_XPU_PRECISION=float32 disables autocast.
                self.precision=os.environ.get('TYPELOGIC_XPU_PRECISION','float16').strip().lower()
                if self.precision not in ('float16','float32'):raise ValueError('TYPELOGIC_XPU_PRECISION must be float16 or float32')
                if self.precision=='float16':model.lm.half()   # 4.5 GB of device memory instead of 9 GB; leaves room for a generalist
                original=model.head.forward
                def float_head(*values,**kwargs):
                    with torch.autocast(self.device,enabled=False):
                        return original(*(v.float() if isinstance(v,torch.Tensor) else v for v in values),**kwargs)
                model.head.forward=float_head
            else:
                # CPU: float32 by default (parity reference). TYPELOGIC_CPU_PRECISION=bfloat16 halves the memory (about 4.5 GB),
                # e.g. to share a 16 GB computer with a generalist; the head still computes in float32.
                self.precision=(self.cpu_precision or os.environ.get('TYPELOGIC_CPU_PRECISION','') or 'float32').strip().lower()
                if self.precision not in ('float32','bfloat16'):raise ValueError('TYPELOGIC_CPU_PRECISION must be float32 or bfloat16')
                if self.precision=='bfloat16':
                    model.lm.to(torch.bfloat16)
                    original=model.head.forward
                    def float_head(*values,**kwargs):
                        return original(*(v.float() if isinstance(v,torch.Tensor) else v for v in values),**kwargs)
                    model.head.forward=float_head
            self.temperature=float(meta.get('temperature',1))
            files,self.fingerprint=checkpoint_fingerprint(self.root)
            manifest=self.root/'manifest.json'
            if manifest.exists():
                expected=json.loads(manifest.read_text())
                for name,value in files.items():
                    if expected['files'].get(name,{}).get('sha256')!=value:raise ValueError('Modified weight or configuration: '+name)
            self.provenance={'files_sha256':files,'manifest_verified':manifest.exists()}
            calibration_path=self.root/'calibration.json'
            if calibration_path.exists():
                local=json.loads(calibration_path.read_text())
                if local.get('checkpoint_sha256')!=self.fingerprint:raise ValueError('Calibration belongs to a different checkpoint')
                self.temperature=float(local['temperature']);self.calibration='local_validation_not_general_guarantee'
            if not 0<self.temperature<100:raise ValueError('Invalid temperature')
            model.head.temperature=self.temperature;model.eval()
            if self.readout=='onnx':
                from .onnx_head import export_pointer,convert
                # Build and verify the graph in memory: loading never writes into the model
                # directory (works from read-only installs and avoids races between instances).
                model.head,self.runtime=convert(model.head,export_pointer(model.head))
            elif self.readout=='rust':
                from .rust_readout import convert as rust_convert
                model.head,self.runtime=rust_convert(model.head)
            self.base_model=json.loads((self.root/'checkpoint.json').read_text())['base_model'] if (self.root/'checkpoint.json').exists() else json.loads((adapter/'training_config.json').read_text()).get('args',{}).get('base','unknown') if (adapter/'training_config.json').exists() else 'unknown'
            self.model,self.tokenizer=model,tokenizer;self.status='ready';self.error=None
        except Exception as exc:self.status='error';self.error=str(exc);raise
    def _predict(self,state,questions):
        if self.device!='cpu' and self.precision=='float16':
            import torch
            with torch.autocast(self.device,dtype=torch.float16):
                return self._predict_inner(state,questions)
        return self._predict_inner(state,questions)
    def _predict_inner(self,state,questions):
        record={'state':state,'questions':[{'instr':q['instr'],'options':q['options'],'label':0} for q in questions]}
        enc=self.model.encode(self.tokenizer,record,max_state=self.max_state,max_branch=self.max_state+2048,strict=True)
        ids=tuple(enc['ids'][:enc['seg'].count(0)]);hit=ids in self.cache
        if hit:
            prefix=self.cache.pop(ids);probabilities=self.model.probs_with_prefix(enc,prefix)
        else:probabilities,prefix=self.model.probs_and_prefix(enc)
        self.cache[ids]=prefix
        while len(self.cache)>2 or (len(self.cache)>1 and sum(map(len,self.cache))>4096):self.cache.popitem(last=False)
        return [p.tolist() for p in probabilities],{'state_tokens':len(ids),'input_tokens':len(enc['ids']),'questions':len(questions),'output_tokens':0},hit
    def evaluate(self,payload):
        state,questions,threshold=validate(payload)
        began=time.perf_counter()
        with self.lock:
            self.load();rows,usage,hit=self._predict(state,questions)
            result=answers(questions,rows,threshold,self.calibration)
            if payload.get('permutation_check',True):
                for q in questions:
                    if q['type']!='choice':continue
                    variants=[]
                    for order in (list(reversed(range(len(q['keys'])))),list(range(1,len(q['keys'])))+[0]):
                        variant={**q,'keys':[q['keys'][i] for i in order],'options':[q['options'][i] for i in order]}
                        probabilities,_,_=self._predict(state,[variant]);variants.append(dict(zip(variant['keys'],probabilities[0])))
                    original=result[q['id']]['probabilities'];chosen=result[q['id']]['choice']
                    shift=max(abs(original[key]-variant[key]) for variant in variants for key in original)
                    flipped=any(max(v,key=v.__getitem__)!=chosen for v in variants)
                    unstable=flipped or shift>.15
                    result[q['id']]['order_diagnostic']={'checked':True,'permutations':2,'max_probability_shift':shift,'top_choice_changed':flipped,'unstable':unstable,'scope':'two permutations; not a robustness certificate'}
                    if unstable:result[q['id']]['review_reasons'].append('option_order_sensitive');result[q['id']]['recommendation']='review'
            else:
                for q in questions:
                    if q['type']=='choice':result[q['id']]['order_diagnostic']={'checked':False};result[q['id']]['review_reasons'].append('order_check_disabled')
            return {'model':'typelogic-qwen35-2b' if self.base_model==BASE_MODEL else 'typelogic-small','version':__version__,'checkpoint_sha256':self.fingerprint,
                    'backend':{'onnx':'torch+onnx','rust':'torch+rust','torch':'torch'}[self.readout],'offline':True,'model_provenance':self.provenance,'base_model':self.base_model,'state_sha256':hashlib.sha256(state.encode()).hexdigest(),
                    'answers':result,'usage':usage,'prefix_cache_hit':hit,'latency_ms':round((time.perf_counter()-began)*1000,2),
                    'external_action_executed':False}
    def health(self):
        return {'status':self.status,'error':self.error,'offline':True,'model':'typelogic-qwen35-2b' if self.base_model==BASE_MODEL else 'typelogic-small','base_model':self.base_model,'base_revision':BASE_REVISION if self.base_model==BASE_MODEL else None,'adapter_present':(self.root/'adapter/head.pt').is_file(),'model_present':(self.root/'adapter/head.pt').is_file() and any((self.root/'backbone').glob('*.safetensors')),'device':self.device,'requested_device':self.requested_device,'precision':getattr(self,'precision','float32'),'max_state_tokens':self.max_state,'temperature':self.temperature,'calibration':self.calibration,'checkpoint_sha256':self.fingerprint,'model_provenance':getattr(self,'provenance',None),'onnx':self.runtime,'readout':self.readout,'core':core_backend(),'model_dir':str(self.root),'cache_entries':len(self.cache)}
