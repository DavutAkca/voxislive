"""Cascade free-tier engine: simultaneous cloud TEXT + local Piper voice.

Architecture (E, validated live + benched 2026-07-12): the same Live translate
model the paid tier uses streams translated TEXT while the speaker talks
(TextLiveTranslator — no audio-out tokens, the expensive leg), a sentence
assembler cuts the stream into speakable units, and a local Piper voice
(local_tts) speaks them with catch-up pacing. Measured floor: first output on
par with the paid engine (~3.5 s), after-pause tail ~4 s vs the paid engine's
own 2.3-3.1 s; the pacing constants below are bench-pinned — none of the
obvious "tighter" variants moved the tail beyond run noise (notes/spike_bench).

Honors the pipeline translator contract (send_pcm16 / wait_ready / stop /
is_alive / _ready / .engine / on_fatal) as a WRAPPER, not a BaseTranslator
subclass: the session machine lives in the inner cloud leg; this thread only
supervises and hosts the TTS worker. Audio is emitted as 24 kHz PCM16 mono —
byte-identical format to the Gemini engine — so Player/recorder/pipeline need
no changes (Piper's 22 050 Hz is resampled here).

If the voice model is unavailable (unregistered language, download failure)
the session degrades to captions-only: translation continues, no local audio.
"""
from __future__ import annotations

import queue
import re
import threading
import time

import numpy as np

OUT_RATE = 24000  # emitted PCM16 rate — matches the Gemini engine exactly

# --- pacing constants (bench-pinned, see notes/spike_bench.py) ---------------
SENT_END = re.compile(r"(.+?[.!?…;:])(\s+|$)", re.S)
MAX_BUF = 180            # flush even without punctuation past this many chars
CLAUSE_FLUSH_CHARS = 80  # long sentence: start speaking from its last comma
IDLE_FLUSH_S = 1.5       # mid-speech trickle gaps below this stay buffered
IDLE_FLUSH_MIN_CHARS = 30
IDLE_FLUSH_HARD_S = 2.5
QUIET_FLUSH_S = 0.7      # source audio silent (video paused): flush fast
STALL_DRAIN_S = 1.5      # no new text: presume the speaker stopped
MERGE_MAX_CHARS = 320    # queued sentences merge into one prosody arc
LOOKAHEAD_S = 1.0        # synth at most this far ahead of playback
_SILENCE_PEAK = 33       # int16 peak below this = silent source frame (~1e-3)


def pick_speed(backlog: int, age: float, stalled: bool, quiet: bool) -> float:
    """Catch-up playback speed. Keys off the sentence's AGE, not just queue
    depth: merging keeps the queue near 0 while lag accrues in playback time
    (the v3 field regression — 7 s lag at 'queue 0')."""
    if backlog >= 2 or age > 3.5 or ((stalled or quiet) and age > 1.5):
        return 1.5
    if backlog == 1 or age > 2.0 or stalled or quiet:
        return 1.2
    return 1.0


class SentenceAssembler:
    """Accumulates streamed text deltas, emits speakable units."""

    def __init__(self, emit):
        self.buf = ""
        self.last_delta = 0.0
        self.emit = emit

    def feed(self, delta: str):
        self.buf += delta
        self.last_delta = time.monotonic()
        while True:
            m = SENT_END.match(self.buf)
            if m:
                self.emit(m.group(1).strip())
                self.buf = self.buf[m.end():]
                continue
            if len(self.buf) >= CLAUSE_FLUSH_CHARS:
                c = self.buf.rfind(", ", 40, len(self.buf))
                if c > 0:
                    self.emit(self.buf[:c + 1].strip())
                    self.buf = self.buf[c + 1:].lstrip()
                    continue
            if len(self.buf) >= MAX_BUF:
                cut = self.buf.rfind(" ", 0, MAX_BUF)
                cut = cut if cut > 40 else MAX_BUF
                self.emit(self.buf[:cut].strip())
                self.buf = self.buf[cut:].lstrip()
                continue
            break

    def flush(self):
        if self.buf.strip():
            self.emit(self.buf.strip())
        self.buf = ""

    def maybe_idle_flush(self, src_quiet_for: float):
        if not self.buf.strip():
            return
        idle = time.monotonic() - self.last_delta
        if idle > QUIET_FLUSH_S and src_quiet_for > QUIET_FLUSH_S:
            self.flush()  # source silent: nothing more is coming — say it now
            return
        if idle > IDLE_FLUSH_HARD_S or \
                (idle > IDLE_FLUSH_S and len(self.buf) >= IDLE_FLUSH_MIN_CHARS):
            self.flush()


def _resample_to_out(x: np.ndarray, rate: int) -> bytes:
    """Mono float32 @rate -> 24 kHz PCM16 bytes (linear; fine for TTS output)."""
    if rate != OUT_RATE and len(x) > 1:
        n = int(len(x) * OUT_RATE / rate)
        x = np.interp(np.linspace(0.0, len(x) - 1.0, n),
                      np.arange(len(x), dtype=np.float64), x)
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


class CascadeTranslator(threading.Thread):
    """Free-tier engine wrapper. run() supervises; dies when the inner cloud
    leg dies so the pipeline's translator-death teardown sees it."""

    def __init__(self, api_key, target_lang, on_audio, on_text, on_status, *,
                 rotate_minutes=13, name="cascade", model=None, voice="Aoede",
                 temperature=0.3, key_provider=None, on_fatal=None,
                 inner_factory=None, tts_factory=None):
        super().__init__(daemon=True, name=name)
        self.engine = "cascade"
        self.target_lang = target_lang
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_status = on_status
        self.on_fatal = on_fatal  # read by the inner leg via the property below
        self._stopping = threading.Event()
        self._sentq: "queue.Queue[tuple[str, float]]" = queue.Queue(maxsize=32)
        self._src_quiet_since = 0.0    # 0 = source currently loud
        self._last_pcm_ts = 0.0        # last frame of ANY kind (gated stream)
        self._play_deadline = 0.0      # when already-emitted audio finishes
        self._asm = SentenceAssembler(self._enqueue_sentence)
        self._tts = None
        self._tts_factory = tts_factory  # tests inject a fake; None = LocalTTS
        self._synth_thread = threading.Thread(
            target=self._synth_loop, daemon=True, name=f"{name}-tts")

        def _inner_on_text(kind, text):
            self.on_text(kind, text)          # live captions, unchanged
            if kind == "out":
                self._asm.feed(text)

        make_inner = inner_factory or self._default_inner
        self._inner = make_inner(
            api_key, target_lang, on_text=_inner_on_text,
            on_status=on_status, rotate_minutes=rotate_minutes,
            name=name, model=model, voice=voice, temperature=temperature,
            key_provider=key_provider)
        # A dead cloud leg is a dead cascade: chain the substitution hook.
        self._inner.on_fatal = self._on_inner_fatal

    @staticmethod
    def _default_inner(api_key, target_lang, *, on_text, on_status,
                       rotate_minutes, name, model, voice, temperature,
                       key_provider):
        from .text_live_translator import TextLiveTranslator  # lazy: vendor SDK
        return TextLiveTranslator(
            api_key, target_lang,
            on_audio=lambda _pcm: None,  # never fires on the TEXT leg
            on_text=on_text, on_status=on_status,
            rotate_minutes=rotate_minutes, name=name, voice=voice,
            temperature=temperature, model=model, key_provider=key_provider)

    # ---- translator contract -------------------------------------------------
    @property
    def _ready(self):
        # Billing liveness gates on the CLOUD leg being live (pipeline reads
        # translator._ready); local TTS state never affects accrual.
        return self._inner._ready

    def send_pcm16(self, data: bytes):
        # Energy gate feeding the fast tail flush: a paused video is true
        # digital silence, cheap to detect on the int16 peak.
        try:
            peak = int(np.abs(np.frombuffer(data, dtype=np.int16)).max()) \
                if data else 0
        except ValueError:
            peak = 0
        self._last_pcm_ts = time.monotonic()
        if peak < _SILENCE_PEAK:
            if self._src_quiet_since == 0.0:
                self._src_quiet_since = time.monotonic()
        else:
            self._src_quiet_since = 0.0
        self._inner.send_pcm16(data)

    def wait_ready(self, timeout: float = 15) -> bool:
        return self._inner.wait_ready(timeout)

    def start(self):
        try:
            factory = self._tts_factory
            self._tts = (factory() if factory is not None
                         else self._make_local_tts())
        except Exception as e:
            # Captions-only degrade: translation must never die for a voice.
            self._tts = None
            self.on_status(f"cascade: local voice unavailable ({e}) — captions only")
        self._inner.start()
        if self._tts is not None:
            self._synth_thread.start()
        super().start()

    def _make_local_tts(self):
        from .local_tts import LocalTTS  # lazy: sherpa wheel optional on OSS
        return LocalTTS(self.target_lang, on_status=self.on_status)

    def stop(self):
        self._stopping.set()
        self._inner.stop()

    # ---- internals -----------------------------------------------------------
    def _on_inner_fatal(self, exc):
        if self.on_fatal is not None:
            return self.on_fatal(exc)
        return False

    def _src_quiet_for(self) -> float:
        now = time.monotonic()
        quiet = now - self._src_quiet_since if self._src_quiet_since > 0 else 0.0
        # The cascade streams GATED (silence never reaches us at all), so a
        # frame drought IS silence: no frames for >0.3 s counts from there.
        if self._last_pcm_ts > 0:
            quiet = max(quiet, now - self._last_pcm_ts - 0.3)
        return max(0.0, quiet)

    def _enqueue_sentence(self, sentence: str):
        try:
            self._sentq.put_nowait((sentence, time.monotonic()))
        except queue.Full:
            # Sustained overrun: keep the freshest content (drop-oldest).
            try:
                self._sentq.get_nowait()
            except queue.Empty:
                pass
            try:
                self._sentq.put_nowait((sentence, time.monotonic()))
            except queue.Full:
                pass

    def _synth_loop(self):
        while not self._stopping.is_set():
            if self._play_deadline - time.monotonic() > LOOKAHEAD_S:
                time.sleep(0.05)   # player has audio — let merges accumulate
                continue
            try:
                text, t_flush = self._sentq.get(timeout=0.2)
            except queue.Empty:
                continue
            merged = [text]
            while len(" ".join(merged)) < MERGE_MAX_CHARS:
                try:
                    nxt, _ = self._sentq.get_nowait()
                    merged.append(nxt)
                except queue.Empty:
                    break
            text = " ".join(merged)
            now = time.monotonic()
            stalled = (self._asm.last_delta > 0.0
                       and now - self._asm.last_delta > STALL_DRAIN_S)
            speed = pick_speed(self._sentq.qsize(), now - t_flush, stalled,
                               self._src_quiet_for() > QUIET_FLUSH_S)
            try:
                samples, rate = self._tts.synth(text, speed=speed)
            except Exception as e:
                self.on_status(f"cascade: tts error ({e})")
                continue
            pcm = _resample_to_out(samples, rate)
            dur = len(pcm) / (OUT_RATE * 2.0)
            now = time.monotonic()
            self._play_deadline = max(now, self._play_deadline) + dur
            self.on_audio(pcm)

    def run(self):
        # Supervisor: tick the idle flush; mirror the inner leg's liveness so
        # pipeline._maybe_handle_translator_dead sees a dead cascade.
        while not self._stopping.is_set():
            self._asm.maybe_idle_flush(self._src_quiet_for())
            if not self._inner.is_alive():
                self._asm.flush()
                return
            time.sleep(0.1)
        self._asm.flush()
