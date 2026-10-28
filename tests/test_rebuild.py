"""Acceptance contracts written before the new 4B training implementation."""
import pytest
from typelogic.schema import validate,answers
from typelogic.learning import distribution,assert_disjoint,teacher_consensus,softmax,select_checkpoint,training_record

def row(state='where is my card',group='a'):
 return {'state':state,'group_id':group,'questions':{'intent':{'type':'choice','instructions':'Intent?','criteria':{'arrival':'Card arrival','lost':'Lost card'}}},'targets':{'intent':[1.,0.]}}

@pytest.mark.parametrize('values',[[True,0],[float('nan'),1],[float('inf'),0],[-1,2],[0,0],[.1,.1]])
def test_invalid_training_distribution(values):
 with pytest.raises(ValueError):distribution(values,2)

def test_id_aligned_distribution():
 assert distribution({'lost':.3,'arrival':.7},['arrival','lost'])==[.7,.3]
 with pytest.raises(ValueError):distribution({'x':1},['arrival','lost'])

@pytest.mark.parametrize('overlap',[row(),row('new words','a'),row(' WHERE   IS MY CARD ','b')])
def test_split_leakage_is_blocked(overlap):
 with pytest.raises(ValueError,match='overlap'):assert_disjoint({'train':[row()],'test':[overlap]})

def test_empty_split_rejected():
 with pytest.raises(ValueError):assert_disjoint({'train':[row()],'test':[]})

def test_consensus_preserves_gold_and_reports_disagreement():
 r=teacher_consensus([[.1,.9],[.9,.1],[.2,.8]],gold=[1,0],weights=[.6,.3,.1],gold_weight=.7)
 assert r['target'][0]>=.7 and r['disagreement']>0 and r['review']
 assert abs(sum(r['target'])-1)<1e-10

def test_softmax_stability_and_temperature():
 assert softmax([10000,10000])==[.5,.5]
 assert max(softmax([2,0],2))<max(softmax([2,0]))
 for t in [0,float('nan'),-1]:
  with pytest.raises(ValueError):softmax([1,2],t)

def test_distribution_overflow_cannot_escape_response_contract():
 _,qs,t=validate(row())
 with pytest.raises(RuntimeError):answers(qs,[[1e308,1e308]],t,'unvalidated')

def test_binary_criteria_are_in_training():
 p={'state':'charged twice','questions':{'refund':{'type':'noul','instructions':'Refund?','criteria':{'true':'Explicit request','false':'No request'}}},'targets':{'refund':[.2,.8]}}
 rec=training_record(p);assert 'Explicit request' in str(rec) and rec['questions'][0]['target']==[.2,.8]

def test_checkpoint_rejects_incompatible_adapter(tmp_path):
 import json
 (tmp_path/'checkpoint.json').write_text(json.dumps({'base_model':'Qwen/Qwen3.5-0.8B-Base','hidden_size':1024,'trained':True}))
 with pytest.raises(ValueError,match='base'):select_checkpoint(tmp_path,'Qwen/Qwen3.5-4B',2560)

def test_checkpoint_must_be_trained(tmp_path):
 import json
 (tmp_path/'checkpoint.json').write_text(json.dumps({'base_model':'Qwen/Qwen3.5-4B','hidden_size':2560,'trained':False}))
 with pytest.raises(ValueError,match='trained'):select_checkpoint(tmp_path,'Qwen/Qwen3.5-4B',2560)

def test_budget_reservation_is_atomic_and_persistent(tmp_path):
 from research.openrouter import BudgetLedger
 from concurrent.futures import ThreadPoolExecutor
 ledger=BudgetLedger(tmp_path/'ledger.json',.1)
 def reserve(_):
  try:ledger.reserve(.03);return True
  except ValueError:return False
 with ThreadPoolExecutor(max_workers=6) as pool:assert sum(pool.map(reserve,range(6)))==3
 assert BudgetLedger(tmp_path/'ledger.json',.1).reserved==pytest.approx(.09)

def test_budget_refuses_invalid_amount(tmp_path):
 from research.openrouter import BudgetLedger
 ledger=BudgetLedger(tmp_path/'ledger.json',1)
 for v in [float('nan'),-.2,True]:
  with pytest.raises(ValueError):ledger.reserve(v)

def test_teacher_response_must_match_request():
 from research.openrouter import parse_decision
 p=row()
 with pytest.raises(ValueError):parse_decision(p,{'answers':{'intent':{'probabilities':{'arrival':1}}}})
 assert parse_decision(p,{'answers':{'intent':{'probabilities':{'lost':.2,'arrival':.8}}}})=={'intent':[.8,.2]}

def test_distillation_loss_gradients_and_ordinal_distance():
 import torch
 from typelogic.objectives import decision_loss
 logits=torch.tensor([0.,1.,-1.],requires_grad=True)
 loss=decision_loss(logits,torch.tensor([.1,.8,.1]),ordinal=True)
 loss.backward();assert torch.isfinite(logits.grad).all() and logits.grad.abs().sum()>0
 near=decision_loss(torch.tensor([0.,10.,0.]),torch.tensor([1.,0.,0.]),ordinal=True,ce_weight=0,brier_weight=0)
 far=decision_loss(torch.tensor([0.,0.,10.]),torch.tensor([1.,0.,0.]),ordinal=True,ce_weight=0,brier_weight=0)
 assert far>near

def test_teacher_wire_rounding_is_normalized_but_large_errors_rejected():
 from research.openrouter import parse_decision
 p=row()
 r=parse_decision(p,{'answers':{'intent':{'probabilities':{'arrival':.8001,'lost':.2}}}})
 assert sum(r['intent'])==pytest.approx(1)
 with pytest.raises(ValueError):parse_decision(p,{'answers':{'intent':{'probabilities':{'arrival':.7,'lost':.1}}}})

def test_checkpoint_preflight_detects_missing_shards(tmp_path):
 import json
 from typelogic.learning import checkpoint_preflight
 (tmp_path/'backbone').mkdir();(tmp_path/'adapter').mkdir()
 (tmp_path/'backbone/config.json').write_text(json.dumps({'text_config':{'hidden_size':2560}}))
 (tmp_path/'backbone/model.safetensors.index.json').write_text(json.dumps({'weight_map':{'x':'model-00001-of-00002.safetensors'}}))
 (tmp_path/'adapter/head.pt').write_bytes(b'x')
 with pytest.raises(FileNotFoundError,match='shard'):checkpoint_preflight(tmp_path)

def test_real_pointer_head_training_updates_weights():
 import torch
 from typelogic.vendor.pointer import PointerHead
 from typelogic.objectives import decision_loss
 torch.manual_seed(1701);head=PointerHead(8,4);state=torch.randn(8);options=torch.randn(3,8);target=torch.tensor([0.,1.,0.]);opt=torch.optim.AdamW(head.parameters(),lr=.03)
 before=decision_loss(head(state,options),target).item()
 for _ in range(20):
  opt.zero_grad();loss=decision_loss(head(state,options),target);loss.backward();opt.step()
 after=decision_loss(head(state,options),target).item()
 assert after<before*.5

def test_permutation_loss_aligned_gradient():
 import torch
 from typelogic.objectives import decision_loss
 a=torch.tensor([1.,2.],requires_grad=True);b=torch.tensor([1.,2.],requires_grad=True)
 loss=decision_loss(a,torch.tensor([0.,1.]),ce_weight=0,brier_weight=0,permuted_logits=b)
 assert loss.item()==pytest.approx(0)
 loss.backward();assert torch.isfinite(a.grad).all()

def test_engine_load_is_serialized_across_callers(tmp_path,monkeypatch):
 import time
 from concurrent.futures import ThreadPoolExecutor
 from typelogic.engine import DecisionEngine
 engine=DecisionEngine(tmp_path);calls=[]
 def inner():
  if engine.model is not None:return
  calls.append(1);time.sleep(.01);engine.model=object()
 monkeypatch.setattr(engine,'_load',inner,raising=False)
 with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:engine.load(),range(3)))
 assert len(calls)==1

def test_uncertainty_on_small_samples_is_not_overstated():
 from typelogic.statistics import wilson,paired_bootstrap
 low,high=wilson(1,1);assert low<.3 and high==pytest.approx(1)
 assert paired_bootstrap([True,False],[True,False])['ci95_percentile']==[0,0]
 with pytest.raises(ValueError):wilson(1,0)
 with pytest.raises(ValueError):paired_bootstrap([True],[])

def test_lora_parameters_receive_gradients_in_small_reference_model():
 import torch
 from peft import LoraConfig,get_peft_model
 from typelogic.objectives import decision_loss
 class Model(torch.nn.Module):
  def __init__(self):super().__init__();self.q_proj=torch.nn.Linear(4,3)
  def forward(self,x):return self.q_proj(x)
 torch.manual_seed(1701);model=get_peft_model(Model(),LoraConfig(r=2,lora_alpha=4,target_modules=['q_proj']))
 optimizer=torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],lr=.03)
 x=torch.ones(4);before=decision_loss(model(x),torch.tensor([0.,1.,0.])).item()
 for _ in range(10):
  optimizer.zero_grad();loss=decision_loss(model(x),torch.tensor([0.,1.,0.]));loss.backward();optimizer.step()
 assert decision_loss(model(x),torch.tensor([0.,1.,0.])).item()<before
 assert all(not p.requires_grad for n,p in model.named_parameters() if 'lora_' not in n)

def test_invalid_teacher_response_retains_raw_evidence_and_cost(tmp_path,monkeypatch):
 import io,json
 from research.openrouter import ResearchClient,BudgetLedger
 ledger=BudgetLedger(tmp_path/'cost.sqlite',1)
 raw={'model':'teacher','answers':{'intent':{'probabilities':{'arrival':.5,'lost':.1}}},'usage':{'cost':.0001},'id':'test-generation'}
 class Response(io.StringIO):
  def __enter__(self):return self
  def __exit__(self,*args):self.close()
 monkeypatch.setattr('urllib.request.urlopen',lambda *args,**kwargs:Response(json.dumps(raw)))
 client=ResearchClient(ledger,[{'id':'teacher','pricing':{'prompt':'0.000001'}}],key='test-key')
 with pytest.raises(ValueError) as failure:client.decide('teacher',row())
 assert failure.value.raw==raw and ledger.report()['reported_cost_usd']==pytest.approx(.0001)
