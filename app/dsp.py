"""Biquad filter primitives shared by the mix core.

  * _lfilter        — Direct Form II Transposed biquad IIR (scipy-free), carries
                      state across blocks so block boundaries produce no clicks.
  * _peaking_coeffs — RBJ Audio EQ Cookbook peaking-filter coefficients.

Consumed by app/mix_core.py (PsychoMixer's per-band carve / M-S widen). float32,
block-continuous state, numpy-only (sub-ms at the block sizes used).
"""
import numpy as np


def _lfilter(b, a, x, zi):
    """Direct Form II Transposed biquad IIR filter — scipy-free drop-in for
    scipy.signal.lfilter restricted to len(b)==len(a)==3 (biquad) filters.
    Carries state (zi) across blocks so block boundaries produce no clicks.
    Pure Python loop is fast enough at the block sizes used."""
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
