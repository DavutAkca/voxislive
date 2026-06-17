"""Dubbing DSP: dynamic frequency carving plus logarithmic attenuation.

Applied to the original audio while the user's translation (TTS) is playing:
  * A peaking-cut bell in the 1–4 kHz range carves out the vocal-presence band,
    preserving bass and treble while opening a clean acoustic corridor for the
    translation.
  * The remaining broadband level is attenuated in dB.
Only applicable in VB-CABLE mode where the engine intercepts the audio stream.
"""
import numpy as np


def _lfilter(b, a, x, zi):
    """Direct Form II Transposed biquad IIR filter — scipy-free drop-in for
    scipy.signal.lfilter restricted to len(b)==len(a)==3 (biquad) filters.
    Carries state (zi) across blocks so block boundaries produce no clicks.
    Only called in VB-CABLE mode; pure Python loop is fast enough there."""
    b0, b1, b2 = float(b[0]), float(b[1]), float(b[2])
    a1, a2 = float(a[1]), float(a[2])
    z0, z1 = float(zi[0]), float(zi[1])
    y = np.empty(len(x), dtype=np.float64)
    for n in range(len(x)):
        xn = float(x[n])
        yn = b0 * xn + z0
        z0 = b1 * xn - a1 * yn + z1
        z1 = b2 * xn - a2 * yn
        y[n] = yn
    return y, np.array([z0, z1])

# Center, width and depth of the carve. Tuned to the consonant-energy region.
CARVE_FREQ_HZ = 2300.0
CARVE_Q = 1.1
CARVE_GAIN_DB = -14.0


def _peaking_coeffs(fs: float, f0: float, q: float, gain_db: float):
    """RBJ Audio EQ Cookbook peaking coefficients (normalized to a0)."""
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * f0 / fs
    alpha = np.sin(w0) / (2 * q)
    cw = np.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cw
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cw
    a2 = 1 - alpha / A
    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    return b, a


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20.0))


class DubbingDucker:
    """Produces a dubbing feel by carving and ducking the original audio while
    the translation is speaking.

    process(chunk, speaking) → processed chunk. `speaking` is a 0..1 soft
    factor (driven by VAD/UI): 0 = clean original, 1 = full carve + attenuation.
    """

    def __init__(self, fs: float, broadband_floor: float = 0.5):
        self._b, self._a = _peaking_coeffs(fs, CARVE_FREQ_HZ, CARVE_Q, CARVE_GAIN_DB)
        self._zi = np.zeros(max(len(self._a), len(self._b)) - 1, dtype=np.float32)
        self.broadband_floor = broadband_floor

    def process(self, chunk: np.ndarray, speaking: float) -> np.ndarray:
        chunk = np.asarray(chunk, dtype=np.float32)
        # A non-finite input sample would propagate through the IIR state and
        # poison every subsequent block; scrub it before filtering.
        if not np.isfinite(chunk).all():
            chunk = np.nan_to_num(chunk, nan=0.0, posinf=1.0, neginf=-1.0)
        # Carry filter state across blocks so the boundary does not click. Reset
        # the state if it ever goes non-finite so one bad sample cannot keep the
        # carve filter ringing forever.
        if not np.isfinite(self._zi).all():
            self._zi = np.zeros_like(self._zi)
        wet, self._zi = _lfilter(self._b, self._a, chunk, zi=self._zi)
        c = float(np.clip(speaking, 0.0, 1.0))
        carved = chunk + c * (wet.astype(np.float32) - chunk)
        broad = 1.0 + c * (self.broadband_floor - 1.0)
        out = (carved * broad).astype(np.float32)
        # Flush denormals — consistent with the limiter — so sustained near-zero
        # tails do not stall the FPU in the realtime callback.
        out[np.abs(out) < 1e-20] = 0.0
        return out
