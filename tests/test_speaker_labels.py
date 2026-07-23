"""Speaker labels end-to-end below the tracker: bridge pairing, the SPK_GAP
soft turn split, the ≥2-speaker display gate, and the export renderers.

The detector itself is covered in test_speaker_id.py; here its change events
are injected directly so the transcript plumbing is deterministic.
"""
import threading

import numpy as np

import app.webui as webui
from app.pipeline import _FRAME, _GatedSource
from app.transcript_store import build_record, render_srt, render_txt, render_vtt
from app.webui import LINE_GAP, SPK_GAP, SRC_LAG_S, Bridge


def _bare_bridge():
    b = object.__new__(Bridge)
    b._text_lock = threading.RLock()
    b._src_buf = ""
    b._src_done = []
    b._src_marks = []
    b._last_src_t = 0.0
    b._cur_line = ""
    b._last_t = 0.0
    b._session_start = 0.0
    b._turn_start = 0.0
    b._lines = []
    b._turns = []
    b._overlay_text = ""
    b._overlay_until = 0.0
    b._cur_spk = None
    b._src_spk = None
    b._spk_seen = set()
    b._pending_spk_break = False
    b.events = []
    b._put_event = b.events.append
    b._obs_write = lambda *a, **k: None
    # No live session/stager off this bare Bridge — backlog is always 0.
    b.controller = type("C", (), {"current_playback_backlog": lambda self: 0.0})()
    return b


def _feed(b, direction, text, t):
    orig = webui.time.time
    webui.time.time = lambda: t
    try:
        b._on_text_locked(direction, text)
    finally:
        webui.time.time = orig


def test_speaker_change_splits_back_to_back_turn():
    """The reported bug: two people speaking back-to-back (no LINE_GAP pause
    anywhere) used to merge into ONE caption turn. A speaker-change event must
    split the translated stream at the next micro-pause (> SPK_GAP) and tag
    each turn with its own speaker.

    Output is fed SRC_LAG_S behind its matching source (the model's real
    simultaneous-interpretation lag — see SRC_LAG_S), since _pop_source's
    cutoff is relative to that lag, not to output-side gaps."""
    b = _bare_bridge()
    b._on_speaker(1)
    _feed(b, "in", "Hallo, wie geht's?", 0.0)
    _feed(b, "out", "Merhaba, nasılsın?", SRC_LAG_S)
    # Speaker 2 starts talking immediately (no pause). The tracker fires.
    b._on_speaker(2)
    _feed(b, "in", "Gut, danke!", 1.0)   # arrives < LINE_GAP after S1's source
    # S2's translation begins only ~1 s after S1's own turn started — far
    # below LINE_GAP, which is exactly why the old code merged the two
    # speakers; SPK_GAP is what splits them instead.
    _feed(b, "out", "İyiyim, teşekkürler!", SRC_LAG_S + SPK_GAP + 0.1)
    b._flush_turns()

    texts = [t["text"] for t in b._turns]
    assert texts == ["Merhaba, nasılsın?", "İyiyim, teşekkürler!"]
    assert [t.get("spk") for t in b._turns] == [1, 2]
    assert [t["src"] for t in b._turns] == ["Hallo, wie geht's?", "Gut, danke!"]


def test_no_speaker_change_keeps_line_gap_semantics():
    """Without a change event, sub-LINE_GAP pauses must NOT split the turn —
    the soft SPK_GAP break exists only while a change is pending."""
    b = _bare_bridge()
    b._on_speaker(1)
    _feed(b, "out", "One long ", 1.0)
    _feed(b, "out", "sentence.", 1.0 + SPK_GAP + 0.2)  # micro-pause, same voice
    b._flush_turns()
    assert [t["text"] for t in b._turns] == ["One long sentence."]


def test_pending_break_disarms_on_natural_turn_boundary():
    """If a change event lands while no line is streaming (a natural LINE_GAP
    boundary already separates the voices), the armed break must not linger and
    split the NEXT speaker's own line at its first micro-pause."""
    b = _bare_bridge()
    b._on_speaker(1)
    _feed(b, "out", "First turn.", 1.0)
    b._on_speaker(2)                       # change during silence
    _feed(b, "out", "Second speaker ", 1.0 + LINE_GAP + 1.0)  # new turn opens
    _feed(b, "out", "keeps one line.", 1.0 + LINE_GAP + 1.0 + SPK_GAP + 0.1)
    b._flush_turns()
    assert [t["text"] for t in b._turns] == ["First turn.",
                                             "Second speaker keeps one line."]


def test_single_speaker_session_sends_no_labels_to_ui():
    """One voice only: the src UI event must carry spk=None (no 'S1' tag ever
    appears), even though the JSON records the raw label."""
    b = _bare_bridge()
    b._on_speaker(1)
    _feed(b, "in", "Hello.", 0.0)
    _feed(b, "out", "Merhaba.", 0.5)
    _feed(b, "out", "next turn", 0.5 + LINE_GAP + 0.1)  # finalizes turn 1
    src_events = [e for e in b.events if e[0] == "src"]
    assert src_events and src_events[0][2] is None
    trans_events = [e for e in b.events if e[0] == "trans"]
    assert all(e[3] is None for e in trans_events)
    assert b._turns[0].get("spk") == 1  # raw label still in the record


def test_multi_speaker_overlay_gets_prefix():
    b = _bare_bridge()
    b._on_speaker(1)
    _feed(b, "in", "Eins.", 0.0)
    b._on_speaker(2)
    _feed(b, "in", "Zwei.", 0.2)
    _feed(b, "out", "Bir.", 0.5)
    assert b._overlay_text.startswith("S1: ")


def test_source_buffer_split_tags_each_side():
    """Words accumulated before the change belong to the previous speaker even
    though they finalize later (the _src_spk bookkeeping)."""
    b = _bare_bridge()
    b._on_speaker(1)
    _feed(b, "in", "Alpha words.", 0.0)
    b._on_speaker(2)                      # splits + queues ("Alpha words.", spk 1)
    _feed(b, "in", "Beta words.", 0.3)    # new buffer starts under spk 2
    assert b._src_done == [(1, "Alpha words.", 0.0)]
    assert b._src_spk == 2
    assert b._pending_spk_break


# ---- export renderers ----

def _record(turns):
    return build_record(0.0, turns)


def test_exports_prefix_only_multi_speaker_sessions():
    single = _record([{"t": 0.0, "src": "a", "text": "A", "spk": 1},
                      {"t": 2.0, "src": "b", "text": "B", "spk": 1}])
    multi = _record([{"t": 0.0, "src": "a", "text": "A", "spk": 1},
                     {"t": 2.0, "src": "b", "text": "B", "spk": 2}])
    assert "S1:" not in render_txt(single)
    txt = render_txt(multi)
    assert "S1: A" in txt and "S2: B" in txt
    srt = render_srt(multi, bilingual=True)
    assert "S1: a\nS1: A" in srt and "S2: b\nS2: B" in srt
    vtt = render_vtt(multi, bilingual=False)
    assert "S1: A" in vtt and "S2: B" in vtt
    # Unlabeled legacy records render exactly as before.
    legacy = _record([{"t": 0.0, "src": "a", "text": "A"}])
    assert render_txt(legacy) == "A\n"


def test_exports_tag_only_where_speaker_changes():
    """One speaker across several consecutive turns is ONE labeled run — the
    tag appears at the change, not on every line (owner feedback 2026-07-10)."""
    rec = _record([
        {"t": 0.0, "text": "A1", "spk": 1},
        {"t": 2.0, "text": "A2", "spk": 1},
        {"t": 4.0, "text": "B1", "spk": 2},
        {"t": 6.0, "text": "B2", "spk": 2},
        {"t": 8.0, "text": "A3", "spk": 1},
    ])
    assert render_txt(rec).splitlines() == \
        ["S1: A1", "A2", "S2: B1", "B2", "S1: A3"]
    srt = render_srt(rec, bilingual=False)
    assert "S1: A1" in srt and "\nA2\n" in srt
    assert "S2: B1" in srt and "\nB2\n" in srt and "S1: A3" in srt


def test_engine_respeak_duplicate_turn_is_dropped():
    """Gemini can re-emit the tail utterance after an internal reconnect; two
    identical consecutive LONG turns are that echo and only one is recorded
    (field transcript 2026-07-10, t=39s). Short repeats are real dialogue."""
    b = _bare_bridge()
    long = "This is a long enough sentence to trigger the guard."
    _feed(b, "out", long, 1.0)
    _feed(b, "out", long, 1.0 + LINE_GAP + 0.1)          # echo -> new turn
    _feed(b, "out", "Next real line.", 1.0 + 2 * (LINE_GAP + 0.1))
    b._flush_turns()
    assert [t["text"] for t in b._turns] == [long, "Next real line."]
    # Short identical turns stay: plausible real repetition.
    b2 = _bare_bridge()
    _feed(b2, "out", "Evet.", 1.0)
    _feed(b2, "out", "Evet.", 1.0 + LINE_GAP + 0.1)
    _feed(b2, "out", "Devam.", 1.0 + 2 * (LINE_GAP + 0.1))
    b2._flush_turns()
    assert [t["text"] for t in b2._turns] == ["Evet.", "Evet.", "Devam."]


def test_build_record_preserves_spk_and_tolerates_absence():
    rec = _record([{"t": 0.0, "text": "x", "spk": 3}, {"t": 1.0, "text": "y"}])
    assert rec["turns"][0]["spk"] == 3
    assert "spk" not in rec["turns"][1]


# ---- capture-side tap ----

class _ScriptedGate:
    def __init__(self):
        self.script = []

    def process(self, frame):
        if self.script:
            return self.script.pop(0)
        return False, []


def test_gated_source_speech_tap_sees_only_speech():
    gate, sent, tapped = _ScriptedGate(), [], []
    src = _GatedSource(16000, gate, sent.append, speech_tap=tapped.append)
    speech = np.ones(_FRAME, dtype=np.float32)
    gate.script = [(True, [speech]), (False, [])]
    src.feed(np.zeros(_FRAME * 2, dtype=np.float32))
    assert len(tapped) == 1 and len(tapped[0]) == 1  # one emit, one frame


def test_gated_source_tap_errors_never_break_capture():
    def bad_tap(frames):
        raise RuntimeError("tracker died")
    gate, sent = _ScriptedGate(), []
    src = _GatedSource(16000, gate, sent.append, speech_tap=bad_tap)
    gate.script = [(True, [np.ones(_FRAME, dtype=np.float32)])]
    src.feed(np.zeros(_FRAME, dtype=np.float32))  # must not raise
    assert len(sent) == 1  # audio still reached the translator
