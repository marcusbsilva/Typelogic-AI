"""Engine modes, chosen in the web app (Engine & privacy), with --mode, or in the launcher.

  cpu     the trained specialist (Qwen3.5-2B + adapter) on the processor. Default.
  gpu     the specialist on an Intel Arc GPU (PyTorch XPU environment), float16.
  hybrid  a generalist through the local Ollama (Plumb-4B by default) for questions with up to 16 options, and the
          specialist for larger catalogs (on the CPU by default, or on the GPU). Ollama uses a GPU by itself when it can.

Settings live in <data folder>/settings.json (shared by the web app, the CLI and the launcher):
  mode, hybrid_device (cpu | gpu), hybrid_combine (route | ensemble), ollama_model, ollama_models (Ollama model folder),
  gpu_python (interpreter of the GPU environment, used by the launcher),
  extract_backend (auto | ollama | needle) and extract_model (Ollama model used to extract evidence, see typelogic/evidence.py).
Mode precedence: --mode, then TYPELOGIC_MODE, then the legacy TYPELOGIC_ENGINE=hybrid / TYPELOGIC_DEVICE=xpu, then the
stored choice, then cpu.
"""
import json
import os
from pathlib import Path

MODES = ('cpu', 'gpu', 'hybrid')
LABELS = {'cpu': 'CPU', 'gpu': 'GPU', 'hybrid': 'Hybrid'}
SETTINGS_FILE = 'settings.json'
DEFAULT_OLLAMA_MODEL = 'crh225/plumb-4b'
DEFAULTS = {'mode': 'cpu', 'hybrid_device': 'cpu', 'hybrid_combine': 'route', 'ollama_model': DEFAULT_OLLAMA_MODEL,
            'ollama_models': None, 'gpu_python': None, 'extract_backend': 'auto',
            'extract_model': 'qwen3.5:4b'}
ROOT = Path(__file__).resolve().parents[1]
RESTART_EXIT_CODE = 75   # the server asks the launcher to restart it (another Python environment is needed)
_gpu_cache = None


def normalize(mode):
    value = str(mode or '').strip().lower()
    if value not in MODES:
        raise ValueError('mode must be cpu, gpu or hybrid')
    return value


def load_settings(data_dir):
    settings = dict(DEFAULTS)
    try:
        stored = json.loads((Path(data_dir) / SETTINGS_FILE).read_text(encoding='utf-8'))
        if isinstance(stored, dict):
            settings.update({k: v for k, v in stored.items() if k in DEFAULTS or k in ('port', 'open_browser')})
    except (OSError, ValueError, TypeError):
        pass
    return validate_settings(settings, strict=False)


def validate_settings(settings, strict=True):
    """Checked copy; with strict=False invalid stored values fall back to the defaults."""
    out = dict(settings)
    checks = {'mode': MODES, 'hybrid_device': ('cpu', 'gpu'), 'hybrid_combine': ('route', 'ensemble'),
              'extract_backend': ('auto', 'ollama', 'needle')}
    for key, allowed in checks.items():
        value = str(out.get(key, DEFAULTS[key])).strip().lower()
        if value not in allowed:
            if strict:
                raise ValueError(f'{key} must be one of: {", ".join(allowed)}')
            value = DEFAULTS[key]
        out[key] = value
    model = str(out.get('ollama_model') or DEFAULT_OLLAMA_MODEL).strip()
    if strict and (len(model) > 200 or any(c.isspace() for c in model)):
        raise ValueError('ollama_model must be an Ollama model tag such as crh225/plumb-4b')
    out['ollama_model'] = model
    extract_model = str(out.get('extract_model') or DEFAULTS['extract_model']).strip()
    if strict and (len(extract_model) > 200 or any(c.isspace() for c in extract_model)):
        raise ValueError('extract_model must be an Ollama model tag such as qwen3.5:4b')
    out['extract_model'] = extract_model
    for key in ('ollama_models', 'gpu_python'):
        out[key] = str(out[key]) if out.get(key) else None
    return out


def save_settings(data_dir, changes):
    """Merge `changes` into settings.json (atomic write) and return the full settings."""
    path = Path(data_dir) / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
        stored = stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        stored = {}
    merged = validate_settings({**DEFAULTS, **stored, **changes})
    stored.update({k: merged[k] for k in changes})
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(stored, indent=2), encoding='utf-8')
    os.replace(tmp, path)
    return merged


def resolve(explicit=None, data_dir=None):
    if explicit:
        return normalize(explicit)
    if os.environ.get('TYPELOGIC_MODE', '').strip():
        return normalize(os.environ['TYPELOGIC_MODE'])
    if os.environ.get('TYPELOGIC_ENGINE', '').strip().lower() == 'hybrid':
        return 'hybrid'
    if os.environ.get('TYPELOGIC_DEVICE', '').strip().lower() in ('xpu', 'gpu'):
        return 'gpu'
    return load_settings(data_dir)['mode'] if data_dir else 'cpu'


def needs_gpu(mode, settings):
    return mode == 'gpu' or (mode == 'hybrid' and settings.get('hybrid_device') == 'gpu')


def gpu_info(refresh=False):
    """{'available', 'backend', 'name', 'reason'} for the GPU path of this Python environment (cached)."""
    global _gpu_cache
    if _gpu_cache is not None and not refresh:
        return _gpu_cache
    try:
        import torch
    except ImportError:
        _gpu_cache = {'available': False, 'backend': None, 'name': None, 'reason': 'PyTorch is not installed'}
        return _gpu_cache
    info = {'available': False, 'backend': None, 'name': None, 'torch': torch.__version__,
            'reason': 'This Python environment has no GPU build of PyTorch (run: TypeLogic.bat setup --gpu)'}
    try:
        if getattr(torch, 'xpu', None) and torch.xpu.is_available():
            info.update(available=True, backend='xpu', name=torch.xpu.get_device_name(0), reason=None)
        elif '+xpu' in torch.__version__:
            info['reason'] = 'PyTorch XPU build found but no Intel GPU is usable (check the Intel Arc driver)'
        elif torch.cuda.is_available():
            info['reason'] = 'NVIDIA GPUs are not supported by the specialist yet; use CPU or Hybrid (Ollama uses the GPU)'
    except Exception as exc:  # noqa: BLE001 - a broken driver must not break the status page
        info['reason'] = f'GPU check failed: {exc}'[:200]
    _gpu_cache = info
    return info


def generalist_dir(root=ROOT):
    folder = os.environ.get('TYPELOGIC_GENERALIST', '').strip() or 'models/qwen35-4b'
    path = Path(folder)
    return path if path.is_absolute() else Path(root) / path


def hybrid_info(settings, root=ROOT):
    from . import ollama
    model = settings['ollama_model']
    tokenizer = (generalist_dir(root) / 'tokenizer.json').is_file()
    names = ollama.tags()
    info = {'ollama_model': model, 'tokenizer': tokenizer, 'ollama_running': names is not None,
            'ollama_installed': names is not None or ollama.executable() is not None,
            'model_pulled': ollama.has_model(names, model) if names is not None else None,
            'models': sorted(n for n in (names or ()) if n)}
    if not tokenizer:
        reason = 'Generalist tokenizer missing (run: TypeLogic.bat models --only tokenizer)'
    elif not info['ollama_installed']:
        reason = 'Ollama is not installed (https://ollama.com)'
    elif names is not None and not info['model_pulled']:
        reason = f'Model not in Ollama (run: TypeLogic.bat models --only plumb, or ollama pull {model})'
    else:
        reason = None
    info.update(available=reason is None, reason=reason)
    return info


def availability(settings, root=ROOT):
    """What each mode needs and whether this computer and this Python environment have it."""
    gpu = dict(gpu_info())
    restart = os.environ.get('TYPELOGIC_GPU_RESTART', '') == '1'   # set by the launcher when a GPU environment exists
    gpu['via_restart'] = not gpu['available'] and restart
    gpu_ok = gpu['available'] or gpu['via_restart']
    hybrid = hybrid_info(settings, root)
    return {'cpu': {'available': True, 'detail': 'Specialist on the processor (float32, about 9 GB of RAM)'},
            'gpu': {**gpu, 'available': gpu_ok,
                    'detail': (f'Specialist on {gpu["name"]} (float16)' if gpu['available'] else
                               'The launcher restarts the app in the GPU environment' if gpu['via_restart'] else gpu['reason'])},
            'hybrid': {**hybrid, 'gpu_device': gpu_ok,
                       'detail': f'Generalist {hybrid["ollama_model"]} through Ollama + specialist for catalogs' if hybrid['available'] else hybrid['reason']}}


def _cpu_readout(use_onnx, readout):
    """ONNX readout when ONNX Runtime is installed; otherwise the PyTorch readout (e.g. a GPU environment without onnx)."""
    if readout or not use_onnx or os.environ.get('TYPELOGIC_READOUT', '').strip():
        return use_onnx, readout
    import importlib.util
    if all(importlib.util.find_spec(m) for m in ('onnx', 'onnxruntime')):
        return use_onnx, readout
    return False, 'torch'


def build(mode, model_dir, settings=None, use_onnx=True, readout=None, root=ROOT):
    """Engine for `mode`; nothing is loaded yet (the caller calls .load())."""
    from .engine import DecisionEngine
    mode = normalize(mode)
    settings = validate_settings({**DEFAULTS, **(settings or {})}, strict=False)
    combine = os.environ.get('TYPELOGIC_HYBRID_MODE', '').strip().lower() or settings['hybrid_combine']
    model = os.environ.get('TYPELOGIC_OLLAMA_MODEL', '').strip() or settings['ollama_model']
    if mode == 'gpu':
        return DecisionEngine(model_dir, False, readout='torch', device='xpu')
    use_onnx, readout = _cpu_readout(use_onnx, readout)
    if mode == 'cpu':
        return DecisionEngine(model_dir, use_onnx, readout=readout, device='cpu')
    from .direct import DirectEngine
    from .hybrid import HybridEngine
    if settings['hybrid_device'] == 'gpu':
        specialist = DecisionEngine(model_dir, False, readout='torch', device='xpu')
    else:   # bfloat16: about 4.5 GB, so the specialist shares 16 GB of RAM with the generalist
        precision = os.environ.get('TYPELOGIC_CPU_PRECISION', '').strip() or 'bfloat16'
        specialist = DecisionEngine(model_dir, use_onnx, readout=readout, device='cpu', cpu_precision=precision)
    generalist = DirectEngine(generalist_dir(root), device=os.environ.get('TYPELOGIC_GENERALIST_DEVICE') or None,
                              dtype=os.environ.get('TYPELOGIC_GENERALIST_DTYPE') or None, ollama_model=model)
    return HybridEngine(specialist, generalist, mode=combine)
