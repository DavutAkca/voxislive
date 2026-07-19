"""Faz 0-3 platform-dispatch seam (app/sysaudio).

Pins the contract the Linux port depends on:
- `is_supported()`: True on Windows always; on Linux only when the PipeWire/
  Pulse tooling (pactl + parec) this backend shells out to is actually present.
- `supports_endpoints()`: True on Windows only -- default-endpoint switching
  (vbcable/meeting mode) has no Linux implementation yet (Faz 5), independent
  of whether the driverless-equivalent path (`is_supported()`) works.
- On a genuinely unsupported platform (or Linux missing pactl/parec), the
  audio accessors raise a clean AudioBackendUnavailable so callers decline a
  session with a friendly status; a supported Linux gets real dispatch to
  `sysaudio.linux.*` for the ducker/capture accessors, while
  `make_loopback_capture`/`endpoints` stay Windows-only regardless (no Linux
  equivalents exist). The startup restore helpers are safe no-ops wherever
  their platform-specific mechanism doesn't apply.
"""
import sys

import pytest

from app import sysaudio


def test_is_supported_tracks_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sysaudio.is_supported() is True
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: None)
    assert sysaudio.is_supported() is False
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert sysaudio.is_supported() is True


def test_supports_endpoints_is_windows_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sysaudio.supports_endpoints() is True
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert sysaudio.supports_endpoints() is False  # even though is_supported() is now True


def test_audio_accessors_raise_when_linux_unsupported(monkeypatch):
    # No pactl/parec on PATH -- a Linux box without PipeWire/Pulse at all.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: None)
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.make_ducker()
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.make_process_loopback(lambda x: None)
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.make_loopback_capture(lambda x: None)
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.make_capture_routing()
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.endpoints()


def test_audio_accessors_dispatch_to_linux_when_supported(monkeypatch):
    from app.sysaudio.linux.capture import PipeWireCapture
    from app.sysaudio.linux.ducking import LinuxSessionDucker

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _FakeHandle:
        capture_monitor = "VoxisCapture.monitor"

        def set_duck_volume(self, level):
            pass

    d = sysaudio.make_ducker(routing_handle=_FakeHandle())
    try:
        assert isinstance(d, LinuxSessionDucker)
    finally:
        d.close()

    cap = sysaudio.make_process_loopback(lambda x: None, routing_handle=_FakeHandle())
    assert isinstance(cap, PipeWireCapture)

    # No Linux equivalent for either of these -- still raise even though
    # is_supported() is True.
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.make_loopback_capture(lambda x: None)
    with pytest.raises(sysaudio.AudioBackendUnavailable):
        sysaudio.endpoints()


def test_make_capture_routing_noop_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sysaudio.make_capture_routing() is None
    sysaudio.teardown_capture_routing(None)  # must not raise


def test_make_capture_routing_dispatches_on_linux(monkeypatch):
    from app.sysaudio.linux import routing

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(routing, "setup_capture_routing",
                        lambda real_sink, **k: f"handle-for-{real_sink}")
    torn_down = []
    monkeypatch.setattr(routing, "teardown_capture_routing", torn_down.append)

    handle = sysaudio.make_capture_routing("MySink")
    assert handle == "handle-for-MySink"
    sysaudio.teardown_capture_routing(handle)
    assert torn_down == ["handle-for-MySink"]


def test_make_virtual_mic_noop_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sysaudio.make_virtual_mic() is None
    assert sysaudio.snapshot_own_audio_streams() is None
    assert sysaudio.pin_newest_own_stream_to_mic(None, None) is None
    sysaudio.teardown_virtual_mic(None)  # must not raise


def test_make_virtual_mic_dispatches_on_linux(monkeypatch):
    from app.sysaudio.linux import virtual_mic

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(virtual_mic, "create_virtual_mic", lambda **k: "a-handle")
    monkeypatch.setattr(virtual_mic, "snapshot_own_streams", lambda: {"1", "2"})
    pin_calls = []

    def _fake_pin(before, sink_name):
        pin_calls.append((before, sink_name))
        return "11"
    monkeypatch.setattr(virtual_mic, "pin_newest_own_stream", _fake_pin)
    torn_down = []
    monkeypatch.setattr(virtual_mic, "teardown_virtual_mic", torn_down.append)

    assert sysaudio.make_virtual_mic() == "a-handle"
    assert sysaudio.snapshot_own_audio_streams() == {"1", "2"}

    class _FakeHandle:
        sink_name = "VoxisMic"

    handle = _FakeHandle()
    result = sysaudio.pin_newest_own_stream_to_mic({"1", "2"}, handle)
    assert result == "11"
    assert pin_calls == [({"1", "2"}, "VoxisMic")]

    sysaudio.teardown_virtual_mic(handle)
    assert torn_down == [handle]


def test_restore_helpers_noop_off_windows_and_linux(monkeypatch):
    # main.py calls these on EVERY launch before the platform is known — off
    # Windows/Linux they must return silently without importing a
    # platform-specific module.
    monkeypatch.setattr(sys, "platform", "darwin")
    sysaudio.restore_pending_ducking()
    sysaudio.restore_endpoints({"output": "x"})


def test_restore_pending_ducking_dispatches_by_platform(monkeypatch):
    from app.sysaudio.linux import routing

    called = []
    monkeypatch.setattr(routing, "restore_pending_routing", lambda: called.append("linux"))
    monkeypatch.setattr(sys, "platform", "linux")
    sysaudio.restore_pending_ducking()
    assert called == ["linux"]


def test_restore_endpoints_noop_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    sysaudio.restore_endpoints({"output": "x"})  # must not import win_audio or raise


def test_unavailable_carries_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd")
    with pytest.raises(sysaudio.AudioBackendUnavailable) as ei:
        sysaudio.make_ducker()
    assert ei.value.platform == "freebsd"


def test_supported_on_windows_host():
    # This dev/CI host is Windows; the guard must be open here (we don't build
    # the heavy COM objects — the existing pipeline/audio tests cover those).
    if sys.platform == "win32":
        assert sysaudio.is_supported() is True


def test_mode_controller_declines_off_windows(monkeypatch):
    # The FULL start()/stop() path must decline cleanly when no audio backend
    # is available. Regression for _restore_defaults() crashing stop() by
    # calling sysaudio.endpoints() unconditionally — start() calls stop()
    # first, so it blew up before the decline guard was ever reached (caught
    # on a real Raspberry Pi 5, 2026-07-18).
    import app.pipeline as pipeline
    from app import i18n
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sysaudio.shutil, "which", lambda name: None)  # no pactl/parec here
    monkeypatch.setattr(i18n, "_current", "en")
    statuses = []
    mc = pipeline.ModeController(
        cfg={}, api_key=None, on_text=lambda *a, **k: None,
        on_status=lambda *a, **k: statuses.append(a[0] if a else None))
    mc.resolve = lambda target=None: ("gemini", "k", "m")  # passes the key check
    mc.start("video")
    assert mc.mode is None                       # no session built
    assert mc._pipelines == []
    assert i18n.t("st_no_audio_backend") in statuses
    mc.stop()                                    # must also be crash-free
