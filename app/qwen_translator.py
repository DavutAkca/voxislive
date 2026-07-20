"""Qwen3.5-LiveTranslate engine — a BaseTranslator subclass.

Shares the field-hardened session machine (bounded drop-oldest queue, carryover
across reconnect, planned rotation, stall/no-output watchdogs, terminal-error
classification) with the Gemini and OpenAI engines via app/base_translator.py,
and adds only the DashScope realtime protocol. Playback catch-up pacing is
shared with Gemini through app/playback_sync.py.

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
  * Output speech runs LONGER than the source (+15-30%), so playback uses the
    shared AdaptivePlaybackStager (WSOLA first, stale trim as last resort).
"""
import base64
import json
import logging

import numpy as np

from .base_translator import BaseTranslator, is_terminal_error
from .config import QWEN_TRANSLATE_MODEL, QWEN_WORKSPACE

_log = logging.getLogger("voxis")

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
        # Duplicate-audio instrumentation (Ivo's "doubled audio" report). We do
        # NOT dedupe blind: the doubling may live in the user's capture/loopback
        # chain rather than our stream — the Voxis-less control recording tells
        # us which. This log-only counter proves whether the SERVER emits
        # overlapping/cumulative audio deltas (the caption stream already does),
        # so the next field report is root-causable. Lifetime counter; the
        # previous-delta buffer resets per session.
        self._prev_audio = b""
        self._dup_audio_count = 0
        self._dup_audio_warned = False

    # ---- session config ---------------------------------------------------
    def _session_update(self) -> str:
        session = {
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            # The ASR model unlocks the source-transcription stream ('in'
            # captions / WER) at no measured latency cost. fun-asr-realtime is
            # Alibaba's designated replacement for qwen3-asr-flash-realtime,
            # whose dated snapshots retire 2026-10-10 (A/B 2026-07-10: session
            # behavior identical between the two).
            "input_audio_transcription": {"language": self.source_lang,
                                          "model": "fun-asr-realtime"},
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
        self._prev_audio = b""

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

    def _detect_dup_audio(self, pcm: bytes) -> str | None:
        """Flag a translated-audio delta that duplicates the one before it.

        Returns the duplication kind (or None). Log-only, warns once per
        translator lifetime; near-silent chunks are skipped (identical silence
        padding is normal and would false-positive). Comparison is against the
        immediately-preceding delta only — enough to catch the two shapes we
        care about: the server re-sending the same block (exact-repeat) or
        re-sending prior audio plus more (cumulative-prefix, the shape the
        caption stream already uses)."""
        prev = self._prev_audio
        a = np.frombuffer(pcm, dtype=np.int16)
        # <10 ms or near-silent — remember it but never treat as a duplicate.
        if a.size < 160 or int(np.abs(a).max()) < 256:
            self._prev_audio = pcm
            return None
        kind = None
        if pcm == prev:
            kind = "exact-repeat"
        elif prev and len(pcm) > len(prev) and pcm[:len(prev)] == prev:
            kind = "cumulative-prefix"
        elif prev and len(prev) > len(pcm) and prev[:len(pcm)] == pcm:
            kind = "overlap-tail"
        self._prev_audio = pcm
        if kind:
            self._dup_audio_count += 1
            if not self._dup_audio_warned:
                self._dup_audio_warned = True
                _log.warning(
                    "qwen duplicate audio delta (%s, %d bytes) — first "
                    "occurrence; server may be emitting overlapping/cumulative "
                    "audio. Counting the rest silently.", kind, len(pcm))
        return kind

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
                    self._detect_dup_audio(pcm)  # log-only instrumentation
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
                        from .i18n import t  # noqa: PLC0415 — lazy, matches module style
                        self.on_status(t("st_clone_hiccup", name=self.name, msg=msg[:80]))
                    continue
                raise RuntimeError(msg)
            elif et in ("session.finished", "session.closed"):
                return
