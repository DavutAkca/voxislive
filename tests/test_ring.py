"""_Ring: the preallocated circular buffer feeding the realtime output callback."""
import numpy as np

from app.audio_io import _Ring


def test_fifo_roundtrip():
    r = _Ring(1.0, rate=100, channels=1)
    r.push(np.arange(10, dtype=np.float32))
    out = r.pull(10)
    assert out.shape == (10, 1)
    np.testing.assert_allclose(out[:, 0], np.arange(10, dtype=np.float32))


def test_underflow_zero_pads_tail():
    r = _Ring(1.0, rate=100)
    r.push(np.ones(4, dtype=np.float32))
    out = r.pull(8)
    np.testing.assert_allclose(out[:4, 0], 1.0)
    np.testing.assert_allclose(out[4:, 0], 0.0)


def test_wraparound_preserves_order():
    r = _Ring(0.1, rate=100)  # cap = 10 (+1 slot)
    for start in range(0, 40, 5):
        r.push(np.arange(start, start + 5, dtype=np.float32))
        out = r.pull(5)
        np.testing.assert_allclose(out[:, 0], np.arange(start, start + 5))


def test_overflow_drop_oldest_keeps_freshest():
    r = _Ring(0.1, rate=100)  # usable capacity 10
    r.push(np.arange(30, dtype=np.float32))
    assert r.overflows == 20
    out = r.pull(10)
    np.testing.assert_allclose(out[:, 0], np.arange(20, 30, dtype=np.float32))


def test_overflow_drop_newest_protects_queued_audio():
    r = _Ring(0.1, rate=100, drop_newest=True)
    r.push(np.arange(8, dtype=np.float32))
    r.push(np.arange(100, 110, dtype=np.float32))  # only 2 fit
    assert r.overflows == 8
    out = r.pull(10)
    np.testing.assert_allclose(out[:8, 0], np.arange(8, dtype=np.float32))
    np.testing.assert_allclose(out[8:, 0], [100.0, 101.0])


def test_mono_upmix_to_stereo():
    r = _Ring(1.0, rate=100, channels=2)
    r.push(np.arange(4, dtype=np.float32))  # mono in, stereo ring
    out = r.pull(4)
    assert out.shape == (4, 2)
    np.testing.assert_allclose(out[:, 0], out[:, 1])


def test_clear_resets_fill():
    r = _Ring(1.0, rate=100)
    r.push(np.ones(10, dtype=np.float32))
    assert r.fill == 10
    r.clear()
    assert r.fill == 0
    np.testing.assert_allclose(r.pull(4), 0.0)
