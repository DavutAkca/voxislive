"""The idle sound check diagnoses system audio and microphone independently."""

from pathlib import Path
import types

import numpy as np
import pytest

from app import webui
from app.webui import Bridge


class _Probe:
    def __init__(self, callback):
        self.callback = callback
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _Timer:
    def __init__(self, _seconds, callback):
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def _bridge():
    bridge = Bridge.__new__(Bridge)
    bridge.cfg = {"devices": {"microphone": ""}}
    bridge.controller = types.SimpleNamespace(mode=None)
    bridge._sc = None
    bridge._sc_level = 0.0
    bridge._sc_mic = None
    bridge._sc_mic_level = 0.0
    bridge._sc_timer = None
    bridge._sc_routing = None
    return bridge


def _install_fakes(monkeypatch):
    made = {}
    monkeypatch.setattr(webui.threading, "Timer", _Timer)
    monkeypatch.setattr(webui.sysaudio, "make_capture_routing", lambda: "routing")
    monkeypatch.setattr(webui.sysaudio, "teardown_capture_routing",
                        lambda routing: made.setdefault("torn_down", routing))

    def make_system(callback, **_kwargs):
        made["system"] = _Probe(callback)
        return made["system"]

    def make_mic(_device, callback):
        made["mic"] = _Probe(callback)
        return made["mic"]

    monkeypatch.setattr(webui.sysaudio, "make_process_loopback", make_system)
    monkeypatch.setattr(webui, "find_device", lambda _name, _kind: None)
    monkeypatch.setattr(webui, "Capture", make_mic)
    return made


def test_soundcheck_starts_both_independent_probes(monkeypatch):
    bridge = _bridge()
    made = _install_fakes(monkeypatch)

    result = bridge.soundcheck_start()

    assert result == {"ok": True, "system_ok": True, "mic_ok": True}
    assert made["system"].started and made["mic"].started
    made["system"].callback(np.array([-0.2, 0.4], dtype=np.float32))
    made["mic"].callback(np.array([-0.6, 0.1], dtype=np.float32))
    assert bridge._sc_level == pytest.approx(0.4)
    assert bridge._sc_mic_level == pytest.approx(0.6)


def test_system_failure_does_not_hide_a_working_microphone(monkeypatch):
    bridge = _bridge()
    made = _install_fakes(monkeypatch)
    monkeypatch.setattr(
        webui.sysaudio, "make_process_loopback",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no loopback")),
    )

    result = bridge.soundcheck_start()

    assert result == {"ok": True, "system_ok": False, "mic_ok": True}
    assert made["mic"].started
    assert made["torn_down"] == "routing"


def test_system_start_failure_releases_partial_probe(monkeypatch):
    bridge = _bridge()
    made = _install_fakes(monkeypatch)

    class _StartFailureProbe(_Probe):
        def start(self):
            raise RuntimeError("could not start")

    def make_system(callback, **_kwargs):
        made["system"] = _StartFailureProbe(callback)
        return made["system"]

    monkeypatch.setattr(webui.sysaudio, "make_process_loopback", make_system)

    result = bridge.soundcheck_start()

    assert result == {"ok": True, "system_ok": False, "mic_ok": True}
    assert made["system"].stopped
    assert made["torn_down"] == "routing"


def test_microphone_failure_does_not_hide_working_system_audio(monkeypatch):
    bridge = _bridge()
    made = _install_fakes(monkeypatch)
    monkeypatch.setattr(
        webui, "Capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no mic")),
    )

    result = bridge.soundcheck_start()

    assert result == {"ok": True, "system_ok": True, "mic_ok": False}
    assert made["system"].started


def test_soundcheck_stop_releases_both_devices(monkeypatch):
    bridge = _bridge()
    made = _install_fakes(monkeypatch)
    bridge.soundcheck_start()

    bridge.soundcheck_stop()

    assert made["system"].stopped and made["mic"].stopped
    assert made["torn_down"] == "routing"
    assert bridge._sc is None and bridge._sc_mic is None
    assert bridge._sc_level == 0.0 and bridge._sc_mic_level == 0.0


def test_output_tone_uses_selected_device(monkeypatch):
    bridge = _bridge()
    bridge.cfg["devices"]["headphones_output"] = "USB Headphones"
    calls = []
    monkeypatch.setattr(
        webui, "find_device",
        lambda name, kind: calls.append(("resolve", name, kind)) or 7,
    )
    monkeypatch.setattr(
        webui, "play_test_tone", lambda device: calls.append(("play", device)))

    result = bridge.soundcheck_play_tone()

    assert result == {"ok": True}
    assert calls == [
        ("resolve", "USB Headphones", "output"),
        ("play", 7),
    ]


def test_soundcheck_ui_separates_system_output_and_microphone():
    html = (Path(webui.WEB_DIR) / "index.html").read_text(encoding="utf-8")

    system_pos = html.index('id="sc-fill"')
    output_pos = html.index('id="sc-output-fill"')
    mic_pos = html.index('id="sc-mic-fill"')

    assert system_pos < output_pos < mic_pos
    assert 'data-i18n="sound_check_system"' in html
    assert 'data-i18n="sound_check_tone"' in html
    assert 'id="sc-output-status"' in html
    assert "sound_check_sent" in html
