"""Gemini Live translation session (the default engine).

gemini-3.5-live-translate-preview: 16 kHz PCM16 in → 24 kHz PCM16 out, one
direction per session. Meeting mode opens two instances. The 15-minute audio
session ceiling is handled by rotating the connection ahead of that limit
(rotate_minutes, plus a hard deadline so the ceiling is never missed even
mid-speech). Each instance owns an asyncio loop on its own thread.

The translate model is a native simultaneous interpreter: fed the continuous
stream it translates as the speaker talks and self-balances quality vs sync, so
the client sends NO realtime_input_config and lets the model own its endpointing.

The reconnect/rotation/watchdog machine is shared with the OpenAI and Qwen
engines in app/base_translator.py; only the Gemini SDK specifics — the
context-manager connection, the config, session resumption + GoAway handling —
live here. This module also owns the process-wide usage accumulator that every
engine funnels into (get_usage / _add_usage).
"""
import threading

from google import genai
from google.genai import types

from .base_translator import BaseTranslator, _Rotate, is_terminal_error
from .config import GEMINI_LIVE_MODEL

# Live session resumption + GoAway handling — enabled only if this google-genai
# build exposes the type (older builds silently skip it; the path still works).
_SUPPORTS_RESUMPTION = hasattr(types, "SessionResumptionConfig")

# Live API audio pricing, USD per minute (input + output).
COST_IN_PER_MIN = 0.0053
COST_OUT_PER_MIN = 0.0315

# Process-cumulative usage accounting across all sessions/instances AND engines:
# the OpenAI/Qwen translators funnel their seconds here too (via
# BaseTranslator._record_usage), so the cost estimate the UI surfaces via
# get_usage() is a single lifetime total.
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
# not recoverable by retrying with the same key. 429 is NOT terminal: a bare
# rate-limit is transient and recovers with backoff; genuine quota exhaustion
# (also 429) is still caught via the "quota"/"resource_exhausted"/"billing"
# phrase markers. HTTP status codes are matched structurally (see
# base_translator.is_terminal_error), never as substrings of the free-form text.
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


def _is_terminal_error(exc) -> bool:
    return is_terminal_error(exc, _TERMINAL_PHRASES)


class LiveTranslator(BaseTranslator):
    """Gemini Live translate-preview engine. Native simultaneous: send NO
    realtime_input_config and let the model own its endpointing. Uses the SDK's
    async context-manager connection with session resumption so a rotation /
    GoAway / drop reconnects seamlessly."""

    HARD_ROTATE_SECONDS = 14.5 * 60
    IN_BYTES_PER_SEC = 32000          # 16 kHz PCM16 → 32000 bytes/sec
    TERMINAL_PHRASES = _TERMINAL_PHRASES
    READY_ON_CONNECT = True

    def __init__(self, api_key, target_lang, on_audio, on_text, on_status,
                 rotate_minutes: float = 13, name: str = "translator",
                 voice: str = "Aoede", temperature: float = 0.3,
                 model: str = GEMINI_LIVE_MODEL):
        super().__init__(api_key, target_lang, on_audio, on_text, on_status,
                         rotate_minutes=rotate_minutes, name=name)
        self.voice = voice
        self.temperature = temperature
        self.model = model
        self._client = None
        # session-resumption token for a seamless reconnect (None = fresh session)
        self._resume_handle = None

    def _build_config(self) -> dict:
        config = {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "temperature": self.temperature,
            # Translation is not reasoning — disable thinking for lower latency.
            "thinking_config": {"thinking_budget": 0},
            "translation_config": {
                "target_language_code": self.target_lang,
                # echo_target_language=False = "stay silent if the input is already
                # in the target language." The model's source detection is biased
                # toward its default language (English), so genuinely non-English
                # audio is frequently mis-judged "already English" when the target
                # is "en" — silently suppressing ALL output (the target=en bug).
                # Echo for "en" so real translation never gets false-suppressed;
                # the cost is that truly-English input is parroted rather than
                # muted, which is the safer failure for an English target.
                # NOTE: this must be == "en" (echo ONLY for the English target).
                # It shipped inverted for a while — echo off for en (the very
                # false-suppression this comment describes) and echo ON for all
                # other targets. Caught in the 2026-07-04 audit.
                "echo_target_language": self.target_lang == "en",
            },
            # Locked prebuilt voice — the strongest stability setting the client
            # API exposes for the translate-preview model.
            "speech_config": {
                "voice_config": {"prebuilt_voice_config": {"voice_name": self.voice}},
            },
        }
        # Resume the prior session so a rotation / GoAway / drop reconnects
        # seamlessly instead of cold-starting (read fresh each connect).
        if _SUPPORTS_RESUMPTION:
            config["session_resumption"] = {"handle": self._resume_handle}
        return config

    def _session_cm(self):
        # Disable WebSocket permessage-deflate: PCM16 audio is high-entropy so it
        # never compresses, and deflate only adds per-frame CPU and a flush
        # boundary on the 32 ms cadence. Client is created once and cached.
        if self._client is None:
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    async_client_args={"compression": None}))
        return self._client.aio.live.connect(model=self.model,
                                             config=self._build_config())

    def _reset_reconnect_state(self):
        # A failed reconnect may be a stale/expired resume handle — drop it so the
        # next attempt starts a fresh session.
        self._resume_handle = None

    async def _sender_frame(self, conn, item):
        await conn.send_realtime_input(
            audio=types.Blob(data=item, mime_type="audio/pcm;rate=16000"))
        self._account_sent(len(item))

    async def _receive_loop(self, conn):
        async for resp in conn.receive():
            if self._stopping.is_set():
                return
            # Any server event proves the connection is alive — reset the stall
            # watchdog (audio, transcription, resumption updates all count).
            self._reset_stall()
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
                        # 24 kHz mono PCM16 → 48000 bytes/sec received.
                        self._record_usage("out_sec", len(part.inline_data.data) / 48000)
                        self._mark_output()
                    if getattr(part, "text", None):
                        self.on_text("out", part.text)
                        self._mark_output()
            it = getattr(sc, "input_transcription", None)
            if it and it.text:
                self.on_text("in", it.text)
                self._mark_input()
            ot = getattr(sc, "output_transcription", None)
            if ot and ot.text:
                self.on_text("out", ot.text)
                self._mark_output()
