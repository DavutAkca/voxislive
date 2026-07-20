"""_GatedSource framing/emit policies and the _Accum preallocated buffer."""
import numpy as np

from app.pipeline import _FRAME, _SMART_SILENCE_MAX_FRAMES, _Accum, _GatedSource


class _ScriptedGate:
    """process() returns (active, to_send) from a script; defaults to silence."""

    def __init__(self):
        self.script = []

    def process(self, frame):
        if self.script:
            return self.script.pop(0)
        return False, []


def _mk(gate, sent, **kw):
    return _GatedSource(16000, gate, sent.append, **kw)


def test_accum_push_pop_and_growth():
    a = _Accum(4)
    a.push(np.arange(3, dtype=np.float32))
    a.push(np.arange(3, 10, dtype=np.float32))  # forces growth
    assert a.n == 10
    np.testing.assert_allclose(a.pop(4), [0, 1, 2, 3])
    assert a.n == 6
    np.testing.assert_allclose(a.pop(6), [4, 5, 6, 7, 8, 9])
    assert a.n == 0


def test_pop_returns_independent_copy():
    a = _Accum(8)
    a.push(np.ones(4, dtype=np.float32))
    out = a.pop(2)
    a.push(np.full(4, 9.0, dtype=np.float32))
    np.testing.assert_allclose(out, 1.0)  # unaffected by later pushes


def test_gated_mode_emits_only_gate_frames():
    gate, sent = _ScriptedGate(), []
    src = _mk(gate, sent)
    gate.script = [(True, [np.ones(_FRAME, dtype=np.float32)]), (False, [])]
    src.feed(np.zeros(_FRAME * 2, dtype=np.float32))
    assert len(sent) == 1
    assert len(sent[0]) == _FRAME * 2  # 512 samples * 2 bytes (pcm16)


def test_smart_mode_pads_then_stops():
    gate, sent = _ScriptedGate(), []
    src = _mk(gate, sent, smart=True)
    n = _SMART_SILENCE_MAX_FRAMES + 20
    src.feed(np.zeros(_FRAME * n, dtype=np.float32))  # gate: all silence
    # Exactly the cap's worth of silence padding, then the stream goes quiet.
    assert len(sent) == _SMART_SILENCE_MAX_FRAMES
    assert all(s == b"\x00" * (_FRAME * 2) for s in sent)


def test_smart_mode_speech_resets_pad_budget():
    gate, sent = _ScriptedGate(), []
    src = _mk(gate, sent, smart=True)
    f = np.ones(_FRAME, dtype=np.float32)
    gate.script = [(False, [])] * 3 + [(True, [f])] + [(False, [])] * 3
    src.feed(np.zeros(_FRAME * 7, dtype=np.float32))
    # 3 pads + 1 speech + 3 pads (budget reset by the speech frame)
    assert len(sent) == 7


def test_suppress_zeroes_input():
    gate, sent = _ScriptedGate(), []
    src = _GatedSource(16000, gate, sent.append, suppress_when=lambda: True,
                       always_send=True)
    src.feed(np.ones(_FRAME, dtype=np.float32))
    assert len(sent) == 1
    assert sent[0] == b"\x00" * (_FRAME * 2)


def test_fullband_path_emits_send_rate_frames():
    gate, sent = _ScriptedGate(), []
    src = _GatedSource(16000, gate, sent.append, send_rate=24000)
    f = np.ones(_FRAME, dtype=np.float32)
    n = 20
    gate.script = [(True, [f])] * n
    src.feed(np.random.default_rng(0).standard_normal(_FRAME * n).astype(np.float32) * 0.1)
    assert len(sent) == n
    # Every payload is a full-band 768-sample (24 kHz) frame regardless of
    # whether it came from history or the startup upsample branch.
    assert all(len(s) == src._send_frame * 2 for s in sent)


def test_send_rate_can_switch_from_openai_to_gemini_without_reopening_capture():
    gate, sent = _ScriptedGate(), []
    src = _GatedSource(16000, gate, sent.append, send_rate=24000)
    frame = np.ones(_FRAME, dtype=np.float32)
    gate.script = [(True, [frame])]
    src.feed(frame)
    assert len(sent[-1]) == 768 * 2

    src.set_send_rate(16000)
    gate.script = [(True, [frame])]
    src.feed(frame)
    assert src._send_rate == 16000
    assert len(sent[-1]) == _FRAME * 2


def test_closed_source_drops_input():
    gate, sent = _ScriptedGate(), []
    src = _mk(gate, sent, always_send=True)
    src.closed = True
    src.feed(np.ones(_FRAME, dtype=np.float32))
    assert sent == []
