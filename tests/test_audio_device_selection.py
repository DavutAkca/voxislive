"""The device selectors must tell the truth about the system-default sentinel."""

from app import webui
from app.webui import Bridge, DEFAULT_DEVICE


def _bridge():
    bridge = Bridge.__new__(Bridge)
    bridge.cfg = {
        "devices": {
            "headphones_output": DEFAULT_DEVICE,
            "microphone": DEFAULT_DEVICE,
        }
    }
    bridge._save_cfg = lambda: True
    bridge._maybe_restart = lambda: None
    bridge._transcript_dir = lambda: "transcripts"
    return bridge


def test_empty_output_is_rendered_as_system_default(monkeypatch):
    monkeypatch.setattr(webui, "t", lambda key, **_: "(Default)")
    bridge = _bridge()

    view = bridge._cfg_view(["Virtual Cable", "USB Headphones"], ["USB Mic"])

    assert view["devices"]["headphones_output_label"] == "(Default)"


def test_output_default_label_round_trips_to_empty_sentinel(monkeypatch):
    monkeypatch.setattr(webui, "t", lambda key, **_: "(Default)")
    bridge = _bridge()
    bridge.cfg["devices"]["headphones_output"] = "Virtual Cable"

    assert bridge.set_device("output", "(Default)") is True

    assert bridge.cfg["devices"]["headphones_output"] == DEFAULT_DEVICE


def test_named_output_is_still_persisted(monkeypatch):
    monkeypatch.setattr(webui, "t", lambda key, **_: "(Default)")
    bridge = _bridge()

    assert bridge.set_device("output", "USB Headphones") is True

    assert bridge.cfg["devices"]["headphones_output"] == "USB Headphones"
