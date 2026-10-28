import types
import unittest

from typelogic.training_backend import resolve_training_backend


class FakeBackend:
    def __init__(self, *, available=True, memory=16 * 1024**3, name='fake-gpu', bf16=False):
        self.available = available
        self.properties = types.SimpleNamespace(total_memory=memory, name=name)
        self.bf16 = bf16

    def is_available(self):
        return self.available

    def get_device_properties(self, index):
        if index != 0:
            raise AssertionError('Only the first accelerator is expected')
        return self.properties

    def is_bf16_supported(self, **kwargs):
        return self.bf16


class TrainingBackendTests(unittest.TestCase):
    def fake_torch(self, **backends):
        return types.SimpleNamespace(**backends)

    def test_xpu_uses_fp16_and_accepts_reviewed_11_gib_floor(self):
        xpu = FakeBackend(memory=11_500_000_000, name='Intel Arc B580', bf16=True)
        resolved = resolve_training_backend(self.fake_torch(xpu=xpu), 'xpu', min_memory_gib=10.5)
        self.assertEqual(resolved['device'], 'xpu')
        self.assertEqual(resolved['name'], 'Intel Arc B580')
        self.assertEqual(resolved['precision'], 'fp16')

    def test_cuda_keeps_existing_bf16_selection(self):
        cuda = FakeBackend(memory=16 * 1024**3, name='Tesla T4', bf16=True)
        resolved = resolve_training_backend(self.fake_torch(cuda=cuda), 'cuda', min_memory_gib=12)
        self.assertEqual(resolved['precision'], 'bf16')

    def test_rejects_unavailable_backend(self):
        xpu = FakeBackend(available=False)
        with self.assertRaisesRegex(RuntimeError, 'not available'):
            resolve_training_backend(self.fake_torch(xpu=xpu), 'xpu', min_memory_gib=10.5)

    def test_rejects_memory_below_backend_floor(self):
        xpu = FakeBackend(memory=10 * 1024**3)
        with self.assertRaisesRegex(RuntimeError, 'requires at least'):
            resolve_training_backend(self.fake_torch(xpu=xpu), 'xpu', min_memory_gib=10.5)

    def test_rejects_unknown_backend(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported training device'):
            resolve_training_backend(self.fake_torch(), 'mps', min_memory_gib=1)


if __name__ == '__main__':
    unittest.main()
