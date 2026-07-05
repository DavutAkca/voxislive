"""Qwen3.5-LiveTranslate engine — a BaseTranslator subclass.

Shares the field-hardened session machine (bounded drop-oldest queue, carryover
across reconnect, planned rotation, stall/no-output watchdogs, terminal-error
classification) with the Gemini and OpenAI engines via app/base_translator.py,
and adds only the DashScope realtime protocol plus the SyncStager pacing this
engine needs.

Verified protocol + behavior (sandbox-qwen-livetranslate/FINDINGS.md +
TEST_REPORT_2026-07-04.md, all live-measured):
  URL    wss://{workspace}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime?model=...
         (intl accounts NEED the workspace MAAS host; dashscope-intl 403s)
  auth   Authorization: Bearer <key>
  in     session.update                  -> config (see _session_update)
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
import base64
import collections
import json
import logging
import threading
import time

import numpy as np

_log = logging.getLogger("voxis")

from .base_translator import BaseTranslator, is_terminal_error
from .config import QWEN_TRANSLATE_MODEL, QWEN_WORKSPACE

URL_TEMPLATE = ("wss://{ws}.ap-southeast-1.maas.aliyuncs.com"
                "/api-ws/v1/realtime?model={model}")
IN_RATE = 16000     # capture side (same as Gemini — the classic gate path)
OUT_RATE = 24000

# Session ceiling is undocumented; the longest measured session (~6 min) never
# dropped. Rotate proactively with carryover well inside the unknown.
_DEFAULT_ROTATE_MINUTES = 25

# Measured economics (FINDINGS): input $7.5/1M tok, audio out $30/1M, text out
# $20/1M -> per-minute rates. NOTE: the shared get_usage() readout currently
# prices every engine's seconds at Gemini rates; these are kept as documentation
# of Qwen's measured economics.
COST_IN_PER_MIN = 0.0032
COST_OUT_PER_MIN = 0.0265

_TERMINAL_PHRASES = (
    "invalidapikey", "invalid_api_key", "invalid api key",
    "access denied", "accessdenied", "unauthorized", "forbidden",
    "arrearage", "quota", "billing",
)


def _is_terminal_error(exc) -> bool:
    return is_terminal_error(exc, _TERMINAL_PHRASES)


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
    directly drifts the dub minutes behind. This stager keeps the ring only
    ~2.5 s deep and, as backlog grows, WSOLA-compresses blocks (>=3 s -> 1.12x,
    >=6 s -> 1.25x, pitch preserved); beyond 12 s the OLDEST audio is trimmed to
    4 s — the loss is always stale content, never the line currently playing.
    Exposes telemetry for the beta UI.
    """

    FEED_AHEAD_S = 2.5
    SPEED_STEPS = ((6.0, 1.25), (3.0, 1.12))
    PENDING_MAX_S = 12.0
    PENDING_KEEP_S = 4.0

    def __init__(self, player, on_status=None):
        self._player = player
        self._on_status = on_status
        self._pending: collections.deque[bytes] = collections.deque()
        self._pending_bytes = 0
        self._lock = threading.Lock()
        self.speed = 1.0
        self.skipped_s = 0.0
        self.sped_s = 0.0
        # Player-feed fault telemetry: a persistently raising feed_tts_pcm16 would
        # silently drop ALL translated audio (text keeps flowing) — the exact
        # "subtitles but no voice" failure. Counted + surfaced once instead of
        # swallowed, so a field report captures it.
        self.feed_errors = 0
        self._feed_err_warned = False
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
                    # A persistent feed fault here is exactly how the translated
                    # voice goes missing while captions still flow. Count EVERY
                    # occurrence (telemetry), but log the stack trace + surface the
                    # status only ONCE — a sustained fault must not flood the log
                    # with a fresh traceback on every 20 ms iteration.
                    self.feed_errors += 1
                    if not self._feed_err_warned:
                        self._feed_err_warned = True
                        _log.exception("qwen stager player-feed failed (#%d)", self.feed_errors)
                        if self._on_status is not None:
                            try:
                                self._on_status(
                                    "translator: audio playback fault — translated "
                                    "voice may be silent")
                            except Exception:
                                pass
            time.sleep(0.02)

    def stop(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._player = None


class QwenTranslator(BaseTranslator):
    HARD_ROTATE_SECONDS = 28 * 60
    IN_BYTES_PER_SEC = IN_RATE * 2    # 16 kHz mono PCM16 → 32000 bytes/sec
    TERMINAL_PHRASES = _TERMINAL_PHRASES
    READY_ON_CONNECT = True

    def __init__(self, api_key, target_lang, on_audio, on_text, on_status,
                 rotate_minutes: float = _DEFAULT_ROTATE_MINUTES,
                 name: str = "translator", model: str = QWEN_TRANSLATE_MODEL,
                 source_lang: str = "auto", clone: str = "off",
                 hotwords: dict | None = None, vad_silence_ms: int = 500,
                 workspace: str = QWEN_WORKSPACE):
        # Qwen wants base ISO codes: Voxis's 79-language BCP-47 targets
        # (pt-BR, zh-Hans, …) are normalized the same way the OpenAI router does,
        # so a regional variant can't bounce the session with InvalidParameter.
        from .config import _norm_lang  # noqa: PLC0415
        norm_target = _norm_lang(target_lang) or target_lang
        super().__init__(api_key, norm_target, on_audio, on_text, on_status,
                         rotate_minutes=rotate_minutes, name=name)
        self.model = model
        self.engine = "qwen"
        self.source_lang = source_lang or "auto"
        self.clone = clone if clone in ("once", "always") else "off"
        self.hotwords = dict(list((hotwords or {}).items())[:50])
        self.vad_silence_ms = int(vad_silence_ms)
        self.workspace = workspace
        self._clone_err_warned = False
        # Cumulative-stream -> increment conversion state (out + in captions).
        self._out_acc = ""
        self._in_acc = ""
        self._last_done_id = None

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
            # Docs + live: cloning REQUIRES voice="default"; plain mode requires
            # the field to be ABSENT.
            session["voice"] = "default"
            session["enable_voice_clone"] = True
            session["voice_clone_options"] = {"frequency": self.clone}
        if self.hotwords:
            session["translation"]["corpus"] = {"phrases": self.hotwords}
        return json.dumps({"type": "session.update", "session": session})

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

    async def _open_session(self, conn):
        await conn.send(self._session_update())

    def _reset_session_state(self):
        # Per-session parse state. _last_done_id is reset here too (it was NOT
        # reset per session before the base-class consolidation), so a new
        # session's first *.done event can never be deduped against a stale
        # response id carried over from the previous session.
        self._out_acc = ""
        self._in_acc = ""
        self._last_done_id = None

    async def _sender_frame(self, conn, item):
        await conn.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(item).decode("ascii"),
        }))
        self._account_sent(len(item))

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

    async def _receive_loop(self, conn):
        async for raw in conn:
            if self._stopping.is_set():
                return
            self._reset_stall()
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
                    self._record_usage("out_sec", len(pcm) / (OUT_RATE * 2))
                    self._mark_audio_output()
            elif et in ("response.audio_transcript.text",
                        "response.audio_transcript.delta", "response.text.delta"):
                txt = ev.get("text") or ev.get("delta")
                if txt:
                    inc = self._delta("_out_acc", txt)
                    if inc:
                        self.on_text("out", inc)
                        self._mark_text_output()
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
                self._mark_text_output()
            elif ("input_audio_transcription" in et
                  or et.startswith("conversation.item.input")):
                txt = (ev.get("transcript") or ev.get("text")
                       or (ev.get("item") or {}).get("transcript"))
                if txt:
                    inc = self._delta("_in_acc", txt)
                    if inc:
                        self.on_text("in", inc)
                    self._mark_input()
            elif et == "error":
                msg = str(ev.get("error") or ev)
                # A per-response clone hiccup (tiny segment with no sample to
                # clone) leaves the session healthy — surface once, keep going.
                if self.clone != "off" and "voice" in msg.lower():
                    if not self._clone_err_warned:
                        self._clone_err_warned = True
                        # Log the FULL DashScope error (voxis.log is local and
                        # scrubbed before any report leaves the device) so the
                        # rejected parameter survives — the 80-char status line
                        # truncates InvalidParameter exactly where the reason is.
                        _log.warning("qwen clone rejected (frequency=%s): %s",
                                     self.clone, msg)
                        self.on_status(f"translator: clone voice hiccup ({msg[:80]})")
                    continue
                raise RuntimeError(msg)
            elif et in ("session.finished", "session.closed"):
                return
