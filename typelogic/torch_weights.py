"""Read tensors from a PyTorch ``torch.save`` zip without importing torch and without running arbitrary pickles.

Only plain tensor checkpoints are accepted (``torch.load(..., weights_only=True)`` equivalent): the unpickler
allows ``collections.OrderedDict``, ``torch._utils._rebuild_tensor_v2`` and float/int storage types, and
refuses every other global. Used to check the Rust readout against the trained head and by tools that run
where torch is unavailable.
"""
import io
import pickle
import zipfile

import numpy as np

_DTYPES = {'FloatStorage': np.float32, 'DoubleStorage': np.float64, 'HalfStorage': np.float16,
           'LongStorage': np.int64, 'IntStorage': np.int32, 'BFloat16Storage': None}


class _Storage:
    def __init__(self, name):
        self.name = name


def _rebuild(storage, offset, size, stride, *_):
    array, = storage
    count = int(np.prod(size)) if size else 1
    if not size:
        return array[offset].copy()
    flat = np.lib.stride_tricks.as_strided(array[offset:], shape=tuple(size), strides=tuple(s * array.itemsize for s in stride))
    if flat.size != count:
        raise ValueError('Inconsistent tensor geometry')
    return np.ascontiguousarray(flat)


class _Unpickler(pickle.Unpickler):
    def __init__(self, data, archive, prefix):
        super().__init__(data)
        self.archive, self.prefix = archive, prefix

    def find_class(self, module, name):
        if (module, name) == ('collections', 'OrderedDict'):
            import collections
            return collections.OrderedDict
        if (module, name) == ('torch._utils', '_rebuild_tensor_v2'):
            return _rebuild
        if module == 'torch' and name in _DTYPES:
            return _Storage(name)
        raise pickle.UnpicklingError(f'Refusing to load {module}.{name}')

    def persistent_load(self, pid):
        kind, storage, key, _location, numel = pid
        if kind != 'storage' or not isinstance(storage, _Storage):
            raise pickle.UnpicklingError('Unsupported persistent object')
        dtype = _DTYPES[storage.name]
        if dtype is None:
            raise pickle.UnpicklingError('bfloat16 storages are not supported by this reader')
        raw = self.archive.read(f'{self.prefix}/data/{key}')
        array = np.frombuffer(raw, dtype=np.dtype(dtype).newbyteorder('<'))
        if array.size != numel:
            raise ValueError('Storage size mismatch')
        return (array,)


def load(path):
    """Return the saved object with tensors as numpy arrays."""
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith('/data.pkl')]
        if len(names) != 1:
            raise ValueError('Not a torch zip checkpoint')
        prefix = names[0][:-len('/data.pkl')]
        return _Unpickler(io.BytesIO(archive.read(names[0])), archive, prefix).load()


def pointer_head(path):
    """Weights of a saved decision head: (q.weight, q.bias, k.weight, k.bias, head_dim, temperature)."""
    meta = load(path)
    head = meta['head']
    arrays = [np.asarray(head[k], dtype=np.float32) for k in ('q.weight', 'q.bias', 'k.weight', 'k.bias')]
    return (*arrays, int(meta.get('head_dim', arrays[0].shape[0])), float(meta.get('temperature', 1.0)))
