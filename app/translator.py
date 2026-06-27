"""Wrapper around a Gemini Live translation session.

gemini-3.5-live-translate-preview: 16 kHz PCM16 in → 24 kHz PCM16 out, one
direction per session. Meeting mode opens two instances. The 15-minute audio
session ceiling is handled by rotating the connection ahead of that limit
(rotate_minutes, plus a hard deadline so the ceiling is never missed even
mid-speech). Each instance owns an asyncio loop on its own thread.

The translate model is a native simultaneous interpreter: fed the continuous
stream it translates as the speaker talks and self-balances quality vs sync, so
the client sends NO realtime_input_config and lets the model own its endpointing.
"""
import asyncio
import threading
import time
import traceback

from google import genai
from google.genai import types

from .config import GEMINI_LIVE_MODEL
from .i18n import t

# Live session resumption + GoAway handling — enabled only if this google-genai
# build exposes the type (older builds silently skip it; the path still works).
_SUPPORTS_RESUMPTION = hasattr(types, "SessionResumptionConfig")

# Bounded input buffer: 50 frames * 32 ms ≈ 1.6 s. Small enough that a network
# stall cannot inflate latency with a long stale backlog; on overflow the
# OLDEST frame is dropped so the session keeps translating live audio.
_QUEUE_MAX = 50

# Hard rotation deadline. The server ceiling is 15 min; we plan a quiet-window
# rotation at rotate_minutes, but if the speaker never pauses we must still cut
# over before the server does. 14.5 min leaves headroom for connect + drain so
# the ceiling is never hit mid-stream (which would drop the connection raw).
_HARD_ROTATE_SECONDS = 14.5 * 60

# After signalling rotation, let the receiver finish the in-flight model_turn so
# the tail of the previous translation is played out rather than truncated.
_ROTATE_DRAIN_SECONDS = 1.5

# Transient drops are retried with backoff; cap consecutive failures so a hard
# outage surfaces a single actionable status instead of spinning forever.
_MAX_TRANSIENT_FAILURES = 8

# A session the server accepts then closes in under this many seconds (no
# exception, no planned rotation) is treated as a covert transient failure —
# otherwise an account/region/endpoint reject would spin a no-backoff reconnect
# loop at full speed. A real session always lives far longer than this.
_MIN_SESSION_SECONDS = 5.0

# Live API audio pricing, USD per minute (input + output).
COST_IN_PER_MIN = 0.0053
COST_OUT_PER_MIN = 0.0315

# Process-cumulative accounting across all sessions/instances — lifetime totals
# behind the cost estimate the UI surfaces via get_usage().
_USAGE_LOCK = threading.Lock()
_USAGE = {"in_sec": 0.0, "out_sec": 0.0, "dropped_frames": 0}


def _add_usage(key: str, amount: float):
    with _USAGE_LOCK:
        _USAGE[key] += amount


def get_usage() -> tuple[float, float, float]:
    """(seconds sent, seconds received, estimated USD) since process start."""
    with _USAGE_LOCK:
        i, o = _USAGE["in_sec"], _USAGE["out_sec"]
    return i, o, i / 60 * COST_IN_PER_MIN + o / 60 * COST_OUT_PER_MIN


# Terminal-failure substrings: auth/permission/quota and 4xx client errors are
# not recoverable by retrying with the same key, so the loop must stop with an
# actionable status rather than reconnect forever (burning no quota but also
# never working). Matched case-insensitively against the exception text.
# Auth/permission/quota are not recoverable by retrying with the same key.
# HTTP-style status codes are matched STRUCTURALLY (from the SDK/transport
# exception), never as substrings of the free-form message — error text routinely
# embeds unrelated numbers (byte counts, ports, request ids) that would otherwise
# be misread as a status code and stop the loop on a transient drop. The phrase
# markers are specific enough to match against the message text directly.
# 429 is NOT terminal: a bare rate-limit is transient and recovers with backoff.
# Genuine quota exhaustion (which also returns 429) is still caught below via the
# "quota"/"resource_exhausted"/"billing" phrase markers, so only a true transient
# rate-limit reconnects instead of killing the session.
_TERMINAL_CODES = {401, 403, 404}
_TERMINAL_PHRASES = (
    "invalid api key",
    "api key not valid",
    "api_key_invalid",
    "permission denied",
    "permissiondenied",
    "unauthenticated",
    "unauthorized",
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "billing",
)


def _is_terminal_error(exc: Exception) -> bool:
    # Prefer a structured status code (google.genai APIError.code, a status_code
    # attr, etc.) over substring sniffing.
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if callable(val):
            try:
                val = val()
            except Exception:
                val = None
        try:
            if val is not None and int(val) in _TERMINAL_CODES:
                return True
        except (TypeError, ValueError):
            pass
    text = str(exc).lower()
    return any(p in text for p in _TERMINAL_PHRASES)


class LiveTranslator(threading.Thread):
    def __init__(
        self,
        api_key: str,
        target_lang: str,
        on_audio,
        on_text,
        on_status,
        rotate_minutes: float = 13,
        name: str = "translator",
        voice: str = "Aoede",
        temperature: float = 0.3,
        model: str = GEMINI_LIVE_MODEL,
    ):
        super().__init__(daemon=True, name=name)
        self.api_key = api_key
        self.target_lang = target_lang
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_status = on_status
        self.voice = voice
        self.temperature = temperature
        self.model = model
        self.rotate_seconds = rotate_minutes * 60
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        # Frames carried over a rotation: re-injected into the next session so
        # the ~1-2 s of unsent source audio at cutover is not lost.
        self._carryover: list[bytes] = []
        self._resume_handle = None  # session-resumption token for seamless reconnect

    def send_pcm16(self, data: bytes):
        if self._loop and self._queue and not self._stopping.is_set():
            try:
                self._loop.call_soon_threadsafe(self._put_nowait, data)
            except RuntimeError:
                # The loop is shutting down — drop the frame.
                pass

    def _put_nowait(self, item):
        try:
            self._queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        # Backpressure: drop the OLDEST frame to keep the freshest audio. Runs on
        # the loop thread with no await, so it is atomic vs the single _sender
        # consumer. Rare path — count the loss so sustained drops are visible
        # in telemetry rather than silently degrading translation quality.
        try:
            self._queue.get_nowait()
            _add_usage("dropped_frames", 1)
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def stop(self):
        self._stopping.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def wait_ready(self, timeout: float = 15) -> bool:
        return self._ready.wait(timeout)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            # The translator thread must not die silently — surface the error.
            if not self._stopping.is_set():
                self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                traceback.print_exc()
        finally:
            self._stopping.set()
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    async def _main(self):
        # Native simultaneous setup: send NO realtime_input_config. The translate
        # model ingests the continuous stream and self-balances quality vs sync
        # ("a few seconds behind the speaker"), which is its lowest-latency mode.
        config = {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "temperature": self.temperature,
            # Translation is not reasoning — disable thinking for lower latency.
            "thinking_config": {"thinking_budget": 0},
            "translation_config": {
                "target_language_code": self.target_lang,
                "echo_target_language": False,
            },
            # Locked prebuilt voice — the strongest stability setting the client
            # API exposes for the translate-preview model.
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}},
            },
        }
        backoff = 1.0
        transient_failures = 0
        last_error_text = None
        client = None
        while not self._stopping.is_set():
            # _ready (waited on by wait_ready) must mean "a session is currently
            # live": clear on every iteration so the gap during reconnect/rotation
            # is never reported ready.
            self._ready.clear()
            # Marks when the current session became live; stays None until ready so
            # the error/exit paths can tell a connect failure from a dropped session.
            started = None
            # Resume the prior session so a rotation / GoAway / drop reconnects
            # seamlessly instead of cold-starting (handle=None = fresh session).
            if _SUPPORTS_RESUMPTION:
                config["session_resumption"] = {"handle": self._resume_handle}
            try:
                if client is None:
                    # Disable WebSocket permessage-deflate: PCM16 audio is
                    # high-entropy so it never compresses, and deflate only adds
                    # per-frame CPU and a flush boundary on the 32 ms cadence.
                    client = genai.Client(
                        api_key=self.api_key,
                        http_options=types.HttpOptions(
                            async_client_args={"compression": None}
                        ),
                    )
                async with client.aio.live.connect(model=self.model, config=config) as session:
                    # Carry the unsent tail of the previous session into this one
                    # before the gate fills the queue with fresh audio, so the
                    # rotation cutover loses no source frames.
                    self._reinject_carryover()
                    self.on_status(t("st_connected", name=self.name, lang=self.target_lang))
                    self._ready.set()
                    backoff = 1.0
                    last_error_text = None
                    # transient_failures is cleared only once the session proves
                    # healthy (lived past _MIN_SESSION_SECONDS or rotated), not on the
                    # bare connect — otherwise an accept-then-immediately-close server
                    # would reset the cap every iteration and never stop reconnecting.
                    started = time.monotonic()
                    sender = asyncio.create_task(self._sender(session, started))
                    receiver = asyncio.create_task(self._receiver(session))
                    done, pending = await asyncio.wait(
                        {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
                    )
                    rotating = any(
                        isinstance(task.exception(), _Rotate)
                        for task in done
                        if not task.cancelled()
                    )
                    if rotating and not self._stopping.is_set():
                        # Overlap-and-drain: keep the old session's receiver alive
                        # for a short grace window so the in-flight model_turn (the
                        # previous translation tail) plays out. Snapshot the unsent
                        # queue AFTER the drain so it captures the true remaining
                        # tail in arrival order — snapshotting before would let
                        # frames that arrive during the grace window jump ahead of
                        # the carryover when reinjected into the next session.
                        await self._drain_receiver(receiver)
                        self._snapshot_carryover()
                    # Session is no longer live — stop reporting ready before teardown.
                    self._ready.clear()
                    for task in pending:
                        task.cancel()
                    for task in done:
                        if task.cancelled():
                            continue
                        exc = task.exception()
                        if exc and not isinstance(exc, _Rotate):
                            raise exc
                # The session ended without raising. A planned rotation or a session
                # that stayed alive past the minimum lifetime is a healthy end: clear
                # the transient machinery and reconnect cleanly. A session the server
                # accepted then closed almost immediately (no exception, no rotation)
                # is a covert failure — back off and bound it like any transient drop
                # so the client never spins reconnecting a dead endpoint forever.
                if rotating or (started is not None
                                and time.monotonic() - started >= _MIN_SESSION_SECONDS):
                    transient_failures = 0
                    if not self._stopping.is_set():
                        self.on_status(t("st_renewing", name=self.name))
                elif not self._stopping.is_set():
                    self._resume_handle = None
                    transient_failures += 1
                    if transient_failures >= _MAX_TRANSIENT_FAILURES:
                        self.on_status(t("st_conn_err", name=self.name, s=0,
                                         e="server closed session immediately"))
                        break
                    self.on_status(t("st_conn_err", name=self.name, s=backoff,
                                     e="server closed session immediately"))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.6, 6)
            except _Rotate:
                # Defensive: a _Rotate must not be treated as a transient error.
                self._ready.clear()
                continue
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as e:
                self._ready.clear()
                if self._stopping.is_set():
                    break
                # Terminal (auth/permission/quota/4xx): retrying with the same key
                # cannot succeed — stop with one actionable status.
                if _is_terminal_error(e):
                    self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                    traceback.print_exc()
                    break
                # A session that ran past the minimum lifetime proves the path works;
                # a later drop starts a fresh failure streak (preserves the original
                # "a successful connection clears the failure count" resilience).
                if started is not None and time.monotonic() - started >= _MIN_SESSION_SECONDS:
                    transient_failures = 0
                transient_failures += 1
                # A failed reconnect may be a stale/expired resume handle — drop it
                # so the next attempt starts a fresh session.
                self._resume_handle = None
                if transient_failures >= _MAX_TRANSIENT_FAILURES:
                    self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                    traceback.print_exc()
                    break
                self.on_status(t("st_conn_err", name=self.name, s=backoff, e=e))
                # Suppress repeated identical tracebacks: a flapping link otherwise
                # floods stderr with the same stack on every retry.
                err_text = repr(e)
                if err_text != last_error_text:
                    traceback.print_exc()
                    last_error_text = err_text
                await asyncio.sleep(backoff)
                # Cap reconnect backoff at 6 s to recover quickly from transient drops.
                backoff = min(backoff * 1.6, 6)

    def _snapshot_carryover(self):
        """Move every unsent queued frame into the carryover buffer (loop thread,
        no await — atomic vs the single sender). Bounded by _QUEUE_MAX."""
        self._carryover = []
        while True:
            try:
                self._carryover.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

    def _reinject_carryover(self):
        """Push carried-over frames into the fresh session's queue, oldest first,
        so the rotation tail is sent before any new audio."""
        if not self._carryover:
            return
        for frame in self._carryover:
            self._put_nowait(frame)
        self._carryover = []

    async def _drain_receiver(self, receiver: asyncio.Task):
        """Give the old session's receiver a brief grace period to emit the tail
        of the in-flight model_turn before the session is torn down."""
        try:
            await asyncio.wait_for(
                asyncio.shield(receiver), timeout=_ROTATE_DRAIN_SECONDS
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            # A receiver error during drain is non-fatal: we are rotating anyway.
            pass

    async def _sender(self, session, started: float):
        while not self._stopping.is_set():
            # Rotation is checked every iteration, independent of the quiet window,
            # so a steady-state stream still rotates on schedule. The hard deadline
            # forces a cutover even mid-speech before the 15-min server ceiling.
            elapsed = time.monotonic() - started
            if elapsed > self.rotate_seconds or elapsed > _HARD_ROTATE_SECONDS:
                raise _Rotate()
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # Quiet window — loop back and re-check the rotation deadline.
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=item, mime_type="audio/pcm;rate=16000")
            )
            secs = len(item) / 32000
            _add_usage("in_sec", secs)

    async def _receiver(self, session):
        async for resp in session.receive():
            if self._stopping.is_set():
                return
            # Track the resumption handle; on GoAway rotate now (carryover + the
            # handle make the reconnect seamless) instead of waiting for the drop.
            sru = getattr(resp, "session_resumption_update", None)
            if sru is not None and getattr(sru, "resumable", False) and getattr(sru, "new_handle", None):
                self._resume_handle = sru.new_handle
            if getattr(resp, "go_away", None) is not None:
                raise _Rotate()
            sc = resp.server_content
            if sc is None:
                continue
            if sc.model_turn:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        self.on_audio(part.inline_data.data)
                        secs = len(part.inline_data.data) / 48000
                        _add_usage("out_sec", secs)
                    if getattr(part, "text", None):
                        self.on_text("out", part.text)
            it = getattr(sc, "input_transcription", None)
            if it and it.text:
                self.on_text("in", it.text)
            ot = getattr(sc, "output_transcription", None)
            if ot and ot.text:
                self.on_text("out", ot.text)


class _Rotate(Exception):
    """Signals a planned reconnect ahead of the 15-minute session ceiling."""
