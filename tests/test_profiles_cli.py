"""Active model profile selection, CLI profiles/route commands and the calibration fingerprint helper."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from typelogic import profiles
from typelogic.engine import DecisionEngine, checkpoint_fingerprint, default_readout

ROOT = Path(__file__).resolve().parents[1]


def make_profile(models, name, base='Qwen/Qwen3.5-2B', trained=True):
    folder = models / name
    (folder / 'adapter').mkdir(parents=True)
    (folder / 'backbone').mkdir()
    (folder / 'adapter/head.pt').write_bytes(b'head')
    (folder / 'backbone/config.json').write_text('{}')
    (folder / 'checkpoint.json').write_text(json.dumps({'base_model': base, 'trained': trained, 'best_epoch': 1,
                                                         'history': [{'epoch': 1, 'validation': {'accuracy': 0.5}}]}))
    return folder


def test_active_profile_resolution_and_selection(tmp_path, monkeypatch):
    monkeypatch.delenv('TYPELOGIC_MODEL_DIR', raising=False)
    models = tmp_path / 'models'
    make_profile(models, 'decision-2b'); make_profile(models, 'trained-v2'); make_profile(models, 'other-base', base='x/y')
    assert profiles.active_model_dir(tmp_path) == models / 'decision-2b'
    profiles.select('trained-v2', tmp_path)
    assert json.loads((models / 'active.json').read_text()) == {'profile': 'trained-v2'}
    assert profiles.active_model_dir(tmp_path) == models / 'trained-v2'
    listed = {p['name']: p for p in profiles.installed(tmp_path)}
    assert listed['trained-v2']['active'] and not listed['decision-2b']['active']
    assert listed['trained-v2']['validation'] == {'accuracy': 0.5} and listed['trained-v2']['ready'] is False
    for bad in ['../x', 'runs', '', 'a/b']:
        with pytest.raises(ValueError):
            profiles.select(bad, tmp_path)
    with pytest.raises(ValueError, match='targets'):
        profiles.select('other-base', tmp_path)
    monkeypatch.setenv('TYPELOGIC_MODEL_DIR', str(tmp_path / 'elsewhere'))
    assert profiles.active_model_dir(tmp_path) == tmp_path / 'elsewhere'


def test_engine_reports_profile_metadata_without_loading(tmp_path, monkeypatch):
    monkeypatch.delenv('TYPELOGIC_READOUT', raising=False)
    folder = make_profile(tmp_path, 'trained-v2')
    engine = DecisionEngine(folder)
    health = engine.health()
    assert health['base_model'] == 'Qwen/Qwen3.5-2B' and health['readout'] == 'onnx' and health['status'] == 'not_loaded'
    assert health['model_dir'] == str(folder.resolve()) and health['core']['backend'] in ('rust', 'python')
    assert default_readout(False) == 'torch'
    monkeypatch.setenv('TYPELOGIC_READOUT', 'rust')
    assert DecisionEngine(folder).readout == 'rust' and DecisionEngine(folder, use_onnx=False).readout == 'torch'
    monkeypatch.setenv('TYPELOGIC_READOUT', 'gpu')
    with pytest.raises(ValueError):
        DecisionEngine(folder)


def test_checkpoint_fingerprint_tracks_weights(tmp_path):
    folder = make_profile(tmp_path, 'p')
    (folder / 'adapter/README.md').write_text('ignored')
    files, first = checkpoint_fingerprint(folder)
    assert set(files) == {'adapter/head.pt', 'backbone/config.json', 'checkpoint.json'}
    (folder / 'adapter/README.md').write_text('still ignored')
    assert checkpoint_fingerprint(folder)[1] == first
    (folder / 'adapter/head.pt').write_bytes(b'changed')
    assert checkpoint_fingerprint(folder)[1] != first


def cli(*args, input=None):
    return subprocess.run([sys.executable, '-I', str(ROOT / 'tools/run_app.py'), *args], input=input, text=True, encoding='utf-8', capture_output=True)


def test_cli_profiles_and_route_commands():
    listed = cli('profiles')
    assert listed.returncode == 0, listed.stderr
    value = json.loads(listed.stdout)
    assert any(p['active'] for p in value['profiles']) or not value['profiles']
    routed = cli('route', '--input', '-', input=json.dumps({'task': 'decision', 'language': 'en', 'latency': 'high', 'text': 'Refund my order'}))
    from typelogic.core import backend
    if backend()['backend'] != 'rust':
        assert routed.returncode == 1 and 'requires the Rust core' in routed.stderr
        return
    assert routed.returncode == 0, routed.stderr
    route = json.loads(routed.stdout)
    assert route['script'] == 'Latn' and {s['model'] for s in route['skipped']} | set(route['candidates']) >= {p['name'] for p in value['profiles']}
    bad = cli('route', '--input', '-', input='{"language": "en"}')
    assert bad.returncode == 1 and 'task' in bad.stderr
