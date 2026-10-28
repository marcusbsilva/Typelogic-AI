"""Deterministic contracts backed by the Rust core (``typelogic._core``) with the Python reference as fallback.

The Python modules (``schema``, ``exports``) remain the
reference implementation and the training code keeps importing them directly. Runtime code imports from
here: results are identical (``tests/test_core_parity.py``), the Rust path is faster, and any input outside
the core's exact domain (for example a lone surrogate) is handled by the reference.

``TYPELOGIC_CORE`` selects the backend: ``auto`` (default), ``python`` (never load the extension) or
``rust`` (fail at import when the extension is missing).
"""
import os

from . import schema as _schema
from . import exports as _exports

MODE = os.environ.get('TYPELOGIC_CORE', 'auto').strip().lower()
if MODE not in ('auto', 'python', 'rust'):
    raise RuntimeError('TYPELOGIC_CORE must be auto, python or rust')

_rust = None
_import_error = None
if MODE != 'python':
    try:
        from . import _core as _rust
    except ImportError as exc:  # extension not built for this platform/interpreter
        _import_error = exc
        if MODE == 'rust':
            raise RuntimeError('TYPELOGIC_CORE=rust but the Rust extension is not installed: ' + str(exc)) from exc

Unsupported = _rust.CoreUnsupported if _rust is not None else type('CoreUnsupported', (Exception,), {})


def backend():
    """Active backend and versions, for health metadata and diagnostics."""
    if _rust is None:
        return {'backend': 'python', 'rust_available': False, 'reason': 'disabled' if MODE == 'python' else str(_import_error)}
    core, unicode = _rust.version()
    return {'backend': 'rust', 'rust_available': True, 'core_version': core, 'unicode_version': unicode, 'fallback': 'python reference'}


def _dispatch(rust_fn, python_fn, *args):
    if _rust is not None:
        try:
            return rust_fn(*args)
        except Unsupported:
            pass
    return python_fn(*args)


def canonical(value):
    return _dispatch(lambda v: _rust.canonical(v), _schema.canonical, value)


def validate(payload):
    return _dispatch(lambda p: _rust.validate(p), _schema.validate, payload)


def answers(questions, rows, threshold, calibration):
    return _dispatch(lambda *a: _rust.answers(*a), _schema.answers, questions, rows, threshold, calibration)


def canonical_bytes(record):
    return _dispatch(lambda r: _rust.canonical_bytes(r), _exports.canonical_bytes, record)


def digests(record):
    return _dispatch(lambda r: _rust.digests(r), _exports.digests, record)


def verification(record):
    return _dispatch(lambda r: _rust.verification(r), _exports.verification, record)


def lite(record):
    return _dispatch(lambda r: _rust.lite(r), _exports.lite, record)


def export_filename(decision_id, format='full'):
    return _dispatch(lambda i, f: _rust.export_filename(i, f), _exports.export_filename, decision_id, format)


def route(request, profiles):
    """LAYA 2.0 router contract over locally installed profiles (Rust only; no Python reference)."""
    if _rust is None:
        raise RuntimeError('Model routing requires the Rust core extension')
    return _rust.route(request, profiles)
