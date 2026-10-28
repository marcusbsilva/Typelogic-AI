"""Pinned Qwen3.5-2B identity and explicit, network-free asset preparation."""
import json
from pathlib import Path
import shutil

BASE_MODEL='Qwen/Qwen3.5-2B'
BASE_REVISION='15852e8c16360a2fea060d615a32b45270f8a8fc'
HIDDEN_SIZE=2048
WEIGHTS_NAME='model.safetensors-00001-of-00001.safetensors'
PROJECT_ROOT=Path(__file__).resolve().parents[1]


def default_model_dir(root=PROJECT_ROOT):
    return Path(root)/'models/decision-2b'


def asset_manifest():
    return json.loads((PROJECT_ROOT/'research/qwen35-2b-assets.json').read_text(encoding='utf-8'))


def asset_path(base,name):
    if not isinstance(name,str) or '\\' in name or Path(name).is_absolute():raise ValueError('Invalid asset path')
    base=Path(base).resolve();path=(base/name).resolve()
    if not path.is_relative_to(base) or path==base:raise ValueError('Invalid asset path')
    return path


def verify_file(path,record):
    from .learning import file_sha256
    if not path.is_file():raise FileNotFoundError('Missing local asset: '+path.name+'. Download the pinned weights manually; startup never downloads files.')
    if path.stat().st_size!=record['bytes'] or file_sha256(path)!=record['sha256']:raise ValueError('Modified or incompatible asset: '+path.name+'. Existing files were not overwritten.')


def verify_backbone(base,manifest=None,metadata_only=False):
    manifest=asset_manifest() if manifest is None else manifest
    if (manifest.get('repo'),manifest.get('revision'),manifest.get('hidden_size'))!=(BASE_MODEL,BASE_REVISION,HIDDEN_SIZE):raise ValueError('Backbone identity mismatch: use the pinned Qwen3.5-2B profile')
    files=manifest['files']
    for required in ('config.json','model.safetensors.index.json','tokenizer.json',WEIGHTS_NAME):
        if required not in files:raise ValueError('Incomplete asset manifest: '+required)
    paths={name:asset_path(base,name) for name in files}
    for name,path in paths.items():
        if metadata_only and name.endswith('.safetensors'):continue
        verify_file(path,files[name])
    config=json.loads(paths['config.json'].read_text(encoding='utf-8'))
    if config.get('text_config',config)['hidden_size']!=HIDDEN_SIZE:raise ValueError('Backbone hidden dimension mismatch')
    index=json.loads(paths['model.safetensors.index.json'].read_text(encoding='utf-8'))
    if set(index['weight_map'].values())!={WEIGHTS_NAME}:raise ValueError('Unexpected backbone shards')
    return {'base_model':BASE_MODEL,'base_revision':BASE_REVISION,'hidden_size':HIDDEN_SIZE,'complete':not metadata_only,'metadata_verified':True,'adapter_trained':False}


def prepare_backbone(base,weights=None,manifest=None):
    """Verify manually placed weights or import a verified local copy. No network."""
    base=Path(base).resolve();manifest=asset_manifest() if manifest is None else manifest
    verify_backbone(base,manifest,metadata_only=True);destination=base/WEIGHTS_NAME
    if destination.exists():verify_file(destination,manifest['files'][WEIGHTS_NAME])
    elif weights is not None:
        source=Path(weights).resolve();verify_file(source,manifest['files'][WEIGHTS_NAME])
        with destination.open('xb') as out:
            try:
                with source.open('rb') as inp:shutil.copyfileobj(inp,out,1024*1024)
            except BaseException:
                out.close();destination.unlink(missing_ok=True);raise
    result=verify_backbone(base,manifest)
    provenance={'repo':BASE_MODEL,'revision':BASE_REVISION,'files':{name:record['sha256'] for name,record in manifest['files'].items()}}
    path=base/'asset-provenance.json'
    if path.exists() and json.loads(path.read_text(encoding='utf-8'))!=provenance:raise ValueError('Existing asset provenance differs; preserve and inspect it before reinstalling')
    if not path.exists():
        with path.open('x',encoding='utf-8') as out:json.dump(provenance,out,indent=2)
    return result
