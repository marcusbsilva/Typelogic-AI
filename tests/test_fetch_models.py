"""tools/fetch_models.py: the adapter zip is installed only when every file matches the profile manifest (no network)."""
import hashlib
import json
import zipfile

import pytest

from tools import fetch_models


def _profile(tmp_path, files):
    folder = tmp_path / 'models' / 'decision-test'
    folder.mkdir(parents=True)
    manifest = {f'adapter/{n}': {'bytes': len(d), 'sha256': hashlib.sha256(d).hexdigest()} for n, d in files.items()}
    (folder / 'manifest.json').write_text(json.dumps({'files': manifest}), encoding='utf-8')
    return folder


def _zip(path, files, prefix='adapter/'):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('MODEL_CARD.md', 'card')
        for n, d in files.items():
            z.writestr(prefix + n, d)
    return str(path)


def test_adapter_zip_verified_installed_and_idempotent(tmp_path, monkeypatch, capsys):
    files = {'head.pt': b'head', 'adapter_model.safetensors': b'weights', 'README.md': b'readme'}
    folder = _profile(tmp_path, files)
    monkeypatch.setattr(fetch_models, 'ROOT', tmp_path)
    fetch_models.adapter(folder, _zip(tmp_path / 'a.zip', files))
    assert {p.name for p in (folder / 'adapter').iterdir()} == set(files)
    assert not [p for p in folder.iterdir() if p.name.startswith('tmp')]      # staging folder removed
    fetch_models.adapter(folder, None)                                         # already installed: no zip needed
    assert 'already installed' in capsys.readouterr().out


def test_adapter_zip_with_a_wrong_or_missing_file_is_refused(tmp_path, monkeypatch):
    files = {'head.pt': b'head', 'README.md': b'readme'}
    folder = _profile(tmp_path, files)
    monkeypatch.setattr(fetch_models, 'ROOT', tmp_path)
    with pytest.raises(SystemExit, match='README.md'):
        fetch_models.adapter(folder, _zip(tmp_path / 'b.zip', {'head.pt': b'head'}))
    with pytest.raises(SystemExit, match='head.pt'):
        fetch_models.adapter(folder, _zip(tmp_path / 'c.zip', {'head.pt': b'other', 'README.md': b'readme'}))
    assert not (folder / 'adapter').exists()
    with pytest.raises(SystemExit, match='Unsafe'):
        fetch_models.adapter(folder, _zip(tmp_path / 'd.zip', files, prefix='../'))
