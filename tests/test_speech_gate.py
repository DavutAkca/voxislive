"""SpeechGate open/close semantics with a scripted (fake) VAD — no ONNX model
load, so the test is fast and deterministic."""
import numpy as np
import pytest

import app.vad as vad_mod
from app.vad import FRAME


class _FakeVAD:
    """Returns pre-scripted probabilities in feed order."""

    def __init__(self):
        self.probs = []

    def prob(self, frame):
        return self.probs.pop(0) if self.probs else 0.0


@pytest.fixture
def gate(monkeypatch):
    fake = _FakeVAD()
    monkeypatch.setattr(vad_mod, "SileroVAD", lambda: fake)
    # 32 ms frames: min_speech=2 frames, hangover=2 frames, preroll=2 frames.
    g = vad_mod.SpeechGate(threshold=0.5, min_speech_ms=64, hangover_ms=64,
                           preroll_ms=64)
    g.vad = fake  # constructed via the monkeypatched factory already, but be explicit
    return g, fake


def _frame(v=0.0):
    return np.full(FRAME, v, dtype=np.float32)


def test_opens_after_min_speech_and_emits_preroll(gate):
    g, fake = gate
    # Two silent frames fill the preroll ring, then two speech frames open it.
    fake.probs = [0.0, 0.0, 0.9, 0.9]
    for i in range(2):
        active, send = g.process(_frame(0.1 * i))
        assert not active and send == []
    active, send = g.process(_frame(1.0))
    assert not active and send == []  # onset 1/2 — pending, not yet open
    active, send = g.process(_frame(2.0))
    assert active
    # preroll (2 silent) + pending (first speech) + current speech frame
    assert len(send) == 4


def test_choppy_speech_opens_non_consecutively(gate):
    """Regression (Ivo/CZ): consonant-dense speech dips the VAD probability
    between voiced frames, so the two above-threshold frames that open the gate
    are NOT consecutive. The old "N consecutive, reset on any dip" rule never
    opened here and emitted the whole utterance as silence."""
    g, fake = gate
    # 0.9 (above), 0.2 (dip, but not a sustained release), 0.9 (above) -> opens.
    fake.probs = [0.9, 0.2, 0.9]
    active, _ = g.process(_frame())
    assert not active                       # onset 1/2
    active, _ = g.process(_frame())
    assert not active                       # dip held, onset preserved (not reset)
    active, send = g.process(_frame())
    assert active                           # 2nd above-threshold frame commits
    assert len(send) == 3                   # the two onset frames + the held dip


def test_stays_open_through_modulated_speech(gate):
    """Once open, frames that dip below threshold but stay above neg_threshold
    (0.35 here) must NOT count toward closing — hysteresis keeps the segment
    alive through natural speech modulation."""
    g, fake = gate
    fake.probs = [0.9, 0.9, 0.4, 0.4, 0.4]  # 0.4 is < threshold(0.5) but > neg(0.35)
    g.process(_frame()); g.process(_frame())  # opens
    for _ in range(3):
        active, send = g.process(_frame())
        assert active and len(send) == 1


def test_stays_open_through_short_pause(gate):
    g, fake = gate
    fake.probs = [0.9, 0.9, 0.0, 0.9]
    g.process(_frame()); g.process(_frame())
    active, send = g.process(_frame())  # 1 silent frame < hangover(2)
    assert active and len(send) == 1
    active, _ = g.process(_frame())
    assert active


def test_closes_after_hangover(gate):
    g, fake = gate
    fake.probs = [0.9, 0.9, 0.0, 0.0, 0.0]
    g.process(_frame()); g.process(_frame())
    g.process(_frame())                # silence 1/2 — still open
    active, _ = g.process(_frame())    # silence 2/2 — closes
    assert not active
    active, send = g.process(_frame())
    assert not active and send == []


def test_transient_blip_never_opens(gate):
    """A lone spike followed by sustained silence (below neg_threshold) is a
    click/transient, not speech — the onset must abort without opening."""
    g, fake = gate
    fake.probs = [0.9, 0.0, 0.0, 0.0]  # single spike, then a full hangover of silence
    for _ in range(4):
        active, send = g.process(_frame())
        assert not active and send == []
