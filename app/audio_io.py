"""Audio I/O layer: device discovery, capture and mixed stereo playback.

The driverless path uses WASAPI loopback for capture and the Windows session
volume API to duck other apps at their source. The VB-CABLE path intercepts
the audio before it reaches the speakers so the engine can apply real DSP.
"""
import collections
import threading

import numpy as np
import sounddevice as sd

from .mix_core import DelayLine, LookaheadLimiter, place_center

# Stream latency hint. PortAudio occasionally fails with WDM-KS -9999 when this
# is set; ModeController retries with this dropped to None on repeated failure.
LATENCY: str | None = "low"


def refresh():
    """Refreshes the PortAudio device list (becomes stale after USB hot-plug)."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass

# Each resampler instance carries streaming state (soxr's internal buffer or the
# fallback's frac/last). It is single-producer by contract: only one thread may
# call a given resampler. Voxis honors this — each pipeline owns its own
# instance and feeds it from one callback/sink thread.
try:
    import soxr

    def _make_resampler(in_rate: int, out_rate: int):
        # Ratio 1.0 (ProcessExclude 16k input, or a device already at the TTS
        # rate) is a no-op — skip the soxr stream call on the hot per-frame path.
        if in_rate == out_rate:
            return lambda x: x
        rs = soxr.ResampleStream(in_rate, out_rate, 1, dtype="float32")
        return lambda x: rs.resample_chunk(x)
except ImportError:
    # Fallback when no soxr wheel is available: simple linear interpolation.
    # Single-producer like the soxr path — the frac/last state is not locked.
    def _make_resampler(in_rate: int, out_rate: int):
        if in_rate == out_rate:
            return lambda x: x  # no-op, matches the soxr branch
        ratio = out_rate / in_rate
        state = {"frac": 0.0, "last": np.zeros(1, dtype=np.float32)}

        def _resample(x: np.ndarray) -> np.ndarray:
            x = np.asarray(x, dtype=np.float32)
            buf = np.concatenate([state["last"], x])
            n_out = int((len(buf) - 1) * ratio - state["frac"])
            if n_out <= 0:
                state["last"] = buf
                return np.zeros(0, dtype=np.float32)
            idx = state["frac"] + np.arange(n_out) / ratio
            # Never let an interpolation index reach the last buffer sample: that
            # sample is only half-formed until the next chunk supplies its
            # neighbor, so reading it now would alias the boundary. Trim n_out so
            # idx max stays < len(buf)-1; np.clip is the final out-of-range guard.
            hi = len(buf) - 1
            keep = int(np.searchsorted(idx, hi - 1e-6, side="left"))
            if keep <= 0:
                state["last"] = buf
                return np.zeros(0, dtype=np.float32)
            idx = idx[:keep]
            idx = np.clip(idx, 0.0, hi)
            out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
            consumed = int(idx[-1])
            state["frac"] = float(idx[-1] - consumed)
            # Clamp the carried tail so a stall cannot let `last` grow unbounded.
            tail = buf[consumed:]
            max_tail = max(8, 4 * len(x))
            if len(tail) > max_tail:
                tail = tail[-max_tail:]
            state["last"] = tail
            return out

        return _resample


def _wasapi_indices() -> list[int] | None:
    """Returns device indices owned by the WASAPI host API (lowest-latency path)."""
    try:
        for ha in sd.query_hostapis():
            if "WASAPI" in ha["name"].upper():
                return list(ha["devices"])
    except Exception:
        pass
    return None


def _default_index(kind: str) -> int | None:
    try:
        idx = sd.default.device[0 if kind == "input" else 1]
        return int(idx) if idx is not None and idx >= 0 else None
    except Exception:
        return None


def find_device(name_substr: str, kind: str, endpoint_id: str | None = None,
                on_status=None, fallback_default: bool = False) -> int | None:
    """Resolves an input/output device to a PortAudio index.

    Resolution order, most reliable first:
      1) `endpoint_id` — a persistent endpoint identifier stored in config. Names
         drift (renames, locale, "(2)" suffixes); IDs do not, so an exact ID
         match wins outright when supplied.
      2) Exact friendly-name match. Among duplicate names the default endpoint
         for `kind` is preferred (it is the Active/selected one).
      3) Substring friendly-name match (same default-preference tie-break).
    WASAPI endpoints are scanned first (clearly lower latency than MME). An empty
    name with no id resolves to the system default (returned as None).

    On no match: if `fallback_default` is set, returns the system default and
    warns via `on_status`; otherwise raises ValueError. Callers resolving a
    virtual cable must leave fallback off so a wrong default can never close a
    record/playback feedback loop.
    """
    if not name_substr and not endpoint_id:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    devs = sd.query_devices()
    wasapi = _wasapi_indices() or []
    order = wasapi + [i for i in range(len(devs)) if i not in set(wasapi)]
    default_idx = _default_index(kind)

    def _usable(i: int) -> bool:
        return devs[i][key] > 0

    # 1) Persistent endpoint ID — PortAudio does not expose the OS endpoint GUID,
    # so we match it against any identifier the device record carries (name plus
    # any id-like field). An exact hit here is authoritative.
    if endpoint_id:
        for i in order:
            if not _usable(i):
                continue
            d = devs[i]
            cand = {str(d.get("name", "")), str(d.get("id", "")),
                    str(d.get("hostapi", "")) + ":" + str(d.get("name", ""))}
            if endpoint_id in cand:
                return i

    if name_substr:
        target = name_substr.lower()
        # Exact match first, then substring. Exact-match-first prevents
        # "CABLE Output" from incorrectly resolving to "CABLE Output (VB-Audio
        # Point)" — that mistake would close a feedback loop. Within each pass
        # the default endpoint is preferred so duplicate names pick the Active one.
        for exact in (True, False):
            matches = []
            for i in order:
                if not _usable(i):
                    continue
                name = devs[i]["name"].lower()
                if (name == target) if exact else (target in name):
                    matches.append(i)
            if matches:
                if default_idx in matches:
                    return default_idx
                return matches[0]

    if fallback_default and default_idx is not None:
        if on_status is not None:
            try:
                on_status(
                    f"Audio device '{name_substr or endpoint_id}' ({kind}) not "
                    f"found — using system default."
                )
            except Exception:
                pass
        return None  # None == system default for the stream constructors
    raise ValueError(
        f"Audio device not found: '{name_substr or endpoint_id}' ({kind}). "
        f"Is VB-CABLE installed? List devices with: python -m app.audio_io"
    )


def list_device_names(kind: str, exclude_virtual: bool = True) -> list[str]:
    """Returns device names for UI selectors (WASAPI endpoints, virtual cables filtered)."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    devs = sd.query_devices()
    idxs = _wasapi_indices() or range(len(devs))
    names: list[str] = []
    for i in idxs:
        d = devs[i]
        if d[key] <= 0:
            continue
        n = d["name"]
        if exclude_virtual and ("CABLE" in n or "VB-Audio" in n):
            continue
        if n not in names:
            names.append(n)
    return names


# Virtual-cable device-name fragments, best match first. Each pair is
# (playback-side, recording-side): Voxis writes the translated voice to the
# playback device; the meeting app records its mic from the recording one.
_CABLE_CANDIDATES = (
    ("CABLE Input", "CABLE Output"),            # VB-CABLE (VB-Audio Virtual Cable)
    ("VoiceMeeter Input", "VoiceMeeter Output"),  # VoiceMeeter
    ("VB-Audio", "VB-Audio"),                   # generic VB-Audio pair (e.g. Point)
)


def detect_virtual_cable() -> tuple[str, str] | None:
    """Finds an installed virtual audio cable for the two-way meeting path.

    Returns (playback_name, recording_name) using full friendly names so callers
    can store them directly in config, or None if no cable is present.
    """
    try:
        outs = list_device_names("output", exclude_virtual=False)
        ins = list_device_names("input", exclude_virtual=False)
    except Exception:
        return None
    for play_sub, rec_sub in _CABLE_CANDIDATES:
        play = next((n for n in outs if play_sub.lower() in n.lower()), None)
        rec = next((n for n in ins if rec_sub.lower() in n.lower()), None)
        if play and rec:
            return play, rec
    return None


def resolve_name(index: int | None, kind: str) -> str:
    info = sd.query_devices(index if index is not None else sd.default.device[0 if kind == "input" else 1])
    return info["name"]


def device_rate(index: int | None, kind: str) -> int:
    info = sd.query_devices(index if index is not None else sd.default.device[0 if kind == "input" else 1])
    return int(info["default_samplerate"])


class Capture:
    """Reads float32 audio from an input device and forwards it to a callback.

    stereo=False (default): downmix to mono for VAD/Gemini.
    stereo=True: preserve both channels (n, 2) so the VB-CABLE path can perform
                 M/S center suppression downstream.

    The PortAudio callback only conforms + copies + enqueues; the consumer
    callback (VAD inference, resampling, premium DSP, websocket handoff) runs on
    a dedicated worker thread — the same capture/processing split as
    ProcessExcludeLoopback, so a consumer stall can never starve the realtime
    callback into xruns. The queue is bounded drop-oldest to bound latency, and
    a persistently faulting consumer flips `failed` so the session's liveness
    gate stops billing (mirrors the ploopback processor)."""

    _QUEUE_MAX = 64  # 64 * 20 ms ≈ 1.3 s of slack before drop-oldest

    def __init__(self, device: int | None, on_chunk, block_ms: int = 20,
                 stereo: bool = False):
        self.rate = device_rate(device, "input")
        info = sd.query_devices(device if device is not None else sd.default.device[0])
        channels = min(2, info["max_input_channels"])
        self.stereo = stereo and channels >= 2
        blocksize = int(self.rate * block_ms / 1000)
        self._on_chunk = on_chunk
        self._queue: collections.deque = collections.deque(maxlen=self._QUEUE_MAX)
        self._has_data = threading.Event()
        self._run = False
        self._err: Exception | None = None
        self.dropped = 0  # chunks lost to drop-oldest (telemetry)
        self._worker = threading.Thread(target=self._drain, daemon=True,
                                        name="capture-feed")

        def _cb(indata, frames, time_info, status):
            # RT callback: shape + copy + enqueue only. Never run user code here.
            if self.stereo:
                out = indata if indata.shape[1] == 2 else np.repeat(indata[:, :1], 2, axis=1)
                data = out.copy()
            else:
                # Level-consistent mono downmix: mean across channels, the same
                # convention as LoopbackCapture and ProcessExclude, so the VAD
                # threshold has identical meaning on every capture backend.
                mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
                data = mono.copy()
            if len(self._queue) == self._QUEUE_MAX:
                self.dropped += 1
            self._queue.append(data)
            self._has_data.set()

        self.stream = sd.InputStream(
            device=device,
            samplerate=self.rate,
            channels=channels,
            dtype="float32",
            blocksize=blocksize,
            latency=LATENCY,
            callback=_cb,
        )

    @property
    def failed(self) -> bool:
        """True once the consumer has faulted persistently — polled by the
        session liveness gate so a dead pipeline stops billing."""
        return self._err is not None

    def _drain(self):
        # Same fault policy as ProcessExcludeLoopback._processor: a single bad
        # chunk is transient, a long back-to-back run means the pipeline behind
        # us is dead — record it so `failed` surfaces and billing stops.
        fails = 0
        while self._run or self._queue:
            if not self._queue:
                self._has_data.wait(0.05)
                self._has_data.clear()
                continue
            try:
                x = self._queue.popleft()
            except IndexError:
                continue
            try:
                self._on_chunk(x)
                fails = 0
            except Exception as e:
                fails += 1
                if fails == 1 or fails % 200 == 0:
                    print(f"[capture] consumer fault #{fails}: {e!r}")
                if fails >= 50 and self._err is None:
                    self._err = e
                continue

    def start(self):
        if self._run:
            return
        # Thread objects are single-use: recreate after any prior run so a
        # restart never hits "threads can only be started once".
        if not self._worker.is_alive():
            self._err = None
            self._worker = threading.Thread(target=self._drain, daemon=True,
                                            name="capture-feed")
        self._run = True
        self._worker.start()
        self.stream.start()

    def stop(self):
        self._run = False
        self._has_data.set()
        self.stream.stop()
        self.stream.close()
        if self._worker.is_alive():
            self._worker.join(timeout=1.0)


class LoopbackCapture:
    """Driverless WASAPI loopback capture (Windows 10 2004+).

    Reads the running mix of the preferred output endpoint. The capture is
    passive — the original audio does not flow through us — so ducking is done
    at the source by SessionDucker.
    """

    def __init__(self, on_chunk, prefer_name: str | None = None, block_ms: int = 20,
                 on_status=None):
        import pyaudiowpatch as pa

        self._pa = pa.PyAudio()
        loops = list(self._pa.get_loopback_device_info_generator())
        if not loops:
            self._pa.terminate()
            raise ValueError("No WASAPI loopback device found (requires Windows 10 2004+).")
        wasapi = self._pa.get_host_api_info_by_type(pa.paWASAPI)
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop = None
        for want in (prefer_name, default_out["name"]):
            if not want:
                continue
            loop = next((d for d in loops if want in d["name"]), None)
            if loop:
                break
        loop = loop or loops[0]

        self.rate = int(loop["defaultSampleRate"])
        self._ch = loop["maxInputChannels"]
        self._on_chunk = on_chunk
        self._on_status = on_status
        self._frames = max(1, int(self.rate * block_ms / 1000))
        self._stream = self._pa.open(
            format=pa.paInt16, channels=self._ch, rate=self.rate, input=True,
            input_device_index=loop["index"], frames_per_buffer=self._frames,
        )
        self._run = False
        # Error storage mirrors ProcessExcludeLoopback: a reader thread dying on
        # an exception leaves the engine silently deaf otherwise.
        self._err: Exception | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="loopback")
        self.device_name = loop["name"]

    @property
    def failed(self) -> bool:
        return self._err is not None

    def _loop(self):
        while self._run:
            try:
                raw = self._stream.read(self._frames, exception_on_overflow=False)
            except Exception as e:
                # Distinguish a real read failure from a normal stop(): stop()
                # clears _run before the next read, so only flag if still running.
                if self._run:
                    self._err = e
                    if self._on_status is not None:
                        try:
                            self._on_status(f"Loopback capture failed: {e}")
                        except Exception:
                            pass
                break
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if self._ch > 1:
                # Level-consistent mono downmix (mean across channels): the VAD
                # threshold then means the same here as on every other backend.
                x = x.reshape(-1, self._ch).mean(axis=1)
            # Consumer guard (mirrors ProcessExcludeLoopback._processor): one
            # bad frame is swallowed, a persistent consumer fault flips _err so
            # `failed` surfaces and billing stops — instead of the reader thread
            # dying silently and the session streaming dead air.
            try:
                self._on_chunk(x)
                self._consumer_fails = 0
            except Exception as e:
                self._consumer_fails = getattr(self, "_consumer_fails", 0) + 1
                if self._consumer_fails == 1 or self._consumer_fails % 200 == 0:
                    print(f"[loopback] consumer fault #{self._consumer_fails}: {e!r}")
                if self._consumer_fails >= 50 and self._err is None:
                    self._err = e

    def start(self):
        if self._run:
            return
        # Thread objects are single-use: recreate after any prior run (clean or
        # failed) so a restart never hits "threads can only be started once".
        if not self._thread.is_alive():
            self._err = None
            self._thread = threading.Thread(target=self._loop, daemon=True, name="loopback")
        self._run = True
        self._thread.start()

    def stop(self):
        self._run = False
        # Join the reader before closing the stream — closing while read()
        # is in flight crashes PortAudio with a segfault.
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        try:
            self._pa.terminate()
        except Exception:
            pass


class _Ring:
    """Thread-safe fixed-capacity circular sample buffer.

    Preallocated (cap, channels) storage with modular read/write indices: push()
    and pull() do at most two slice copies each and never reallocate, so the
    output callback's pull() is O(frames) with zero per-callback allocation (the
    old np.concatenate/slice ring churned the heap on every push and pull, the
    top realtime glitch source). Mono input is auto-promoted; channel mismatches
    upmix (mono → multi) or truncate. pull() always returns (n, ch), zero-padding
    the tail on underflow to preserve stream continuity.

    Overflow policy (drop_newest):
      * False (default) — drop OLDEST: the freshest audio always plays. Used for
        the TTS ring where the unplayed tail of the *current* utterance must win
        over stale backlog.
      * True            — drop NEWEST: protects the START of an utterance from
        cross-turn pile-up; the late surplus is discarded instead of overwriting
        what is already queued. Either way `overflows` counts dropped samples so
        the condition is observable.
    """

    def __init__(self, max_seconds: float, rate: int, channels: int = 1,
                 drop_newest: bool = False):
        self.ch = channels
        # One extra slot keeps the full/empty states distinguishable.
        self._cap = max(1, int(max_seconds * rate)) + 1
        self._buf = np.zeros((self._cap, channels), dtype=np.float32)
        self._r = 0
        self._w = 0
        self._lock = threading.Lock()
        self.drop_newest = drop_newest
        self.overflows = 0  # total samples dropped on overflow (telemetry)

    def _conform(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] != self.ch:
            x = np.repeat(x, self.ch, axis=1) if x.shape[1] == 1 else x[:, :self.ch]
        return x

    def _fill_unlocked(self) -> int:
        return (self._w - self._r) % self._cap

    def _write_unlocked(self, x: np.ndarray) -> None:
        n = x.shape[0]
        w, cap = self._w, self._cap
        end = w + n
        if end <= cap:
            self._buf[w:end] = x
        else:  # wrap: at most two contiguous copies
            first = cap - w
            self._buf[w:] = x[:first]
            self._buf[:end - cap] = x[first:]
        self._w = end % cap

    def push(self, x: np.ndarray):
        x = self._conform(x)
        n = x.shape[0]
        if n == 0:
            return
        with self._lock:
            cap = self._cap
            free = cap - 1 - self._fill_unlocked()
            if n <= free:
                self._write_unlocked(x)
                return
            # Overflow: keep only what fits under the chosen policy.
            self.overflows += n - free
            if self.drop_newest:
                # Keep what is already queued; append only as much of the new
                # chunk as fits, discarding the late surplus (protects the START
                # of the utterance already in flight).
                if free > 0:
                    self._write_unlocked(x[:free])
            else:
                # Drop oldest: the result holds the most recent cap-1 samples of
                # (existing tail ++ new chunk). Writing the new samples over the
                # unread old ones IS the eviction; advancing the read pointer to
                # cap-1 behind the new frontier discards the rest in order.
                x = x[-(cap - 1):]  # samples older than this cannot survive
                self._write_unlocked(x)
                self._r = (self._w - (cap - 1)) % cap

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros((n, self.ch), dtype=np.float32)
        with self._lock:
            avail = min(n, self._fill_unlocked())
            r, cap = self._r, self._cap
            end = r + avail
            if end <= cap:
                out[:avail] = self._buf[r:end]
            else:  # wrap: at most two contiguous copies
                first = cap - r
                out[:first] = self._buf[r:]
                out[first:avail] = self._buf[:end - cap]
            self._r = end % cap
        return out

    @property
    def fill(self) -> int:
        with self._lock:
            return self._fill_unlocked()

    def clear(self):
        with self._lock:
            self._r = 0
            self._w = 0


def _mix_to_stereo(tts_mono: np.ndarray, amb: np.ndarray, tts_gain: float,
                   width: float, route_ambient: bool, mid_gain: float = 1.0) -> np.ndarray:
    """Psychoacoustic stereo mix. Pure and testable; a handful of elementwise
    ops on a ~20 ms block (~9 µs), cheap enough to run in the audio callback.

    * TTS mono → phantom center (place_center, L == R).
    * Ambient (when present) → M/S decomposition:
        - Mid  = (L+R)/2  → center-panned dialogue + mono bass. Reduced by
                  mid_gain to suppress the original speaker (center-removal).
        - Side = (L−R)/2  → stereo music/SFX. Pushed outward by `width`.
      Reconstruct: L = Mid·mid_gain + Side·width + TTS ; R = Mid·mid_gain − Side·width + TTS.
    * route_ambient False: sides empty, only the center TTS plays.

    NB: numpy is the single source of truth here. A GPU path was removed — a
    host↔device round-trip inside the real-time callback adds PCIe-copy/sync
    jitter and xrun risk and is strictly slower than numpy for a block this size.

    Brickwall limiting happens outside this function (stereo-linked).
    """
    center = place_center(tts_mono, tts_gain)
    if not route_ambient:
        return center
    # mid_gain may be a scalar or a (n,) array — the delayed control envelope.
    mg = np.asarray(mid_gain, dtype=np.float32)
    m = 0.5 * (amb[:, 0] + amb[:, 1]) * mg
    s = 0.5 * (amb[:, 0] - amb[:, 1]) * float(width)
    out = np.empty_like(center)
    out[:, 0] = (m + s) + center[:, 0]
    out[:, 1] = (m - s) + center[:, 1]
    return out


class Player:
    """Mixes TTS (phantom-center) and optional stereo ambient (M/S widened) onto
    a 2-channel float32 stream feeding the output device. In driverless mode the
    ambient is never fed in, so route_ambient stays False and only the center
    TTS is audible — the original audio plays straight from Windows.
    """

    def __init__(self, device: int | None, tts_in_rate: int = 24000, channels: int = 2,
                 max_ambient_delay_ms: float = 400.0, tts_enhance=None):
        self.rate = device_rate(device, "output")
        self.channels = channels
        self.tts_gain = 1.0
        # The user's system volume, mirrored onto our output. Only ever moved off
        # 1.0 in vbcable mode, where Windows' default endpoint is the cable and the
        # volume keys therefore no longer reach what the user is hearing.
        self.master_gain = 1.0
        self.width = 1.25
        self.mid_gain = 1.0
        self.route_ambient = False
        self.delay_target_samples = 0.0
        # Hard ceiling on ambient delay — soundtrack never drifts from the
        # video far enough to break A/V sync.
        self._max_delay_samples = float(max_ambient_delay_ms) * self.rate / 1000.0
        # Three channels: [ambL, ambR, mid_gain] travel through the same delay
        # line so the suppression envelope stays aligned with the delayed dialog.
        self._amb_delay = DelayLine(self.rate, channels=3, max_slew=1.0,
                                    max_delay=self._max_delay_samples)
        # Leftover odd byte from a streamed PCM chunk — carried to the next push.
        # Guarded by _tts_lock: feed_tts_pcm16 mutates the carry and pushes the
        # ring, while clear_tts resets both. Without the lock a session rotation
        # or mid-stream clear racing a feed could strand a byte and shift the
        # whole int16 stream by one byte → full-scale noise.
        self._tts_rem = b""
        self._tts_lock = threading.Lock()
        self.passthrough = _Ring(2.0, self.rate, channels=2)
        # Ambient (passthrough) rate conversion + jitter buffer. The capture
        # device (e.g. CABLE Output) and this output device are INDEPENDENT
        # WASAPI clocks: without rate conversion AND a small standing buffer the
        # ring under/overruns and the pulled blocks click. Resamplers are
        # per-channel (the streaming resampler is mono) and identity/no-op when
        # the rates match — so a matched-rate device pays only the jitter buffer.
        self._pass_in_rate = None
        self._pass_rs_l = None
        self._pass_rs_r = None
        self._pass_prefill = int(self.rate * 0.08)  # ~80 ms drift/jitter slack
        # Headroom must exceed the longest single translated utterance: Gemini
        # delivers a turn's audio faster than realtime and the callback drains at
        # realtime, so the unplayed tail of a long sentence sits here. With
        # drop_newest the surplus from cross-turn pile-up is discarded instead of
        # overwriting the START of the utterance already queued, and tts.overflows
        # makes the condition observable. 45 s covers any realistic sentence
        # (~4 MB) while bounding pile-up far below the old 120 s.
        self.tts = _Ring(45.0, self.rate, channels=1, drop_newest=True)
        self._tts_in_rate = tts_in_rate
        # Optional injected voice-enhancement callable (mono float @ tts_in_rate
        # -> mono float). Supplied by the caller on builds that have it; None
        # leaves the TTS stream untouched. Kept as an opaque callable so this
        # module carries no dependency on the enhancement implementation.
        self._tts_enhance = tts_enhance
        self._tts_resample = _make_resampler(tts_in_rate, self.rate)
        blocksize = int(self.rate * 0.02)
        # Stereo-linked look-ahead limiter: gain is derived from the max peak
        # across both channels so the phantom-center image cannot shift on
        # transients (no inter-channel drift).
        self._limiter = LookaheadLimiter(self.rate, ceiling=0.97)

        def _cb(outdata, frames, time_info, status):
            # Snapshot scalar controls once so a concurrent setter cannot change
            # the math mid-block (e.g. tts_gain applied to half the frames).
            route_ambient = self.route_ambient
            tts_gain = self.tts_gain
            mid_gain = self.mid_gain
            width = self.width
            delay_target = self.delay_target_samples
            tts_mono = self.tts.pull(frames).reshape(-1)
            if route_ambient:
                # Drain the ambient ring ONLY once routing is live. Pulling during
                # the prefill window would empty the jitter buffer as fast as it
                # fills, so the fill threshold would never trip and ambient would
                # never route (→ video muted, only TTS audible).
                amb = self.passthrough.pull(frames)
                self._amb_delay.set_target(delay_target)
                mg = np.full((frames, 1), mid_gain, dtype=np.float32)
                d = self._amb_delay.process(np.concatenate([amb, mg], axis=1))
                stereo = _mix_to_stereo(tts_mono, d[:, :2], tts_gain,
                                        width, True, d[:, 2])
            else:
                amb = np.zeros((frames, 2), dtype=np.float32)
                stereo = _mix_to_stereo(tts_mono, amb, tts_gain, width, False)
            y = self._limiter.process(stereo)
            # The user's own volume control, applied AFTER the limiter: the limiter
            # protects the mix, the volume scales what leaves the app. 1.0 unless a
            # mirror is running (see endpoint_volume — vbcable mode only).
            master = self.master_gain
            if master != 1.0:
                y = y * master
            # Cheap finite-value safety net on the device-bound buffer: a single
            # non-finite sample reaching the driver can blast the speakers.
            y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
            outdata[:] = y if self.channels == 2 else y.mean(axis=1, keepdims=True)

        self.stream = sd.OutputStream(
            device=device,
            samplerate=self.rate,
            channels=channels,
            dtype="float32",
            blocksize=blocksize,
            latency=LATENCY,
            callback=_cb,
        )

    @property
    def tts_active(self) -> bool:
        return self.tts.fill > 0

    def clear_tts(self):
        # Drop the queued audio and the half-sample carry together under the
        # same lock: a stranded odd byte surviving a clear would re-pair with the
        # next turn's first byte and shift the int16 stream into full-scale noise.
        with self._tts_lock:
            self._tts_rem = b""
            self.tts.clear()

    def configure_passthrough(self, in_rate: int):
        """Set up ambient rate conversion from the capture device's rate to this
        output device's rate. Two mono streaming resamplers (one per channel); a
        1.0 ratio is a no-op, so matched-rate devices pay nothing here. Call once
        after the capture is opened and before audio flows."""
        self._pass_in_rate = int(in_rate)
        if int(in_rate) != self.rate:
            self._pass_rs_l = _make_resampler(int(in_rate), self.rate)
            self._pass_rs_r = _make_resampler(int(in_rate), self.rate)
        else:
            self._pass_rs_l = self._pass_rs_r = None

    def feed_passthrough(self, chunk: np.ndarray):
        # Rate-convert the captured ambient to the output rate, per channel (the
        # streaming resampler is mono). Skipped when configure_passthrough saw a
        # matched rate. Without it the two device clocks drift the ring into
        # under/overrun and every pulled block clicks.
        chunk = np.asarray(chunk, dtype=np.float32)
        if self._pass_rs_l is not None and chunk.ndim == 2 and chunk.shape[1] >= 2:
            l = self._pass_rs_l(np.ascontiguousarray(chunk[:, 0]))
            r = self._pass_rs_r(np.ascontiguousarray(chunk[:, 1]))
            n = min(len(l), len(r))
            if n == 0:
                return
            chunk = np.stack([l[:n], r[:n]], axis=1)
        self.passthrough.push(chunk)
        # Enable the M/S widening path only once a small jitter buffer has built
        # up, so the first blocks (and minor clock drift) never pull a near-empty
        # ring and silence-pad it into clicks. A mono chunk is auto-promoted by
        # _Ring (L == R → Side = 0); a real stereo capture is required for the
        # width effect to be audible. route_ambient latches on (set only here;
        # __init__ leaves it False), so a transient dip cannot toggle it back off.
        if not self.route_ambient and self.passthrough.fill >= self._pass_prefill:
            self.route_ambient = True

    def feed_tts_pcm16(self, data: bytes):
        # Streaming chunks may split a 16-bit sample in half — carry the odd byte.
        # The carry + ring push are atomic under _tts_lock so a concurrent
        # clear_tts (session rotation) cannot interleave and desync the stream.
        with self._tts_lock:
            data = self._tts_rem + data
            if len(data) & 1:
                self._tts_rem = data[-1:]
                data = data[:-1]
            else:
                self._tts_rem = b""
            if not data:
                return
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            # Scrub non-finite samples before they reach the ring/limiter; a
            # corrupt PCM frame must not poison the gain envelope downstream.
            x = np.nan_to_num(x, copy=False)
            # Optional voice enhancement on the decoded mono stream, ahead of the
            # user gain / resample / limiter. The callable is self-guarding and
            # returns the input unchanged on any error, so the TTS path is never
            # broken by it.
            if self._tts_enhance is not None:
                x = np.asarray(self._tts_enhance(x, self._tts_in_rate), dtype=np.float32)
            if self.tts_gain != 1.0:
                x = x * self.tts_gain
            self.tts.push(self._tts_resample(x))

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()


if __name__ == "__main__":
    # Device listing: python -m app.audio_io
    for i, dev in enumerate(sd.query_devices()):
        io = []
        if dev["max_input_channels"]:
            io.append(f"in:{dev['max_input_channels']}")
        if dev["max_output_channels"]:
            io.append(f"out:{dev['max_output_channels']}")
        print(f"[{i:3d}] {dev['name']}  ({', '.join(io)}, {int(dev['default_samplerate'])} Hz)")
