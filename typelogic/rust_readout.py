"""Rust implementation of the decision-head readout, accepted only after numerical parity with the PyTorch head.

Mirrors ``onnx_head.convert``: the same three random probes (2, 5 and 17 alternatives) must agree with the
PyTorch head within 3e-5 before the Rust readout replaces it.
"""
import numpy as np

TOLERANCE = 3e-5


def build(qw, qb, kw, kb, temperature=1.0):
    from . import _core
    qw, qb, kw, kb = (np.ascontiguousarray(a, dtype='<f4') for a in (qw, qb, kw, kb))
    dp, d = qw.shape
    if kw.shape != (dp, d) or qb.shape != (dp,) or kb.shape != (dp,):
        raise ValueError('Pointer head tensor shapes do not match')
    return _core.PointerHead(d, dp, qw.tobytes(), qb.tobytes(), kw.tobytes(), kb.tobytes(), float(temperature))


def logits(head, decision, options):
    decision = np.ascontiguousarray(decision, dtype='<f4')
    options = np.ascontiguousarray(options, dtype='<f4')
    return np.asarray(head.logits(decision.tobytes(), options.tobytes()), dtype=np.float64)


def convert(head):
    """Replace a PyTorch ``PointerHead`` by the Rust readout after parity checks -> (module, runtime metadata)."""
    import torch
    rust = build(*(t.detach().cpu().float().numpy() for t in (head.q.weight, head.q.bias, head.k.weight, head.k.bias)), head.temperature)
    rng = np.random.default_rng(7913)
    largest = 0.
    for count in (2, 5, 17):
        decision = rng.standard_normal(head.q.in_features).astype(np.float32)
        items = rng.standard_normal((count, head.q.in_features)).astype(np.float32)
        with torch.no_grad():
            expected = head(torch.from_numpy(decision), torch.from_numpy(items)).numpy()
        actual = logits(rust, decision, items)
        np.testing.assert_allclose(actual, expected, rtol=TOLERANCE, atol=TOLERANCE)
        largest = max(largest, float(np.max(np.abs(actual - expected))))

    class Readout(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.temperature = head.temperature

        def forward(self, decision, items):
            value = logits(rust, decision.detach().cpu().float().numpy(), items.detach().cpu().float().numpy())
            return torch.from_numpy(value.astype(np.float32)).to(decision.device)

    from . import core
    return Readout(), {'runtime': 'typelogic-core ' + core.backend().get('core_version', '?'), 'providers': ['RustCPU'],
                       'max_abs_logit_difference': largest, 'telemetry_disabled': True}
