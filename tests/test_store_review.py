"""The Store-rating ask.

The Store shows stars per market and only once a market has collected a few, so
the first ratings matter more than the hundredth — but an ask at the wrong moment
collects one-star ratings instead. These pin the gate: only a session that
actually worked earns the prompt, it is offered exactly once, and it never fires
outside the Store build (a sideloaded .exe has no listing to rate).
"""
import time
import types

import pytest

from app import store_review, webui


@pytest.fixture
def bridge(monkeypatch):
    """A Bridge stand-in carrying only what _note_good_session reads."""
    saved = {}
    monkeypatch.setattr(webui, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(store_review, "available", lambda: True)
    b = types.SimpleNamespace(
        _session_error=False,
        _session_start=time.time() - 10 * 60,   # a ten-minute session
        cfg={"good_sessions": 0, "review_prompted": False},
        events=[],
    )
    b._put_event = b.events.append
    b.REVIEW_MIN_SECONDS = webui.Bridge.REVIEW_MIN_SECONDS
    b.REVIEW_AFTER_SESSIONS = webui.Bridge.REVIEW_AFTER_SESSIONS
    b.note = lambda: webui.Bridge._note_good_session(b)
    b.saved = saved
    return b


def test_prompts_on_the_third_clean_session(bridge):
    bridge.note()
    bridge.note()
    assert bridge.events == [], "asked too early"
    assert bridge.cfg["good_sessions"] == 2

    bridge.note()
    assert bridge.events == [("review", None)]
    assert bridge.cfg["review_prompted"] is True


def test_asks_only_once_ever(bridge):
    for _ in range(6):
        bridge.note()
    assert bridge.events.count(("review", None)) == 1


def test_a_failed_session_does_not_count(bridge):
    bridge._session_error = True
    for _ in range(5):
        bridge.note()
    assert bridge.events == []
    assert bridge.cfg["good_sessions"] == 0


def test_a_session_that_produced_nothing_does_not_count(bridge):
    # _session_start is only set by the first translated token.
    bridge._session_start = 0.0
    for _ in range(5):
        bridge.note()
    assert bridge.events == []


def test_a_short_session_does_not_count(bridge):
    bridge._session_start = time.time() - 5.0
    for _ in range(5):
        bridge.note()
    assert bridge.events == []
    assert bridge.cfg["good_sessions"] == 0


def test_never_asks_outside_the_store_build(bridge, monkeypatch):
    monkeypatch.setattr(store_review, "available", lambda: False)
    for _ in range(5):
        bridge.note()
    assert bridge.events == []
    assert bridge.cfg["good_sessions"] == 0


def test_bookkeeping_failure_never_breaks_stop(bridge, monkeypatch):
    def boom(_cfg):
        raise OSError("disk full")

    monkeypatch.setattr(webui, "save_config", boom)
    bridge.note()  # must not raise — this runs inside session teardown


def test_review_page_is_not_opened_off_store(monkeypatch):
    monkeypatch.setattr(store_review.paths, "is_store_build", lambda: False)
    opened = []
    monkeypatch.setattr(store_review.os, "startfile", opened.append)
    assert store_review.open_review_page() is False
    assert opened == []


def test_review_page_targets_the_voxis_listing(monkeypatch):
    monkeypatch.setattr(store_review.paths, "is_store_build", lambda: True)
    opened = []
    monkeypatch.setattr(store_review.os, "startfile", opened.append)
    assert store_review.open_review_page() is True
    assert opened == [f"ms-windows-store://review/?ProductId={store_review.PRODUCT_ID}"]
