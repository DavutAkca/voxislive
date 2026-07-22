"""Mode pipelines: capture → VAD gate → Gemini Live → playback.

IncomingPipeline routes system audio (or the meeting partner's voice) to the
translator and sends the synthesized translation to the user's headphones.
OutgoingPipeline routes the user's microphone to the translator and pipes the
translation into the virtual microphone consumed by Teams/Zoom/etc.
"""
import collections
import logging
import os
import threading
import time
import uuid

import numpy as np

from .audio_io import Capture, Player, find_device, resolve_name, _make_resampler
from .config import (ENGINE_CASCADE, ENGINE_GEMINI, ENGINE_OPENAI, ENGINE_QWEN,
                     gate_params, stream_gated)
from .engines import make_translator
from .i18n import t
from .playback_sync import AdaptivePlaybackStager

from . import audio_io
from . import sysaudio
from . import voxis_client

_log = logging.getLogger("voxis")

# Open-Core hook: optional premium package resolved once at import time.
# Absence is the normal OSS path.
try:
    import premium as _premium  # type: ignore[import-not-found]
except ImportError:
    _premium = None
# LiveTranslator (google.genai) and SpeechGate (onnxruntime) are imported lazily
# inside the pipeline constructors so the heavy runtimes don't load until a
# session actually starts — shaving ~1-2 s off cold app startup.
_FRAME = 512  # 32 ms @ 16 kHz — Silero VAD v5 frame size


def _f32_to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _rms_level(prev: float, mono: np.ndarray, gain: float = 6.0) -> float:
    """Asymmetric-smoothed input level (0..1) for the UI meter: fast attack, slow
    release. Drives only telemetry — never the audio path."""
    if mono.size == 0:
        target = 0.0
    else:
        rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
        target = min(1.0, rms * gain)
    a = 0.5 if target > prev else 0.12
    return prev + (target - prev) * a


class RTTEstimator:
    """Estimates cloud round-trip time (speech onset → first TTS chunk) with EMA
    smoothing. The estimate drives the ambient delay line so the original audio
    stays aligned with the translation when the spatial path is engaged.

    The alignment is utterance-level, not sample-accurate — translation reorders
    and re-times words, so this only locks onsets. Onsets that fire while TTS is
    already playing are skipped to avoid contaminated samples.
    """

    def __init__(self, fs: float, ema_alpha: float = 0.1,
                 min_seconds: float = 0.2, max_seconds: float = 3.0):
        self.fs = float(fs)
        self.alpha = float(ema_alpha)
        self.min_s = float(min_seconds)
        self.max_s = float(max_seconds)
        self._ema = None            # guarded EMA for the ambient delay line
        self._t_onset = None
        self._t_first_onset = None  # first speech onset of the session
        self._first_audio_s = None  # session first-audio latency (display/analysis)
        self._lock = threading.Lock()

    def mark_onset(self) -> None:
        """Guarded onset (delay-line): the caller skips it while TTS is playing."""
        with self._lock:
            if self._t_onset is None:
                self._t_onset = time.monotonic()

    def mark_first_onset(self) -> None:
        """Records the session's FIRST speech time (no guards), for the honest
        first-audio readout — captured even if TTS is already playing."""
        with self._lock:
            if self._t_first_onset is None:
                self._t_first_onset = time.monotonic()

    def mark_tts(self, audible: bool = True) -> None:
        with self._lock:
            now = time.monotonic()
            # Honest, backlog-proof readout: span from the session's FIRST speech
            # to the FIRST AUDIBLE translated audio. Measured ONCE, before any
            # backlog exists, so it's comparable across engines — unlike a
            # per-utterance onset→next-chunk reading, which a continuously-playing
            # engine (OpenAI plays the previous translation while you speak the
            # next) drives to a meaningless ~0.0-0.2 s. Gating on audible output
            # ignores an initial near-silent/padding chunk that would understate it.
            if audible and self._first_audio_s is None and self._t_first_onset is not None:
                fa = now - self._t_first_onset
                if 0.1 <= fa <= 30.0:
                    self._first_audio_s = round(fa, 2)
            # Delay-line EMA from the GUARDED onset only (uncontaminated), capped.
            if self._t_onset is not None:
                dt = now - self._t_onset
                self._t_onset = None
                if self.min_s <= dt <= self.max_s:
                    self._ema = dt if self._ema is None else self.alpha * dt + (1 - self.alpha) * self._ema

    def target_samples(self) -> float:
        with self._lock:
            return 0.0 if self._ema is None else self._ema * self.fs

    @property
    def rtt_seconds(self) -> float | None:
        return self._ema

    @property
    def first_audio_seconds(self) -> float | None:
        """Session first-audio latency (first speech → first translated audio) for
        the UI/analysis readout — backlog-proof and comparable across engines."""
        return self._first_audio_s


# Smart-stream silence ceiling: after this many consecutive non-speech frames
# stop padding with silence. ~512/16000 s per frame, so 48 frames ≈ 1.5 s of
# trailing pad before we let a genuine gap form. The gap lets the translator's
# bounded input queue drain to empty, which is what allows the _sender's 0.5 s
# read timeout to surface and the 13-min rotation to fire on a quiet window.
# Billing tradeoff: Smooth (smart=True) streams continuous audio, so this pad
# is the only thing that bounds billed silence; Saver (gated) never pads, so it
# bills fewer minutes at the cost of clipped lead-ins on the next utterance.
_SMART_SILENCE_MAX_FRAMES = 48


class _Accum:
    """Preallocated float32 frame accumulator: append chunks, pop fixed-size
    frames. Replaces the per-chunk np.concatenate churn (the same realloc
    pattern the playback ring was rewritten to eliminate) with two slice copies
    per cycle and zero steady-state allocation beyond the popped frame."""

    __slots__ = ("_buf", "n")

    def __init__(self, cap: int):
        self._buf = np.empty(cap, dtype=np.float32)
        self.n = 0

    def push(self, x: np.ndarray) -> None:
        m = len(x)
        if m == 0:
            return
        need = self.n + m
        if need > len(self._buf):  # rare: grow geometrically, then stabilize
            nb = np.empty(max(need, 2 * len(self._buf)), dtype=np.float32)
            nb[:self.n] = self._buf[:self.n]
            self._buf = nb
        self._buf[self.n:need] = x
        self.n = need

    def pop(self, k: int) -> np.ndarray:
        """Remove and return the first k samples as an independent copy (the
        gate stores frames across calls, so views into _buf are not safe)."""
        out = self._buf[:k].copy()
        rem = self.n - k
        if rem:
            self._buf[:rem] = self._buf[k:self.n]
        self.n = rem
        return out


class _GatedSource:
    """Resamples captured audio to 16 kHz, splits into 512-sample frames and runs
    each frame through the VAD gate.

    Modes:
      * default: gated — only speech frames are forwarded to the translator.
      * smart:   continuous stream; speech frames are real audio, non-speech
                 frames become silence. Keeps latency low while music/noise is
                 filtered out before reaching the cloud. After a sustained gap
                 silence padding stops so the queue can drain and rotation fire.
      * always_send: raw continuous stream, including silence (legacy agent path).
    """

    def __init__(self, in_rate: int, gate, send_fn, suppress_when=None,
                 always_send=False, smart=False, send_rate=16000,
                 speech_tap=None):
        self._in_rate = int(in_rate)
        self._resample = _make_resampler(in_rate, 16000)
        self._gate = gate
        self._send = send_fn
        self._suppress_when = suppress_when
        # Optional observer of the gate's speech decisions (16 kHz frames incl.
        # preroll) — feeds the speaker-change tracker. Must be O(1)/non-raising:
        # it runs on the capture thread under this object's lock.
        self._speech_tap = speech_tap
        self._always_send = always_send
        self._smart = smart
        self._silence = _f32_to_pcm16(np.zeros(_FRAME, dtype=np.float32))
        self._acc = _Accum(8 * _FRAME)
        self._lock = threading.Lock()
        self._silent_run = 0
        self.speech_active = False
        self.closed = False
        # Full-band send path. The VAD always runs at 16 kHz, but OpenAI ingests
        # 24 kHz — so when send_rate != 16000 we keep a PARALLEL send-rate buffer +
        # a short history and emit the time-aligned full-band frames matching the
        # gate's send decision (incl. its preroll). send_rate==16000 -> the classic
        # single-stream path (Gemini), byte-for-byte unchanged.
        self._configure_send_rate(int(send_rate))

    def _configure_send_rate(self, send_rate: int) -> None:
        """Replace the engine-facing resampler and its rate-specific buffers.

        The VAD side always stays at 16 kHz. Only the parallel full-band path is
        rebuilt, so a live OpenAI (24 kHz) -> Gemini (16 kHz) failover can keep
        the already-open capture device without mislabelling 24 kHz PCM as 16 kHz.
        Caller holds ``_lock`` after construction.
        """
        self._send_rate = int(send_rate)
        if self._send_rate != 16000:
            self._resample_send = _make_resampler(self._in_rate, self._send_rate)
            self._send_frame = _FRAME * self._send_rate // 16000
            self._acc_send = _Accum(8 * self._send_frame)
            self._hist_max = 64
            self._hist_send: collections.deque = collections.deque(
                maxlen=self._hist_max)
            self._silence_send = _f32_to_pcm16(
                np.zeros(self._send_frame, dtype=np.float32))
        else:
            self._resample_send = None
            self._send_frame = _FRAME
            self._acc_send = None
            self._hist_send = collections.deque()
            self._silence_send = self._silence

    def set_send_rate(self, send_rate: int) -> None:
        """Atomically retarget future PCM frames to a replacement engine."""
        send_rate = int(send_rate)
        if send_rate not in (16000, 24000):
            raise ValueError(f"unsupported translator input rate: {send_rate}")
        with self._lock:
            if send_rate == self._send_rate:
                return
            self._configure_send_rate(send_rate)

    def feed(self, chunk: np.ndarray):
        if self.closed:
            return
        # Classic loopback path zeros the input while TTS is playing so the
        # translator never re-translates its own output. Sending silence (not
        # nothing) preserves stream continuity.
        if self._suppress_when is not None and self._suppress_when():
            chunk = np.zeros_like(chunk)
        with self._lock:
            self._acc.push(self._resample(chunk))
            if self._resample_send is not None:
                self._acc_send.push(self._resample_send(chunk))
            while self._acc.n >= _FRAME:
                frame = self._acc.pop(_FRAME)
                # Pull the time-aligned full-band frame into history (lockstep with
                # the 16 kHz VAD frame: 768 @24k <-> 512 @16k = same 32 ms window).
                if self._resample_send is not None and self._acc_send.n >= self._send_frame:
                    self._hist_send.append(self._acc_send.pop(self._send_frame))
                active, to_send = self._gate.process(frame)
                self.speech_active = active
                if self._speech_tap is not None and to_send:
                    try:
                        self._speech_tap(to_send)
                    except Exception:
                        pass  # labeling is best-effort; never break capture
                if self._smart:
                    if to_send:
                        self._silent_run = 0
                        self._emit(to_send)
                    elif self._silent_run < _SMART_SILENCE_MAX_FRAMES:
                        # Pad short gaps to keep the model's stream continuous,
                        # but stop once the gap is genuine so the queue drains
                        # and rotation can fire on the quiet window.
                        self._silent_run += 1
                        self._emit_silence()
                elif self._always_send:
                    self._emit([frame])
                else:
                    self._emit(to_send)

    def _emit(self, frames16):
        """Forward the gate's decided frames at the engine's send rate. For the
        16 kHz path that's the frames themselves; for a higher send rate it's the
        time-aligned full-band frames from history (same count = same window)."""
        if not frames16:
            return
        if self._resample_send is None:
            for f in frames16:
                self._send(_f32_to_pcm16(f))
            return
        k = len(frames16)
        sframes = list(self._hist_send)[-k:]
        if len(sframes) == k:
            for sf in sframes:
                self._send(_f32_to_pcm16(sf))
        else:
            # Startup transient before history is deep enough — upsample so no
            # audio is dropped (first few frames only).
            xo = getattr(self, "_xo", None)
            xn = getattr(self, "_xn", None)
            if xo is None or xn is None:
                xo = np.linspace(0.0, 1.0, num=_FRAME, endpoint=False)
                xn = np.linspace(0.0, 1.0, num=self._send_frame, endpoint=False)
            for f in frames16:
                self._send(_f32_to_pcm16(np.interp(xn, xo, f).astype(np.float32)))

    def _emit_silence(self):
        self._send(self._silence_send if self._resample_send is not None else self._silence)


class IncomingPipeline:
    def __init__(self, cfg: dict, resolve, mode: str, on_text, on_status,
                 session_dir: str | None = None, on_speaker=None):
        # Default the premium flag before either capture branch so callers and
        # teardown can always read it, even on the driverless path that never
        # probes premium hardware.
        self._use_premium = False
        self._sducker = None
        self._routing_handle = None
        self.capture = None
        self._source = None
        self._recorder = None
        self.player = None
        self.translator = None
        self._stager = None
        self._mode = mode
        self._spk_tracker = None
        # Per-session output folder (transcript + WAVs share it); None → the
        # recorder falls back to the flat transcripts root.
        self._session_dir = session_dir
        vad_cfg = gate_params(cfg)
        # Playback target: fall back to the system default (with a warning) when
        # the configured device is absent — a config carried over from another
        # machine, or a renamed/unplugged endpoint, must not hard-fail the whole
        # session. The CABLE feedback guard below still rejects a bad default.
        out_dev = find_device(cfg["devices"]["headphones_output"], "output",
                              on_status=on_status, fallback_default=True)
        out_name = resolve_name(out_dev, "output")
        if "CABLE" in out_name or "VB-Audio" in out_name:
            raise ValueError(
                f"Translation output is routed to a virtual cable ({out_name}). "
                "This creates a feedback loop. Set devices.headphones_output in "
                "config.json to the real headphone device (list devices with: "
                "python -m app.audio_io)."
            )

        self.player = Player(
            out_dev, tts_in_rate=24000,
            max_ambient_delay_ms=float(cfg.get("max_ambient_delay_ms", 400)),
            # Premium voice-enhancement runs on every official path (incl.
            # driverless, where the spatial split never fires). Absent on the OSS
            # build (_premium is None), so the TTS stream is untouched there.
            tts_enhance=(_premium.enhance_tts if _premium is not None else None),
        )
        self.player.tts_gain = float(cfg.get("tts_volume", 1.0))
        self._rtt = RTTEstimator(self.player.rate)
        # UI telemetry: smoothed input level (0..1) and speech-onset edge tracking.
        self.input_level = 0.0
        self._prev_active = False

        # Optional speaker-change tracker (incoming direction only). Construct it
        # after Player succeeds so an earlier device-resolution failure cannot
        # orphan the tracker's worker thread.
        if on_speaker is not None and cfg.get("speaker_labels", True):
            try:
                from .speaker_id import SpeakerTracker  # noqa: PLC0415
                self._spk_tracker = SpeakerTracker(on_change=on_speaker)
            except Exception:
                _log.exception("speaker tracker init failed — labels disabled")

        # Resolve engine + key + model for this target ONCE (locally for BYOK,
        # server-side for SaaS) so the capture send-rate matches the engine.
        try:
            self._engine, _key, _model = resolve(cfg["target_language_incoming"])
        except Exception:
            self._teardown_resources()
            raise
        # Kept for the mid-session engine substitution below.
        self.cfg = cfg
        self._resolve = resolve
        self._on_status = on_status
        self._on_text = on_text
        self._failover_done = False

        # Gemini and Qwen can deliver a long translated turn faster than
        # realtime. Pace their already-generated audio through a client-side
        # WSOLA stager so an accumulating playback tail catches up to the live
        # captions without raising the voice's pitch. OpenAI self-times its
        # output; cascade controls speed while synthesizing locally.
        if self._engine in (ENGINE_GEMINI, ENGINE_QWEN):
            try:
                self._stager = AdaptivePlaybackStager(
                    self.player, on_status=on_status, input_rate=24000)
            except Exception:
                self._teardown_resources()
                raise

        # While the free-voice preview speaks, the paid voice stands down: two
        # voices over one another would demo nothing. Captions keep flowing and
        # the recorder keeps its faithful copy of what the engine produced —
        # only playback is withheld, and only for the length of the clip.
        self._preview_mute = False
        # A few seconds of the paid voice, kept so it can be replayed back-to-back
        # against the free voice AFTER the session — which is the only moment the
        # user is reliably looking at Voxis rather than at what they came to watch.
        # Bounded and audible-only: silence would replay as a dead button.
        self._pro_ring: collections.deque = collections.deque()
        self._pro_ring_bytes = 0

        # Audio-caption synchronization: buffer text tokens until the first audible
        # TTS chunk of a turn arrives so caption text and audio pop up in perfect sync,
        # eliminating perceived voice-lag.
        self._text_buffer = []
        self._text_lock = threading.Lock()
        self._audio_started = False
        self._flush_timer = None

        def _flush_text_buffer():
            with self._text_lock:
                items = list(self._text_buffer)
                self._text_buffer.clear()
                self._audio_started = True
                if self._flush_timer:
                    try:
                        self._flush_timer.cancel()
                    except Exception:
                        pass
                    self._flush_timer = None
            for txt, nl, spk_tag in items:
                on_text(txt, nl, spk_tag)

        def _synced_on_text(text, newline=False, spk=None):
            if newline:
                with self._text_lock:
                    self._audio_started = False
            with self._text_lock:
                if self._audio_started:
                    on_text(text, newline, spk)
                    return
                self._text_buffer.append((text, newline, spk))
                if not self._flush_timer:
                    self._flush_timer = threading.Timer(0.22, _flush_text_buffer)
                    self._flush_timer.daemon = True
                    self._flush_timer.start()

        def _tts_sink(data: bytes):
            # Gate the first-audio metric on AUDIBLE output: an initial near-silent
            # / padding chunk (OpenAI pads its stream) would understate latency.
            a = np.frombuffer(data, dtype=np.int16)
            audible = a.size > 0 and int(np.abs(a).max()) > 512
            if audible:
                _flush_text_buffer()
            self._rtt.mark_tts(audible=audible)
            if self._recorder is not None:
                self._recorder.feed_translated(data)
            if self._preview_mute:
                return          # not heard → not part of "what you just heard"
            if audible and self._engine != ENGINE_CASCADE:
                # The A/B card labels this ring "Pro voice". A cascade session
                # flows through the same sink — record its Piper audio here and
                # the comparison would play the free voice under a Pro label.
                self._keep_pro_audio(data)
            if self._stager is not None:
                self._stager.feed(data)
            else:
                self.player.feed_tts_pcm16(data)
        self._tts_sink = _tts_sink
        try:
            self.translator = make_translator(
                cfg, cfg["target_language_incoming"], engine=self._engine, key=_key,
                model=_model, on_audio=_tts_sink, on_text=_synced_on_text,
                on_status=on_status, name=t("name_in"),
                on_fatal=self._failover_to_gemini,
                # SaaS resolvers hang the Gemini key fountain off the resolve fn so
                # a single-use ephemeral key can be refreshed on every rotation
                # (dev/BYOK resolvers carry none — raw keys need no refetch).
                key_provider=getattr(resolve, "gemini_key_provider", None),
                # Qwen voice-cloning only when this is a real beta session (webui
                # sets it on the beta resolver); off on the standard Qwen route.
                beta_active=getattr(resolve, "beta_active", False),
            )
        except Exception:
            self._teardown_resources()
            raise
        # Bound through self, not to the translator instance, so a mid-session
        # engine swap redirects the capture without rebuilding the gate/capture.
        def send_fn(pcm: bytes):
            self.translator.send_pcm16(pcm)

        self._original_mode = cfg["original_audio"]
        self._duck_gain = cfg["duck_gain"]
        self.driverless = cfg.get("capture_backend", "driverless") != "vbcable"

        # Acquire OS resources (COM/pycaw ducker, capture, gate) under one guard:
        # a mid-init failure on any line must release the ones already held, or a
        # rejected start() leaks a SessionDucker COM handle or an open WASAPI
        # capture that keeps the device busy on the next retry.
        try:
            self._acquire_capture(cfg, vad_cfg, send_fn, out_name, on_status)
        except Exception:
            self._teardown_resources()
            raise

    def _failover_to_gemini(self, exc) -> bool:
        return _swap_to_gemini(self, "target_language_incoming", t("name_in"), exc)

    def _stream_is_gated(self, cfg) -> bool:
        """Cascade (free tier) ALWAYS streams gated — cloud input tokens are its
        whole bill, and omitting silence roughly halves them on film content.
        Paid engines keep the preset's choice (Saver gated, the rest smart)."""
        return stream_gated(cfg) or self._engine == ENGINE_CASCADE

    def _ingest_input(self, mono: np.ndarray) -> None:
        """Account for raw captured audio before optional consumers process it.

        The UI meter is capture telemetry, not VAD/translator telemetry. Keep it
        truthful even when recording or speech processing raises; the capture
        worker reports that downstream failure through its liveness channel.
        """
        self.input_level = _rms_level(self.input_level, mono)
        if self._recorder is not None:
            self._recorder.feed_source(mono)
        self._source.feed(mono)

    def _acquire_capture(self, cfg, vad_cfg, send_fn, out_name, on_status):
        from .vad import SpeechGate  # noqa: PLC0415
        # OpenAI ingests 24 kHz; Gemini 16 kHz. Capture + the gate's send path run
        # at this rate so OpenAI gets full-band audio; the VAD stays 16 kHz inside.
        send_rate = 24000 if self._engine == ENGINE_OPENAI else 16000
        if self.driverless:
            # Driverless mode: system audio loopback; ducking is applied so the
            # user hears a real dubbing effect without any virtual-cable
            # install. Windows: WASAPI loopback + per-app session-volume API.
            # Linux: Option-A routing (VoxisCapture + ducked loopback) --
            # make_capture_routing is a no-op returning None on Windows, so
            # this line changes nothing there.
            self._routing_handle = sysaudio.make_capture_routing()
            self._sducker = sysaudio.make_ducker(routing_handle=self._routing_handle)

            def on_loop(chunk: np.ndarray):
                lvl = self._sducker.current
                # Windows' ducker attenuates the SAME mix its loopback then
                # captures, so the level must be compensated back up for
                # VAD/Gemini. Linux's capture point sits upstream of the duck
                # (see ducking.LinuxSessionDucker.duck_affects_capture) so it
                # is never tainted in the first place -- compensating it too
                # would wrongly amplify already-full-level audio.
                if getattr(self._sducker, "duck_affects_capture", True) and lvl < 0.95:
                    chunk = np.clip(chunk / max(lvl, 0.15), -1.0, 1.0)
                # Observe the raw capture before VAD/translator work. A consumer
                # fault must not make a healthy WASAPI signal look like −∞ dB;
                # the capture liveness path reports that downstream fault apart.
                self._ingest_input(chunk)
                # Mark speech onset here too (the spatial path does it in vbcable
                # mode) so the ear-voice latency readout populates in driverless.
                active = self._source.speech_active
                if active and not self._prev_active:
                    self._rtt.mark_first_onset()  # session first-speech (no guard)
                    if not self.player.tts_active:
                        self._rtt.mark_onset()
                self._prev_active = active
                speaking = self._source.speech_active or self.player.tts_active
                if self._original_mode == "mix":
                    self._sducker.target = 1.0
                elif self._original_mode == "mute_during_speech":
                    self._sducker.target = 0.0 if speaking else 1.0
                else:
                    self._sducker.target = self._duck_gain if speaking else 1.0

            suppress = None
            # Preferred path on Win10 2004+: process-exclude loopback. Our own TTS
            # is excluded at the hardware level so the translator keeps receiving
            # input even while playback is active. Activation can fail TRANSIENTLY
            # (COM race / the 5 s activation timeout when the audio service is busy
            # at startup); that failure would drop the session onto the classic
            # loopback path, whose echo-suppress zeroes the input while TTS plays —
            # on continuous speech that silences everything after the first
            # utterance. Retry a few times before accepting the degraded fallback so
            # a transient hiccup doesn't break the whole session. A genuinely
            # unsupported OS fails fast (immediate HRESULT, no 5 s wait), so the
            # retries cost little there.
            self.capture = None
            pe_err = None
            for attempt in range(3):
                try:
                    self.capture = sysaudio.make_process_loopback(
                        on_loop, rate=send_rate, routing_handle=self._routing_handle)
                    pe_err = None
                    break
                except Exception as e:
                    pe_err = e
                    self.capture = None
                    if attempt < 2:
                        time.sleep(0.6)
            if self.capture is None:
                # Genuinely unavailable after retries — fall back, but surface a
                # clear, actionable warning (impact + remedy) instead of a silent
                # half-working session. The error rides along for field diagnosis.
                on_status(t("st_classic_capture_warning", e=pe_err))
                self.capture = sysaudio.make_loopback_capture(
                    on_loop, prefer_name=out_name, on_status=on_status)
                def suppress():
                    return self.player.tts_active
            self._source = _GatedSource(
                self.capture.rate, SpeechGate(**vad_cfg), send_fn,
                suppress_when=suppress, smart=not self._stream_is_gated(cfg),
                send_rate=send_rate,
                speech_tap=self._spk_tracker.feed if self._spk_tracker else None,
            )
        else:
            # VB-CABLE: audio is intercepted before reaching the speakers. The
            # capture is stereo and we apply M/S center-suppression so the
            # original dialogue (Mid) is ducked while stereo music/SFX (Side)
            # is preserved. The TTS sits in the phantom center.
            cap_dev = find_device(cfg["devices"]["system_capture"], "input")
            self._use_premium = _premium is not None and _premium.hardware_ok()
            self._speak = 0.0
            self._prev_active = False

            def on_chunk(chunk: np.ndarray):
                mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
                self._ingest_input(mono)
                active = self._source.speech_active
                if active and not self._prev_active:
                    self._rtt.mark_first_onset()  # session first-speech (no guard)
                    if not self.player.tts_active:
                        self._rtt.mark_onset()
                self._prev_active = active
                self.player.delay_target_samples = self._rtt.target_samples()
                speaking = self._source.speech_active or self.player.tts_active
                if self._use_premium:
                    processed = _premium.execute_vocal_split(chunk, speaking)
                    self.player.feed_passthrough(processed)
                else:
                    # Smooth ramp (~80 ms) so the Mid-gain transition does not click.
                    self._speak += ((1.0 if speaking else 0.0) - self._speak) * 0.25
                    if self._original_mode == "mix":
                        mid = 1.0
                    elif self._original_mode == "mute_during_speech":
                        mid = 1.0 - self._speak
                    else:
                        mid = 1.0 - self._speak * (1.0 - self._duck_gain)
                    self.player.mid_gain = float(mid)
                    self.player.feed_passthrough(chunk)

            self.capture = Capture(cap_dev, on_chunk, stereo=True)
            # Match the ambient resampler to the capture device's true rate so the
            # M/S passthrough never clicks on a capture/playback clock mismatch.
            self.player.configure_passthrough(self.capture.rate)
            self._source = _GatedSource(
                self.capture.rate, SpeechGate(**vad_cfg), send_fn,
                smart=not self._stream_is_gated(cfg), send_rate=send_rate,
                speech_tap=self._spk_tracker.feed if self._spk_tracker else None,
            )

        # One-shot capture diagnostic (rendered in the transcript, so a field
        # report — "translation stops after the first line" — can be mapped to the
        # path that produced it without a repro). The two paths that can starve the
        # model on continuous speech are the classic-loopback echo-suppress
        # (suppress=True zeroes input while TTS plays) and original=mute_during_speech.
        on_status(
            "capture: backend=%s mode=%s suppress=%s smart=%s original=%s duck=%.2f engine=%s rate=%d"
            % (
                type(self.capture).__name__,
                "vbcable" if not self.driverless else "driverless",
                self._source._suppress_when is not None,
                self._source._smart,
                cfg.get("original_audio"),
                float(cfg.get("duck_gain", 0.0)),
                self._engine,
                send_rate,
            )
        )

        # Opt-in dual-track recorder (default OFF). Created here so the source WAV
        # rate matches the capture's true rate (self.capture.rate — the rate the
        # source-tap chunks are delivered at, on both the driverless and vbcable
        # paths). Any failure is non-fatal: a broken recorder must never stop a
        # translation session.
        # Recording is a VIDEO/GAME-mode capability only. In meeting mode the
        # source track is another person's live voice, which recording without
        # their consent is legally fraught (all-party-consent jurisdictions), so
        # we never record it — regardless of the opt-in toggle. The toggle's UI
        # hint states this; here we just skip + log so the behavior is traceable.
        if cfg.get("record_audio") and self._mode == "meeting":
            _log.info("audio recording skipped in meeting mode (two-party consent)")
        if cfg.get("record_audio") and self._mode != "meeting":
            try:
                from .audio_recorder import DualTrackRecorder  # noqa: PLC0415
                from . import paths  # noqa: PLC0415
                # Write into this session's own folder (so the WAVs sit beside the
                # transcript JSON and share its stamp); fall back to the flat root
                # when no session folder was supplied (non-webui callers).
                out_dir = self._session_dir or paths.transcripts_dir(cfg)
                stamp = None
                if self._session_dir:
                    base = os.path.basename(self._session_dir.rstrip("\\/"))
                    stamp = base[len("voxis_"):] if base.startswith("voxis_") else base
                self._recorder = DualTrackRecorder(
                    out_dir, source_rate=self.capture.rate,
                    tag=self._mode or "video", stamp=stamp, on_status=on_status)
            except Exception:
                self._recorder = None
                _log.exception("audio recorder init failed — continuing without it")

    def _teardown_resources(self):
        """Release whatever resources were acquired. Safe to call with partial
        init: each handle is closed independently and best-effort. Includes the
        Player and translator (both opened before the capture guard) so an open
        OutputStream / Live socket does not leak when init fails mid-way."""
        if self._source is not None:
            self._source.closed = True
        cap = self.capture
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass
        duck = self._sducker
        if duck is not None:
            try:
                duck.close()
            except Exception:
                pass
            self._sducker = None
        handle = self._routing_handle
        if handle is not None:
            try:
                sysaudio.teardown_capture_routing(handle)
            except Exception:
                pass
            self._routing_handle = None
        for attr in ("_stager", "player", "translator", "_recorder", "_spk_tracker"):
            comp = getattr(self, attr, None)
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass

    # ~8 s of paid voice at 24 kHz PCM16 — long enough to recognise a voice,
    # short enough that the ring is never a memory concern.
    PRO_RING_BYTES = 8 * 24000 * 2

    def _keep_pro_audio(self, data: bytes):
        self._pro_ring.append(data)
        self._pro_ring_bytes += len(data)
        while self._pro_ring_bytes > self.PRO_RING_BYTES and len(self._pro_ring) > 1:
            self._pro_ring_bytes -= len(self._pro_ring.popleft())

    def recent_pro_pcm(self) -> bytes:
        """The last few seconds the paid voice actually SPOKE. Survives stop() on
        purpose: the A/B card is offered once the user comes back to the window."""
        return b"".join(self._pro_ring)

    def play_free_preview(self, pcm: bytes, seconds: float):
        """Speak `pcm` (the free tier's voice, same 24 kHz PCM16 contract) in place
        of the paid voice, then hand the paid voice back. The clip is dropped into
        the live Player, so it lands on the same device, gain and limiter the user
        is already listening to — the comparison is honest only if nothing else
        about the path changes. Best-effort: a timer failure must not strand the
        paid voice muted, so the unmute is also attempted on stop()."""
        self._preview_mute = True
        try:
            stager = getattr(self, "_stager", None)
            if stager is not None:
                stager.clear()            # drop paid audio not yet handed to Player
            self.player.clear_tts()      # drop paid audio already queued
            self.player.feed_tts_pcm16(pcm)
        except Exception:
            self._preview_mute = False
            raise
        # +0.4 s so the ring finishes draining before the paid voice resumes.
        t = threading.Timer(max(0.5, seconds) + 0.4, self._end_free_preview)
        t.daemon = True
        t.start()

    def _end_free_preview(self):
        self._preview_mute = False

    @staticmethod
    def capture_rate(dev) -> int:
        return audio_io.device_rate(dev, "input")

    def start_translator(self):
        self.translator.start()

    def start_io(self):
        # Warm the Live socket BEFORE capturing audio so the first utterance
        # does not stack the cold WS/TLS/setup handshake on top of translation.
        if not self.translator.wait_ready(timeout=6):
            raise RuntimeError(t("st_server_unreachable"))
        self.player.start()
        self.capture.start()

    def start(self):
        self.start_translator()
        self.start_io()

    def stop(self):
        # A preview timer that never fired must not leave the next session's paid
        # voice muted — this object is torn down, but clear the flag regardless.
        self._preview_mute = False
        _stop_all(self)
        d = getattr(self, "_sducker", None)
        if d is not None:
            d.close()
        handle = getattr(self, "_routing_handle", None)
        if handle is not None:
            try:
                sysaudio.teardown_capture_routing(handle)
            except Exception:
                pass
            self._routing_handle = None


def _swap_to_gemini(pipe, target_key, name, exc):
    """Shared mid-session engine substitution for both directions.

    A spent DashScope balance surfaces as a terminal 'arrearage'/'quota' reject.
    The server cannot see it — the Qwen key is still configured, so every voiced
    target keeps routing to Qwen — which would take the whole 29-language tier
    dark, one dead session at a time. Gemini is the 79-language catch-all, so it
    can serve whatever Qwen was serving.

    Runs on the dying translator's thread (BaseTranslator._give_up), off the audio
    path. True = handled, and the failure stays silent for the user. Never
    retried: Gemini is the last resort, so a second failure is a real outage and
    must surface."""
    if pipe._failover_done or pipe._engine == ENGINE_GEMINI:
        return False
    pipe._failover_done = True

    target = pipe.cfg[target_key]
    try:
        engine, key, model = pipe._resolve(target, force_gemini=True)
    except Exception:
        _log.exception("failover: could not obtain a Gemini key")
        return False
    if engine != ENGINE_GEMINI or not key:
        return False

    _log.warning("engine %s gave up (%s) — failing over to Gemini", pipe._engine, exc)
    old = pipe.translator
    try:
        new = make_translator(
            pipe.cfg, target, engine=ENGINE_GEMINI, key=key, model=model,
            on_audio=pipe._tts_sink, on_text=pipe._on_text,
            on_status=pipe._on_status, name=name,
            # No on_fatal: Gemini is the last resort — a further failure must
            # reach the user instead of looping. The failover key comes from the
            # legacy (no-caps) endpoint and is always raw, so the provider is
            # passed only for symmetry — a raw key never consults it.
            key_provider=getattr(pipe._resolve, "gemini_key_provider", None),
        )
    except Exception:
        _log.exception("failover: could not build the Gemini translator")
        return False

    # Gemini labels its input as 16 kHz. OpenAI captures at 24 kHz, so swapping
    # only the translator would make Gemini play the source 1.5x slow. Retarget
    # the gate before publishing the replacement translator. Qwen is already
    # 16 kHz and this is therefore a no-op on the common failover path.
    source = getattr(pipe, "_source", None)
    if source is not None:
        try:
            source.set_send_rate(16000)
        except Exception:
            try:
                new.stop()
            except Exception:
                pass
            _log.exception("failover: could not switch capture to Gemini input rate")
            return False

    # Both Qwen and Gemini use the incoming adaptive playback stager. Clear its
    # provider-side pending audio at the boundary, but keep the worker alive so
    # the replacement Gemini stream gets the same catch-up behavior. Outgoing
    # pipelines intentionally have no stager.
    stager = getattr(pipe, "_stager", None)
    if stager is not None:
        try:
            stager.clear()
        except Exception:
            pass

    # And drop whatever the dead engine had already buffered — see _swap_to_cascade.
    # Qwen queues seconds ahead of the source, so without this the user goes on
    # hearing the engine that just died, reading sentences the captions have long
    # passed, while the replacement waits its turn behind the backlog.
    for player in (getattr(pipe, "player", None),
                   getattr(pipe, "monitor_player", None)):
        if player is not None:
            try:
                player.clear_tts()
            except Exception:
                pass

    from_engine = pipe._engine
    pipe._engine = ENGINE_GEMINI
    if target_key == "target_language_incoming" and stager is None:
        # An incoming OpenAI -> Gemini failover starts without a stager because
        # OpenAI paces its own stream. Install one before Gemini can emit audio.
        pipe._stager = AdaptivePlaybackStager(
            pipe.player, on_status=pipe._on_status, input_rate=24000)
    pipe.translator = new   # the send path indirects through pipe.translator
    new.start()
    try:
        old.stop()
    except Exception:
        pass
    pipe._on_status(t("st_engine_failover"))
    # Tell the server which engine died: the server cannot see a spent
    # DashScope balance itself (the key stays configured), so its routing
    # watcher counts these events and flips qwen_enabled off for everyone
    # once several licenses report the same dead engine.
    voxis_client.report_event_async("engine_failover", None, {
        "from": from_engine, "reason": type(exc).__name__ if exc else "",
    })
    return True


class OutgoingPipeline:
    def __init__(self, cfg: dict, resolve, on_text, on_status):
        from .vad import SpeechGate  # noqa: PLC0415
        vad_cfg = gate_params(cfg)
        mic_dev = find_device(cfg["devices"]["microphone"], "input")

        # Windows: Player targets VB-CABLE's real Input device directly (a
        # separately-addressable PortAudio device). Linux has no such
        # pre-existing virtual cable -- make_virtual_mic() creates the
        # "VoxisMic" null-sink there and returns None on Windows, where this
        # whole branch is skipped and the existing cable_out path runs
        # unchanged below. See sysaudio/linux/virtual_mic.py for why the
        # stream is pinned via an explicit one-time move rather than any
        # default-source switch (confirmed empirically: changing the default
        # source drags an already-open capture stream exactly like Faz 3's
        # sink-side finding, so it must never be touched).
        self.monitor_player = None
        self.player = None
        self.translator = None
        self.capture = None
        self._source = None
        try:
            self._mic_handle = sysaudio.make_virtual_mic()
            before_streams = sysaudio.snapshot_own_audio_streams()
            if self._mic_handle is not None:
                self.player = Player(
                    None, channels=1,
                    tts_enhance=(_premium.enhance_tts
                                 if _premium is not None else None),
                )
                moved = sysaudio.pin_newest_own_stream_to_mic(
                    before_streams, self._mic_handle)
                if moved is None:
                    raise RuntimeError(
                        "could not pin the outgoing TTS stream onto the virtual mic")
            else:
                cable_out = find_device(
                    cfg["devices"]["meeting_mic_playback"], "output")
                self.player = Player(
                    cable_out, channels=1,
                    tts_enhance=(_premium.enhance_tts
                                 if _premium is not None else None),
                )
        except Exception:
            self._teardown_resources()
            raise
        # Optional confidence monitor: the primary Player remains pinned to the
        # virtual microphone while a second Player renders the identical PCM to
        # the user's selected headphones. Construct it only after the Linux pin
        # above, otherwise the monitor could be mistaken for the new virtual-mic
        # stream. Failure is soft: the call still receives the translation.
        if cfg.get("monitor_outgoing_translation"):
            monitor = None
            try:
                monitor_out = find_device(
                    cfg["devices"].get("headphones_output", ""), "output",
                    on_status=on_status, fallback_default=True)
                monitor = Player(
                    monitor_out,
                    tts_enhance=(_premium.enhance_tts
                                 if _premium is not None else None),
                )
                monitor.tts_gain = float(cfg.get("tts_volume", 1.0))
                self.monitor_player = monitor
            except Exception:
                if monitor is not None:
                    try:
                        monitor.stop()
                    except Exception:
                        pass
                self.monitor_player = None
                _log.exception("outgoing confidence monitor unavailable")
        try:
            self._engine, _key, _model = resolve(cfg["target_language_outgoing"])
        except Exception:
            self._teardown_resources()
            raise
        self.cfg = cfg
        self._resolve = resolve
        self._on_text = on_text
        self._on_status = on_status
        self._failover_done = False
        # Outgoing feeds the virtual mic directly — no SyncStager on this leg.
        self._tts_sink = self._feed_translated_audio
        try:
            self.translator = make_translator(
                cfg, cfg["target_language_outgoing"], engine=self._engine, key=_key,
                model=_model, on_audio=self._tts_sink, on_text=on_text,
                on_status=on_status, name=t("name_out"),
                noise_reduction="near_field", on_fatal=self._failover_to_gemini,
                key_provider=getattr(resolve, "gemini_key_provider", None),
                beta_active=getattr(resolve, "beta_active", False),
            )
        except Exception:
            self._teardown_resources()
            raise
        self.input_level = 0.0
        send_rate = 24000 if self._engine == ENGINE_OPENAI else 16000
        # Capture opens the mic InputStream at construction. Guard the capture +
        # gate acquisition so a mid-init failure releases the mic stream and the
        # already-open Player/translator instead of leaking them when
        # ModeController.start discards the half-built pipeline.
        try:
            self.capture = Capture(mic_dev, self._on_mic)
            self._source = _GatedSource(
                # Indirect through self so a mid-session engine swap
                # (_failover_to_gemini) redirects the mic without rebuilding it.
                self.capture.rate, SpeechGate(**vad_cfg),
                lambda pcm: self.translator.send_pcm16(pcm),
                smart=not stream_gated(cfg), send_rate=send_rate,
            )
        except Exception:
            self._teardown_resources()
            raise

    def _teardown_resources(self):
        """Best-effort release of whatever was acquired; safe with partial init."""
        if self._source is not None:
            self._source.closed = True
        for comp in (self.capture, getattr(self, "player", None),
                     getattr(self, "monitor_player", None),
                     getattr(self, "translator", None)):
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass
        handle = getattr(self, "_mic_handle", None)
        if handle is not None:
            try:
                sysaudio.teardown_virtual_mic(handle)
            except Exception:
                pass
            self._mic_handle = None

    def _on_mic(self, c: np.ndarray):
        mono = c.mean(axis=1) if getattr(c, "ndim", 1) > 1 else c
        self.input_level = _rms_level(self.input_level, mono)
        self._source.feed(c)

    def _feed_translated_audio(self, data: bytes) -> None:
        """Send outgoing translation to the call and optional local monitor."""
        self.player.feed_tts_pcm16(data)
        if self.monitor_player is not None:
            self.monitor_player.feed_tts_pcm16(data)

    def start_translator(self):
        self.translator.start()

    def start_io(self):
        if not self.translator.wait_ready(timeout=6):
            raise RuntimeError(t("st_server_unreachable"))
        self.player.start()
        if self.monitor_player is not None:
            try:
                self.monitor_player.start()
            except Exception:
                _log.exception("could not start outgoing confidence monitor")
                try:
                    self.monitor_player.stop()
                except Exception:
                    pass
                self.monitor_player = None
        self.capture.start()

    def start(self):
        self.start_translator()
        self.start_io()

    def _failover_to_gemini(self, exc) -> bool:
        return _swap_to_gemini(self, "target_language_outgoing", t("name_out"), exc)

    def stop(self):
        _stop_all(self)
        handle = getattr(self, "_mic_handle", None)
        if handle is not None:
            try:
                sysaudio.teardown_virtual_mic(handle)
            except Exception:
                pass
            self._mic_handle = None


def _stop_all(pipeline):
    """Stops all components independently so a failure in one does not block the
    others. Components may be None if init failed mid-way before acquiring them."""
    src = getattr(pipeline, "_source", None)
    if src is not None:
        src.closed = True
    fns = []
    for attr in ("capture", "_stager", "player", "monitor_player", "translator",
                 "_recorder", "_spk_tracker"):
        comp = getattr(pipeline, attr, None)
        if comp is not None:
            fns.append(comp.stop)
    for fn in fns:
        try:
            fn()
        except Exception:
            pass
    # Join the translator thread so its receiver cannot deliver a late
    # on_text/on_audio into the NEXT session's freshly-cleared transcript
    # buffers (the 'ghost turn' desync — audit P2-4/P2-7). stop() already set
    # _stopping and nudged the event loop, so the sender wakes within ~0.5 s and
    # the thread converges well inside this bound; the timeout only guards the
    # rare mid-backoff case (during which no receiver is emitting anyway).
    tr = getattr(pipeline, "translator", None)
    if tr is not None and hasattr(tr, "join") and hasattr(tr, "is_alive"):
        try:
            if tr.is_alive():
                tr.join(timeout=2.0)
        except Exception:
            pass


def _event_error_class(exc: Exception) -> str:
    """Coarse, PII-free label for a session-start failure, for the activation
    funnel. Never carries the raw message (which may hold device names/paths)."""
    msg = str(exc)
    if "-9999" in msg or "PaError" in msg or "host error" in msg or "host API" in msg:
        return "audio_device"
    if isinstance(exc, (ImportError, OSError)):
        return "os_import"
    low = msg.lower()
    if "timeout" in low or "ready" in low:
        return "translator_timeout"
    return "other"


class ModeController:
    """Single-active-mode controller (video | meeting). start() implicitly stops
    any current session first."""

    # Usage report interval. Smaller = less time lost when the process is
    # killed abruptly (Ctrl+C, crash, network drop).
    HEARTBEAT_SECONDS: float = 6.0

    def __init__(self, cfg: dict, api_key: str | None, on_text, on_status,
                 on_usage_reported=None, on_quota_exceeded=None,
                 on_session_failed=None, on_speaker=None):
        self.cfg = cfg
        self.api_key = api_key       # legacy field; key resolution now via self.resolve
        self.resolve = None          # callable(target)->(engine, key, model); set by the bridge
        self.on_text = on_text
        self.on_status = on_status
        # Speaker-change events (incoming direction) from the local tracker;
        # None disables labeling entirely.
        self.on_speaker = on_speaker
        self.on_usage_reported = on_usage_reported or (lambda: None)
        # Fired (at most once per session) when the server reports the license is
        # exhausted via a 402 on /usage/report. The server cannot stop a running
        # Live session, so the bridge wires this to its session teardown.
        self.on_quota_exceeded = on_quota_exceeded or (lambda: None)
        # Fired (at most once per session) when a translator thread dies
        # unexpectedly mid-session. Billing already stops via _is_session_live,
        # but capture/ducking/endpoint redirection stay live — the bridge wires
        # this to tear the session down.
        self.on_session_failed = on_session_failed or (lambda: None)
        self._quota_exhausted = threading.Event()
        self._session_failed = threading.Event()
        self._capture_dead_notified = False
        self._pipelines: list = []
        # Survives the session so the post-session A/B card can replay it.
        self._last_pro_pcm: bytes = b""
        self.mode: str | None = None
        self._session_id: str | None = None
        self._session_start: float | None = None
        self._session_mode: str | None = None
        # _last_report is the watermark for "minutes already billed". Every read
        # AND write goes through _heartbeat_lock so the periodic heartbeat and
        # the stop() tail can never both consume the same interval (double-count).
        self._last_report: float | None = None
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def _switch_defaults(self, mode: str):
        """Swap Windows default endpoints when required and remember the prior
        selection so stop() can restore it.

        Driverless mode leaves the output device untouched (the user keeps
        hearing audio on their normal device; loopback reads it). Meeting mode
        still flips the default microphone to the virtual cable since the
        outbound direction needs a driver."""
        # Windows-only default-endpoint switching (vbcable/meeting mode); no-op
        # elsewhere. Deliberately NOT gated on is_supported() -- Linux's Faz 3
        # driverless-equivalent path is supported without this (see
        # sysaudio.supports_endpoints), and never touches the default sink.
        if not sysaudio.supports_endpoints():
            return
        win_audio = sysaudio.endpoints()
        from .config import save_config

        driverless = self.cfg.get("capture_backend", "driverless") != "vbcable"
        saved = self.cfg.get("_pending_default_restore") or {}

        if not driverless:
            if "output" not in saved:
                out_id, out_name = win_audio.get_default("output")
                if "CABLE" in out_name or "VB-Audio" in out_name:
                    # Only resolve when a real device name is configured; an empty
                    # headphones_output must not be matched to an arbitrary endpoint
                    # (find_endpoint_id now rejects blanks), so keep the current id.
                    hp = (self.cfg.get("devices", {}).get("headphones_output") or "").strip()
                    if hp:
                        out_id = win_audio.find_endpoint_id(hp)
                saved["output"] = out_id
            win_audio.set_default(
                win_audio.find_endpoint_id("CABLE Input (VB-Audio Virtual Cable)"))
            self.on_status(t("st_redirected"))

        if mode == "meeting" and getattr(self, "_outgoing_ok", False):
            try:
                if "input" not in saved:
                    in_id, in_name = win_audio.get_default("input")
                    if not ("CABLE" in in_name or "VB-Audio" in in_name):
                        saved["input"] = in_id
                win_audio.set_default(
                    win_audio.find_endpoint_id(self.cfg["devices"]["meeting_virtual_mic"]))
            except Exception:
                self.on_status(t("st_virtual_mic_missing",
                                 dev=self.cfg["devices"]["meeting_virtual_mic"]))

        if saved:
            self.cfg["_pending_default_restore"] = saved
            save_config(self.cfg)

    def _restore_defaults(self):
        # Windows-only endpoint restore; no-op elsewhere so stop()/start()
        # (which always calls stop() first) never crash on Linux. See
        # _switch_defaults for why this checks supports_endpoints(), not
        # is_supported().
        if not sysaudio.supports_endpoints():
            return
        win_audio = sysaudio.endpoints()
        from .config import save_config

        saved = self.cfg.get("_pending_default_restore")
        if not saved:
            return
        try:
            win_audio.restore(saved)
        except Exception as e:
            # Restore failed (transient COM/device error). KEEP the snapshot so the
            # next stop() or app launch retries — clearing it here would strand the
            # default endpoint on the virtual cable with no record to recover from.
            self.on_status(t("st_restore_fail", e=e))
            return
        self.on_status(t("st_restored"))
        self.cfg["_pending_default_restore"] = None
        save_config(self.cfg)

    def _build(self, mode: str) -> list:
        resolve = self.resolve
        session_dir = getattr(self, "_session_dir_out", None)
        if mode == "video":
            return [IncomingPipeline(self.cfg, resolve, mode, self.on_text,
                                     self.on_status, session_dir=session_dir,
                                     on_speaker=self.on_speaker)]
        if mode == "meeting":
            pipes = [IncomingPipeline(self.cfg, resolve, "meeting",
                                      self.on_text, self.on_status,
                                      session_dir=session_dir,
                                      on_speaker=self.on_speaker)]
            try:
                # Outbound direction requires a virtual microphone driver. If
                # none is installed, the meeting gracefully falls back to
                # listen-only.
                pipes.append(OutgoingPipeline(self.cfg, resolve,
                                              self.on_text, self.on_status))
                self._outgoing_ok = True
            except Exception:
                self._outgoing_ok = False
                self.on_status(t("st_meeting_listen_only"))
            return pipes
        raise ValueError(f"Unknown mode: {mode}")

    def _is_session_live(self) -> bool:
        """True only while at least one pipeline is actively translating.

        Gates billing on real liveness: the translator must have completed its
        warmup handshake (_ready) and not be shutting down (_stopping). Outage /
        reconnect-storm time, where the socket is down and no audio flows, is NOT
        accrued — the user is not getting service, so they are not billed for it.

        NOTE: _ready is cleared by the translator on every reconnect/backoff
        iteration, so ordinary outages stop accrual within one heartbeat. The
        remaining hole — a silently hung socket that never raises — is closed
        by the translators' stall watchdog (_STALL_ROTATE_SECONDS): sent audio
        with zero server events forces a rotation, whose gap clears _ready.
        """
        for p in self._pipelines:
            # A capture backend that died mid-session (device unplug, exclusive-
            # mode grab, sleep/resume) means no audio is flowing for this pipeline
            # — it is not providing service, so don't accrue billing for it.
            cap = getattr(p, "capture", None)
            if cap is not None and getattr(cap, "failed", False):
                continue
            tr = getattr(p, "translator", None)
            if tr is None:
                continue
            ready = getattr(tr, "_ready", None)
            stopping = getattr(tr, "_stopping", None)
            live = (ready is None or ready.is_set()) and \
                   (stopping is None or not stopping.is_set())
            if live and tr.is_alive():
                return True
        return False

    def _consume_minutes(self, accrue: bool) -> tuple[str | None, float, str | None]:
        """Atomic get-and-reset of billable minutes since the last watermark.

        Advances _last_report to now and returns (session_id, delta_minutes,
        source) under _heartbeat_lock so the heartbeat and the stop() tail can
        never bill the same interval twice. When accrue is False the watermark is
        still advanced (so outage time is skipped, not deferred) but delta is
        zeroed — the elapsed wall-clock is intentionally dropped, not billed."""
        with self._heartbeat_lock:
            sid = self._session_id
            last = self._last_report
            smode = self._session_mode
            if not sid or last is None:
                return None, 0.0, None
            now = time.monotonic()
            self._last_report = now
            delta = (now - last) / 60.0 if accrue else 0.0
            if delta < 0:
                delta = 0.0
            source = "video" if smode == "video" else "meeting_incoming"
            return sid, delta, source

    def _heartbeat_loop(self):
        """Periodic usage reporter. Consumes the delta since the last beat and
        signals the UI to refresh the quota badge. on_usage_reported is fanned
        out to its own thread so a slow UI callback cannot stall the heartbeat
        cadence (and thus skew the next interval's billable delta)."""
        while not self._heartbeat_stop.wait(self.HEARTBEAT_SECONDS):
            sid, delta, source = self._consume_minutes(accrue=self._is_session_live())
            self._maybe_warn_capture_dead()
            self._maybe_handle_translator_dead()
            if not sid:
                continue
            if delta > 0:
                # Same client clamp the stop() tail applies: one interval can never
                # legitimately exceed a heartbeat (+ bounded slack). On sleep/resume
                # the monotonic clock jumps while capture may not flag failed, which
                # would otherwise bill the whole wall-clock gap as one delta. Cap only
                # the REPORTED value — the watermark already advanced past the excess
                # in _consume_minutes, so the surplus is dropped, not deferred.
                max_beat = (self.HEARTBEAT_SECONDS + 2.0) / 60.0
                voxis_client.report_usage_async(
                    sid, min(delta, max_beat), source, self.current_engine() or "gemini",
                    on_quota_exceeded=self._fire_quota_exceeded)
            self._dispatch_usage_reported()

    def _maybe_warn_capture_dead(self):
        """Once per session, if a capture backend died mid-session, surface it so a
        green badge + silent transcript don't read as 'working'. Billing already
        stops via _is_session_live; this is the user-facing half."""
        if self._capture_dead_notified:
            return
        for p in self._pipelines:
            cap = getattr(p, "capture", None)
            if cap is not None and getattr(cap, "failed", False):
                self._capture_dead_notified = True
                voxis_client.report_event_async("capture_lost", self._session_id,
                                                {"mode": self._session_mode})
                try:
                    self.on_status(t("st_capture_lost"))
                except Exception:
                    pass
                return

    def _maybe_handle_translator_dead(self):
        """Tear the session down (once) if a translator thread died mid-session.

        A translator that completed warmup (_ready set) and is no longer alive,
        while not intentionally stopping (_stopping), has crashed out of its run
        loop — capture, ducking and the endpoint redirection are still live and
        the badge is a false green, but no translation is happening. We only fire
        when the whole session is no longer live (_is_session_live False), so a
        meeting whose outgoing leg died but incoming still works keeps running.
        A transient reconnect keeps the thread alive, so it is not flagged.

        bridge.stop() dispatches teardown to its own thread, so calling
        on_session_failed inline from the heartbeat thread cannot self-join."""
        if self._session_failed.is_set() or not self.mode:
            return
        any_dead = False
        for p in self._pipelines:
            tr = getattr(p, "translator", None)
            if tr is None:
                continue
            stopping = getattr(tr, "_stopping", None)
            if stopping is not None and stopping.is_set():
                continue
            ready = getattr(tr, "_ready", None)
            if (ready is None or ready.is_set()) and not tr.is_alive():
                any_dead = True
        if any_dead and not self._is_session_live():
            self._session_failed.set()
            try:
                self.on_session_failed()
            except Exception:
                pass

    def _fire_quota_exceeded(self):
        """Server signaled the license is exhausted (402). Fire-once per session:
        several in-flight heartbeat reports can each receive the 402, but only the
        first should trigger teardown. Reset in start()."""
        if self._quota_exhausted.is_set():
            return
        self._quota_exhausted.set()
        try:
            self.on_quota_exceeded()
        except Exception:
            pass

    def _dispatch_usage_reported(self):
        """Run the UI quota-refresh callback off the heartbeat thread."""
        threading.Thread(
            target=self._safe_usage_reported, daemon=True, name="voxis-usage-cb"
        ).start()

    def _safe_usage_reported(self):
        try:
            self.on_usage_reported()
        except Exception:
            pass

    def start(self, mode: str, session_dir: str | None = None):
        self.stop()
        if self.resolve is None:
            raise RuntimeError(t("st_no_key"))
        # No native OS audio backend on this platform (e.g. a Linux box without
        # PipeWire/Pulse tooling on PATH, or any other unsupported OS). Decline
        # the session with a friendly status instead of crashing deep in the
        # capture build. Windows and PipeWire-equipped Linux are supported, so
        # this is a no-op there.
        if not sysaudio.is_supported():
            self.on_status(t("st_no_audio_backend"))
            return False
        # Per-session output folder decided by the caller (webui) so the recorder's
        # WAVs land beside the transcript JSON in one self-contained folder.
        self._session_dir_out = session_dir
        # Premium auto-routing: when a virtual cable is present, run the
        # music-preserving spatial (vbcable) path; otherwise driverless. The user
        # never chooses. No-op on the OSS build (premium absent → stays driverless).
        if _premium is not None and hasattr(_premium, "resolve_capture_backend"):
            try:
                self.cfg["capture_backend"] = _premium.resolve_capture_backend(self.cfg)
            except Exception:
                pass
        # Correlation id shared by this session's funnel milestones. Generated
        # before the retry loop so session_start/live/error all carry the same id.
        sid = uuid.uuid4().hex[:16]
        voxis_client.report_event_async("session_start", sid, {"mode": mode})
        last_err: Exception | None = None
        for attempt in range(3):
            # Stale PortAudio device list is a known cause of WDM-KS -9999.
            audio_io.refresh()
            pipes: list = []
            try:
                pipes = self._build(mode)
                # Two-phase start so meeting mode's two Live sessions warm up
                # concurrently: kick off every connection first, then wait on
                # them — the handshakes overlap, so total ≈ max, not sum.
                for p in pipes:
                    p.start_translator()
                for p in pipes:
                    p.start_io()
                self._pipelines = pipes
                self.mode = mode
                # Client-generated session_id is a correlation tag ONLY: the
                # server must derive billable minutes from server-observed
                # session state (connect/disconnect, key issuance), never trust
                # this id or the reported delta as authoritative. See _consume /
                # tail report for the matching client-side sanity clamp.
                self._session_id = sid
                self._session_start = time.monotonic()
                self._last_report = self._session_start
                self._session_mode = mode
                # New session — re-arm the one-shot quota cutoff + capture-death
                # notice + translator-death teardown.
                self._quota_exhausted.clear()
                self._session_failed.clear()
                self._capture_dead_notified = False
                # Never run two heartbeats at once: a prior thread that survived
                # stop()'s bounded join would race this one on _last_report and
                # double-bill. Refuse to start until it is truly gone.
                prior = self._heartbeat_thread
                if prior is not None and prior.is_alive():
                    raise RuntimeError("previous heartbeat thread still running")
                self._heartbeat_stop = threading.Event()
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True, name="voxis-heartbeat"
                )
                self._heartbeat_thread.start()
                try:
                    self._switch_defaults(mode)
                except Exception as e:
                    self.on_status(t("st_autoswitch_fail", e=e))
                # AFTER the default endpoint has been flipped, never before: the
                # mirror follows whichever endpoint the user's volume keys reach,
                # and in vbcable mode that only becomes the cable here. Starting it
                # earlier latched it onto the headphones and the keys did nothing —
                # which is exactly the bug this fixes.
                self._start_volume_mirror()
                self.on_status(t("st_mode_started", mode=mode))
                voxis_client.report_event_async("session_live", sid, {
                    "mode": mode,
                    "backend": self.cfg.get("capture_backend", "driverless"),
                    "engine": self.current_engine() or "",
                })
                return True
            except Exception as e:
                for p in pipes:
                    try:
                        p.stop()
                    except Exception:
                        pass
                last_err = e
                msg = str(e)
                if attempt == 2 or not ("-9999" in msg or "PaError" in msg or "host error" in msg):
                    voxis_client.report_event_async("session_error", sid, {
                        "mode": mode, "reason": _event_error_class(e),
                        "attempt": attempt + 1,
                    })
                    raise
                if attempt == 1:
                    # Last attempt: drop the low-latency request entirely.
                    audio_io.LATENCY = None
                self.on_status(t("st_audio_retry", n=attempt + 1))
                time.sleep(0.6)
        voxis_client.report_event_async("session_error", sid, {
            "mode": mode, "reason": _event_error_class(last_err) if last_err else "other",
            "attempt": 3,
        })
        raise last_err

    def set_tts_volume(self, volume: float):
        self.cfg["tts_volume"] = volume
        for p in self._pipelines:
            if isinstance(p, IncomingPipeline):
                p.player.tts_gain = volume
            elif isinstance(p, OutgoingPipeline) and p.monitor_player is not None:
                p.monitor_player.tts_gain = volume

    def set_duck_gain(self, gain: float):
        self.cfg["duck_gain"] = gain
        for p in self._pipelines:
            if isinstance(p, IncomingPipeline) and hasattr(p, "_duck_gain"):
                p._duck_gain = gain

    def set_audio_mode(self, mode: str):
        self.cfg["original_audio"] = mode
        for p in self._pipelines:
            if isinstance(p, IncomingPipeline) and hasattr(p, "_original_mode"):
                p._original_mode = mode

    def incoming(self) -> "IncomingPipeline | None":
        """The live incoming pipeline, if a session is running. The free-voice
        preview borrows its Player so the comparison plays on exactly the path
        the user is already hearing."""
        for p in self._pipelines:
            if isinstance(p, IncomingPipeline):
                return p
        return None

    def _start_volume_mirror(self):
        """vbcable only: the session has just made the CABLE the default endpoint,
        so the volume keys, the OSD and the mute key now act on the cable — not on
        the headphones Voxis plays to. Mirror the cable's level onto our output so
        the user's own volume control reaches what they actually hear.

        Driverless never needs this: there the default endpoint IS our output
        device, so Windows already attenuates us — mirroring would attenuate twice.
        Best-effort; a missing mirror is a nuisance, a crashed session is not."""
        self._vol_mirror = None
        if self.cfg.get("capture_backend", "driverless") != "vbcable":
            return
        inc = self.incoming()
        if inc is None:
            return
        try:
            from .endpoint_volume import EndpointVolumeMirror  # noqa: PLC0415
            self._vol_mirror = EndpointVolumeMirror(inc.player)
            self._vol_mirror.start()
        except Exception:
            _log.info("endpoint volume mirror not started", exc_info=True)

    def _stop_volume_mirror(self):
        m = getattr(self, "_vol_mirror", None)
        if m is not None:
            m.stop()          # also hands the player's gain back at full scale
            self._vol_mirror = None

    def recent_pro_pcm(self) -> bytes:
        """The last seconds the paid voice spoke — during the session from the live
        ring, after it from the snapshot taken at stop()."""
        inc = self.incoming()
        if inc is not None:
            live = inc.recent_pro_pcm()
            if live:
                return live
        return getattr(self, "_last_pro_pcm", b"")

    # ---------- UI telemetry ----------
    def current_level(self) -> float:
        """Smoothed input level (0..1) across active pipelines, for the UI meter."""
        lv = 0.0
        for p in self._pipelines:
            lv = max(lv, float(getattr(p, "input_level", 0.0)))
        return round(lv, 3)

    def current_latency(self) -> float | None:
        """Estimated ear-voice span (seconds) from the incoming pipeline, or None
        until enough onsets have been measured."""
        for p in self._pipelines:
            rtt = getattr(getattr(p, "_rtt", None), "first_audio_seconds", None)
            if rtt:
                return round(rtt, 2)
        return None

    def current_engine(self):
        """Engine of the active incoming pipeline (for the UI readout), or None."""
        for p in self._pipelines:
            e = getattr(p, "_engine", None)
            if e:
                return e
        return None

    def is_playing(self) -> bool:
        """True while translated TTS is actively playing back."""
        return any(getattr(getattr(p, "player", None), "tts_active", False)
                   for p in self._pipelines)

    def stop(self):
        # Stop and JOIN the heartbeat FIRST so _last_report has exactly one
        # remaining reader. With the heartbeat gone there is no concurrent
        # writer, so the tail consume below sees a quiesced watermark and cannot
        # double-bill the interval the last beat may have been mid-way through.
        accrue = self._is_session_live()
        self._heartbeat_stop.set()
        ht = self._heartbeat_thread
        if ht is not None and ht.is_alive():
            ht.join(timeout=1.0)
        self._heartbeat_thread = None

        # Tail report covering the time since the last heartbeat, consumed
        # atomically while session state is still set. Captured before the
        # session_id is cleared so an immediate restart cannot lose it.
        sid, delta, source = self._consume_minutes(accrue=accrue)
        tail_engine = self.current_engine() or "gemini"  # capture before pipelines clear

        self._stop_volume_mirror()

        # Keep the last seconds of paid voice before the pipelines are dropped:
        # the A/B card is offered AFTER the session, when the user is finally
        # looking at Voxis instead of at what they came to watch.
        inc = self.incoming()
        if inc is not None:
            try:
                pcm = inc.recent_pro_pcm()
                if pcm:   # a cascade session has none; keep the last paid snapshot
                    self._last_pro_pcm = pcm
            except Exception:
                pass

        for p in self._pipelines:
            try:
                p.stop()
            except Exception as e:
                self.on_status(t("st_stop_err", e=e))
        if self._pipelines:
            self.on_status(t("st_stopped"))
        self._pipelines = []
        self.mode = None
        with self._heartbeat_lock:
            self._session_id    = None
            self._session_start = None
            self._session_mode  = None
            self._last_report   = None
        self._restore_defaults()

        # Client clamp: a single tail delta cannot exceed one heartbeat interval
        # plus the bounded join wait — anything larger means a clock/state glitch,
        # so drop it rather than send the server an implausible bill. The server
        # remains the billing authority and re-derives minutes from its own
        # observed session state regardless.
        if sid and delta > 0:
            max_tail = (self.HEARTBEAT_SECONDS + 2.0) / 60.0
            voxis_client.report_usage_async(sid, min(delta, max_tail), source, tail_engine)
            self._dispatch_usage_reported()
