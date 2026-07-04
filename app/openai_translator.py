"""OpenAI gpt-realtime-translate engine — a BaseTranslator subclass.

Shares the field-hardened session machine (bounded drop-oldest queue, carryover
across reconnect, rotation, transient/terminal handling, stall/no-output
watchdogs) with the Gemini and Qwen engines via app/base_translator.py, and adds
only the OpenAI realtime *translations* websocket protocol.

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

KEY DIFFERENCES vs Gemini, all confined to this file:
  * Input is 24 kHz. The pipeline gate delivers 24 kHz PCM16 directly, so frames
    are sent as-is (IN_BYTES_PER_SEC = 48000).
  * Config is sent AFTER connect (session.update via _open_session), not bundled
    into connect; READY_ON_CONNECT is False so _ready fires only once the server
    confirms the session (session.created/updated), so the first frame never
    precedes the language being set.
  * No voice/temperature (dynamic voice adaptation); ~60-min session cap so
    rotation is rare (~55 min) vs Gemini's ~13 min.
  * The key is passed EXPLICITLY as a Bearer header — never read from the ambient
    OPENAI_API_KEY env (that shadowing was the false 'account_deactivated' bug).
"""
import base64
import json

import websockets

from .base_translator import BaseTranslator, is_terminal_error
from .config import OPENAI_TRANSLATE_MODEL

URL_TEMPLATE = "wss://api.openai.com/v1/realtime/translations?model={model}"
GATE_RATE = 16000   # what the VAD/_GatedSource hands us
OAI_RATE = 24000    # what the OpenAI translations endpoint ingests/emits

# Terminal (non-retryable with the same key) OpenAI failure markers. 429 excluded:
# a bare rate-limit is transient; genuine quota is caught by "insufficient_quota".
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


def _is_terminal_error(exc) -> bool:
    return is_terminal_error(exc, _TERMINAL_PHRASES)


class OpenAITranslator(BaseTranslator):
    # OpenAI realtime caps at ~60 min; rotate well before it — far less churn
    # than Gemini's 13-min cycle.
    HARD_ROTATE_SECONDS = 58 * 60
    IN_BYTES_PER_SEC = OAI_RATE * 2   # 24 kHz mono PCM16 → 48000 bytes/sec
    TERMINAL_PHRASES = _TERMINAL_PHRASES
    # The server confirms the applied config with session.created/updated; only
    # then is it safe to report ready.
    READY_ON_CONNECT = False

    def __init__(self, api_key, target_lang, on_audio, on_text, on_status,
                 rotate_minutes: float = 55, name: str = "translator",
                 model: str = OPENAI_TRANSLATE_MODEL,
                 noise_reduction: str | None = None,
                 safety_identifier: str | None = None):
        super().__init__(api_key, target_lang, on_audio, on_text, on_status,
                         rotate_minutes=rotate_minutes, name=name)
        self.model = model
        self.engine = "openai"
        # near_field for a real mic (meeting outgoing); omit (None) for the clean
        # digital system-loopback mix — suppression on already-clean audio hurts.
        self.noise_reduction = noise_reduction
        self.safety_identifier = safety_identifier

    async def _connect(self):
        # Explicit Bearer; handle the websockets header-kwarg rename (>=13 uses
        # additional_headers, <13 extra_headers). NEVER read OPENAI_API_KEY env.
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.safety_identifier:
            headers["OpenAI-Safety-Identifier"] = self.safety_identifier
        url = URL_TEMPLATE.format(model=self.model)
        # Parity with the Gemini path: disable permessage-deflate. PCM16/base64 is
        # high-entropy so it never compresses; deflate only adds per-frame CPU.
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
            "session": {"audio": {"input": audio_in,
                                  "output": {"language": self.target_lang}}},
        })

    async def _open_session(self, conn):
        # Config first: set the output language before any audio so the very first
        # utterance is translated to the right target.
        await conn.send(self._session_update())

    async def _sender_frame(self, conn, item):
        # The gate already delivers 24 kHz PCM16 (full-band) for OpenAI, so
        # forward frames as-is — no upsample here.
        await conn.send(json.dumps({
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(item).decode("ascii"),
        }))
        self._account_sent(len(item))

    async def _receive_loop(self, conn):
        async for raw in conn:
            if self._stopping.is_set():
                return
            # Any server event proves liveness — reset the stall watchdog.
            self._reset_stall()
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
                    self._record_usage("out_sec", len(pcm) / 48000)
                    self._mark_output()
            elif etype == "session.output_transcript.delta":
                txt = ev.get("delta") or ev.get("text")
                if txt:
                    self.on_text("out", txt)
                    self._mark_output()
            elif etype == "session.input_transcript.delta":
                txt = ev.get("delta") or ev.get("text")
                if txt:
                    self.on_text("in", txt)
                    self._mark_input()
            elif etype in ("session.created", "session.updated"):
                # Session is live and the language config is applied.
                self._ready.set()
            elif etype == "session.closed":
                return
            elif etype == "error":
                # Surface so _main classifies terminal (auth/quota) vs transient.
                raise RuntimeError(str(ev.get("error") or ev))
