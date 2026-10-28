"""Installed model profiles and the active one. No network, no weight loading.

Resolution of the active model directory:
1. ``TYPELOGIC_MODEL_DIR`` (explicit path),
2. ``models/active.json`` -> ``{"profile": "<folder under models/>"}``,
3. ``models/decision-2b`` (``model_profile.default_model_dir``).
"""
import json
import os
import tempfile
from pathlib import Path

from .model_profile import BASE_MODEL, PROJECT_ROOT, WEIGHTS_NAME, default_model_dir

ACTIVE_FILE = 'active.json'


def models_root(root=PROJECT_ROOT):
    return Path(root) / 'models'


def _profile_name(name):
    if not isinstance(name, str) or not name or name != Path(name).name or name.startswith('.') or name in ('runs',):
        raise ValueError('Profile must be a folder name directly under models/')
    return name


def active_model_dir(root=PROJECT_ROOT):
    explicit = os.environ.get('TYPELOGIC_MODEL_DIR', '').strip()
    if explicit:
        return Path(explicit)
    active = models_root(root) / ACTIVE_FILE
    if active.is_file():
        name = _profile_name(json.loads(active.read_text(encoding='utf-8')).get('profile'))
        return models_root(root) / name
    return default_model_dir(root)


def describe(path):
    """Metadata of one profile folder from small JSON files only."""
    path = Path(path)
    info = {'name': path.name, 'path': str(path), 'trained': False, 'backbone_weights_present': (path / 'backbone' / WEIGHTS_NAME).is_file(),
            'adapter_present': (path / 'adapter/head.pt').is_file(), 'manifest_present': (path / 'manifest.json').is_file(),
            'calibration_present': (path / 'calibration.json').is_file()}
    checkpoint = path / 'checkpoint.json'
    if checkpoint.is_file():
        try:
            meta = json.loads(checkpoint.read_text(encoding='utf-8'))
        except ValueError:
            info['error'] = 'checkpoint.json is not valid JSON'
            return info
        history = meta.get('history') or []
        best = next((h for h in history if h.get('epoch') == meta.get('best_epoch')), None)
        info.update(trained=bool(meta.get('trained')), base_model=meta.get('base_model'), base_revision=meta.get('base_revision'),
                    training_steps=meta.get('training_steps'), best_epoch=meta.get('best_epoch'), device=meta.get('device'),
                    accelerator=meta.get('accelerator_name'), datasets=meta.get('datasets'),
                    validation=(best or {}).get('validation'), final_test_evaluated=bool(meta.get('final_test_evaluated')),
                    calibration_fitted=bool(meta.get('calibration_fitted')), deployment_eligible=bool(meta.get('deployment_eligible')))
    info['ready'] = info['trained'] and info['adapter_present'] and info['backbone_weights_present'] and info['manifest_present']
    return info


def installed(root=PROJECT_ROOT):
    """Every folder under models/ that holds a trained adapter, active one flagged."""
    active = active_model_dir(root).resolve()
    out = []
    for path in sorted(models_root(root).iterdir() if models_root(root).is_dir() else []):
        if path.is_dir() and path.name != 'runs' and (path / 'adapter/head.pt').is_file():
            info = describe(path)
            info['active'] = path.resolve() == active
            out.append(info)
    return out


def select(name, root=PROJECT_ROOT):
    """Make a profile active by writing models/active.json atomically. Weights are not touched."""
    name = _profile_name(name)
    path = models_root(root) / name
    info = describe(path)
    if not info['adapter_present'] or not info['trained']:
        raise ValueError(f'{name} is not a trained TypeLogic profile (needs checkpoint.json and adapter/head.pt)')
    if info.get('base_model') != BASE_MODEL:
        raise ValueError(f'{name} targets {info.get("base_model")}, not {BASE_MODEL}')
    payload = json.dumps({'profile': name}, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(dir=models_root(root), prefix='.active-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(payload)
        os.replace(temporary, models_root(root) / ACTIVE_FILE)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return describe(path)


def router_profiles(root=PROJECT_ROOT, specialist=None):
    """Profiles in the router contract (Rust ``route``): installed decision models plus an optional specialist."""
    rows = []
    for info in installed(root):
        rows.append({'name': info['name'], 'tasks': ['decision', 'classification', 'routing'], 'languages': ['en'],
                     'latency': 'medium', 'available': bool(info['ready']), 'priority': 10 if info['active'] else 1})
    if specialist is not None:
        rows.append({'name': 'local-specialist', 'tasks': ['classification'], 'languages': ['*'], 'latency': 'low',
                     'available': (Path(specialist) / 'profile.json').is_file(), 'priority': 5})
    return rows
