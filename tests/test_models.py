import copy,json,os
from pathlib import Path
import pytest

np = pytest.importorskip('numpy')
from typelogic.engine import DecisionEngine
from typelogic.profiles import active_model_dir
from typelogic.specialist import SpecialistEngine,state_inputs
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def real_engine():
    # The active profile (models/active.json; models/decision-2b when none is selected).
    model_dir=active_model_dir(ROOT)
    # Loading the 2B backbone on CPU needs several GB of RAM; never do it implicitly
    # (for example while a training run is using the same machine).
    if os.environ.get('TYPELOGIC_REAL_INFERENCE')!='1':pytest.skip('Set TYPELOGIC_REAL_INFERENCE=1 to run real 2B inference tests')
    if not (model_dir/'adapter/head.pt').exists() or not any((model_dir/'backbone').glob('*.safetensors')):pytest.skip('Model weights not downloaded (python tools/fetch_models.py)')
    engine=DecisionEngine(model_dir);engine.load();return engine

def test_real_pointer_onnx_parity_and_telemetry(real_engine):
    assert real_engine.runtime['providers']==['CPUExecutionProvider']
    assert real_engine.runtime['max_abs_logit_difference']<3e-5 and real_engine.runtime['telemetry_disabled']

def test_real_question_isolation_prefix_cache_and_reproducibility(real_engine):
    p=json.loads((ROOT/'examples/decision.json').read_text());p['permutation_check']=False
    a=real_engine.evaluate(p);b=real_engine.evaluate(p)
    assert b['prefix_cache_hit'] and b['usage']['output_tokens']==0
    for key in a['answers']:
        np.testing.assert_allclose(list(a['answers'][key]['probabilities'].values()),list(b['answers'][key]['probabilities'].values()),atol=2e-5)
        solo={**p,'questions':{key:p['questions'][key]}};result=real_engine.evaluate(solo)
        np.testing.assert_allclose(list(a['answers'][key]['probabilities'].values()),list(result['answers'][key]['probabilities'].values()),atol=2e-5)
    assert not a['external_action_executed']

def test_real_context_overflow_is_rejected(real_engine):
    p=json.loads((ROOT/'examples/decision.json').read_text());p['state']='word '*2500
    with pytest.raises(ValueError,match='state exceeds'):real_engine.evaluate(p)

def test_real_choice_order_diagnostics(real_engine):
    p=json.loads((ROOT/'examples/decision.json').read_text());result=real_engine.evaluate(p)
    answer=next(a for a in result['answers'].values() if a['type']=='choice')
    assert answer['order_diagnostic']['checked'] and answer['order_diagnostic']['permutations']==2
    assert answer['requires_review'] and not answer['eligible_for_automation']

@pytest.fixture(scope='module')
def specialist():
    if not (ROOT/'examples/specialist-demo/profile.json').exists():pytest.skip('Example specialist not trained: run tools/train_specialist.py --out examples/specialist-demo (see docs/GUIDE.md)')
    engine=SpecialistEngine(ROOT/'examples/specialist-demo');engine.load();return engine

def test_specialist_option_order_does_not_relabel_outputs(specialist):
    p=json.loads((ROOT/'examples/specialist-validation.jsonl').read_text().splitlines()[0]);a=specialist.evaluate(p)
    swapped=copy.deepcopy(p);key=next(k for k,v in swapped['questions'].items() if v['type']=='choice');criteria=swapped['questions'][key]['criteria'];swapped['questions'][key]['criteria']=dict(reversed(list(criteria.items())))
    b=specialist.evaluate(swapped)
    assert a['answers']==b['answers'] and b['prefix_cache_hit'] and b['backend']=='onnx'
    changed=copy.deepcopy(p);changed['questions'][key]['instructions']+=' different'
    with pytest.raises(ValueError,match='only accepts'):specialist.evaluate(changed)

def test_specialist_context_is_not_silently_truncated(specialist):
    with pytest.raises(ValueError,match='no silent truncation'):state_inputs('word '*300,specialist.meta['config'])

def test_specialist_rejects_tampered_graph(tmp_path):
    original=ROOT/'examples/specialist-demo'
    if not (original/'profile.json').exists():pytest.skip('Example specialist not trained: run tools/train_specialist.py --out examples/specialist-demo')
    for name in ('profile.json','state.onnx','readout.onnx'):(tmp_path/name).write_bytes((original/name).read_bytes())
    with (tmp_path/'state.onnx').open('ab') as f:f.write(b'tampered')
    engine=SpecialistEngine(tmp_path)
    with pytest.raises(ValueError,match='Modified ONNX graph'):engine.load()
    assert engine.encoder is None and engine.status=='error'
