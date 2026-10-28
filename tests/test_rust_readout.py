"""Rust readout parity on the trained decision heads, and the torch-free weight reader's safety."""
import pickle
import zipfile
from pathlib import Path

import pytest

np = pytest.importorskip('numpy')

from typelogic import core, torch_weights

ROOT = Path(__file__).resolve().parents[1]
HEADS = sorted(ROOT.glob('models/*/adapter/head.pt')) + sorted(ROOT.glob('research/runs/*/adapter/head.pt'))
needs_rust = pytest.mark.skipif(core.backend()['backend'] != 'rust', reason='Rust extension not built')


@needs_rust
@pytest.mark.parametrize('path', HEADS[:3] or [None], ids=lambda p: str(p.relative_to(ROOT)) if p else 'none')
def test_rust_readout_matches_float64_reference_on_trained_head(path):
    if path is None:
        pytest.skip('No trained head installed')
    from typelogic import rust_readout
    qw, qb, kw, kb, dp, temperature = torch_weights.pointer_head(path)
    assert qw.shape == kw.shape == (dp, 2048) and qb.shape == kb.shape == (dp,)
    head = rust_readout.build(qw, qb, kw, kb, temperature)
    rng = np.random.default_rng(7913)
    for count in (2, 5, 17, 255):
        decision = rng.standard_normal(2048).astype(np.float32)
        options = rng.standard_normal((count, 2048)).astype(np.float32)
        q = qw.astype(np.float64) @ decision + qb
        exact = ((options.astype(np.float64) @ kw.T.astype(np.float64) + kb) @ q) / np.sqrt(dp) / temperature
        torch_like = ((options @ kw.T + kb) @ (qw @ decision + qb)) * np.float32(1 / np.sqrt(dp)) / np.float32(temperature)
        got = rust_readout.logits(head, decision, options)
        np.testing.assert_allclose(got, exact, rtol=0, atol=1e-9)
        np.testing.assert_allclose(got, torch_like, rtol=3e-5, atol=3e-5)
        probabilities = np.asarray(head.probabilities(decision.tobytes(), options.tobytes()))
        assert abs(probabilities.sum() - 1) < 1e-12 and probabilities.argmax() == got.argmax()


@needs_rust
def test_rust_readout_rejects_bad_shapes_and_weights():
    from typelogic import rust_readout
    w = np.zeros((4, 8), np.float32); b = np.zeros(4, np.float32)
    with pytest.raises(ValueError, match='shapes'):
        rust_readout.build(w, b, np.zeros((4, 9), np.float32), b)
    bad = w.copy(); bad[0, 0] = np.nan
    with pytest.raises(ValueError, match='non-finite'):
        rust_readout.build(bad, b, w, b)
    with pytest.raises(ValueError, match='temperature'):
        rust_readout.build(w, b, w, b, 0.0)
    head = rust_readout.build(w, b, w, b)
    with pytest.raises(ValueError, match='shapes'):
        rust_readout.logits(head, np.zeros(7, np.float32), np.zeros((2, 8), np.float32))


class _Evil:
    def __reduce__(self):
        import os
        return (os.system, ('echo pwned',))


def test_weight_reader_refuses_arbitrary_globals(tmp_path):
    path = tmp_path / 'evil.pt'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('archive/data.pkl', pickle.dumps({'head': _Evil()}))
    with pytest.raises(pickle.UnpicklingError, match='Refusing'):
        torch_weights.load(path)
