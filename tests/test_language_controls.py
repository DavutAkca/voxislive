"""Language-direction controls must update both targets as one operation."""

from app.webui import Bridge


def test_language_swap_is_atomic():
    bridge = Bridge.__new__(Bridge)
    bridge.cfg = {
        "target_language_incoming": "ru",
        "target_language_outgoing": "en",
    }
    calls = []
    bridge._prefetch_session_key = lambda: calls.append("prefetch")
    bridge._maybe_restart = lambda: calls.append("restart")
    bridge._save_cfg = lambda: calls.append("save") or True

    result = bridge.swap_languages()

    assert result == {"ok": True, "incoming": "en", "outgoing": "ru"}
    assert bridge.cfg == {
        "target_language_incoming": "en",
        "target_language_outgoing": "ru",
    }
    assert calls == ["save", "prefetch", "restart"]


def test_language_swap_rolls_back_when_config_cannot_be_saved():
    bridge = Bridge.__new__(Bridge)
    bridge.cfg = {
        "target_language_incoming": "ru",
        "target_language_outgoing": "en",
    }
    bridge._save_cfg = lambda: False
    bridge._prefetch_session_key = lambda: (_ for _ in ()).throw(
        AssertionError("must not prefetch after a failed save"))
    bridge._maybe_restart = lambda: (_ for _ in ()).throw(
        AssertionError("must not restart after a failed save"))

    result = bridge.swap_languages()

    assert result == {"ok": False, "incoming": "ru", "outgoing": "en"}
    assert bridge.cfg == {
        "target_language_incoming": "ru",
        "target_language_outgoing": "en",
    }
