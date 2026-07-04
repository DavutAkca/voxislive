"""Shared session machine for every realtime translation engine.

All three engines — Gemini Live (SDK), OpenAI gpt-realtime-translate and
Qwen3.5-LiveTranslate (both raw websockets) — run the SAME field-hardened
lifecycle: a bounded drop-oldest input queue, carryover across a planned
rotation, backoff-bounded reconnect with transient/terminal classification, a
covert-immediate-close guard, and two watchdogs (stall on sent-audio-with-no-
server-events, and no-output-despite-input). That machinery lived copy-pasted in
three files and drifted (see the 2026-07-04 audit P0 #6); it now lives here once.

Subclass contract — a concrete engine supplies only its protocol:
  * class attrs: HARD_ROTATE_SECONDS, IN_BYTES_PER_SEC (usage divisor),
    TERMINAL_PHRASES, READY_ON_CONNECT (True = _ready fires the moment the
    connection opens, as Gemini/Qwen do; False = the receiver sets it on a
    server 'session live' event, as OpenAI does).
  * async _connect(self) -> raw connection (websocket engines only; the base
    wraps it in a close-on-exit context manager). Gemini overrides _session_cm
    instead, since its connection IS an async context manager (the SDK).
  * async _open_session(self, conn): send any post-connect config (websocket
    session.update). No-op for Gemini (config is bundled into connect()).
  * async _sender_frame(self, conn, item): send ONE input frame, then call
    self._account_sent(len(item)) for usage/stall accounting.
  * async _receive_loop(self, conn): parse the server event stream, calling the
    shared helpers _reset_stall / _mark_input / _mark_output, self.on_audio /
    self.on_text, self._record_usage("out_sec", …), and raising _Rotate on a
    GoAway. Gemini-only resume-handle capture lives here too.
  * optional hooks: _reset_session_state (reset per-connection parse state),
    _reset_reconnect_state (drop state that must not survive a failed reconnect,
    e.g. Gemini's stale resume handle).

The behavior constants below are the pre-consolidation values; each engine keeps
its own via class attrs. They are NOT tuning knobs to revisit here.
"""
import asyncio
import contextlib
import threading
import time
import traceback

from .i18n import t


class _Rotate(Exception):
    """Signals a planned reconnect ahead of the session ceiling (or a watchdog
    trip / GoAway). Never treated as a transient error."""


# HTTP-style status codes that mean "retrying with the same key cannot succeed".
# 429 is deliberately absent: a bare rate-limit is transient and recovers with
# backoff (genuine quota exhaustion is caught by the per-engine phrase markers).
_DEFAULT_TERMINAL_CODES = frozenset({401, 403, 404})


def is_terminal_error(exc, phrases, codes=_DEFAULT_TERMINAL_CODES) -> bool:
    """True when `exc` is a non-retryable auth/permission/quota/4xx failure.

    Prefers a STRUCTURED status code (google.genai APIError.code, a status_code
    attr, …) over substring sniffing, because free-form error text routinely
    embeds unrelated numbers (byte counts, ports, request ids) that would be
    misread as a status code. The phrase markers are specific enough to match the
    message text directly."""
    for attr in ("code", "status_code"):
        val = getattr(exc, attr, None)
        if callable(val):
            try:
                val = val()
            except Exception:
                val = None
        try:
            if val is not None and int(val) in codes:
                return True
        except (TypeError, ValueError):
            pass
    text = str(exc).lower()
    return any(p in text for p in phrases)


class BaseTranslator(threading.Thread):
    # --- behavior constants (overridable per engine) ------------------------
    # Bounded input buffer: 50 frames * 32 ms ≈ 1.6 s. On overflow the OLDEST
    # frame is dropped so the session keeps translating the freshest audio.
    QUEUE_MAX = 50
    # After signalling rotation, let the receiver finish the in-flight model_turn
    # so the tail of the previous translation plays out rather than truncated.
    ROTATE_DRAIN_SECONDS = 1.5
    # Cap consecutive transient failures so a hard outage surfaces one actionable
    # status instead of spinning forever.
    MAX_TRANSIENT_FAILURES = 8
    # A session the server accepts then closes under this many seconds (no error,
    # no planned rotation) is a covert transient failure — otherwise an
    # account/region/endpoint reject would spin a no-backoff reconnect loop.
    MIN_SESSION_SECONDS = 5.0
    # Stall watchdog: this many SECONDS OF SENT AUDIO with zero server events
    # means the socket is silently dead (TCP black hole after sleep/resume) — a
    # planned rotation is forced. Measured in sent-audio seconds, never wall
    # clock, so quiet periods can never trip it.
    STALL_ROTATE_SECONDS = 20.0
    # No-output watchdog: input transcription arriving but NO output (audio or
    # output transcription) for this long surfaces one actionable status. Gated
    # on RECENT input so a genuine quiet stretch can never trip it.
    NO_OUTPUT_WARN_SECONDS = 12.0
    INPUT_RECENT_SECONDS = 4.0
    # Hard rotation deadline (per engine's session ceiling) and the usage
    # accounting divisor (bytes/sec of the engine's INPUT sample rate).
    HARD_ROTATE_SECONDS = 14.5 * 60
    IN_BYTES_PER_SEC = 32000  # 16 kHz PCM16
    # Terminal-failure classification.
    TERMINAL_CODES = _DEFAULT_TERMINAL_CODES
    TERMINAL_PHRASES = ()
    # When _ready fires: True = on connect (Gemini/Qwen), False = the receiver
    # sets it on a server 'session live' event (OpenAI).
    READY_ON_CONNECT = True

    # Lazily-bound usage sink (translator._add_usage). Kept off the module import
    # so base_translator stays free of google.genai on the websocket cold path.
    _USAGE_ADD = None

    def __init__(self, api_key, target_lang, on_audio, on_text, on_status, *,
                 rotate_minutes, name="translator"):
        super().__init__(daemon=True, name=name)
        self.api_key = api_key
        self.target_lang = target_lang
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_status = on_status
        self.rotate_seconds = rotate_minutes * 60
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        # Frames carried over a rotation: re-injected into the next session so the
        # ~1-2 s of unsent source audio at cutover is not lost.
        self._carryover: list[bytes] = []
        # Stall watchdog accumulator (loop-thread only, no locking): seconds of
        # audio sent since the last server event. Sender adds, receiver zeroes.
        self._sent_since_recv = 0.0
        # No-output watchdog state (loop-thread only): last input/output monotonic
        # timestamps + a once-per-session latch.
        self._last_input_ts = 0.0
        self._last_output_ts = 0.0
        self._no_output_warned = False
        # When the current session became live (None until ready), so the
        # error/exit paths can tell a connect failure from a dropped session.
        self._session_started: float | None = None
        # Reconnect backoff + last-printed error, promoted to fields so a healthy
        # reconnect (inside _run_session) can reset them.
        self._backoff = 1.0
        self._last_error_text = None

    # --- public contract (identical across engines) -------------------------
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
        # the loop thread with no await, so it is atomic vs the single sender
        # consumer. Count the loss so sustained drops are visible in telemetry.
        try:
            self._queue.get_nowait()
            self._record_usage("dropped_frames", 1)
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def stop(self):
        self._stopping.set()
        # Nudge the loop so a stop lands promptly even mid-backoff / mid-connect
        # (guarded for an already-closed loop).
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass

    def wait_ready(self, timeout: float = 15) -> bool:
        return self._ready.wait(timeout)

    # --- usage / watchdog helpers (shared) ----------------------------------
    def _record_usage(self, key: str, amount: float):
        add = BaseTranslator._USAGE_ADD
        if add is None:
            from .translator import _add_usage  # lazy: keep vendor runtimes off cold start
            add = BaseTranslator._USAGE_ADD = _add_usage
        add(key, amount)

    def _account_sent(self, nbytes: int):
        secs = nbytes / self.IN_BYTES_PER_SEC
        self._record_usage("in_sec", secs)
        self._sent_since_recv += secs

    def _reset_stall(self):
        # Any server event proves the connection is alive.
        self._sent_since_recv = 0.0

    def _mark_input(self):
        self._last_input_ts = time.monotonic()

    def _mark_output(self):
        self._last_output_ts = time.monotonic()

    def _reset_watchdogs(self):
        self._sent_since_recv = 0.0
        self._last_input_ts = 0.0
        self._last_output_ts = 0.0
        self._no_output_warned = False

    # --- subclass hooks (defaults) ------------------------------------------
    def _session_cm(self):
        """Connection lifecycle as an async context manager. Default = websocket
        style: dial via _connect(), close on exit. Gemini overrides with the SDK's
        own async context manager."""
        return self._ws_session_cm()

    @contextlib.asynccontextmanager
    async def _ws_session_cm(self):
        ws = await self._connect()
        try:
            yield ws
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    async def _connect(self):
        raise NotImplementedError

    async def _open_session(self, conn):
        """Post-connect session config. No-op for engines that bundle config into
        connect (Gemini)."""
        return None

    async def _sender_frame(self, conn, item):
        raise NotImplementedError

    async def _receive_loop(self, conn):
        raise NotImplementedError

    def _reset_session_state(self):
        """Reset per-connection parse state (e.g. cumulative-caption accumulators)
        on each new session. Base: nothing."""
        return None

    def _reset_reconnect_state(self):
        """Drop connection-scoped state that must not survive a FAILED reconnect
        (e.g. Gemini's stale resume handle). Base: nothing."""
        return None

    def _is_terminal(self, exc) -> bool:
        return is_terminal_error(exc, self.TERMINAL_PHRASES, self.TERMINAL_CODES)

    # --- thread body --------------------------------------------------------
    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue(maxsize=self.QUEUE_MAX)
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
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()

    async def _main(self):
        self._backoff = 1.0
        self._last_error_text = None
        transient_failures = 0
        while not self._stopping.is_set():
            # _ready (waited on by wait_ready) must mean "a session is currently
            # live": clear on every iteration so the reconnect/rotation gap is
            # never reported ready.
            self._ready.clear()
            self._session_started = None
            try:
                rotating = await self._run_session()
                started = self._session_started
                # A planned rotation, or a session that stayed alive past the
                # minimum lifetime, is a healthy end: clear the transient
                # machinery and reconnect cleanly. A session the server accepted
                # then closed almost immediately (no error, no rotation) is a
                # covert failure — back off and bound it like any transient drop.
                if rotating or (started is not None
                                and time.monotonic() - started >= self.MIN_SESSION_SECONDS):
                    transient_failures = 0
                    if not self._stopping.is_set():
                        self.on_status(t("st_renewing", name=self.name))
                elif not self._stopping.is_set():
                    self._reset_reconnect_state()
                    transient_failures += 1
                    if transient_failures >= self.MAX_TRANSIENT_FAILURES:
                        self.on_status(t("st_conn_err", name=self.name, s=0,
                                         e="server closed session immediately"))
                        break
                    self.on_status(t("st_conn_err", name=self.name, s=self._backoff,
                                     e="server closed session immediately"))
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 1.6, 6)
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
                if self._is_terminal(e):
                    self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                    traceback.print_exc()
                    break
                # A session that ran past the minimum lifetime proves the path
                # works; a later drop starts a fresh failure streak.
                started = self._session_started
                if started is not None and time.monotonic() - started >= self.MIN_SESSION_SECONDS:
                    transient_failures = 0
                transient_failures += 1
                self._reset_reconnect_state()
                if transient_failures >= self.MAX_TRANSIENT_FAILURES:
                    self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                    traceback.print_exc()
                    break
                self.on_status(t("st_conn_err", name=self.name, s=self._backoff, e=e))
                # Suppress repeated identical tracebacks: a flapping link would
                # otherwise flood stderr with the same stack on every retry.
                err_text = repr(e)
                if err_text != self._last_error_text:
                    traceback.print_exc()
                    self._last_error_text = err_text
                await asyncio.sleep(self._backoff)
                # Cap reconnect backoff at 6 s to recover quickly from drops.
                self._backoff = min(self._backoff * 1.6, 6)

    async def _run_session(self) -> bool:
        """One connect→serve→teardown cycle. Returns True if the session ended on
        a planned rotation (vs a plain close). Raises on a real error so _main can
        classify it terminal/transient."""
        async with self._session_cm() as conn:
            # Config send + carryover reinjection both complete before the sender
            # task starts, so their order is immaterial; do config first to mirror
            # the websocket engines (Gemini's _open_session is a no-op).
            await self._open_session(conn)
            self._reinject_carryover()
            self.on_status(t("st_connected", name=self.name, lang=self.target_lang))
            if self.READY_ON_CONNECT:
                self._ready.set()
            # A live connection proves the path works: reset backoff so a later
            # drop starts fresh, and clear the dedup so its first error prints.
            self._backoff = 1.0
            self._last_error_text = None
            self._reset_watchdogs()
            self._reset_session_state()
            self._session_started = time.monotonic()
            started = self._session_started
            sender = asyncio.create_task(self._sender(conn, started))
            receiver = asyncio.create_task(self._receive_loop(conn))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            rotating = any(isinstance(tsk.exception(), _Rotate)
                           for tsk in done if not tsk.cancelled())
            if rotating and not self._stopping.is_set():
                # Overlap-and-drain: keep the old receiver alive briefly so the
                # in-flight model_turn plays out, THEN snapshot the unsent queue so
                # frames arriving during the grace window keep arrival order.
                await self._drain_receiver(receiver)
                self._snapshot_carryover()
            # Session is no longer live — stop reporting ready before teardown.
            self._ready.clear()
            for tsk in pending:
                tsk.cancel()
            for tsk in done:
                if tsk.cancelled():
                    continue
                exc = tsk.exception()
                if exc and not isinstance(exc, _Rotate):
                    raise exc
        return rotating

    # --- carryover (shared) -------------------------------------------------
    def _snapshot_carryover(self):
        """Move every unsent queued frame into the carryover buffer (loop thread,
        no await — atomic vs the single sender). Bounded by QUEUE_MAX."""
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
            await asyncio.wait_for(asyncio.shield(receiver),
                                   timeout=self.ROTATE_DRAIN_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            # A receiver error during drain is non-fatal: we are rotating anyway.
            pass

    # --- sender loop (shared; frame send delegated to _sender_frame) --------
    async def _sender(self, conn, started: float):
        while not self._stopping.is_set():
            # Rotation is checked every iteration, independent of the quiet window,
            # so a steady-state stream still rotates on schedule. The hard deadline
            # forces a cutover even mid-speech before the server ceiling.
            elapsed = time.monotonic() - started
            if elapsed > self.rotate_seconds or elapsed > self.HARD_ROTATE_SECONDS:
                raise _Rotate()
            # Stall watchdog: sent audio piling up with zero server events means a
            # silently dead socket — rotate instead of streaming into a black hole
            # (a hung TCP connection may never raise in the receiver).
            if self._sent_since_recv >= self.STALL_ROTATE_SECONDS:
                self._sent_since_recv = 0.0
                self.on_status("translator: no server events for %ds of sent "
                               "audio — reconnecting" % int(self.STALL_ROTATE_SECONDS))
                raise _Rotate()
            # No-output watchdog: input transcription recently but no output.
            now = time.monotonic()
            if self._last_input_ts > 0.0 and not self._no_output_warned:
                if now - self._last_input_ts <= self.INPUT_RECENT_SECONDS:
                    last_out = self._last_output_ts if self._last_output_ts > 0.0 else started
                    if now - last_out >= self.NO_OUTPUT_WARN_SECONDS:
                        self._no_output_warned = True
                        self.on_status(t("st_no_output_warning"))
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # Quiet window — loop back and re-check the rotation deadline.
                continue
            if not item:
                continue
            await self._sender_frame(conn, item)
