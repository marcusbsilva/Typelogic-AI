"""Guard train/inference parity of the trained adapter (MODEL-PARITY.json).

The trained LoRA and decision head only make sense with the exact encoder, contract and evaluation code
they were trained and evaluated with. Edit these files only together with a new training run.
"""
import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'MODEL-PARITY.json'


def _sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _manifest():
    if not MANIFEST.is_file():
        pytest.skip('MODEL-PARITY.json absent')
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def test_encoder_contract_and_protocol_files_unchanged():
    changed = [name for name, digest in _manifest()['code'].items() if _sha256(ROOT / name) != digest]
    assert not changed, 'Files that define train/inference parity changed: ' + ', '.join(changed)


def test_trained_adapter_files_unchanged_when_installed():
    present = {n: d for n, d in _manifest()['trained_adapter'].items() if (ROOT / n).is_file()}
    if not present:
        pytest.skip('Trained profile not installed in this copy')
    changed = [n for n, d in present.items() if _sha256(ROOT / n) != d]
    assert not changed, 'Trained adapter files changed: ' + ', '.join(changed)


def test_active_profile_is_the_trained_one():
    from typelogic.profiles import active_model_dir
    manifest = _manifest()
    if not (ROOT / manifest['active_profile']).is_dir():
        pytest.skip('Trained profile not installed in this copy')
    assert active_model_dir(ROOT).resolve() == (ROOT / manifest['active_profile']).resolve()
    assert manifest['run_adapter_equals_profile_adapter'] is True


def test_datasets_unchanged_when_present():
    present = {n: d for n, d in _manifest()['datasets'].items() if (ROOT / n).is_file()}
    if not present:
        pytest.skip('Datasets not installed')
    assert all(_sha256(ROOT / n) == d for n, d in present.items())
