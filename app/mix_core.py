"""Voxis DSP / mix core — psychoacoustic processing of the translation (TTS)
output chain.

Scope: in driverless session-ducking mode Voxis owns only its own TTS buffer
(the original ambient audio is reduced at the source by Windows and plays
directly), so ambient EQ / delay cannot be applied there. The components below:

  * LookaheadLimiter — look-ahead brickwall limiter on the Gemini TTS output.
                       Prevents 0 dBFS overshoot, which eliminates digital
                       clipping and TTS time-stretch distortion.
  * place_center     — places mono TTS in a phantom-center (equal L/R) image.
  * DelayLine        — fractional-read circular delay line. Wired but dormant;
                       activated only on the passthrough (VB-CABLE) path when
                       ambient audio is available.
  * PsychoMixer      — composes the above. ambient=None → TTS-only with limiter;
                       ambient supplied → full mixer (carve + M/S widen +
                       delay + sum + limit).

Aligned with app/dsp.py: float32, block-continuous state (no boundary clicks),
RBJ coefficients. numpy is already sub-ms at this block size — no native code.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

# Reuses the peaking-cut coefficients and biquad filter from app/dsp.py.
from .dsp import _lfilter, _peaking_coeffs  # noqa: F401


def _as_stereo(x: np.ndarray) -> np.ndarray:
    """Normalizes mono (n,) or stereo (n,2) input to float32 (n,2)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = np.repeat(x[:, None], 2, axis=1)
    return x


def place_center(mono: np.ndarray, gain: float = 1.0) -> np.ndarray:
    """Mono TTS → phantom center (equal energy on both channels)."""
    m = np.asarray(mono, dtype=np.float32).reshape(-1)
    out = np.empty((m.shape[0], 2), dtype=np.float32)
    out[:, 0] = m * gain
    out[:, 1] = m * gain
    return out


class LookaheadLimiter:
    """Look-ahead peak limiter with a true brickwall guarantee.

    For each sample the required gain gr = min(1, ceil/|x|) is computed. A
    rolling minimum over an L-sample window provides the look-ahead so the gain
    can drop before the peak arrives. The signal is delayed by L samples and
    multiplied by the resulting gain envelope. Attack is instantaneous (the
    min-filter pre-dip handles this); release is a slow one-pole.

    Block-continuous: the delay line, required-gain tail and gain state all
    carry across blocks, so block boundaries produce no discontinuities.
    """

    def __init__(self, fs: float, lookahead_ms: float = 1.5,
                 ceiling: float = 0.97, release_ms: float = 80.0):
        self.fs = float(fs)
        self.L = max(1, int(round(fs * lookahead_ms / 1000.0)))
        self.ceil = float(ceiling)
        self.rel = float(np.exp(-1.0 / max(1.0, fs * release_ms / 1000.0)))
        self._delay = None
        self._grtail = np.ones(self.L, dtype=np.float32)
        self._g = 1.0

    @property
    def latency_samples(self) -> int:
        return self.L

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        mono_in = x.ndim == 1
        if mono_in:
            x = x[:, None]
        n, ch = x.shape
        if self._delay is None or self._delay.shape[1] != ch:
            self._delay = np.zeros((self.L, ch), dtype=np.float32)
        if n == 0:
            return x.reshape(-1) if mono_in else x

        # A single corrupt TTS sample must not permanently poison the envelope:
        # reset the carried gain to unity if it ever leaves the finite range.
        if not np.isfinite(self._g):
            self._g = 1.0

        # 1) Required per-sample gain (<= 1); peak across channels. nan_to_num
        # keeps a non-finite input sample from driving gr to nan/inf.
        peak = np.max(np.abs(x), axis=1) + 1e-9
        peak = np.nan_to_num(peak, nan=1.0, posinf=np.float32(3.0e38), neginf=1e-9)
        gr = np.minimum(1.0, self.ceil / peak).astype(np.float32)

        # 2) Look-ahead: rolling minimum over the next L samples.
        grext = np.concatenate([self._grtail, gr])
        win = self.L + 1
        la = sliding_window_view(grext, win).min(axis=1).astype(np.float32)
        self._grtail = grext[-self.L:]

        # 3) Instantaneous attack, slow one-pole release — vectorized, no
        # per-sample Python loop in the RT path.
        out_g = self._release_envelope(la)
        self._g = float(out_g[-1])

        # 4) Delay the signal by L samples and apply the gain envelope.
        xext = np.concatenate([self._delay, x], axis=0)
        delayed = xext[:n]
        self._delay = xext[n:]
        y = (delayed * out_g[:, None]).astype(np.float32)
        # Flush denormals and clamp any residual non-finite product so a poisoned
        # input cannot escape downstream; the brickwall ceiling already bounds y.
        y = np.nan_to_num(y, nan=0.0, posinf=self.ceil, neginf=-self.ceil)
        y[np.abs(y) < 1e-20] = 0.0
        return y.reshape(-1) if mono_in else y

    # Power-decay scan stays exact while rel**-i is well below float64 range;
    # capping the chunk length keeps any block size numerically safe.
    _ENV_CHUNK = 4096

    def _release_envelope(self, la: np.ndarray) -> np.ndarray:
        """Vectorized attack-min / one-pole-release gain envelope.

        Reproduces the former per-sample recursion exactly:
            g[i] = min(la[i], rel*g[i-1] + (1-rel)*la[i])
        i.e. instantaneous attack (la pulls the gain straight down) plus a slow
        one-pole release back up toward la. The min form is the nonlinearity
        that lfilter alone cannot express; instead the gap d = g - la (always
        <= 0, reset to 0 on each attack) follows a clamped linear recursion
            d[i] = min(0, rel*d[i-1] + rel*(la[i-1] - la[i])).
        Dividing by rel**i turns it into a reset-to-zero prefix sum whose closed
        form is the cumulative-maximum ("max drawdown") of the rescaled input,
        so the whole envelope is a handful of numpy reductions — no per-sample
        or per-segment Python loop in the RT path. The carried _g seeds the
        first sample, keeping the envelope block-continuous.
        """
        la = la.astype(np.float64)
        n = la.shape[0]
        if n <= self._ENV_CHUNK:
            return self._env_scan(la)
        # Long block: process in capped chunks so rel**-i cannot overflow,
        # carrying _g across so the result is identical to one big scan.
        out = np.empty(n, dtype=np.float32)
        g_save = self._g
        for s in range(0, n, self._ENV_CHUNK):
            seg = self._env_scan(la[s:s + self._ENV_CHUNK])
            out[s:s + seg.shape[0]] = seg
            self._g = float(seg[-1])
        self._g = g_save  # process() owns the final state update
        return out

    def _env_scan(self, la: np.ndarray) -> np.ndarray:
        rel = self.rel
        n = la.shape[0]
        i = np.arange(n)
        # v[i] = rel*(la[i-1]-la[i]); the i=0 feed term carries the prior gain
        # via la[-1] := _g so blocks join without a discontinuity.
        v = np.empty(n, dtype=np.float64)
        v[0] = rel * (self._g - la[0])
        if n > 1:
            v[1:] = rel * (la[:-1] - la[1:])
        # q[i] = min(0, q[i-1] + v[i]*rel**-i); reset-to-zero scan == drawdown.
        w = v * np.power(rel, -i)
        c = np.cumsum(w)
        cm = np.maximum(np.maximum.accumulate(c), 0.0)
        d = (c - cm) * np.power(rel, i)
        return (la + d).astype(np.float32)


class DelayLine:
    """Fractional-read circular delay line for ambient ↔ TTS RTT synchronization.

    On the VB-CABLE / passthrough path the stereo ambient (and its M/S
    center-suppression control envelope) are delayed by the measured cloud RTT
    so they line up with the translated audio.

    Two-stage convergence (required — otherwise the line never catches up):
      * |target − delay| > resync threshold → snap immediately (initial lock or
        large jitter); a 1.5 s delay would otherwise take ~16 min to slew in.
        An equal-power cross-fade over the snap hides the pointer jump that would
        otherwise click.
      * smaller error → ≤ max_slew (default 1 sample) per block.
    Delay is constant within a block (no pitch artifacts); boundary steps are
    smoothed by fractional reads. Fully vectorized, audio-callback safe.
    """

    def __init__(self, fs: float, max_seconds: float = 3.5, channels: int = 2,
                 max_slew: float = 1.0, resync_threshold: float | None = None,
                 max_delay: float | None = None, xfade_ms: float = 4.0):
        cap = 1
        need = int(fs * max_seconds) + 8
        while cap < need:
            cap <<= 1
        self.fs = float(fs)
        self.cap = cap
        self.mask = cap - 1
        self.ch = channels
        self.buf = np.zeros((cap, channels), dtype=np.float32)
        self.w = 0
        self.delay = 0.0
        self._target = 0.0
        self.max_slew = float(max_slew)
        self.resync = float(resync_threshold if resync_threshold is not None else 0.15 * fs)
        # Hard ceiling — None means unbounded (up to buffer capacity).
        self.max_delay = None if max_delay is None else float(max_delay)
        # Equal-power cross-fade length applied on a resync snap so the read
        # pointer's discontinuity does not click.
        self._xfade = max(1, int(round(fs * xfade_ms / 1000.0)))
        self._snapped_from: float | None = None

    def set_target(self, delay_samples: float) -> None:
        ceil = self.cap - 2 if self.max_delay is None else min(self.cap - 2, self.max_delay)
        self._target = float(max(0.0, min(ceil, delay_samples)))

    @property
    def current_delay(self) -> float:
        return self.delay

    def _step_delay(self) -> None:
        err = self._target - self.delay
        if abs(err) > self.resync:
            # Remember the pre-snap delay so process() can cross-fade the seam.
            self._snapped_from = self.delay
            self.delay = self._target
        elif abs(err) <= self.max_slew:
            self.delay = self._target
        else:
            self.delay += self.max_slew if err > 0 else -self.max_slew

    def _read(self, frontier_w: int, delay: float, n: int) -> np.ndarray:
        """Fractional read of n samples ending at write frontier `frontier_w`,
        each delayed by `delay`. Output sample j reads (frontier_w - n + j) -
        delay, so the freshest sample is at delay behind the frontier and every
        interpolation pair (i0, i1) sits strictly behind it — no pair straddles
        the wrap seam into not-yet-written (cap-old) territory."""
        cap, mask, buf = self.cap, self.mask, self.buf
        rp = np.mod((frontier_w - n - delay) + np.arange(n), cap)
        i0 = np.floor(rp).astype(np.int64)
        frac = (rp - i0).astype(np.float32)[:, None]
        i1 = (i0 + 1) & mask
        i0 &= mask
        return (buf[i0] * (1.0 - frac) + buf[i1] * frac).astype(np.float32)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        n = x.shape[0]
        self._step_delay()
        delay = self.delay
        snapped_from = self._snapped_from
        self._snapped_from = None

        old_w = self.w
        # Write the block, then read against a frozen snapshot of the new
        # frontier. Reads are computed from that single snapshot so one output
        # sample can never blend a slot's pre- and post-write values at the
        # wrap seam.
        widx = (old_w + np.arange(n)) & self.mask
        self.buf[widx] = x
        new_w = old_w + n
        self.w = new_w & self.mask

        out = self._read(new_w, delay, n)
        if snapped_from is not None:
            # Equal-power blend from the old delay path into the new one over the
            # head of the block so the snap is inaudible.
            old_path = self._read(new_w, snapped_from, n)
            k = min(self._xfade, n)
            t = (np.arange(k, dtype=np.float32) + 1.0) / (k + 1.0)
            fade_in = np.sin(0.5 * np.pi * t)[:, None]
            fade_out = np.cos(0.5 * np.pi * t)[:, None]
            out[:k] = old_path[:k] * fade_out + out[:k] * fade_in
        return out


class PsychoMixer:
    """TTS + optional ambient psychoacoustic mixer.

    process(tts, ambient=None, speaking=0.0):
      ambient is None → TTS only: center placement + brickwall limit.
      ambient given  → full path: delay ambient → carve 1–4 kHz (gated by
                       `speaking`) → M/S widen → sum with center TTS → limit.

    tts: mono (n,) or (n,2). ambient: (n,) or (n,2). speaking: 0..1 (TTS active).
    """

    def __init__(self, fs: float, tts_gain: float = 1.0, width: float = 1.25,
                 carve_freqs=(1300.0, 3200.0), carve_q=1.0, carve_db=-12.0,
                 ceiling: float = 0.97):
        self.fs = float(fs)
        self.tts_gain = float(tts_gain)
        self.width = float(width)
        # Two peaking-cuts to flatten the 1–4 kHz presence band; a single bell
        # is not wide enough.
        self._carve = [_peaking_coeffs(fs, f, carve_q, carve_db) for f in carve_freqs]
        # Block-continuous filter state: separate zi per section per channel.
        self._zi = [[np.zeros(2), np.zeros(2)] for _ in carve_freqs]
        self.delay = DelayLine(fs)
        self.limiter = LookaheadLimiter(fs, ceiling=ceiling)

    def set_rtt(self, seconds: float) -> None:
        self.delay.set_target(seconds * self.fs)

    def _carve_band(self, ambient: np.ndarray, speaking: float) -> np.ndarray:
        """Carves the 1–4 kHz presence band. Per-channel, per-section filter
        state carries across blocks so boundaries produce no clicks. speaking
        cross-fades between dry (0) and fully carved (1)."""
        c = float(np.clip(speaking, 0.0, 1.0))
        amb = _as_stereo(ambient)
        out = amb.copy()
        for ch in range(2):
            wet = amb[:, ch].astype(np.float64)
            for k, (b, a) in enumerate(self._carve):
                wet, self._zi[k][ch] = _lfilter(b, a, wet, zi=self._zi[k][ch])
            out[:, ch] = amb[:, ch] + c * (wet.astype(np.float32) - amb[:, ch])
        return out.astype(np.float32)

    def process(self, tts, ambient=None, speaking: float = 0.0) -> np.ndarray:
        tts_st = place_center(tts, self.tts_gain) if np.ndim(tts) == 1 \
            else _as_stereo(tts) * self.tts_gain

        if ambient is None:
            return self.limiter.process(tts_st)

        amb = self.delay.process(ambient)
        amb = self._carve_band(amb, speaking)
        m = 0.5 * (amb[:, 0] + amb[:, 1])
        s = 0.5 * (amb[:, 0] - amb[:, 1]) * self.width
        mixed = np.empty_like(amb)
        mixed[:, 0] = (m + s) + tts_st[:, 0]
        mixed[:, 1] = (m - s) + tts_st[:, 1]
        return self.limiter.process(mixed)


if __name__ == "__main__":
    # Smoke checks: brickwall guarantee, block-continuity (no NaN/clicks), latency.
    fs = 24000
    lim = LookaheadLimiter(fs, ceiling=0.97)
    rng = np.random.default_rng(0)

    peak_seen = 0.0
    nan = False
    for _ in range(50):
        block = (rng.standard_normal((1024, 2)).astype(np.float32)) * 2.0
        y = lim.process(block)
        peak_seen = max(peak_seen, float(np.max(np.abs(y))))
        nan = nan or bool(np.isnan(y).any())
    print(f"[limiter] peak_out={peak_seen:.4f}  ceiling=0.970  "
          f"brickwall_ok={peak_seen <= 0.9701}  nan={nan}  "
          f"latency={lim.latency_samples} smp ({1000*lim.L/fs:.2f} ms)")

    mx = PsychoMixer(fs)
    tts = np.sin(2 * np.pi * 200 * np.arange(1024) / fs).astype(np.float32) * 1.5
    out = mx.process(tts, ambient=None, speaking=0.0)
    print(f"[mixer/ducking] out_shape={out.shape} peak={np.max(np.abs(out)):.4f} "
          f"stereo_equal={np.allclose(out[:,0], out[:,1])}")

    amb = (rng.standard_normal((1024, 2)).astype(np.float32)) * 1.2
    out2 = mx.process(tts, ambient=amb, speaking=1.0)
    print(f"[mixer/passthrough] out_shape={out2.shape} peak={np.max(np.abs(out2)):.4f} "
          f"brickwall_ok={np.max(np.abs(out2)) <= 0.9701}")
