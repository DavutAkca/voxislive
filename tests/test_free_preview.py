"""The inverse demo: the free voice must be able to speak the user's own line,
and the paid voice must always come back.

The failure that matters here is not a crash — it is the paid voice never
resuming. A stranded mute would silence a paying session, so the unmute is
pinned from both ends (the timer AND stop()).
"""
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import free_preview  # noqa: E402
from app import local_tts  # noqa: E402
from app.pipeline import IncomingPipeline  # noqa: E402


class _FakeVoice:
    """Stands in for a loaded Piper voice: 0.5 s of 22.05 kHz audio."""

    def __init__(self, *a, **kw):
        self.said = []

    def synth(self, text, speed=1.0):
        self.said.append(text)
        return np.zeros(11025, dtype=np.float32), 22050


@pytest.fixture(autouse=True)
def _no_real_voice(monkeypatch):
    free_preview._voices.clear()
    monkeypatch.setattr(local_tts, "LocalTTS", _FakeVoice)
    monkeypatch.setattr(local_tts, "voice_available", lambda lang: lang.startswith("tr"))


def test_synth_returns_24k_pcm16_like_the_paid_engines():
    # Format parity is the whole reason this can be fed to the live Player.
    pcm = free_preview.synth_pcm16("tr", "merhaba dünya")
    assert isinstance(pcm, bytes)
    assert len(pcm) % 2 == 0
    # 0.5 s at 22 050 Hz resampled to 24 000 Hz, 2 bytes/sample.
    assert free_preview.duration_seconds(pcm) == pytest.approx(0.5, abs=0.02)


def test_long_line_is_clipped_to_a_demo():
    # A demo, not a session: a long clip would hold the paid voice muted while
    # the speaker keeps talking.
    free_preview.synth_pcm16("tr", "kelime " * 200)
    voice = free_preview._voices["tr"]
    assert len(voice.said[0]) <= free_preview.PREVIEW_MAX_CHARS + 1
    assert voice.said[0].endswith("…")


def test_empty_line_refuses():
    with pytest.raises(local_tts.VoiceUnavailable):
        free_preview.synth_pcm16("tr", "   ")


def test_voice_is_cached_across_calls():
    free_preview.synth_pcm16("tr", "bir")
    first = free_preview._voices["tr"]
    free_preview.synth_pcm16("tr", "iki")
    assert free_preview._voices["tr"] is first  # no second ~60 MB load


def _fake_pipe():
    player = types.SimpleNamespace(cleared=0, fed=[],
                                   clear_tts=lambda: None,
                                   feed_tts_pcm16=lambda d: None)
    fed = []
    player.feed_tts_pcm16 = fed.append
    pipe = types.SimpleNamespace(player=player, _preview_mute=False, _fed=fed)
    pipe._end_free_preview = lambda: IncomingPipeline._end_free_preview(pipe)
    return pipe


def test_preview_mutes_the_paid_voice_then_hands_it_back():
    pipe = _fake_pipe()
    IncomingPipeline.play_free_preview(pipe, b"\x00\x00" * 100, 0.05)
    assert pipe._preview_mute is True          # paid voice stands down
    assert pipe._fed == [b"\x00\x00" * 100]    # free voice plays on the SAME player
    time.sleep(1.0)                            # clip + the 0.4 s drain margin
    assert pipe._preview_mute is False         # …and the paid voice is back


def _ring_pipe():
    pipe = types.SimpleNamespace(_pro_ring=__import__("collections").deque(),
                                 _pro_ring_bytes=0,
                                 PRO_RING_BYTES=IncomingPipeline.PRO_RING_BYTES)
    return pipe


def test_pro_ring_is_bounded_to_the_last_few_seconds():
    # It exists to be replayed after the session; it must not grow with it.
    pipe = _ring_pipe()
    chunk = b"\x01\x02" * 24000          # 1 s of 24 kHz PCM16
    for _ in range(30):
        IncomingPipeline._keep_pro_audio(pipe, chunk)
    pcm = IncomingPipeline.recent_pro_pcm(pipe)
    assert len(pcm) <= IncomingPipeline.PRO_RING_BYTES
    assert free_preview.duration_seconds(pcm) == pytest.approx(8.0, abs=1.0)


def test_ring_keeps_the_most_recent_audio():
    pipe = _ring_pipe()
    IncomingPipeline._keep_pro_audio(pipe, b"\xaa\xaa" * 24000 * 9)   # 9 s, evicted
    IncomingPipeline._keep_pro_audio(pipe, b"\xbb\xbb" * 100)         # newest
    assert IncomingPipeline.recent_pro_pcm(pipe).endswith(b"\xbb\xbb" * 100)


def test_a_failing_player_does_not_strand_the_mute():
    pipe = _fake_pipe()

    def boom(_):
        raise RuntimeError("device gone")

    pipe.player.feed_tts_pcm16 = boom
    with pytest.raises(RuntimeError):
        IncomingPipeline.play_free_preview(pipe, b"\x00\x00", 0.05)
    assert pipe._preview_mute is False  # a dead device must not silence the session
