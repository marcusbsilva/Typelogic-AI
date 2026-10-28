"""A locally trained fixed profile; entire inference graph runs in ONNX Runtime."""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import threading
import time
import zlib
import numpy as np
from .core import validate,answers,canonical
from . import __version__

def flattened(state,vocab,limit):
    ids=[];paths=[]
    def emit(text,depth,sibling,path):
        words=str(text).lower().replace(',',' ').replace('.',' ').split()
        for word in words or ['']:
            if len(ids)>=limit:return
            ids.append(1+zlib.crc32(word.encode())%(vocab-1) if word else 1)
            paths.append((depth,sibling,zlib.crc32(path.encode())))
    def walk(value,path='',depth=0,sibling=0):
        if isinstance(value,dict):
            for i,(key,child) in enumerate(sorted(value.items(),key=lambda item:str(item[0]))):
                child_path=path+'.'+str(key) if path else str(key);emit(key,depth,i,child_path);walk(child,child_path,depth+1,i)
        elif isinstance(value,list):
            for i,child in enumerate(value):walk(child,f'{path}[{i}]',depth+1,i)
        else:emit(value,depth,sibling,path or '$')
    walk(state);return ids,paths

def state_inputs(state,cfg):
    length=cfg['max_state_len'];tokens,paths=flattened(state,cfg['vocab_size'],length+1)
    if len(tokens)>length:raise ValueError(f'State exceeds the specialist limit of {length} tokens; no silent truncation')
    ids=np.zeros((1,length),np.int64);positions=np.zeros((1,length,3),np.int64);padding=np.ones((1,length),bool)
    if tokens:ids[0,:len(tokens)]=tokens;positions[0,:len(paths)]=paths;padding[0,:len(tokens)]=False
    padding[:,0]=False
    return {'ids':ids,'paths':positions,'padding':padding},len(tokens)

class SpecialistEngine:
    def __init__(self,root):
        self.root=Path(root);self.lock=threading.RLock();self.encoder=None;self.readout=None;self.meta=None;self.status='not_loaded';self.error=None;self.cache=OrderedDict()
    def load(self):
        if self.encoder is not None:return
        from .offline import install
        install();self.status='loading'
        try:
            import onnxruntime as ort
            ort.disable_telemetry_events()
            meta=json.loads((self.root/'profile.json').read_text());parity=meta.get('validation',{}).get('onnx_parity',{});difference=parity.get('max_abs_probability_difference')
            if not isinstance(meta.get('trained_steps'),int) or meta['trained_steps']<1 or not isinstance(difference,(int,float)) or not np.isfinite(difference) or not 0<=difference<=2e-5 or parity.get('validation_states',0)<2:raise ValueError('Profile lacks valid training and export verification')
            if set(meta.get('graph_sha256',{}))!={'state.onnx','readout.onnx'}:raise ValueError('Hashes of both ONNX graphs are required')
            for name,expected in meta['graph_sha256'].items():
                if hashlib.sha256((self.root/name).read_bytes()).hexdigest()!=expected:raise ValueError('Modified ONNX graph: '+name)
            validate({'state':'profile','questions':meta['questions']})
            if not 32<=meta['config']['max_state_len']<=2048:raise ValueError('Invalid scenario limit')
            options=ort.SessionOptions();options.intra_op_num_threads=2
            encoder=ort.InferenceSession(str(self.root/'state.onnx'),options,providers=['CPUExecutionProvider'])
            readout=ort.InferenceSession(str(self.root/'readout.onnx'),options,providers=['CPUExecutionProvider'])
            self.encoder,self.readout,self.meta=encoder,readout,meta;self.status='ready';self.error=None;self.runtime=ort.__version__
        except Exception as exc:self.status='error';self.error=str(exc);raise
    def evaluate(self,payload):
        _,incoming,threshold=validate(payload);began=time.perf_counter()
        with self.lock:
            self.load();_,questions,_=validate({'state':'profile','questions':self.meta['questions']})
            normalize=lambda items:{q['id']:{'type':q['type'],'instructions':q['instr'],'criteria':q['criteria'],'abstain_option':q['abstain_option']} for q in items}
            if canonical(normalize(incoming))!=canonical(normalize(questions)):raise ValueError('This specialist only accepts the questions and alternatives in its trained profile')
            inputs,count=state_inputs(payload['state'],self.meta['config']);key=hashlib.sha256(canonical(payload['state']).encode()).hexdigest();hit=key in self.cache
            hidden=self.cache.pop(key) if hit else self.encoder.run(None,inputs)[0];self.cache[key]=hidden
            while len(self.cache)>8:self.cache.popitem(last=False)
            probabilities,evidence=self.readout.run(None,{'hidden':hidden,'padding':inputs['padding']});rows=[];offset=0
            for q in questions:n=len(q['keys']);rows.append(probabilities[0,offset:offset+n].tolist());offset+=n
            output=answers(questions,rows,threshold,'local_specialist_validation_not_general_guarantee')
            for q,value in zip(questions,evidence[0]):
                output[q['id']]['experimental_evidence_confidence']=float(value);output[q['id']]['experimental_evidence_confidence_validated']=False
                if q['type']=='choice':output[q['id']]['order_diagnostic']={'checked':False,'reason':'fixed_exported_profile'};output[q['id']]['review_reasons'].append('specialist_order_robustness_not_verified')
            return {'model':self.meta.get('name','local-specialist'),'version':__version__,'backend':'onnx','offline':True,'answers':output,'state_sha256':key,'prefix_cache_hit':hit,'usage':{'state_tokens':count,'questions':len(questions),'output_tokens':0},'latency_ms':round((time.perf_counter()-began)*1000,2),'external_action_executed':False}
    def health(self):
        return {'status':self.status,'error':self.error,'offline':True,'model':(self.meta or {}).get('name','local-specialist'),'model_present':(self.root/'profile.json').exists(),'device':'cpu','max_state_tokens':(self.meta or {}).get('config',{}).get('max_state_len'),'profile':(self.meta or {}).get('questions'),'onnx':{'runtime':getattr(self,'runtime',None),'telemetry_disabled':True},'cache_entries':len(self.cache)}
