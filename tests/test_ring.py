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


# --- declick: underrun de-pop (the Qwen "click/pop on voice transition" fix) ---

def test_declick_off_by_default_still_hard_pads():
    # The plain ring is unchanged: an underrun still zero-pads with a hard edge.
    r = _Ring(1.0, rate=48000)
    r.push(np.full(200, 0.8, dtype=np.float32))
    out = r.pull(500)[:, 0]
    assert out[199] == np.float32(0.8) and out[200] == 0.0  # hard step, no ramp
    assert getattr(r, "underruns", 0) == 0


def test_declick_fades_out_into_underrun():
    r = _Ring(1.0, rate=48000, declick=True)  # ramp ~96 samples
    r.push(np.full(200, 0.8, dtype=np.float32))
    out = r.pull(500)[:, 0]
    # Without declick the drop from 0.8 to 0.0 is a single 0.8 step. The ramp
    # spreads it over ~2 ms, so no adjacent-sample jump approaches that.
    assert np.max(np.abs(np.diff(out))) < 0.05
    assert abs(out[-1]) < 1e-6            # still reaches true silence
    assert r.underruns == 1


def test_declick_fades_in_on_resume():
    r = _Ring(2.0, rate=48000, declick=True)
    r.push(np.full(100, 0.7, dtype=np.float32))
    first = r.pull(300)[:, 0]            # underruns -> ring is now "starved"
    assert r.underruns == 1
    r.push(np.full(300, 0.7, dtype=np.float32))
    second = r.pull(300)[:, 0]          # resume -> leading edge must ramp up
    seam = np.concatenate([first[-5:], second])
    assert np.max(np.abs(np.diff(seam))) < 0.05  # no hard silence->0.7 jump
    assert second[0] < np.float32(0.7)           # attenuated leading edge


def test_declick_noop_while_fully_fed():
    # A never-starved ring must return bit-identical audio with declick on/off.
    rate = 48000
    sig = np.sin(np.linspace(0, 30, 4800, dtype=np.float32))
    plain, dk = _Ring(2.0, rate=rate), _Ring(2.0, rate=rate, declick=True)
    plain.push(sig.copy())
    dk.push(sig.copy())
    np.testing.assert_array_equal(plain.pull(4800), dk.pull(4800))
    assert dk.underruns == 0
