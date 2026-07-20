"""Cascade free-tier engine: pacing policy, sentence assembly, and the
translator contract via injected fakes (no network, no sherpa).

The pacing/flush constants are BENCH-PINNED (notes/spike_bench.py, 2026-07-12):
these tests keep the field-tuned behavior from regressing, they are not knobs
to revisit casually.
"""
import threading
import time

import numpy as np
import pytest

from app.cascade_translator import (CLAUSE_FLUSH_CHARS, MAX_BUF,
                                    CascadeTranslator, SentenceAssembler,
                                    _resample_to_out, pick_speed)
from app.i18n import t


# ---------------------------------------------------------------- pick_speed
@pytest.mark.parametrize("backlog,age,stalled,quiet,expected", [
    (0, 0.0, False, False, 1.0),   # fresh, nothing queued
    (1, 0.0, False, False, 1.2),   # one behind
    (2, 0.0, False, False, 1.5),   # two behind
    (0, 2.1, False, False, 1.2),   # aging sentence, queue empty (v3 regression)
    (0, 3.6, False, False, 1.5),   # old sentence — drain hard
    (0, 0.1, True, False, 1.2),    # text stalled: brisk even when fresh
    (0, 1.6, True, False, 1.5),    # stalled + aged: hard drain
    (0, 0.1, False, True, 1.2),    # source silent (video paused)
    (0, 1.6, False, True, 1.5),    # source silent + aged
])
def test_pick_speed_matrix(backlog, age, stalled, quiet, expected):
    assert pick_speed(backlog, age, stalled, quiet) == expected


# ------------------------------------------------------------------ assembler
def collect():
    out = []
    return out, SentenceAssembler(out.append)


def test_sentences_split_on_terminal_punctuation():
    out, asm = collect()
    asm.feed("Merhaba dünya. Bu ikinci cümle! Ve yarım")
    assert out == ["Merhaba dünya.", "Bu ikinci cümle!"]
    assert asm.buf.strip() == "Ve yarım"


def test_clause_flush_speaks_from_last_comma():
    out, asm = collect()
    head = "a" * 60 + ", " + "b" * (CLAUSE_FLUSH_CHARS - 40)
    asm.feed(head)  # long, no terminal punctuation, has a comma past 40 chars
    assert len(out) == 1 and out[0].endswith(",")


def test_max_buf_flush_without_any_punctuation():
    out, asm = collect()
    asm.feed("kelime " * 40)  # > MAX_BUF chars, no punctuation, no comma
    assert out and all(len(s) <= MAX_BUF for s in out)


def test_quiet_flush_beats_idle_window():
    out, asm = collect()
    asm.feed("yarım kalan bir cümle")
    asm.last_delta = time.monotonic() - 0.8   # idle 0.8 s
    asm.maybe_idle_flush(src_quiet_for=0.0)   # source loud: stays buffered
    assert out == []
    asm.maybe_idle_flush(src_quiet_for=0.8)   # source silent: flushes now
    assert out == ["yarım kalan bir cümle"]


def test_idle_flush_needs_sentence_sized_buffer():
    out, asm = collect()
    asm.feed("kısa parça")                    # < IDLE_FLUSH_MIN_CHARS
    asm.last_delta = time.monotonic() - 1.8
    asm.maybe_idle_flush(src_quiet_for=0.0)   # mid-speech trickle: hold
    assert out == []
    asm.last_delta = time.monotonic() - 2.6   # past the hard window
    asm.maybe_idle_flush(src_quiet_for=0.0)
    assert out == ["kısa parça"]


# ------------------------------------------------------------------ resample
def test_resample_emits_24k_pcm16():
    pcm = _resample_to_out(np.zeros(22050, dtype=np.float32), 22050)
    assert len(pcm) == 24000 * 2  # 1 s of audio -> 1 s @ 24 kHz PCM16


# ------------------------------------------------- wrapper contract via fakes
class FakeInner:
    def __init__(self):
        self._ready = threading.Event()
        self._ready.set()
        self.sent = []
        self.stopped = False
        self.on_fatal = None
        self._alive = True

    def send_pcm16(self, data):
        self.sent.append(data)

    def wait_ready(self, timeout=15):
        return True

    def start(self):
        pass

    def stop(self):
        self.stopped = True
        self._alive = False

    def is_alive(self):
        return self._alive


class FakeTTS:
    def synth(self, text, speed=1.0):
        return np.zeros(2205, dtype=np.float32), 22050  # 0.1 s of silence


def make_cascade(monkeypatch=None):
    events = {"audio": [], "text": [], "status": []}
    inner = FakeInner()

    def inner_factory(api_key, target_lang, *, on_text, **kw):
        inner.on_text = on_text
        return inner

    tr = CascadeTranslator(
        "key", "tr",
        on_audio=events["audio"].append,
        on_text=lambda kind, txt: events["text"].append((kind, txt)),
        on_status=events["status"].append,
        inner_factory=inner_factory, tts_factory=FakeTTS)
    return tr, inner, events


def test_contract_surface_and_text_to_audio_flow():
    tr, inner, events = make_cascade()
    assert tr.engine == "cascade"
    tr.start()
    try:
        assert tr.wait_ready(1)
        assert tr._ready.is_set()          # billing gate delegates to inner
        # A complete translated sentence must reach BOTH captions and the TTS.
        inner.on_text("out", "Merhaba dünya. ")
        deadline = time.monotonic() + 3.0
        while not events["audio"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert events["text"] == [("out", "Merhaba dünya. ")]
        assert events["audio"], "synth loop never emitted audio"
        # 0.1 s @22050 resampled -> ~0.1 s @24 kHz PCM16 (~4800 bytes).
        assert abs(len(events["audio"][0]) - 4800) <= 8
    finally:
        tr.stop()
        tr.join(timeout=2)
    assert inner.stopped


def test_send_pcm16_forwards_and_tracks_silence():
    tr, inner, _ = make_cascade()
    loud = (np.ones(512, dtype=np.int16) * 5000).tobytes()
    quiet = np.zeros(512, dtype=np.int16).tobytes()
    tr.send_pcm16(loud)
    assert tr._src_quiet_since == 0.0
    tr.send_pcm16(quiet)
    assert tr._src_quiet_since > 0.0
    tr.send_pcm16(loud)
    assert tr._src_quiet_since == 0.0
    assert inner.sent == [loud, quiet, loud]


def test_frame_drought_counts_as_silence_on_gated_stream():
    # The cascade streams GATED: silence frames never arrive at all, so quiet
    # detection must key off "no frames lately", or the fast tail flush dies.
    tr, _, _ = make_cascade()
    loud = (np.ones(512, dtype=np.int16) * 5000).tobytes()
    tr.send_pcm16(loud)
    assert tr._src_quiet_for() == 0.0        # just heard speech
    tr._last_pcm_ts = time.monotonic() - 1.5  # then the gate went dry
    assert tr._src_quiet_for() > 1.0          # drought minus 0.3 s grace


def test_dead_inner_ends_the_supervisor_thread():
    tr, inner, _ = make_cascade()
    tr.start()
    inner._alive = False
    tr.join(timeout=2)
    assert not tr.is_alive()   # pipeline's translator-death teardown relies on this


def test_tts_failure_degrades_to_captions_only():
    events = {"status": [], "text": []}
    inner = FakeInner()

    def inner_factory(api_key, target_lang, *, on_text, **kw):
        inner.on_text = on_text
        return inner

    def broken_tts():
        raise RuntimeError("no voice for this language")

    tr = CascadeTranslator(
        "key", "xx",
        on_audio=lambda pcm: pytest.fail("no audio expected in captions-only"),
        on_text=lambda kind, txt: events["text"].append((kind, txt)),
        on_status=events["status"].append,
        inner_factory=inner_factory, tts_factory=broken_tts)
    tr.start()
    try:
        inner.on_text("out", "Ahoj svete. ")
        time.sleep(0.2)
        assert events["text"], "captions must survive a dead voice"
        assert t("st_no_voice_warning") in events["status"]
    finally:
        tr.stop()
        tr.join(timeout=2)
