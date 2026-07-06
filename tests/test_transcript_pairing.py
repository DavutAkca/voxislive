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
from app.webui import Bridge, LINE_GAP


def _bare_bridge():
    """A Bridge with only the transcript buffers wired up — no ModeController,
    no window — so the pure text-pairing logic can be driven directly."""
    b = object.__new__(Bridge)
    b._text_lock = threading.RLock()
    b._cur_src = b._last_src = ""
    b._last_src_t = 0.0
    b._cur_line = ""
    b._last_t = 0.0
    b._session_start = 0.0
    b._turn_start = 0.0
    b._lines = []
    b._turns = []
    b._overlay_text = ""
    b._overlay_until = 0.0
    b._put_event = lambda *a, **k: None      # swallow UI events
    b._obs_write = lambda *a, **k: None       # swallow OBS file writes
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


def test_continuous_source_does_not_repeat_across_turns():
    """Qwen streams source ASR continuously (no LINE_GAP pause). Each translation
    turn must capture its OWN source, not re-emit the growing leading segment."""
    b = _bare_bridge()
    # Utterance 1: source then its translation.
    _feed(b, "in", "First sentence.", 0.0)
    _feed(b, "out", "Translation one.", 0.1)
    # Utterance 2: source keeps flowing with NO >LINE_GAP gap between "in" deltas,
    # then a new translation turn opens after a LINE_GAP silence in the OUT stream.
    _feed(b, "in", " Second sentence.", 1.0)
    _feed(b, "out", "Translation two.", 0.2 + LINE_GAP)
    # Utterance 3.
    _feed(b, "in", " Third sentence.", 1.0 + LINE_GAP)
    _feed(b, "out", "Translation three.", 0.3 + 2 * LINE_GAP)
    b._flush_turns()

    srcs = [t["src"] for t in b._turns]
    texts = [t["text"] for t in b._turns]
    assert texts == ["Translation one.", "Translation two.", "Translation three."]
    # The regression the fix targets: _cur_src was never consumed, so it grew
    # unbounded and EVERY turn's src contained the leading "First sentence."
    # (srcs == ["First…", "First… Second…", "First… Second… Third…"]). After the
    # fix each source segment is consumed once and appears in exactly one turn.
    assert sum("First" in s for s in srcs) == 1
    assert sum("Second" in s for s in srcs) == 1
    assert sum("Third" in s for s in srcs) == 1
    assert "First" in srcs[0]


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
