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

    - min_speech_ms: speech must persist this long before the gate opens (rejects short transients).
    - preroll_ms: when opening, a buffer of previous frames is emitted so the first word is not cut.
    - hangover_ms: gate stays open this long after speech stops so brief pauses do not close it.
    """

    def __init__(self, threshold=0.5, min_speech_ms=200, hangover_ms=800, preroll_ms=300):
        self.vad = SileroVAD()
        self.threshold = threshold
        self.min_speech = max(1, round(min_speech_ms / FRAME_MS))
        self.hangover = max(1, round(hangover_ms / FRAME_MS))
        self.preroll = collections.deque(maxlen=max(1, round(preroll_ms / FRAME_MS)))
        self.active = False
        self._onset = 0
        self._silence = 0
        self._pending: list[np.ndarray] = []

    def process(self, frame: np.ndarray) -> tuple[bool, list[np.ndarray]]:
        """Processes one 512-sample frame; returns (speech_active, frames_to_send)."""
        p = self.vad.prob(frame)
        send: list[np.ndarray] = []
        if not self.active:
            if p >= self.threshold:
                self._onset += 1
                self._pending.append(frame)
                if self._onset >= self.min_speech:
                    self.active = True
                    self._silence = 0
                    send = list(self.preroll) + self._pending
                    self._pending = []
                    self.preroll.clear()
            else:
                for f in self._pending:
                    self.preroll.append(f)
                self._pending = []
                self._onset = 0
                self.preroll.append(frame)
        else:
            send = [frame]
            if p >= self.threshold:
                self._silence = 0
            else:
                self._silence += 1
                if self._silence >= self.hangover:
                    self.active = False
                    self._onset = 0
        return self.active, send
