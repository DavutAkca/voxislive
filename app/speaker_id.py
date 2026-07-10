"""Client-side speaker-change detection (who is talking: S1 / S2 / ...).

None of the cloud engines expose diarization (Gemini live-translate has no
speaker labels or timestamps; its docs note voices "get stuck" on rapid
multi-speaker turns), so back-to-back speakers used to merge into one caption
line. This module detects speaker *changes* locally so the UI can split and
tag turns: a CAM++ speaker-embedding ONNX model (3D-Speaker zh+en "common
advanced", 192-dim, CPU) is run over the VAD-gated 16 kHz speech stream, and
consecutive embedding windows are clustered online into anonymous labels
(1, 2, ...). Labels are identities-within-a-session, not names.

Accuracy expectations (measured on the sherpa-onnx speaker test set with this
exact featurizer): same-speaker cosine ~0.78, different-speaker ~0.16 on full
utterances; short 1.5 s windows narrow the margin, hence the hysteresis and
the 2-window confirmation before a new speaker is minted. Overlapping speech
and heavily music-mixed content degrade it — labeling is best-effort by
design and everything here fails soft (labels vanish, translation continues).

The whole pipeline is numpy + onnxruntime (already shipped for Silero VAD):
no torch, no new dependencies. Feature extraction is a Kaldi-compatible
80-dim log-mel fbank (povey window, snip-edges, preemph 0.97) with the
model's "global-mean" normalization, validated against the reference
implementation's similarity scores.
"""
import collections
import hashlib
import logging
import os
import tempfile
import threading
import time
import urllib.request

import numpy as np

from .paths import model_path

_log = logging.getLogger("voxis")

# Pinned to the sherpa-onnx model release (immutable tag) and hash-verified,
# mirroring vad.py: a truncated/tampered download must fail loudly here, not
# as an inscrutable onnxruntime error mid-session. NB the upstream tag really
# is spelled "recongition".
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/"
             "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx")
MODEL_SHA256 = "aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"
MODEL_PATH = model_path("speaker_campplus.onnx")
_DOWNLOAD_TIMEOUT = 60  # ~27 MB; a dead CDN must not hang the worker forever

SAMPLE_RATE = 16000

# ---- Kaldi-compatible fbank (numpy) ----
# Must match the model's training features (kaldi-native-fbank defaults):
# 25 ms / 10 ms frames, 512-point FFT, 80 mel bins 20 Hz..Nyquist, povey
# window, per-frame DC removal, preemphasis 0.97, log power-mel with epsilon
# floor, waveform in [-1, 1] (the model metadata says normalize_samples=1).
_FRAME_LEN = 400
_FRAME_SHIFT = 160
_N_FFT = 512
_N_MELS = 80
_LOW_FREQ = 20.0
_PREEMPH = 0.97


def _mel(f):
    return 1127.0 * np.log(1.0 + f / 700.0)


def _mel_banks() -> np.ndarray:
    n_bins = _N_FFT // 2 + 1
    fft_freqs = np.arange(n_bins) * SAMPLE_RATE / _N_FFT
    mel_pts = np.linspace(_mel(_LOW_FREQ), _mel(SAMPLE_RATE / 2.0), _N_MELS + 2)
    mels = _mel(fft_freqs)
    banks = np.zeros((_N_MELS, n_bins), dtype=np.float32)
    for m in range(_N_MELS):
        left, center, right = mel_pts[m], mel_pts[m + 1], mel_pts[m + 2]
        up = (mels - left) / (center - left)
        down = (right - mels) / (right - center)
        banks[m] = np.maximum(0.0, np.minimum(up, down))
    return banks


_BANKS = _mel_banks()
_WINDOW = ((0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_FRAME_LEN)
                               / (_FRAME_LEN - 1))) ** 0.85).astype(np.float32)


def fbank(wav: np.ndarray) -> np.ndarray:
    """wav: float32 [-1, 1] @ 16 kHz → (T, 80) log-mel features."""
    n = len(wav)
    if n < _FRAME_LEN:
        return np.zeros((0, _N_MELS), dtype=np.float32)
    n_frames = 1 + (n - _FRAME_LEN) // _FRAME_SHIFT  # snip_edges
    idx = np.arange(_FRAME_LEN)[None, :] + _FRAME_SHIFT * np.arange(n_frames)[:, None]
    frames = wav[idx].astype(np.float32)
    frames -= frames.mean(axis=1, keepdims=True)  # remove DC offset
    # Kaldi preemphasis: first sample subtracts itself scaled, not a neighbor.
    frames = np.concatenate([frames[:, :1] * (1.0 - _PREEMPH),
                             frames[:, 1:] - _PREEMPH * frames[:, :-1]], axis=1)
    frames *= _WINDOW
    spec = np.abs(np.fft.rfft(frames, n=_N_FFT)) ** 2
    return np.log(np.maximum(spec @ _BANKS.T, 1e-10)).astype(np.float32)


def _download_model() -> None:
    """Fetch the embedding model with a timeout, verify SHA-256, move into place
    atomically (same contract as vad._download_model)."""
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "voxis"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        raise RuntimeError(
            f"speaker model hash mismatch (got {digest[:16]}…, expected "
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


class SpeakerEmbedder:
    """CAM++ embedding extraction: float32 speech @16 kHz → unit 192-vector."""

    def __init__(self):
        import onnxruntime as ort  # heavy import stays off app startup
        if not os.path.exists(MODEL_PATH):
            _download_model()
        opts = ort.SessionOptions()
        # Single-threaded like the VAD session: the model runs off the audio
        # path on ~2 s windows every ~0.8 s of speech, so latency is not
        # critical and one core keeps the CPU footprint predictable.
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(
            MODEL_PATH, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        # Warm up: pay the first-inference JIT/allocation cost at construction.
        self.embed(np.zeros(SAMPLE_RATE, dtype=np.float32))

    def embed(self, wav: np.ndarray) -> np.ndarray:
        feats = fbank(wav)
        feats -= feats.mean(axis=0, keepdims=True)  # model's global-mean CMN
        e = self.sess.run(None, {"x": feats[None]})[0][0]
        return e / max(float(np.linalg.norm(e)), 1e-9)


class SpeakerTracker:
    """Online speaker-change detector over VAD-gated speech frames.

    feed() is called from the capture thread (under the _GatedSource lock) so
    it only enqueues; all DSP/inference happens on the worker thread. Model
    load/download also happens on the worker, so session start is never
    delayed and a missing model degrades to "no labels" silently.

    Clustering: unit embeddings over trailing ≤WINDOW_S of speech, compared
    against per-speaker EMA centroids by cosine similarity. Tuned for
    PRECISION over recall — on real content (music beds, fast cuts, room
    changes) the same voice produces scattered embeddings, and a label that
    flickers or mints phantom speakers is worse than one that reacts a second
    late (field finding, 2026-07-10 promo-video transcript):
      * A SWITCH to another known speaker fires only when the current one no
        longer matches at all (sims[cur] < ATTACH_SIM), the target decisively
        wins (SWITCH_MARGIN), and SWITCH_CONFIRM consecutive windows agree.
      * A NEW speaker is minted only from NEW_CONFIRM consecutive sub-floor
        windows that also agree with EACH OTHER (NEW_CONSISTENCY) — noise and
        music-polluted windows disagree pairwise, a genuinely new voice does
        not. Clearly-alien windows (< NEW_NOW_SIM vs everything) shortcut to
        two windows. The centroid seeds from the pending windows' mean.
      * Ambiguity (sub-floor best, cap reached, unconfirmed switch) always
        resolves to "stay with the current speaker".
      * Centroids EMA-update only on confident windows (≥ UPDATE_SIM) so a
        polluted window cannot drag a centroid toward the music bed.
    on_change(label) fires on every accepted label change, including the first
    assignment (label 1). Labels are 1-based ints, stable for the session.
    """

    WINDOW_S = 2.0        # embedding context (trailing speech)
    MIN_WINDOW_S = 1.0    # don't embed less than this much speech
    HOP_S = 0.8           # re-embed cadence, in accumulated NEW speech
    RESET_GAP_S = 1.5     # a silence gap this long resets the window buffer
    ATTACH_SIM = 0.45     # cosine floor to count as "matches this speaker"
    UPDATE_SIM = 0.55     # centroid EMA update only on confident windows
    NEW_NOW_SIM = 0.25    # below this vs ALL centroids: clearly a new voice
    SWITCH_MARGIN = 0.10  # extra sim required to switch away from current
    SWITCH_CONFIRM = 2    # consecutive agreeing windows before a switch fires
    NEW_CONFIRM = 3       # consecutive sub-floor windows to mint a speaker
    NEW_CONSISTENCY = 0.50  # those windows must also agree with each other
    MAX_SPEAKERS = 6      # hard cap; past it ambiguity stays with current
    EMA = 0.15            # centroid update rate

    def __init__(self, on_change, embedder_factory=None):
        self._on_change = on_change
        # Injection point for tests (a fake embedder avoids the 27 MB model).
        self._embedder_factory = embedder_factory or SpeakerEmbedder
        self._queue: collections.deque = collections.deque(maxlen=2048)
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._win: collections.deque = collections.deque()  # (t, samples)
        self._win_samples = 0
        self._new_samples = 0
        self._last_feed_t: float | None = None
        self._centroids: list[np.ndarray] = []
        self._current: int | None = None       # 0-based index into _centroids
        self._pending_new: list[tuple[np.ndarray, float]] = []  # (emb, best_sim)
        self._pending_switch: tuple[int, int] | None = None     # (target, count)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="voxis-speaker-id")
        self._thread.start()

    # ---- capture-thread side ----
    def feed(self, frames, t: float | None = None) -> None:
        """Enqueue gate-emitted speech frames. O(1) + one small concat; never
        blocks and never raises into the audio path."""
        if self._stopping.is_set():
            return
        try:
            chunk = frames[0] if len(frames) == 1 else np.concatenate(frames)
            self._queue.append((time.monotonic() if t is None else t,
                                chunk.astype(np.float32, copy=False)))
            self._wake.set()
        except Exception:
            pass

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        # Bounded: the worker may be mid-download; it is a daemon and checks
        # _stopping before emitting anything, so an overrun is harmless.
        self._thread.join(timeout=2.0)

    @property
    def n_speakers(self) -> int:
        return len(self._centroids)

    # ---- worker side ----
    def _run(self):
        try:
            embedder = self._embedder_factory()
        except Exception:
            _log.exception("speaker embedder unavailable — labels disabled")
            return
        while not self._stopping.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            while self._queue and not self._stopping.is_set():
                try:
                    t, chunk = self._queue.popleft()
                except IndexError:
                    break
                self._ingest(t, chunk)
                # Check the hop INSIDE the drain loop: if ingest ever outpaces
                # inference (burst after a stall), whole hops must still be
                # embedded in order, not skipped wholesale.
                if (self._win_samples >= self.MIN_WINDOW_S * SAMPLE_RATE
                        and self._new_samples >= self.HOP_S * SAMPLE_RATE):
                    self._new_samples = 0
                    try:
                        self._process(embedder)
                    except Exception:
                        _log.exception("speaker embedding failed — window skipped")

    def _ingest(self, t: float, chunk: np.ndarray):
        if self._last_feed_t is not None and t - self._last_feed_t > self.RESET_GAP_S:
            # Real silence gap: never embed a window spanning it (the two sides
            # are likely different utterances, possibly different speakers).
            self._win.clear()
            self._win_samples = 0
            self._new_samples = 0
        self._last_feed_t = t
        self._win.append((t, chunk))
        self._win_samples += len(chunk)
        self._new_samples += len(chunk)
        limit = int(self.WINDOW_S * SAMPLE_RATE)
        while self._win and self._win_samples - len(self._win[0][1]) >= limit:
            self._win_samples -= len(self._win.popleft()[1])

    def _process(self, embedder):
        wav = np.concatenate([c for _, c in self._win])
        limit = int(self.WINDOW_S * SAMPLE_RATE)
        if len(wav) > limit:
            wav = wav[-limit:]
        label = self._assign(embedder.embed(wav))
        if label is not None and not self._stopping.is_set():
            try:
                self._on_change(label)
            except Exception:
                _log.exception("on_change speaker callback failed")

    def _assign(self, e: np.ndarray) -> int | None:
        """Cluster one embedding; returns the 1-based label when the current
        speaker CHANGES (incl. the first assignment), else None. Every
        ambiguous outcome resolves to the CURRENT speaker — see class doc."""
        if not self._centroids:
            self._centroids.append(e)
            self._current = 0
            return 1
        sims = [float(e @ c) for c in self._centroids]
        best = int(np.argmax(sims))
        cur = self._current

        if sims[best] >= self.ATTACH_SIM:
            self._pending_new.clear()
            if best == cur or cur is None:
                self._pending_switch = None
                if sims[best] >= self.UPDATE_SIM:
                    self._update_centroid(best, e)
                if best == cur:
                    return None
            else:
                # Switch candidate. Fire only when the current speaker no
                # longer matches AND the target decisively wins AND this holds
                # for SWITCH_CONFIRM consecutive windows — a mixed/polluted
                # window matching two centroids at once must not flip labels.
                if sims[cur] >= self.ATTACH_SIM or \
                        sims[best] - sims[cur] < self.SWITCH_MARGIN:
                    self._pending_switch = None
                    return None
                tgt, count = self._pending_switch or (best, 0)
                if tgt != best:
                    tgt, count = best, 0
                count += 1
                if count < self.SWITCH_CONFIRM:
                    self._pending_switch = (tgt, count)
                    return None
                self._pending_switch = None
                if sims[best] >= self.UPDATE_SIM:
                    self._update_centroid(best, e)
            self._current = best
            return best + 1

        # Sub-floor: matches nobody well. Never jump between known speakers on
        # such a window; either mint a genuinely new voice or stay put.
        self._pending_switch = None
        if len(self._centroids) >= self.MAX_SPEAKERS:
            self._pending_new.clear()
            return None  # at capacity: ambiguity stays with the current voice
        self._pending_new.append((e, sims[best]))
        alien = all(s < self.NEW_NOW_SIM for _, s in self._pending_new)
        need = 2 if alien else self.NEW_CONFIRM
        if len(self._pending_new) < need:
            return None
        embs = [p for p, _ in self._pending_new]
        # A real new speaker yields windows that agree with each other; music
        # bleed / crosstalk yields scattered ones. Inconsistent evidence is
        # discarded down to the newest window rather than minting a phantom.
        pair = [float(a @ b) for i, a in enumerate(embs) for b in embs[i + 1:]]
        if pair and float(np.mean(pair)) < self.NEW_CONSISTENCY:
            self._pending_new = self._pending_new[-1:]
            return None
        seed = np.mean(embs, axis=0)
        self._centroids.append(seed / max(float(np.linalg.norm(seed)), 1e-9))
        self._pending_new.clear()
        self._current = len(self._centroids) - 1
        return self._current + 1

    def _update_centroid(self, idx: int, e: np.ndarray) -> None:
        c = (1.0 - self.EMA) * self._centroids[idx] + self.EMA * e
        self._centroids[idx] = c / max(float(np.linalg.norm(c)), 1e-9)
