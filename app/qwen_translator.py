"""Qwen3.5-LiveTranslate engine — a duck-typed twin of OpenAITranslator.

Mirrors the translator contract (send_pcm16/start/stop/wait_ready/is_alive +
_ready/_stopping/name) and the field-hardened session machine (bounded
drop-oldest queue, carryover across reconnect, planned rotation, stall/no-output
watchdogs, terminal-error classification) so the pipeline and ModeController
need no changes — but speaks the DashScope realtime protocol.

Verified protocol + behavior (sandbox-qwen-livetranslate/FINDINGS.md +
TEST_REPORT_2026-07-04.md, all live-measured):
  URL    wss://{workspace}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime?model=...
         (intl accounts NEED the workspace MAAS host; dashscope-intl 403s)
  auth   Authorization: Bearer <key>
  in     session.update                  -> config (see _session_config)
         input_audio_buffer.append      -> base64 PCM16 @ 16 kHz mono
         session.finish                 -> flush + end
  out    response.audio.delta           -> base64 PCM16 @ 24 kHz (translated)
         response.audio_transcript.text -> CUMULATIVE caption stream (repeats!)
         response.audio_transcript.done -> final caption (fires per response)
         conversation.item.* / input_audio_transcription.* -> source ASR stream

Hard-won rules baked in here — do not "improve" these without re-measuring:
  * NEVER send `voice` unless cloning; with clone frequency once/always the
    voice MUST be "default" (anything else / omission -> InvalidParameter).
  * NEVER send input_audio_buffer.commit and NEVER use semantic_vad — both
    flip the session into a turn-based mode that silently drops or rejects
    continuous audio (coverage collapses from ~99% to ~35-55%).
  * turn_detection {type: server_vad, silence_duration_ms} IS accepted
    (undocumented) and is the only safe segmentation knob (500 ms validated).
  * `source: "auto"` works (undocumented); the field is a hint, not a filter.
  * Output speech runs LONGER than the source (+15-30%), so playback is paced
    through SyncStager (WSOLA speed-up first, oldest-first trim as last resort)
    instead of being fed straight into the Player ring like Gemini/OpenAI.
"""
import asyncio
import base64
import collections
import json
import threading
import time
import traceback

import numpy as np

from .config import QWEN_TRANSLATE_MODEL, QWEN_WORKSPACE
from .i18n import t

URL_TEMPLATE = ("wss://{ws}.ap-southeast-1.maas.aliyuncs.com"
                "/api-ws/v1/realtime?model={model}")
IN_RATE = 16000     # capture side (same as Gemini — the classic gate path)
OUT_RATE = 24000

_QUEUE_MAX = 50
_ROTATE_DRAIN_SECONDS = 1.5
_MAX_TRANSIENT_FAILURES = 8
_MIN_SESSION_SECONDS = 5.0
_STALL_ROTATE_SECONDS = 20.0
_NO_OUTPUT_WARN_SECONDS = 12.0
_INPUT_RECENT_SECONDS = 4.0
# Session ceiling is undocumented; the longest measured session (~6 min) never
# dropped. Rotate proactively with carryover well inside the unknown.
_DEFAULT_ROTATE_MINUTES = 25
_HARD_ROTATE_SECONDS = 28 * 60

# Measured economics (FINDINGS): input $7.5/1M tok, audio out $30/1M, text out
# $20/1M -> per-minute rates for the shared usage/cost readout.
COST_IN_PER_MIN = 0.0032
COST_OUT_PER_MIN = 0.0265

_USAGE_ADD = None


def _record_usage(key: str, amount: float):
    global _USAGE_ADD
    if _USAGE_ADD is None:
        from .translator import _add_usage
        _USAGE_ADD = _add_usage
    _USAGE_ADD(key, amount)


_TERMINAL_CODES = {401, 403, 404}
_TERMINAL_PHRASES = (
    "invalidapikey", "invalid_api_key", "invalid api key",
    "access denied", "accessdenied", "unauthorized", "forbidden",
    "arrearage", "quota", "billing",
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
    """Signals a planned reconnect ahead of the (unknown) session ceiling."""


def _wsola(x: np.ndarray, speed: float, sr: int) -> np.ndarray:
    """Pitch-preserving time compression (WSOLA): 30 ms frames, 50% overlap,
    ±8 ms similarity search — clean for speech at <= 1.3x. Runs on the stager
    thread, never in the audio callback. (Validated: duration ratio exact,
    zero-crossing rate — i.e. pitch — preserved within 1%.)"""
    frame = int(sr * 0.030)
    hop_out = frame // 2
    hop_in = int(hop_out * speed)
    search = int(sr * 0.008)
    if speed <= 1.01 or len(x) < frame + hop_in + search + 1:
        return x
    win = np.hanning(frame).astype(np.float32)
    n_frames = (len(x) - frame - search) // hop_in
    if n_frames < 2:
        return x
    out = np.zeros(n_frames * hop_out + frame, dtype=np.float32)
    norm = np.zeros_like(out)
    sel = 0
    out[:frame] += x[:frame] * win
    norm[:frame] += win
    for k in range(1, n_frames):
        target = k * hop_in
        lo = max(0, target - search)
        hi = min(len(x) - frame, target + search)
        tmpl = x[sel + hop_out: sel + hop_out + frame]
        if hi <= lo or len(tmpl) < frame:
            sel = target
        else:
            corr = np.correlate(x[lo:hi + frame], tmpl, mode="valid")
            sel = lo + int(np.argmax(corr))
        o = k * hop_out
        out[o:o + frame] += x[sel:sel + frame] * win
        norm[o:o + frame] += win
    np.maximum(norm, 1e-6, out=norm)
    return out / norm


class SyncStager:
    """Live-sync pacing between the Qwen stream and the Player.

    Qwen's translated speech is longer than the source and carries no silence
    padding (unlike OpenAI's self-timing stream), so feeding the Player ring
    directly drifts the dub minutes behind the picture. This stager keeps the
    ring only ~2.5 s deep and, as backlog grows, WSOLA-compresses blocks
    (>=3 s -> 1.12x, >=6 s -> 1.25x, pitch preserved); beyond 12 s the OLDEST
    audio is trimmed to 4 s — the loss is always stale content, never the line
    currently playing. Exposes telemetry for the beta UI.
    """

    FEED_AHEAD_S = 2.5
    SPEED_STEPS = ((6.0, 1.25), (3.0, 1.12))
    PENDING_MAX_S = 12.0
    PENDING_KEEP_S = 4.0

    def __init__(self, player):
        self._player = player
        self._pending: collections.deque[bytes] = collections.deque()
        self._pending_bytes = 0
        self._lock = threading.Lock()
        self.speed = 1.0
        self.skipped_s = 0.0
        self.sped_s = 0.0
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="qwen-stager")
        self._thread.start()

    @property
    def backlog_s(self) -> float:
        p = self._player
        ring = p.tts.fill / p.rate if p is not None else 0.0
        return self._pending_bytes / (OUT_RATE * 2) + ring

    def feed(self, data: bytes):
        trimmed = 0
        with self._lock:
            self._pending.append(data)
            self._pending_bytes += len(data)
            if self._pending_bytes > int(self.PENDING_MAX_S * OUT_RATE * 2):
                keep = int(self.PENDING_KEEP_S * OUT_RATE * 2)
                while self._pending and self._pending_bytes > keep:
                    c = self._pending.popleft()
                    self._pending_bytes -= len(c)
                    trimmed += len(c)
        if trimmed:
            self.skipped_s += trimmed / (OUT_RATE * 2)

    def _loop(self):
        block = int(0.6 * OUT_RATE) * 2
        while self._run:
            player = self._player
            if player is not None:
                try:
                    while (self._run and self._pending_bytes
                           and player.tts.fill / player.rate < self.FEED_AHEAD_S):
                        buf = bytearray()
                        with self._lock:
                            while self._pending and len(buf) < block:
                                c = self._pending.popleft()
                                self._pending_bytes -= len(c)
                                buf.extend(c)
                        if not buf:
                            break
                        backlog = self.backlog_s
                        speed = 1.0
                        for thresh, sp in self.SPEED_STEPS:
                            if backlog >= thresh:
                                speed = sp
                                break
                        self.speed = speed
                        data = bytes(buf)
                        if speed > 1.0:
                            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                            y = _wsola(x, speed, OUT_RATE)
                            self.sped_s += max(0.0, (len(x) - len(y)) / OUT_RATE)
                            data = (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                        player.feed_tts_pcm16(data)
                except Exception:
                    pass
            time.sleep(0.02)

    def stop(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._player = None


class QwenTranslator(threading.Thread):
    def __init__(
        self,
        api_key: str,
        target_lang: str,
        on_audio,
        on_text,
        on_status,
        rotate_minutes: float = _DEFAULT_ROTATE_MINUTES,
        name: str = "translator",
        model: str = QWEN_TRANSLATE_MODEL,
        source_lang: str = "auto",
        clone: str = "off",              # off | once | always
        hotwords: dict | None = None,    # translation.corpus.phrases (<=50)
        vad_silence_ms: int = 500,       # server_vad silence_duration_ms; 0 = model default
        workspace: str = QWEN_WORKSPACE,
    ):
        super().__init__(daemon=True, name=name)
        self.api_key = api_key
        self.target_lang = target_lang
        self.on_audio = on_audio
        self.on_text = on_text
        self.on_status = on_status
        self.model = model
        self.engine = "qwen"
        self.source_lang = source_lang or "auto"
        self.clone = clone if clone in ("once", "always") else "off"
        self.hotwords = dict(list((hotwords or {}).items())[:50])
        self.vad_silence_ms = int(vad_silence_ms)
        self.workspace = workspace
        self.rotate_seconds = rotate_minutes * 60
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._carryover: list[bytes] = []
        self._sent_since_recv = 0.0
        self._last_input_ts = 0.0
        self._last_output_ts = 0.0
        self._no_output_warned = False
        self._clone_err_warned = False
        # Cumulative-stream -> increment conversion state (out + in captions).
        self._out_acc = ""
        self._in_acc = ""
        self._last_done_id = None

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

    # ---- session config ---------------------------------------------------
    def _session_update(self) -> str:
        session = {
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            # The ASR model unlocks the source-transcription stream ('in'
            # captions / WER) at no measured latency cost.
            "input_audio_transcription": {"language": self.source_lang,
                                          "model": "qwen3-asr-flash-realtime"},
            "translation": {"language": self.target_lang},
        }
        if self.vad_silence_ms > 0:
            session["turn_detection"] = {"type": "server_vad",
                                         "silence_duration_ms": self.vad_silence_ms}
        if self.clone != "off":
            # Docs + live: cloning REQUIRES voice="default"; plain mode
            # requires the field to be ABSENT.
            session["voice"] = "default"
            session["enable_voice_clone"] = True
            session["voice_clone_options"] = {"frequency": self.clone}
        if self.hotwords:
            session["translation"]["corpus"] = {"phrases": self.hotwords}
        return json.dumps({"type": "session.update", "session": session})

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
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()

    async def _connect(self):
        import websockets  # lazy: keep it off the cold path
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = URL_TEMPLATE.format(ws=self.workspace, model=self.model)
        try:
            return await websockets.connect(url, additional_headers=headers,
                                            max_size=None, compression=None)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers,
                                            max_size=None, compression=None)

    async def _main(self):
        backoff = 1.0
        transient_failures = 0
        last_error_text = None
        while not self._stopping.is_set():
            self._ready.clear()
            started = None
            try:
                ws = await self._connect()
                try:
                    await ws.send(self._session_update())
                    self._reinject_carryover()
                    self.on_status(t("st_connected", name=self.name, lang=self.target_lang))
                    self._ready.set()
                    backoff = 1.0
                    last_error_text = None
                    started = time.monotonic()
                    self._sent_since_recv = 0.0
                    self._last_input_ts = 0.0
                    self._last_output_ts = 0.0
                    self._no_output_warned = False
                    self._out_acc = ""
                    self._in_acc = ""
                    sender = asyncio.create_task(self._sender(ws, started))
                    receiver = asyncio.create_task(self._receiver(ws))
                    done, pending = await asyncio.wait(
                        {sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    rotating = any(isinstance(task.exception(), _Rotate)
                                   for task in done if not task.cancelled())
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
            await asyncio.wait_for(asyncio.shield(receiver),
                                   timeout=_ROTATE_DRAIN_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            pass

    async def _sender(self, ws, started: float):
        while not self._stopping.is_set():
            elapsed = time.monotonic() - started
            if elapsed > self.rotate_seconds or elapsed > _HARD_ROTATE_SECONDS:
                raise _Rotate()
            if self._sent_since_recv >= _STALL_ROTATE_SECONDS:
                self._sent_since_recv = 0.0
                self.on_status("translator: no server events for %ds of sent "
                               "audio — reconnecting" % int(_STALL_ROTATE_SECONDS))
                raise _Rotate()
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
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(item).decode("ascii"),
            }))
            secs = len(item) / (IN_RATE * 2)
            _record_usage("in_sec", secs)
            self._sent_since_recv += secs

    # ---- cumulative-stream -> increment conversion --------------------------
    def _delta(self, acc_attr: str, txt: str) -> str:
        """Qwen's caption streams repeat CUMULATIVE text; the product transcript
        expects increments (Gemini/OpenAI semantics). Emit only what extends the
        accumulator; genuinely new text after a reset is emitted whole."""
        acc = getattr(self, acc_attr)
        if txt.startswith(acc):
            inc = txt[len(acc):]
            setattr(self, acc_attr, txt)
            return inc
        if acc.startswith(txt):
            return ""
        setattr(self, acc_attr, txt)
        return (" " if acc else "") + txt

    async def _receiver(self, ws):
        async for raw in ws:
            if self._stopping.is_set():
                return
            self._sent_since_recv = 0.0
            try:
                ev = json.loads(raw)
            except (ValueError, TypeError):
                continue
            et = ev.get("type", "")
            if et == "response.audio.delta":
                b64 = ev.get("delta") or ev.get("audio")
                if b64:
                    pcm = base64.b64decode(b64)
                    self.on_audio(pcm)
                    _record_usage("out_sec", len(pcm) / (OUT_RATE * 2))
                    self._last_output_ts = time.monotonic()
            elif et in ("response.audio_transcript.text",
                        "response.audio_transcript.delta", "response.text.delta"):
                txt = ev.get("text") or ev.get("delta")
                if txt:
                    inc = self._delta("_out_acc", txt)
                    if inc:
                        self.on_text("out", inc)
                        self._last_output_ts = time.monotonic()
            elif et in ("response.audio_transcript.done", "response.text.done"):
                # Fires once per event TYPE per response with identical text —
                # dedupe by response id, flush any tail, reset the accumulator.
                rid = ev.get("response_id")
                if rid is not None and rid == self._last_done_id:
                    continue
                self._last_done_id = rid
                txt = (ev.get("transcript") or ev.get("text") or "").strip()
                if txt:
                    inc = self._delta("_out_acc", txt)
                    if inc.strip():
                        self.on_text("out", inc)
                self._out_acc = ""
                self._last_output_ts = time.monotonic()
            elif ("input_audio_transcription" in et
                  or et.startswith("conversation.item.input")):
                txt = (ev.get("transcript") or ev.get("text")
                       or (ev.get("item") or {}).get("transcript"))
                if txt:
                    inc = self._delta("_in_acc", txt)
                    if inc:
                        self.on_text("in", inc)
                    self._last_input_ts = time.monotonic()
            elif et == "error":
                msg = str(ev.get("error") or ev)
                # A per-response clone hiccup (tiny segment with no sample to
                # clone) leaves the session healthy — surface once, keep going.
                if self.clone != "off" and "voice" in msg.lower():
                    if not self._clone_err_warned:
                        self._clone_err_warned = True
                        self.on_status(f"translator: clone voice hiccup ({msg[:80]})")
                    continue
                raise RuntimeError(msg)
            elif et in ("session.finished", "session.closed"):
                return
