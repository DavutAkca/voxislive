"""Bridge source<->translation pairing + save-flush robustness.

Regression coverage for the Qwen-beta transcript bugs Ivo reported on 1.0.26:
  * the JSON `src` field repeated the first source segment in every turn, and
  * a session whose translation stream dropped out reported "nothing to save".

Both stem from the transcript recorder coupling turn boundaries to LINE_GAP
timing gaps that Gemini's paused source stream produces but Qwen's continuous,
cumulative ASR does not.
"""
import threading

import app.webui as webui
from app.webui import Bridge, LINE_GAP, SRC_LAG_S


def _bare_bridge():
    """A Bridge with only the transcript buffers wired up — no ModeController,
    no window — so the pure text-pairing logic can be driven directly."""
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
    # Speaker-labeling state (see Bridge.__init__).
    b._cur_spk = None
    b._src_spk = None
    b._spk_seen = set()
    b._pending_spk_break = False
    b._put_event = lambda *a, **k: None      # swallow UI events
    b._obs_write = lambda *a, **k: None       # swallow OBS file writes
    # No live session/stager off this bare Bridge — backlog is always 0.
    b.controller = type("C", (), {"current_playback_backlog": lambda self: 0.0})()
    return b


def _feed(b, direction, text, t):
    """Drive _on_text_locked with a controlled monotonic clock so LINE_GAP
    boundaries are deterministic (no reliance on wall-clock spacing)."""
    orig = webui.time.time
    webui.time.time = lambda: t
    try:
        b._on_text_locked(direction, text)
    finally:
        webui.time.time = orig


def test_continuous_source_does_not_leak_into_earlier_turn():
    """Qwen streams source ASR continuously (no LINE_GAP pause) — the source
    stream alone gives no per-turn boundary, and translation trails it by the
    model's simultaneous-interpretation lag (SRC_LAG_S). Each translation
    turn claims only the source heard by (now - SRC_LAG_S) at the moment it
    finalizes, leaving anything heard more recently than that queued for a
    LATER turn instead of leaking into this one. Words that arrive after a
    turn's cutoff but before that turn is popped must NOT appear in that
    turn's src (the bug this replaced: grabbing "everything buffered right
    now" over-claimed into whichever turn finished first)."""
    b = _bare_bridge()
    # Continuous source, one word every second, never pausing >LINE_GAP —
    # nothing ever rolls into _src_done; it all stays live in _src_buf.
    for i, w in enumerate(["Uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho"]):
        _feed(b, "in", w + " ", float(i))
    # Turn 1 opens (no pop yet — first output of the session).
    _feed(b, "out", "T1.", 8.0)
    # More source keeps arriving WHILE turn 1 is the current line — this must
    # end up on a later turn, not turn 1, even though it's already in
    # _src_buf by the time turn 1's pop runs.
    _feed(b, "in", "nueve ", 9.0)
    _feed(b, "in", "diez ", 10.0)
    # Turn 1 finalizes (output pause) — cutoff = 10.6 - SRC_LAG_S.
    _feed(b, "out", "T2.", 10.6)
    assert 10.6 - SRC_LAG_S < 9.0, "test assumes cutoff lands before 'nueve'"
    # Turn 2 finalizes, now well past the cutoff for the remaining words.
    _feed(b, "out", "T3.", 10.6 + 0.1 + LINE_GAP)
    b._flush_turns()

    texts = [t["text"] for t in b._turns]
    srcs = [t["src"] for t in b._turns]
    assert texts == ["T1.", "T2.", "T3."]
    # Turn 1 claims only what had been heard well before it finalized —
    # "nueve"/"diez" (heard after turn 1's cutoff) are NOT in it.
    assert srcs[0] == "Uno dos tres cuatro cinco seis siete ocho"
    assert "nueve" not in srcs[0] and "diez" not in srcs[0]
    # They land on turn 2 instead — nothing dropped, nothing duplicated.
    assert srcs[1] == "nueve diez"
    assert srcs[2] is None


def test_gemini_paused_source_still_pairs_per_turn():
    """Gemini's source stream pauses (>LINE_GAP) between utterances, rolling
    _cur_src into _last_src. That path must keep pairing correctly after the fix."""
    b = _bare_bridge()
    _feed(b, "in", "Hello there.", 0.0)
    _feed(b, "out", "Merhaba oradaki.", 0.1)
    # A real speech pause: next source arrives >LINE_GAP later, so it rolls over.
    _feed(b, "in", "How are you?", 5.0)
    _feed(b, "out", "Nasilsin?", 5.1 + LINE_GAP)
    b._flush_turns()

    srcs = [t["src"] for t in b._turns]
    texts = [t["text"] for t in b._turns]
    assert texts == ["Merhaba oradaki.", "Nasilsin?"]
    assert srcs[0] == "Hello there."
    assert srcs[1] == "How are you?"


def test_two_sources_complete_before_one_turn_keep_both():
    """Two source utterances both pause-complete (>LINE_GAP) before a single
    translation turn finalizes. The old single-slot _last_src overwrote the first
    with the second, so its JSON `src` was lost and later turns went blank (the
    Gemini-on-a-movie regression Ivo reported on 1.0.27). The FIFO must preserve
    both and hand them to their turns in order — neither src is ever empty."""
    b = _bare_bridge()
    # Two speakers, each source separated by a real >LINE_GAP pause, BEFORE the
    # (lagging) translations for either arrive.
    _feed(b, "in", "Alice speaks.", 0.0)
    _feed(b, "in", "Bob replies.", 5.0)          # >LINE_GAP after Alice -> queued
    _feed(b, "in", "Alice again.", 10.0)         # >LINE_GAP after Bob   -> queued
    # Now the three translation turns finalize, each opening after a LINE_GAP gap.
    _feed(b, "out", "T-Alice.", 11.0)
    _feed(b, "out", "T-Bob.", 11.1 + LINE_GAP)
    _feed(b, "out", "T-Alice2.", 11.2 + 2 * LINE_GAP)
    b._flush_turns()

    srcs = [t["src"] for t in b._turns]
    texts = [t["text"] for t in b._turns]
    assert texts == ["T-Alice.", "T-Bob.", "T-Alice2."]
    # Every turn carries a non-empty, correctly-ordered source — no overwrite, no
    # blank src.
    assert "" not in srcs
    assert srcs == ["Alice speaks.", "Bob replies.", "Alice again."]


def test_flush_saves_source_when_translation_stream_dropped():
    """If Qwen drops its translation text entirely but source ASR arrived, the
    session must still be saveable (source-only turn) rather than "nothing to save"."""
    b = _bare_bridge()
    _feed(b, "in", "Source only, no translation came back.", 0.0)
    assert b._turns == []          # nothing folded yet
    b._flush_turns()
    assert len(b._turns) == 1
    assert b._turns[0]["src"] == "Source only, no translation came back."
    assert b._turns[0]["text"] == ""


def test_flush_does_not_add_spurious_source_only_turn_to_normal_session():
    """A normal session with real translation must NOT get an extra empty-text
    source-only turn appended when a residual source tail lingers at stop."""
    b = _bare_bridge()
    _feed(b, "in", "One.", 0.0)
    _feed(b, "out", "Bir.", 0.1)
    # Residual source arrives after the translation turn (its translation is still
    # "in flight" at stop) — must not become a bogus trailing turn.
    _feed(b, "in", "Two.", 5.0)
    b._flush_turns()
    # Only the real translation turn is recorded.
    assert [t["text"] for t in b._turns] == ["Bir."]


def test_empty_session_saves_nothing():
    b = _bare_bridge()
    b._flush_turns()
    assert b._turns == []


def test_opt_in_problem_report_keeps_source_only_recovery_text():
    b = _bare_bridge()
    b._turns = [{"src": "Recovered source", "text": ""}]
    assert b._collect_transcript() == "Recovered source"
