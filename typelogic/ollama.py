"""Local Ollama server helpers: probe, start (never download) and unload models. Loopback only."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_URL = 'http://127.0.0.1:11434'


def url():
    """Ollama address; only this computer is accepted (TYPELOGIC_OLLAMA_URL may change the port)."""
    from urllib.parse import urlparse
    value = (os.environ.get('TYPELOGIC_OLLAMA_URL') or DEFAULT_URL).rstrip('/')
    if (urlparse(value).hostname or '').lower() not in ('127.0.0.1', 'localhost', '::1'):
        raise PermissionError('TYPELOGIC_OLLAMA_URL must point to this computer (127.0.0.1)')
    return value


def _get(path, timeout=2):
    with urllib.request.urlopen(url() + path, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def tags():
    """Pulled model names, or None when the server does not answer."""
    try:
        data = _get('/api/tags')
    except (OSError, ValueError):
        return None
    return {m.get('name') for m in data.get('models', [])} | {m.get('model') for m in data.get('models', [])}


def has_model(names, model):
    return bool(names) and (model in names or f'{model}:latest' in names)


def executable():
    found = shutil.which('ollama')
    if found:
        return found
    candidates = []
    if sys.platform == 'win32':
        candidates.append(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/Ollama/ollama.exe')
    else:
        candidates += [Path('/usr/local/bin/ollama'), Path('/usr/bin/ollama'), Path('/Applications/Ollama.app/Contents/Resources/ollama')]
    return next((str(p) for p in candidates if p.is_file()), None)


def _models_folder(model, extra=None):
    """Folder that holds the manifest of `model` (settings, OLLAMA_MODELS or the default ~/.ollama/models)."""
    name, _, tag = model.partition(':')
    if '/' not in name:
        name = f'library/{name}'
    relative = Path('manifests/registry.ollama.ai', *name.split('/'), tag or 'latest')
    for folder in (extra, os.environ.get('OLLAMA_MODELS'), str(Path.home() / '.ollama/models')):
        if folder and (Path(folder) / relative).is_file():
            return folder
    return None


def ensure_running(model=None, wait=40, models_dir=None):
    """Start `ollama serve` on the loopback address when nothing answers. Returns a short status string."""
    if tags() is not None:
        return 'running'
    exe = executable()
    if not exe:
        return 'not installed'
    env = dict(os.environ, OLLAMA_HOST='127.0.0.1:11434')
    folder = (_models_folder(model, models_dir) if model else None) or models_dir
    if folder:
        env['OLLAMA_MODELS'] = folder
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    subprocess.Popen([exe, 'serve'], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, start_new_session=sys.platform != 'win32')
    for _ in range(wait * 2):
        time.sleep(0.5)
        if tags() is not None:
            return 'started'
    return 'did not start'


def unload_all():
    """Ask the server to release every loaded model (frees GPU memory). Best effort."""
    try:
        loaded = _get('/api/ps').get('models', [])
        for m in loaded:
            body = json.dumps({'model': m.get('name') or m.get('model'), 'keep_alive': 0}).encode()
            request = urllib.request.Request(url() + '/api/generate', data=body, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(request, timeout=10).read()
        return len(loaded)
    except (OSError, ValueError):
        return 0
