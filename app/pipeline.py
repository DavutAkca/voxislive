"""Mode pipelines: capture → VAD gate → Gemini Live → playback.

IncomingPipeline routes system audio (or the meeting partner's voice) to the
translator and sends the synthesized translation to the user's headphones.
OutgoingPipeline routes the user's microphone to the translator and pipes the
translation into the virtual microphone consumed by Teams/Zoom/etc.
"""
import threading
import time
import uuid

import numpy as np

from . import audio_io
from . import voxis_client

# Open-Core hook: optional premium package resolved once at import time.
# Absence is the normal OSS path.
try:
    import premium as _premium  # type: ignore[import-not-found]
except ImportError:
    _premium = None
from .audio_io import Capture, Player, find_device, resolve_name, _make_resampler
from .config import gate_params, stream_gated
from .dsp import DubbingDucker
from .i18n import t
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
        self._ema = None
        self._t_onset = None
        self._lock = threading.Lock()

    def mark_onset(self) -> None:
        with self._lock:
            if self._t_onset is None:
                self._t_onset = time.monotonic()

    def mark_tts(self) -> None:
        with self._lock:
            if self._t_onset is None:
                return
            dt = time.monotonic() - self._t_onset
            self._t_onset = None
            if dt < self.min_s or dt > self.max_s:
                return
            self._ema = dt if self._ema is None else self.alpha * dt + (1 - self.alpha) * self._ema

    def target_samples(self) -> float:
        with self._lock:
            return 0.0 if self._ema is None else self._ema * self.fs

    @property
    def rtt_seconds(self) -> float | None:
        return self._ema


# Smart-stream silence ceiling: after this many consecutive non-speech frames
# stop padding with silence. ~512/16000 s per frame, so 48 frames ≈ 1.5 s of
# trailing pad before we let a genuine gap form. The gap lets the translator's
# bounded input queue drain to empty, which is what allows the _sender's 0.5 s
# read timeout to surface and the 13-min rotation to fire on a quiet window.
# Billing tradeoff: Smooth (smart=True) streams continuous audio, so this pad
# is the only thing that bounds billed silence; Saver (gated) never pads, so it
# bills fewer minutes at the cost of clipped lead-ins on the next utterance.
_SMART_SILENCE_MAX_FRAMES = 48


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
                 always_send=False, smart=False):
        self._resample = _make_resampler(in_rate, 16000)
        self._gate = gate
        self._send = send_fn
        self._suppress_when = suppress_when
        self._always_send = always_send
        self._smart = smart
        self._silence = _f32_to_pcm16(np.zeros(_FRAME, dtype=np.float32))
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._silent_run = 0
        self.speech_active = False
        self.closed = False

    def feed(self, chunk: np.ndarray):
        if self.closed:
            return
        # Classic loopback path zeros the input while TTS is playing so the
        # translator never re-translates its own output. Sending silence (not
        # nothing) preserves stream continuity.
        if self._suppress_when is not None and self._suppress_when():
            chunk = np.zeros_like(chunk)
        with self._lock:
            self._buf = np.concatenate([self._buf, self._resample(chunk)])
            while len(self._buf) >= _FRAME:
                frame, self._buf = self._buf[:_FRAME], self._buf[_FRAME:]
                active, to_send = self._gate.process(frame)
                self.speech_active = active
                if self._smart:
                    if to_send:
                        self._silent_run = 0
                        for f in to_send:
                            self._send(_f32_to_pcm16(f))
                    elif self._silent_run < _SMART_SILENCE_MAX_FRAMES:
                        # Pad short gaps to keep the model's stream continuous,
                        # but stop once the gap is genuine so the queue drains
                        # and the 13-min rotation can fire on the quiet window.
                        self._silent_run += 1
                        self._send(self._silence)
                elif self._always_send:
                    self._send(_f32_to_pcm16(frame))
                else:
                    for f in to_send:
                        self._send(_f32_to_pcm16(f))


class IncomingPipeline:
    def __init__(self, cfg: dict, api_key: str, mode: str, on_text, on_status):
        # Default the premium flag before either capture branch so callers and
        # teardown can always read it, even on the driverless path that never
        # probes premium hardware.
        self._use_premium = False
        self._sducker = None
        self.capture = None
        self._source = None
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

        def _tts_sink(data: bytes):
            self._rtt.mark_tts()
            self.player.feed_tts_pcm16(data)

        # Gemini Live emits the translated 24 kHz audio directly.
        from .translator import LiveTranslator  # noqa: PLC0415
        self.translator = LiveTranslator(
            api_key, cfg["target_language_incoming"],
            on_audio=_tts_sink,
            on_text=on_text, on_status=on_status,
            rotate_minutes=cfg["session_rotate_minutes"], name=t("name_in"),
            voice=cfg.get("gemini_voice", "Aoede"),
            temperature=float(cfg.get("gemini_temperature", 0.3)),
        )
        send_fn = self.translator.send_pcm16

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

    def _acquire_capture(self, cfg, vad_cfg, send_fn, out_name, on_status):
        from .vad import SpeechGate  # noqa: PLC0415
        if self.driverless:
            # Driverless mode: WASAPI loopback captures system audio; ducking is
            # applied at source via the Windows session-volume API so the user
            # hears a real dubbing effect without any virtual-cable install.
            from .session_duck import SessionDucker
            self._sducker = SessionDucker()

            def on_loop(chunk: np.ndarray):
                lvl = self._sducker.current
                if lvl < 0.95:
                    # Compensate the lowered source so VAD/Gemini do not pump.
                    chunk = np.clip(chunk / max(lvl, 0.15), -1.0, 1.0)
                self._source.feed(chunk)
                self.input_level = _rms_level(self.input_level, chunk)
                # Mark speech onset here too (the spatial path does it in vbcable
                # mode) so the ear-voice latency readout populates in driverless.
                active = self._source.speech_active
                if active and not self._prev_active and not self.player.tts_active:
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
            try:
                # Preferred path on Win10 2004+: process-exclude loopback. Our
                # own TTS is excluded at the hardware level so the translator
                # keeps receiving input even while playback is active.
                from .process_loopback import ProcessExcludeLoopback
                self.capture = ProcessExcludeLoopback(on_loop)
            except Exception as e:
                on_status(f"Process-exclude loopback unavailable ({e}) — classic mode.")
                from .audio_io import LoopbackCapture
                self.capture = LoopbackCapture(on_loop, prefer_name=out_name,
                                               on_status=on_status)
                suppress = lambda: self.player.tts_active
            self._source = _GatedSource(
                self.capture.rate, SpeechGate(**vad_cfg), send_fn,
                suppress_when=suppress, smart=not stream_gated(cfg),
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
                self._source.feed(mono)
                self.input_level = _rms_level(self.input_level, mono)
                active = self._source.speech_active
                if active and not self._prev_active and not self.player.tts_active:
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
            self._source = _GatedSource(self.capture.rate, SpeechGate(**vad_cfg), send_fn,
                                        smart=not stream_gated(cfg))

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
        for attr in ("player", "translator"):
            comp = getattr(self, attr, None)
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass

    @staticmethod
    def capture_rate(dev) -> int:
        return audio_io.device_rate(dev, "input")

    def start_translator(self):
        self.translator.start()

    def start_io(self):
        # Warm the Live socket BEFORE capturing audio so the first utterance
        # does not stack the cold WS/TLS/setup handshake on top of translation.
        self.translator.wait_ready(timeout=6)
        self.player.start()
        self.capture.start()

    def start(self):
        self.start_translator()
        self.start_io()

    def stop(self):
        _stop_all(self)
        d = getattr(self, "_sducker", None)
        if d is not None:
            d.close()


class OutgoingPipeline:
    def __init__(self, cfg: dict, api_key: str, on_text, on_status):
        from .translator import LiveTranslator  # noqa: PLC0415
        from .vad import SpeechGate  # noqa: PLC0415
        vad_cfg = gate_params(cfg)
        mic_dev = find_device(cfg["devices"]["microphone"], "input")
        cable_out = find_device(cfg["devices"]["meeting_mic_playback"], "output")

        self.player = Player(
            cable_out, channels=1,
            tts_enhance=(_premium.enhance_tts if _premium is not None else None),
        )
        self.translator = LiveTranslator(
            api_key,
            cfg["target_language_outgoing"],
            on_audio=self.player.feed_tts_pcm16,
            on_text=on_text,
            on_status=on_status,
            rotate_minutes=cfg["session_rotate_minutes"],
            name=t("name_out"),
            voice=cfg.get("gemini_voice", "Aoede"),
            temperature=float(cfg.get("gemini_temperature", 0.3)),
        )
        self.input_level = 0.0
        self.capture = None
        self._source = None
        # Capture opens the mic InputStream at construction. Guard the capture +
        # gate acquisition so a mid-init failure releases the mic stream and the
        # already-open Player/translator instead of leaking them when
        # ModeController.start discards the half-built pipeline.
        try:
            self.capture = Capture(mic_dev, self._on_mic)
            self._source = _GatedSource(
                self.capture.rate, SpeechGate(**vad_cfg), self.translator.send_pcm16,
                smart=not stream_gated(cfg),
            )
        except Exception:
            self._teardown_resources()
            raise

    def _teardown_resources(self):
        """Best-effort release of whatever was acquired; safe with partial init."""
        if self._source is not None:
            self._source.closed = True
        for comp in (self.capture, getattr(self, "player", None),
                     getattr(self, "translator", None)):
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass

    def _on_mic(self, c: np.ndarray):
        mono = c.mean(axis=1) if getattr(c, "ndim", 1) > 1 else c
        self.input_level = _rms_level(self.input_level, mono)
        self._source.feed(c)

    def start_translator(self):
        self.translator.start()

    def start_io(self):
        self.translator.wait_ready(timeout=6)
        self.player.start()
        self.capture.start()

    def start(self):
        self.start_translator()
        self.start_io()

    def stop(self):
        _stop_all(self)


def _stop_all(pipeline):
    """Stops all components independently so a failure in one does not block the
    others. Components may be None if init failed mid-way before acquiring them."""
    src = getattr(pipeline, "_source", None)
    if src is not None:
        src.closed = True
    fns = []
    for attr in ("capture", "player", "translator"):
        comp = getattr(pipeline, attr, None)
        if comp is not None:
            fns.append(comp.stop)
    for fn in fns:
        try:
            fn()
        except Exception:
            pass


class ModeController:
    """Single-active-mode controller (video | meeting). start() implicitly stops
    any current session first."""

    # Usage report interval. Smaller = less time lost when the process is
    # killed abruptly (Ctrl+C, crash, network drop).
    HEARTBEAT_SECONDS: float = 6.0

    def __init__(self, cfg: dict, api_key: str | None, on_text, on_status,
                 on_usage_reported=None):
        self.cfg = cfg
        self.api_key = api_key
        self.on_text = on_text
        self.on_status = on_status
        self.on_usage_reported = on_usage_reported or (lambda: None)
        self._pipelines: list = []
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
        from . import win_audio
        from .config import save_config

        driverless = self.cfg.get("capture_backend", "driverless") != "vbcable"
        saved = self.cfg.get("_pending_default_restore") or {}

        if not driverless:
            if "output" not in saved:
                out_id, out_name = win_audio.get_default("output")
                if "CABLE" in out_name or "VB-Audio" in out_name:
                    out_id = win_audio.find_endpoint_id(self.cfg["devices"]["headphones_output"])
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
        from . import win_audio
        from .config import save_config

        saved = self.cfg.get("_pending_default_restore")
        if not saved:
            return
        try:
            win_audio.restore(saved)
            self.on_status(t("st_restored"))
        except Exception as e:
            self.on_status(t("st_restore_fail", e=e))
        self.cfg["_pending_default_restore"] = None
        save_config(self.cfg)

    def _build(self, mode: str) -> list:
        if mode == "video":
            return [IncomingPipeline(self.cfg, self.api_key, mode, self.on_text, self.on_status)]
        if mode == "meeting":
            pipes = [IncomingPipeline(self.cfg, self.api_key, "meeting",
                                      self.on_text, self.on_status)]
            try:
                # Outbound direction requires a virtual microphone driver. If
                # none is installed, the meeting gracefully falls back to
                # listen-only.
                pipes.append(OutgoingPipeline(self.cfg, self.api_key,
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

        NOTE: this uses translator-thread Events as the liveness proxy because
        the translator does not yet expose a public is_connected()/last_audio_at.
        A precise "producing audio in the last N seconds" gate needs that signal
        (see cross_file_needs) — _ready stays set across a transient reconnect,
        so a long mid-session outage can still over-accrue until that lands.
        """
        for p in self._pipelines:
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
            if not sid:
                continue
            if delta > 0:
                voxis_client.report_usage_async(sid, delta, source)
            self._dispatch_usage_reported()

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

    def start(self, mode: str):
        self.stop()
        if not self.api_key:
            raise RuntimeError(t("st_no_key"))
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
                self._session_id = uuid.uuid4().hex[:16]
                self._session_start = time.monotonic()
                self._last_report = self._session_start
                self._session_mode = mode
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
                self.on_status(t("st_mode_started", mode=mode))
                return
            except Exception as e:
                for p in pipes:
                    try:
                        p.stop()
                    except Exception:
                        pass
                last_err = e
                msg = str(e)
                if attempt == 2 or not ("-9999" in msg or "PaError" in msg or "host error" in msg):
                    raise
                if attempt == 1:
                    # Last attempt: drop the low-latency request entirely.
                    audio_io.LATENCY = None
                self.on_status(t("st_audio_retry", n=attempt + 1))
                time.sleep(0.6)
        raise last_err

    def set_tts_volume(self, volume: float):
        self.cfg["tts_volume"] = volume
        for p in self._pipelines:
            if isinstance(p, IncomingPipeline):
                p.player.tts_gain = volume

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
            rtt = getattr(getattr(p, "_rtt", None), "rtt_seconds", None)
            if rtt:
                return round(rtt, 2)
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
            voxis_client.report_usage_async(sid, min(delta, max_tail), source)
            self._dispatch_usage_reported()
