"""mix_core: brickwall limiter guarantees and DelayLine convergence."""
import numpy as np

from app.mix_core import DelayLine, LookaheadLimiter, place_center


def test_limiter_brickwall_on_hot_noise():
    fs = 24000
    lim = LookaheadLimiter(fs, ceiling=0.97)
    rng = np.random.default_rng(0)
    peak = 0.0
    for _ in range(30):
        y = lim.process(rng.standard_normal((1024, 2)).astype(np.float32) * 3.0)
        assert np.isfinite(y).all()
        peak = max(peak, float(np.max(np.abs(y))))
    assert peak <= 0.9701


def test_limiter_transparent_below_ceiling():
    fs = 24000
    lim = LookaheadLimiter(fs, ceiling=0.97)
    x = (np.sin(2 * np.pi * 440 * np.arange(4096) / fs) * 0.5).astype(np.float32)
    y = lim.process(x)
    # Latency = L samples; the delayed signal should pass essentially unchanged.
    L = lim.latency_samples
    np.testing.assert_allclose(y[L:], x[:-L], atol=1e-4)


def test_limiter_survives_nan_input():
    lim = LookaheadLimiter(24000)
    x = np.full((256, 2), np.nan, dtype=np.float32)
    y = lim.process(x)
    assert np.isfinite(y).all()
    # And recovers on the next clean block.
    y2 = lim.process(np.ones((256, 2), dtype=np.float32) * 0.5)
    assert np.isfinite(y2).all()


def test_place_center_equal_channels():
    m = np.arange(8, dtype=np.float32)
    st = place_center(m, gain=0.5)
    assert st.shape == (8, 2)
    np.testing.assert_allclose(st[:, 0], st[:, 1])
    np.testing.assert_allclose(st[:, 0], m * 0.5)


def test_delayline_zero_delay_identity():
    dl = DelayLine(1000, channels=1)
    x = np.arange(64, dtype=np.float32)[:, None]
    out = dl.process(x)
    np.testing.assert_allclose(out[:, 0], x[:, 0], atol=1e-5)


def test_delayline_integer_delay_shifts_signal():
    dl = DelayLine(1000, channels=1, max_slew=1e9, resync_threshold=1e9)
    dl.set_target(10.0)
    x = np.arange(1, 101, dtype=np.float32)[:, None]
    out = dl.process(x)
    # After a 10-sample delay, out[j] == x[j-10] for j >= 10.
    np.testing.assert_allclose(out[10:, 0], x[:-10, 0], atol=1e-4)
    np.testing.assert_allclose(out[:10, 0], 0.0, atol=1e-4)


def test_delayline_snap_is_finite_and_bounded():
    dl = DelayLine(48000, channels=2)
    dl.set_target(0.5 * 48000)  # 0.5 s → way past resync threshold → snap
    rng = np.random.default_rng(1)
    for _ in range(10):
        out = dl.process(rng.standard_normal((480, 2)).astype(np.float32))
        assert np.isfinite(out).all()
    assert abs(dl.current_delay - 0.5 * 48000) < 1.0


def test_delayline_max_delay_clamps_target():
    dl = DelayLine(1000, channels=1, max_delay=50.0)
    dl.set_target(500.0)
    dl.process(np.zeros((32, 1), dtype=np.float32))
    assert dl.current_delay <= 50.0
