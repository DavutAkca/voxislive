"""Playback-backlog signal riding the 'trans' UI event.

A NEW caption line's on-screen appearance can be held back by the client
(index.html onTrans/flushPendingTurn) so it doesn't visibly precede audio
still queued behind a playback backlog. Two things must hold: the backlog
reading itself (ModeController.current_playback_backlog), and that webui only
attaches it on a genuine new-line boundary (recomputing it per token would be
wasted work — see _on_text_locked).
"""
import threading

from app.pipeline import IncomingPipeline, ModeController
from app.webui import Bridge, LINE_GAP


class _FakeStager:
    def __init__(self, backlog_s):
        self.backlog_s = backlog_s


def _controller_with_stager(stager):
    ctl = ModeController.__new__(ModeController)
    pipe = IncomingPipeline.__new__(IncomingPipeline)
    if stager is not None:
        pipe._stager = stager
    ctl._pipelines = [pipe]
    return ctl


def test_current_playback_backlog_reads_the_incoming_stager():
    ctl = _controller_with_stager(_FakeStager(3.456))
    assert ctl.current_playback_backlog() == 3.46


def test_current_playback_backlog_zero_without_a_stager():
    # Cascade (free-tier) engine paces its own local synthesis and never
    # builds a stager at all.
    ctl = _controller_with_stager(None)
    assert ctl.current_playback_backlog() == 0.0


def test_current_playback_backlog_zero_when_idle():
    ctl = ModeController.__new__(ModeController)
    ctl._pipelines = []
    assert ctl.current_playback_backlog() == 0.0


def _bare_bridge_with_backlog(backlog_s):
    b = object.__new__(Bridge)
    b._text_lock = threading.RLock()
    b._src_buf = ""
    b._src_done = []
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
    b._src_append_target = None
    b.events = []
    b._put_event = b.events.append
    b._obs_write = lambda *a, **k: None
    b.controller = type(
        "C", (), {"current_playback_backlog": lambda self: backlog_s})()
    return b


def test_new_line_event_carries_the_live_backlog():
    b = _bare_bridge_with_backlog(2.5)
    import app.webui as webui
    orig = webui.time.time
    try:
        webui.time.time = lambda: 0.0
        b._on_text_locked("out", "Merhaba.")   # first token: no line to close yet
        trans = [e for e in b.events if e[0] == "trans"]
        assert trans[-1][2] is False           # not a newline -- nothing to delay
        assert trans[-1][4] == 0.0

        # A LINE_GAP pause then a second turn's first token closes turn 1:
        # THIS is the event whose backlog value gates the new bubble's
        # client-side delay.
        webui.time.time = lambda: LINE_GAP + 0.1
        b._on_text_locked("out", "Nasilsin?")
        trans = [e for e in b.events if e[0] == "trans"]
        assert trans[-1][2] is True
        assert trans[-1][4] == 2.5
    finally:
        webui.time.time = orig
