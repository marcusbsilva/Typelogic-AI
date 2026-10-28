"""Test-first contracts for the Qwen3.5-2B migration; fixtures are not trained weights."""
import json
from pathlib import Path
import pytest
from typelogic import model_profile as profile
from typelogic.learning import file_sha256,select_checkpoint


def _local_backbone():
    # Backbone of the active profile (models/active.json), falling back to models/decision-2b.
    from typelogic.profiles import active_model_dir
    base=active_model_dir(profile.PROJECT_ROOT)/'backbone'
    if not (base/'config.json').is_file():
        pytest.skip('Qwen3.5-2B backbone not downloaded (python tools/fetch_models.py --only backbone)')
    return base


def assets(tmp_path):
    base=tmp_path/'backbone';base.mkdir()
    contents={'config.json':json.dumps({'text_config':{'hidden_size':2048}}).encode(),
              'model.safetensors.index.json':json.dumps({'weight_map':{'x':profile.WEIGHTS_NAME}}).encode(),
              'tokenizer.json':b'{}',profile.WEIGHTS_NAME:b'test weights'}
    for name,data in contents.items():(base/name).write_bytes(data)
    manifest={'repo':profile.BASE_MODEL,'revision':profile.BASE_REVISION,'hidden_size':2048,
              'files':{name:{'bytes':len(data),'sha256':file_sha256(base/name),'bundled':not name.endswith('.safetensors')} for name,data in contents.items()}}
    return base,manifest


def test_default_student_is_official_2b():
    assert profile.BASE_MODEL=='Qwen/Qwen3.5-2B' and profile.HIDDEN_SIZE==2048
    assert profile.default_model_dir(Path('/app'))==Path('/app/models/decision-2b')


def test_missing_and_modified_weights_are_rejected(tmp_path):
    base,manifest=assets(tmp_path)
    assert profile.verify_backbone(base,manifest)['complete']
    weights=base/profile.WEIGHTS_NAME;weights.unlink()
    with pytest.raises(FileNotFoundError,match='manually'):profile.verify_backbone(base,manifest)
    weights.write_bytes(b'wrong')
    with pytest.raises(ValueError,match='Modified'):profile.verify_backbone(base,manifest)


def test_manual_import_preserves_source_and_does_not_overwrite(tmp_path):
    base,manifest=assets(tmp_path);weights=base/profile.WEIGHTS_NAME
    source=tmp_path/'download.safetensors';weights.rename(source)
    assert profile.prepare_backbone(base,source,manifest)['complete']
    assert source.read_bytes()==weights.read_bytes()
    provenance=json.loads((base/'asset-provenance.json').read_text())
    assert provenance['files'][profile.WEIGHTS_NAME]==manifest['files'][profile.WEIGHTS_NAME]['sha256']
    weights.write_bytes(b'existing bad weights')
    with pytest.raises(ValueError,match='Modified'):profile.prepare_backbone(base,source,manifest)
    assert weights.read_bytes()==b'existing bad weights'


def test_wrong_download_is_not_copied(tmp_path):
    base,manifest=assets(tmp_path);(base/profile.WEIGHTS_NAME).unlink()
    source=tmp_path/'wrong';source.write_bytes(b'wrong base')
    with pytest.raises(ValueError):profile.prepare_backbone(base,source,manifest)
    assert not (base/profile.WEIGHTS_NAME).exists() and not (base/'asset-provenance.json').exists()


@pytest.mark.parametrize('name',['../outside','/tmp/outside','..\\outside'])
def test_manifest_cannot_escape_base(tmp_path,name):
    base,manifest=assets(tmp_path);manifest['files'][name]={'bytes':1,'sha256':'x'}
    with pytest.raises(ValueError,match='path'):profile.verify_backbone(base,manifest)


@pytest.mark.parametrize('base,hidden,trained',[('Qwen/Qwen3.5-4B',2560,True),('Qwen/Qwen3.5-0.8B-Base',1024,True),('Qwen/Qwen3.5-2B',2560,True),('Qwen/Qwen3.5-2B',2048,False)])
def test_reject_old_or_untrained_adapter(tmp_path,base,hidden,trained):
    (tmp_path/'checkpoint.json').write_text(json.dumps({'base_model':base,'hidden_size':hidden,'trained':trained}))
    with pytest.raises(ValueError):select_checkpoint(tmp_path)


def test_valid_2b_checkpoint_and_revision(tmp_path):
    meta={'base_model':profile.BASE_MODEL,'base_revision':profile.BASE_REVISION,'hidden_size':2048,'trained':True}
    (tmp_path/'checkpoint.json').write_text(json.dumps(meta));assert select_checkpoint(tmp_path)==meta
    (tmp_path/'checkpoint.json').write_text(json.dumps({**meta,'base_revision':'main'}))
    with pytest.raises(ValueError,match='revision'):select_checkpoint(tmp_path)


def test_prepared_engine_does_not_claim_ready(tmp_path):
    from typelogic.engine import DecisionEngine
    (tmp_path/'model-profile.json').write_text(json.dumps({'base_model':profile.BASE_MODEL,'base_revision':profile.BASE_REVISION,'hidden_size':2048}))
    engine=DecisionEngine(tmp_path);health=engine.health()
    assert health['base_model']==profile.BASE_MODEL and not health['model_present'] and not health['adapter_present']
    with pytest.raises(FileNotFoundError):engine.load()
    assert engine.model is None and engine.status=='error'


def test_head_checked_before_backbone_allocation():
    import torch
    from typelogic.learning import validate_head
    head={'head_dim':4,'head':{'q.weight':torch.zeros(4,1024),'k.weight':torch.zeros(4,1024),'q.bias':torch.zeros(4),'k.bias':torch.zeros(4)}}
    with pytest.raises(ValueError,match='dimension'):validate_head(head,2048)
    head['head']['q.weight']=torch.zeros(4,2048);head['head']['k.weight']=torch.zeros(4,2048)
    assert validate_head(head,2048)==4
    head['head']['q.bias'][0]=float('nan')
    with pytest.raises(ValueError,match='nonfinite'):validate_head(head,2048)


def test_bundled_metadata_and_tokenizer():
    base=_local_backbone()
    from typelogic.vendor.pointer import load_tokenizer,SPECIAL,encode
    manifest=profile.asset_manifest()
    assert manifest['files'][profile.WEIGHTS_NAME]['bytes']==4548221488
    profile.verify_backbone(base,manifest,metadata_only=True)
    tokenizer=load_tokenizer(str(base));ids=[tokenizer.convert_tokens_to_ids(s) for s in SPECIAL]
    assert len(set(ids))==5 and all(isinstance(i,int) and 0<=i<248320 for i in ids)
    assert all(isinstance(i,int) for i in encode(tokenizer,{'state':'Card missing','questions':[{'instr':'Intent?','options':['Lost card','Card arrival'],'label':0}]},strict=True)['ids'])


def test_training_configuration_rejects_other_base_and_ignored_alpha():
    from tools.train_backbone import validate_profile
    cfg=json.loads((profile.PROJECT_ROOT/'research/training-2b.json').read_text());validate_profile(cfg)
    with pytest.raises(ValueError,match='identity'):validate_profile({**cfg,'base_revision':'main'})
    with pytest.raises(ValueError,match='alpha'):validate_profile({**cfg,'lora_alpha':7})


def test_adapter_install_rejects_untrained_before_modification(tmp_path):
    from tools.install_models import install_trained
    trained=tmp_path/'run';trained.mkdir()
    (trained/'checkpoint.json').write_text(json.dumps({'base_model':profile.BASE_MODEL,'base_revision':profile.BASE_REVISION,'hidden_size':2048,'trained':False}))
    target=tmp_path/'target';target.mkdir()
    with pytest.raises(ValueError,match='trained'):install_trained(target,trained)
    assert list(target.iterdir())==[]


def test_transformers_recognizes_official_2b_architecture_without_weights():
    base=_local_backbone()
    import torch
    from transformers import AutoConfig,AutoModelForCausalLM
    config=AutoConfig.from_pretrained(base,local_files_only=True,trust_remote_code=False)
    with torch.device('meta'):model=AutoModelForCausalLM.from_config(config)
    assert model.model.config.hidden_size==2048 and model.model.config.num_hidden_layers==24
    assert all(p.device.type=='meta' for p in model.parameters())


def test_trained_adapter_install_and_no_overwrite(tmp_path,monkeypatch):
    import torch
    from safetensors.torch import save_file
    from tools.install_models import install_trained
    base,manifest=assets(tmp_path)
    monkeypatch.setattr(profile,'asset_manifest',lambda:manifest)
    trained=tmp_path/'run';adapter=trained/'adapter';adapter.mkdir(parents=True)
    meta={'base_model':profile.BASE_MODEL,'base_revision':profile.BASE_REVISION,'hidden_size':2048,'trained':True}
    (trained/'checkpoint.json').write_text(json.dumps(meta))
    torch.save({'head_dim':4,'head':{'q.weight':torch.zeros(4,2048),'k.weight':torch.zeros(4,2048),'q.bias':torch.zeros(4),'k.bias':torch.zeros(4)}},adapter/'head.pt')
    for name in ('adapter_config.json','tokenizer.json','tokenizer_config.json'):(adapter/name).write_text('{}')
    save_file({'fixture':torch.zeros(1)},adapter/'adapter_model.safetensors')
    result=install_trained(tmp_path,trained)
    assert result['adapter_installed'] and not result['deployment_eligible']
    assert (tmp_path/'adapter/head.pt').read_bytes()==(adapter/'head.pt').read_bytes()
    assert (tmp_path/'manifest.json').is_file()
    with pytest.raises(FileExistsError):install_trained(tmp_path,trained)


def test_reduced_qwen35_hybrid_lora_backward(tmp_path):
    """Real forward/backward on a tiny random architecture, never a 2B quality test."""
    import torch
    from types import SimpleNamespace
    from transformers import Qwen3_5TextConfig,AutoModelForCausalLM
    from typelogic.vendor.pointer import DecisionModel,SPECIAL
    from typelogic.objectives import decision_loss
    torch.set_num_threads(1);torch.manual_seed(1701)
    cfg=Qwen3_5TextConfig(vocab_size=64,hidden_size=32,intermediate_size=64,num_hidden_layers=4,num_attention_heads=4,num_key_value_heads=2,head_dim=8,
        layer_types=['linear_attention','linear_attention','linear_attention','full_attention'],linear_num_key_heads=2,linear_num_value_heads=2,linear_key_head_dim=8,linear_value_head_dim=8,
        rope_parameters={'rope_type':'default','rope_theta':10000000.,'partial_rotary_factor':1.,'mrope_section':[1,1,2],'mrope_interleaved':True},pad_token_id=0)
    base=tmp_path/'tiny';AutoModelForCausalLM.from_config(cfg).save_pretrained(base)
    class Tokenizer:
        pad_token_id=0
        def convert_tokens_to_ids(self,s):return SPECIAL.index(s)+1
        def __call__(self,s,**kwargs):return SimpleNamespace(input_ids=[10+sum(w.encode())%50 for w in s.split()])
    tok=Tokenizer();model=DecisionModel(str(base),tok,'cpu',lora=2,head_dim=4)
    enc=model.encode(tok,{'state':'my card is missing','questions':[{'instr':'Select intent','options':['lost card','cash transfer'],'label':0}]},strict=True)
    trainable=[p for p in model.parameters() if p.requires_grad];before=[p.detach().clone() for p in trainable]
    optimizer=torch.optim.AdamW(trainable,lr=.01)
    logits=model(enc)[0];loss=decision_loss(logits,torch.tensor([1.,0.]));loss.backward()
    assert all(p.grad is None or torch.isfinite(p.grad).all() for p in trainable)
    assert any(p.grad is not None and p.grad.abs().sum()>0 for name,p in model.named_parameters() if 'lora_' in name)
    optimizer.step()
    assert any(not torch.equal(old,p) for old,p in zip(before,trainable)) and model.hybrid


def test_backbone_alone_cannot_be_loaded_as_trained_checkpoint(tmp_path):
    from typelogic.learning import checkpoint_preflight
    assets(tmp_path)
    (tmp_path/'model-profile.json').write_text(json.dumps({'base_model':profile.BASE_MODEL,'base_revision':profile.BASE_REVISION,'hidden_size':2048}))
    with pytest.raises(FileNotFoundError,match='LoRA and pointer head'):checkpoint_preflight(tmp_path)


def test_metadata_recovery_never_requests_tensor_files(tmp_path,monkeypatch):
    import sys,huggingface_hub
    from tools import fetch_training_assets
    requested=[]
    monkeypatch.setattr(huggingface_hub,'hf_hub_download',lambda repo,filename,**kwargs:requested.append(filename))
    monkeypatch.setattr(fetch_training_assets,'verify_backbone',lambda *args,**kwargs:None)
    monkeypatch.setattr(sys,'argv',['fetch_training_assets','--destination',str(tmp_path/'new')])
    fetch_training_assets.main()
    assert 'config.json' in requested and 'tokenizer.json' in requested
    assert not any(name.endswith('.safetensors') for name in requested)
