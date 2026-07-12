"""The inverse demo: speak the user's own translated line in the FREE tier's
local voice while they are still on the paid engine.

Why this exists. A label tells; a comparison teaches. A user who has only ever
heard the paid voice cannot value it — "Pro voice" is just a word — and meets the
free voice for the first time at the wall, where the loss reads as *"this product
got worse"* rather than *"I lost something"*. Playing the free voice ONCE, early,
on a sentence they just heard, and then handing the paid voice straight back,
makes the difference concrete while it is still reversible. A comparison you can
undo creates desire; a comparison you cannot undo creates anger.

It must be the real thing, not a mock-up: synthesis goes through the same local
Piper path (`local_tts`) and the same 24 kHz resample the cascade engine uses, so
what the user hears here is exactly what the free tier sounds like.
"""
from __future__ import annotations

import threading
import time

from . import local_tts
from .cascade_translator import OUT_RATE, _resample_to_out

# A demo, not a session: one or two sentences is enough to hear a voice, and a
# long clip would hold the paid voice muted while the source keeps talking.
PREVIEW_MAX_CHARS = 220

_voices: dict[str, local_tts.LocalTTS] = {}
_lock = threading.Lock()


def voice_available(lang: str) -> bool:
    """Whether the free tier can SPEAK this language at all. False means the free
    tier is captions-only here — which is worth telling the user plainly."""
    return local_tts.voice_available(lang)


def _voice(lang: str, on_status=None) -> local_tts.LocalTTS:
    """One cached voice per language. Loading (and the ~60 MB first-use download)
    happens under the lock so two rapid clicks can't race two downloads."""
    key = local_tts._norm(lang)
    with _lock:
        v = _voices.get(key)
        if v is None:
            v = local_tts.LocalTTS(lang, on_status=on_status)
            _voices[key] = v
        return v


def synth_pcm16(lang: str, text: str, on_status=None) -> bytes:
    """The line, spoken by the free tier's voice, as 24 kHz PCM16 — byte-identical
    in format to what the paid engines emit, so it can be fed to the live Player
    with no special-casing. Raises local_tts.VoiceUnavailable when this language
    has no free voice or the download/verify fails."""
    line = (text or "").strip()
    if not line:
        raise local_tts.VoiceUnavailable("nothing to preview")
    if len(line) > PREVIEW_MAX_CHARS:
        line = line[:PREVIEW_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    samples, rate = _voice(lang, on_status=on_status).synth(line)
    return _resample_to_out(samples, rate)


def duration_seconds(pcm: bytes) -> float:
    return len(pcm) / (OUT_RATE * 2.0)


def play_standalone(cfg: dict, pcm: bytes) -> float:
    """Play a clip with no session running — the A/B card lives AFTER the session,
    because that is the only moment the user is reliably looking at Voxis rather
    than at the film they came to watch.

    Opens a short-lived Player on the user's configured output device (the same
    class the session uses, so gain and limiter are identical) and blocks for the
    length of the clip. Callers must be off the UI thread."""
    from . import audio_io  # noqa: PLC0415 - heavy (PortAudio); session path only

    dev = audio_io.find_device(cfg["devices"]["headphones_output"], "output",
                               fallback_default=True)
    player = audio_io.Player(dev, tts_in_rate=OUT_RATE)
    player.tts_gain = float(cfg.get("tts_volume", 1.0))
    secs = duration_seconds(pcm)
    player.start()
    try:
        player.feed_tts_pcm16(pcm)
        time.sleep(secs + 0.5)   # let the ring drain before the device closes
    finally:
        player.stop()
    return secs
