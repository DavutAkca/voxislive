"""OpenAI gpt-realtime-translate engine — a duck-typed twin of LiveTranslator.

Mirrors LiveTranslator's contract (send_pcm16/start/stop/wait_ready/is_alive +
_ready/_stopping/name) and its field-hardened machine (bounded drop-oldest queue,
carryover across reconnect, rotation, transient/terminal error handling) so the
pipeline and ModeController need no changes — but speaks the OpenAI realtime
*translations* protocol over a raw websocket instead of the Gemini SDK.

Verified protocol (see sandbox-openai-realtime/FINDINGS.md):
  URL    wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
  auth   Authorization: Bearer <key>   (NO OpenAI-Beta header)
  in     session.update                     -> set session.audio.output.language
         session.input_audio_buffer.append  -> base64 PCM16 @ 24 kHz mono
         session.close                      -> flush + end
  out    session.output_audio.delta         -> base64 PCM16 @ 24 kHz (translated)
         session.output_transcript.delta    -> translated caption
         session.input_transcript.delta     -> source caption
         session.created / session.updated  -> session live (config applied)

KEY DIFFERENCES vs Gemini, all handled here so the rest of the app is unchanged:
  * Input is 24 kHz. The pipeline gate now delivers 24 kHz PCM16 directly
    (full-band; the VAD still runs at 16 kHz upstream), so frames are sent as-is.
  * Config is sent AFTER connect (session.update), not bundled into connect();
    _ready fires only once the session is live so the first frame never precedes
    the language being set.
  * No voice/temperature (dynamic voice adaptation); ~60-min session cap so
    rotation is rare (~55 min) vs Gemini's ~13 min.
  * The key is passed EXPLICITLY as a Bearer header — never read from the ambient
    OPENAI_API_KEY env (that shadowing was the false 'account_deactivated' bug).
"""
import asyncio
import base64
import json
import threading
import time
import traceback

import numpy as np
import websockets

from .config import OPENAI_TRANSLATE_MODEL
from .i18n import t

URL_TEMPLATE = "wss://api.openai.com/v1/realtime/translations?model={model}"
GATE_RATE = 16000   # what the VAD/_GatedSource hands us
OAI_RATE = 24000    # what the OpenAI translations endpoint ingests/emits

# Mirror LiveTranslator's machine constants.
_QUEUE_MAX = 50
_ROTATE_DRAIN_SECONDS = 1.5
_MAX_TRANSIENT_FAILURES = 8
# OpenAI realtime cap is ~60 min; plan a rotation well before it. Far less churn
# than Gemini's 13-min cycle.
_HARD_ROTATE_SECONDS = 58 * 60
# A session the server accepts then closes in under this many seconds (no error,
# no planned rotation) is treated as a covert transient failure, so an
# account/region/endpoint reject can't spin a no-backoff reconnect loop forever.
_MIN_SESSION_SECONDS = 5.0

# Stall watchdog (mirrors LiveTranslator): this many seconds of SENT audio with
# zero server events means the websocket is silently dead — force a planned
# rotation. Measured in sent-audio seconds so quiet periods can never trip it.
_STALL_ROTATE_SECONDS = 20.0

# No-output watchdog (mirrors LiveTranslator): if input transcription has been
# arriving yet NO output (audio or output transcription) for this many seconds,
# surface one actionable status. Wall-clock, gated on RECENT input activity.
_NO_OUTPUT_WARN_SECONDS = 12.0
_INPUT_RECENT_SECONDS = 4.0

# Usage is recorded into LiveTranslator's process-wide accumulator so the in-app
# minutes/cost readout (webui get_usage) aggregates both engines into one total.
# Imported lazily on first frame to keep google.genai off the OpenAI cold path.
_USAGE_ADD = None


def _record_usage(key: str, amount: float):
    global _USAGE_ADD
    if _USAGE_ADD is None:
        from .translator import _add_usage
        _USAGE_ADD = _add_usage
    _USAGE_ADD(key, amount)

# Terminal (non-retryable with the same key) OpenAI failure markers + 4xx codes.
# 429 excluded: a bare rate-limit is transient (retry w/ backoff, bounded by
# _MAX_TRANSIENT_FAILURES); genuine quota is still caught by "insufficient_quota".
_TERMINAL_CODES = {401, 403, 404}
_TERMINAL_PHRASES = (
    "account_deactivated",
    "insufficient_quota",
    "invalid_api_key",
    "invalid api key",
    "invalid_request_error",
    "unauthorized",
    "permission",
    "billing",
)


def _is_terminal_error(exc: Exception) -> bool:
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


class _Rotate(Exception):
    """Signals a planned reconnect ahead of the session ceiling."""


class OpenAITranslator(threading.Thread):
    def __init__(
        self,
        api_key: str,
        target_lang: str,
        on_audio,
        on_text,
        on_status,
        rotate_minutes: float = 55,
        name: str = "translator",
        model: str = OPENAI_TRANSLATE_MODEL,
        noise_reduction: str | None = None,
        safety_identifier: str | None = None,
    ):
        super().__init__(daemon=True, name=name)
        self.api_key = api_key
        self.target_lang = target_lang
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_status = on_status
        self.model = model
        self.engine = "openai"
        # near_field for a real mic (meeting outgoing); omit (None) for the clean
        # digital system-loopback mix — suppression on already-clean audio hurts.
        self.noise_reduction = noise_reduction
        self.safety_identifier = safety_identifier
        self.rotate_seconds = rotate_minutes * 60
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._carryover: list[bytes] = []
        # Stall watchdog accumulator (loop-thread only, no locking needed).
        self._sent_since_recv = 0.0
        # No-output watchdog state (loop-thread only):
        self._last_input_ts = 0.0
        self._last_output_ts = 0.0
        self._no_output_warned = False

    # ---- public contract (identical to LiveTranslator) ------------------
    def send_pcm16(self, data: bytes):
        if self._loop and self._queue and not self._stopping.is_set():
            try:
                self._loop.call_soon_threadsafe(self._put_nowait, data)
            except RuntimeError:
                pass

    def _put_nowait(self, item):
        try:
            self._queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        # Backpressure: drop the OLDEST frame to keep the freshest audio. Count
        # the loss (same telemetry as the Gemini path) so sustained drops are
        # visible instead of silently degrading translation quality.
        try:
            self._queue.get_nowait()
            _record_usage("dropped_frames", 1)
        except asyncio.QueueEmpty:
            pass
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def stop(self):
        self._stopping.set()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    # ---- thread body ----------------------------------------------------
    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
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

    async def _connect(self):
        # Explicit Bearer; handle the websockets header-kwarg rename (>=13 uses
        # additional_headers, <13 extra_headers). NEVER read OPENAI_API_KEY env.
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.safety_identifier:
            headers["OpenAI-Safety-Identifier"] = self.safety_identifier
        url = URL_TEMPLATE.format(model=self.model)
        # Parity with the Gemini path: disable permessage-deflate. PCM16/base64 is
        # high-entropy so it never compresses; deflate only adds per-frame CPU and a
        # flush boundary on the realtime cadence.
        try:
            return await websockets.connect(url, additional_headers=headers,
                                            max_size=None, compression=None)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers,
                                            max_size=None, compression=None)

    def _session_update(self) -> str:
        audio_in = {"transcription": {"model": "gpt-realtime-whisper"}}
        if self.noise_reduction:
            audio_in["noise_reduction"] = {"type": self.noise_reduction}
        return json.dumps({
            "type": "session.update",
            "session": {"audio": {"input": audio_in, "output": {"language": self.target_lang}}},
        })

    async def _main(self):
        backoff = 1.0
        transient_failures = 0
        last_error_text = None
        while not self._stopping.is_set():
            self._ready.clear()
            # Marks when the current session became live; stays None until ready so
            # the error/exit paths can tell a connect failure from a dropped session.
            started = None
            try:
                ws = await self._connect()
                try:
                    # Config first: set the output language before any audio so the
                    # very first utterance is translated to the right target.
                    await ws.send(self._session_update())
                    self._reinject_carryover()
                    self.on_status(t("st_connected", name=self.name, lang=self.target_lang))
                    backoff = 1.0
                    last_error_text = None
                    # transient_failures is cleared only once the session proves
                    # healthy (lived past _MIN_SESSION_SECONDS or rotated), not on the
                    # bare connect — otherwise an accept-then-immediately-close server
                    # would reset the cap every iteration and never stop reconnecting.
                    started = time.monotonic()
                    self._sent_since_recv = 0.0
                    self._last_input_ts = 0.0
                    self._last_output_ts = 0.0
                    self._no_output_warned = False
                    sender = asyncio.create_task(self._sender(ws, started))
                    receiver = asyncio.create_task(self._receiver(ws))
                    done, pending = await asyncio.wait(
                        {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
                    )
                    rotating = any(
                        isinstance(task.exception(), _Rotate)
                        for task in done
                        if not task.cancelled()
                    )
                    if rotating and not self._stopping.is_set():
                        await self._drain_receiver(receiver)
                        self._snapshot_carryover()
                    self._ready.clear()
                    for task in pending:
                        task.cancel()
                    for task in done:
                        if task.cancelled():
                            continue
                        exc = task.exception()
                        if exc and not isinstance(exc, _Rotate):
                            raise exc
                finally:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                # The session ended without raising. A planned rotation or a session
                # that stayed alive past the minimum lifetime is a healthy end: clear
                # the transient machinery and reconnect cleanly. A session the server
                # accepted then closed almost immediately (no error, no rotation) is a
                # covert failure — back off and bound it like any transient drop so
                # the client never spins reconnecting a dead endpoint forever.
                if rotating or (started is not None
                                and time.monotonic() - started >= _MIN_SESSION_SECONDS):
                    transient_failures = 0
                    if not self._stopping.is_set():
                        self.on_status(t("st_renewing", name=self.name))
                elif not self._stopping.is_set():
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
                self._ready.clear()
                continue
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as e:
                self._ready.clear()
                if self._stopping.is_set():
                    break
                if _is_terminal_error(e):
                    self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                    traceback.print_exc()
                    break
                # A session that ran past the minimum lifetime proves the path works;
                # a later drop starts a fresh failure streak (preserves the "a
                # successful connection clears the failure count" resilience).
                if started is not None and time.monotonic() - started >= _MIN_SESSION_SECONDS:
                    transient_failures = 0
                transient_failures += 1
                if transient_failures >= _MAX_TRANSIENT_FAILURES:
                    self.on_status(t("st_conn_err", name=self.name, s=0, e=e))
                    traceback.print_exc()
                    break
                self.on_status(t("st_conn_err", name=self.name, s=backoff, e=e))
                err_text = repr(e)
                if err_text != last_error_text:
                    traceback.print_exc()
                    last_error_text = err_text
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.6, 6)

    def _snapshot_carryover(self):
        self._carryover = []
        while True:
            try:
                self._carryover.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

    def _reinject_carryover(self):
        if not self._carryover:
            return
        for frame in self._carryover:
            self._put_nowait(frame)
        self._carryover = []

    async def _drain_receiver(self, receiver: asyncio.Task):
        try:
            await asyncio.wait_for(asyncio.shield(receiver), timeout=_ROTATE_DRAIN_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass

    async def _sender(self, ws, started: float):
        while not self._stopping.is_set():
            elapsed = time.monotonic() - started
            if elapsed > self.rotate_seconds or elapsed > _HARD_ROTATE_SECONDS:
                raise _Rotate()
            # Stall watchdog: sent audio piling up with zero server events means
            # a silently dead socket — rotate instead of streaming into a black
            # hole (a hung TCP connection may never raise in the receiver).
            if self._sent_since_recv >= _STALL_ROTATE_SECONDS:
                self._sent_since_recv = 0.0
                self.on_status("translator: no server events for %ds of sent "
                               "audio — reconnecting" % int(_STALL_ROTATE_SECONDS))
                raise _Rotate()
            # No-output watchdog: if we have input transcription recently but no output, warning is triggered.
            now = time.monotonic()
            if self._last_input_ts > 0.0 and not self._no_output_warned:
                if now - self._last_input_ts <= _INPUT_RECENT_SECONDS:
                    last_out = self._last_output_ts if self._last_output_ts > 0.0 else started
                    if now - last_out >= _NO_OUTPUT_WARN_SECONDS:
                        self._no_output_warned = True
                        self.on_status(t("st_no_output_warning"))
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not item:
                continue
            # The gate already delivers 24 kHz PCM16 (full-band) for OpenAI, so
            # forward frames as-is — no upsample here.
            await ws.send(json.dumps({
                "type": "session.input_audio_buffer.append",
                "audio": base64.b64encode(item).decode("ascii"),
            }))
            # 24 kHz mono PCM16 → 24000*2 = 48000 bytes/sec sent.
            secs = len(item) / 48000
            _record_usage("in_sec", secs)
            self._sent_since_recv += secs

    async def _receiver(self, ws):
        async for raw in ws:
            if self._stopping.is_set():
                return
            # Any server event proves liveness — reset the stall watchdog.
            self._sent_since_recv = 0.0
            try:
                ev = json.loads(raw)
            except (ValueError, TypeError):
                continue
            etype = ev.get("type", "")
            if etype == "session.output_audio.delta":
                b64 = ev.get("delta") or ev.get("audio")
                if b64:
                    pcm = base64.b64decode(b64)
                    self.on_audio(pcm)
                    # 24 kHz mono PCM16 → 48000 bytes/sec received.
                    _record_usage("out_sec", len(pcm) / 48000)
                    self._last_output_ts = time.monotonic()
            elif etype == "session.output_transcript.delta":
                txt = ev.get("delta") or ev.get("text")
                if txt:
                    self.on_text("out", txt)
                    self._last_output_ts = time.monotonic()
            elif etype == "session.input_transcript.delta":
                txt = ev.get("delta") or ev.get("text")
                if txt:
                    self.on_text("in", txt)
                    self._last_input_ts = time.monotonic()
            elif etype in ("session.created", "session.updated"):
                # Session is live and the language config is applied.
                self._ready.set()
            elif etype == "session.closed":
                return
            elif etype == "error":
                # Surface so _main classifies terminal (auth/quota) vs transient.
                raise RuntimeError(str(ev.get("error") or ev))
