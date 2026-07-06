"""Silero VAD (ONNX) speech detection and gate.

Only frames containing speech are forwarded to the translator; music, effects
and silence are filtered locally. The model operates on 16 kHz 512-sample
(32 ms) frames.
"""
import collections
import hashlib
import os
import tempfile
import urllib.request

import numpy as np
import onnxruntime as ort

from .paths import model_path

# Pinned to an immutable release tag (master is a moving target) and verified
# by hash: a truncated/tampered download must fail loudly here, not surface as
# an inscrutable onnxruntime load error at session start.
MODEL_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"
MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"
MODEL_PATH = model_path("silero_vad.onnx")
_DOWNLOAD_TIMEOUT = 30  # socket-level; an unreachable CDN can't hang session start

SAMPLE_RATE = 16000
FRAME = 512  # 32 ms @ 16 kHz — the frame size Silero v5 expects.
FRAME_MS = 1000.0 * FRAME / SAMPLE_RATE


def _download_model() -> None:
    """Fetch the VAD model with a timeout, verify its SHA-256, then move it into
    place atomically so a crash mid-download can never leave a truncated model
    that would then fail every session start."""
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "voxis"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        data = resp.read()  # ~2.3 MB — fine to buffer
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        raise RuntimeError(
            f"VAD model hash mismatch (got {digest[:16]}…, expected "
            f"{MODEL_SHA256[:16]}…) — refusing to use an unverified model."
        )
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(MODEL_PATH), suffix=".onnx.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, MODEL_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class SileroVAD:
    def __init__(self):
        if not os.path.exists(MODEL_PATH):
            _download_model()
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3  # errors only
        # CPU is the right device for this 1 MB model at single-frame (32 ms)
        # inference: warm CPU runs ~0.1 ms/frame, while a GPU provider adds
        # per-call host->device dispatch with no throughput win at batch=1. A
        # GPU provider also probes its runtime at session creation — when the
        # CUDA toolkit is absent (the common case) that probe spams stderr
        # (missing cublasLt64_12.dll) and stalls startup before silently falling
        # back to CPU. So we request CPU directly: faster, quiet, deterministic.
        self.sess = ort.InferenceSession(
            MODEL_PATH, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.reset()
        # Warm up: pay the first-inference JIT/allocation (and any GPU kernel
        # build) cost here at construction, not on the first live speech frame.
        self.prob(np.zeros(FRAME, dtype=np.float32))
        self.reset()

    # Silero v5 prepends 64 samples of context from the previous frame; without
    # this prefix the model returns ~0 probabilities silently (no error).
    CONTEXT = 64

    def reset(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(self.CONTEXT, dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        """frame: float32 in [-1, 1], length FRAME. Returns speech probability."""
        x = np.concatenate([self._context, frame.astype(np.float32)])
        out, self._state = self.sess.run(
            None,
            {
                "input": x[np.newaxis, :],
                "state": self._state,
                "sr": np.array(16000, dtype=np.int64),
            },
        )
        self._context = x[-self.CONTEXT:]
        return float(out[0][0])


class SpeechGate:
    """Translates per-frame VAD probabilities into send/drop decisions.

    Mirrors Silero's reference state machine (VADIterator / get_speech_timestamps)
    rather than a bespoke one, because the bespoke onset logic misfired on
    consonant-dense languages:

    - threshold / neg_threshold: HYSTERESIS. Speech is triggered on a single frame
      >= threshold; it is only released below neg_threshold (= threshold - 0.15 by
      default, per Silero). A single threshold for both open and close chops
      naturally-modulating speech into "silence" mid-utterance.
    - min_speech_ms: an onset must accumulate this much *cumulative* (NOT
      consecutive) above-threshold speech before the gate commits — rejects short
      clicks/transients without demanding an unbroken voiced run. The earlier
      "N consecutive frames, reset on any dip" rule meant that Czech (frequent
      unvoiced fricatives/stops dip the VAD probability every ~100 ms) rarely
      reached the consecutive count, so the gate never opened and entire spoken
      stretches were emitted as silence. English, with longer voiced runs, hid it.
    - preroll_ms: on commit, a buffer of previous frames is emitted so the first
      word is not cut (past audio — zero forward latency).
    - hangover_ms: frames below neg_threshold must persist this long before the
      gate closes, so brief intra-sentence pauses do not end the segment.
    """

    def __init__(self, threshold=0.5, min_speech_ms=200, hangover_ms=800,
                 preroll_ms=300, neg_threshold=None):
        self.vad = SileroVAD()
        self.threshold = threshold
        # Release hysteresis: default 0.15 below the trigger, matching Silero.
        self.neg_threshold = (threshold - 0.15) if neg_threshold is None else neg_threshold
        self.min_speech = max(1, round(min_speech_ms / FRAME_MS))
        self.hangover = max(1, round(hangover_ms / FRAME_MS))
        self.preroll = collections.deque(maxlen=max(1, round(preroll_ms / FRAME_MS)))
        self.active = False
        self._above = 0       # cumulative above-threshold frames since the trigger
        self._silence = 0     # consecutive frames below neg_threshold
        self._pending: list[np.ndarray] = []

    def _abort_onset(self) -> None:
        """A tentative onset turned out to be a transient: recycle its buffered
        frames into the preroll ring and return to idle."""
        for f in self._pending:
            self.preroll.append(f)
        self._pending = []
        self._above = 0
        self._silence = 0

    def process(self, frame: np.ndarray) -> tuple[bool, list[np.ndarray]]:
        """Processes one 512-sample frame; returns (speech_active, frames_to_send)."""
        p = self.vad.prob(frame)
        send: list[np.ndarray] = []

        if self.active:
            send = [frame]
            if p < self.neg_threshold:
                self._silence += 1
                if self._silence >= self.hangover:
                    self.active = False
                    self._above = 0
                    self._silence = 0
                    self._pending = []
            else:
                self._silence = 0
            return self.active, send

        # Inactive. A frame >= threshold triggers / advances a tentative onset;
        # dips no longer reset it — only a sustained drop below neg_threshold does.
        if p >= self.threshold:
            self._pending.append(frame)
            self._above += 1
            self._silence = 0
            if self._above >= self.min_speech:
                self.active = True
                send = list(self.preroll) + self._pending
                self._pending = []
                self.preroll.clear()
                self._above = 0
            return self.active, send

        if self._above > 0:
            # Mid-onset but this frame is sub-threshold: hold it, and only abort
            # if we stay below the release threshold for a full hangover.
            self._pending.append(frame)
            if p < self.neg_threshold:
                self._silence += 1
                if self._silence >= self.hangover:
                    self._abort_onset()
            # Between neg_threshold and threshold: ambiguous — keep waiting.
            return False, []

        # Idle: keep the preroll ring primed with recent context.
        self.preroll.append(frame)
        return False, []
