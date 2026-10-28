"""Small, framework-independent helpers for Colab training."""
import hashlib
import os
import tempfile
from pathlib import Path

def precision(native_bf16):
    return 'bf16' if native_bf16 else 'fp16'

def groups(order, size):
    if size < 1:
        raise ValueError('Accumulation must be positive')
    for start in range(0, len(order), size):
        yield order[start:start + size]

def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = hashlib.sha256(payload).hexdigest().encode() + b'\n'
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name+'.tmp-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(header)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

def read_checked(path):
    raw = Path(path).read_bytes()
    header, separator, payload = raw.partition(b'\n')
    if not separator or header != hashlib.sha256(payload).hexdigest().encode():
        raise ValueError('Checkpoint checksum mismatch; do not resume this file')
    return payload
