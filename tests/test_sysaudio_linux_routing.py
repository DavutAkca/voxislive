"""Faz 3 Option-A routing (app/sysaudio/linux/routing.py, ducking.py).

Pins the pactl call sequence + crash-safety snapshot contract using a fake
`pactl` runner, so these run without real PipeWire on the dev machine. Real
hardware behavior (actual routing + audible duck, and the discovery that the
system default sink must NEVER change -- see routing.py's module docstring
"Architecture history") is proved separately against a real RPi5 + USB speaker
(see linux/phase3_flipped_test.py, linux/phase3_duck_demo.sh, 2026-07-19).
"""
import json
import logging
import os
import subprocess
import time

import pytest

from app.sysaudio.linux import routing


class _FakePactl:
    """Records every `pactl` invocation and returns canned output."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.sinks_short = ""      # `pactl list sinks short` response
        self.sink_inputs = ""      # `pactl list sink-inputs` (full) response
        self._next_module_id = 100

    def __call__(self, *args, timeout=5.0):
        args = list(args)  # tuple slices never equal list literals -- normalize once
        self.calls.append(args)
        if args[:2] == ["load-module", "module-null-sink"]:
            mid = str(self._next_module_id); self._next_module_id += 1
            return mid
        if args[:2] == ["load-module", "module-loopback"]:
            mid = str(self._next_module_id); self._next_module_id += 1
            return mid
        if args == ["list", "sinks", "short"]:
            return self.sinks_short
        if args == ["list", "sink-inputs"]:
            return self.sink_inputs
        return ""


@pytest.fixture
def fake_pactl(monkeypatch, tmp_path):
    fp = _FakePactl()
    fp.sinks_short = "64\tRealSink\tPipeWire\tfoo\n99\tVoxisCapture\tPipeWire\tfoo\n"
    monkeypatch.setattr(routing, "_pactl", fp)
    monkeypatch.setattr(routing.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(routing, "_restore_path", lambda: str(tmp_path / "restore.json"))
    monkeypatch.setattr(routing, "SWEEP_INTERVAL", 0.02)  # fast background sweeps in tests
    return fp


def test_setup_requires_pactl(monkeypatch):
    monkeypatch.setattr(routing.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        routing.setup_capture_routing("RealSink")


def test_setup_creates_sink_and_loopback_never_touches_default(fake_pactl):
    handle = routing.setup_capture_routing("RealSink", duck_level=1.0)
    try:
        assert handle.real_sink == "RealSink"
        assert handle.capture_monitor == f"{routing.CAPTURE_SINK_NAME}.monitor"
        calls = fake_pactl.calls
        assert ["load-module", "module-null-sink", f"sink_name={routing.CAPTURE_SINK_NAME}",
               "sink_properties=device.description=Voxis_Capture"] in calls
        assert any(c[:2] == ["load-module", "module-loopback"] for c in calls)
        # The load-bearing invariant: the system default sink is NEVER touched.
        assert not any(c[:1] == ["set-default-sink"] for c in calls)
    finally:
        routing.teardown_capture_routing(handle)


def test_setup_sweeps_preexisting_streams_excluding_own_pid(fake_pactl, monkeypatch):
    # Sink id 64 == RealSink (see fixture). Sink-input #42 (some other app,
    # pid 5555) is on RealSink and must be swept; #43 is already on a
    # different sink and must be left alone; #44 is on RealSink too but is
    # OUR OWN pid (Player's already-open stream) and must NEVER be swept.
    # The background sweep is disabled here -- it would keep "rediscovering"
    # #42 forever since this fake doesn't mutate its canned sink_inputs after
    # a move (unlike real pactl); the background mechanism itself is covered
    # by test_background_sweep_catches_streams_that_start_later below.
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    my_pid = str(os.getpid())
    fake_pactl.sink_inputs = (
        'Sink Input #42\n'
        '\tSink: 64\n'
        '\tapplication.process.id = "5555"\n'
        'Sink Input #43\n'
        '\tSink: 99\n'
        '\tapplication.process.id = "6666"\n'
        'Sink Input #44\n'
        '\tSink: 64\n'
        f'\tapplication.process.id = "{my_pid}"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    try:
        moved = [c[1] for c in fake_pactl.calls if c[0] == "move-sink-input"]
        assert moved == ["42"]
    finally:
        routing.teardown_capture_routing(handle)


def test_background_sweep_catches_streams_that_start_later(fake_pactl):
    handle = routing.setup_capture_routing("RealSink")
    try:
        fake_pactl.calls.clear()
        # A stream that "starts" after setup -- appears on RealSink only now.
        fake_pactl.sink_inputs = (
            'Sink Input #77\n\tSink: 64\n\tapplication.process.id = "9999"\n'
        )
        deadline = time.time() + 2.0
        moved = []
        while time.time() < deadline:
            moved = [c[1] for c in fake_pactl.calls if c[0] == "move-sink-input"]
            if moved:
                break
            time.sleep(0.02)
        # The fake's sink_inputs is static (never reflects "already moved"), so
        # under scheduling delay more than one sweep tick may elapse before this
        # loop notices -- each re-move is idempotent in real pactl, so the
        # invariant is "77 got moved, nothing else did", not "exactly once".
        assert moved and set(moved) == {"77"}
    finally:
        routing.teardown_capture_routing(handle)


def test_teardown_stops_sweep_and_unloads_modules(fake_pactl):
    handle = routing.setup_capture_routing("RealSink")
    thread = handle._sweep_thread
    assert thread is not None and thread.is_alive()
    fake_pactl.calls.clear()
    routing.teardown_capture_routing(handle)
    assert not thread.is_alive()
    unloaded = [c[1] for c in fake_pactl.calls if c[0] == "unload-module"]
    assert handle._sink_module_id in unloaded
    assert handle._loopback_module_id in unloaded
    # Teardown must not touch default-sink either.
    assert not any(c[:1] == ["set-default-sink"] for c in fake_pactl.calls)


def test_snapshot_written_and_cleared(fake_pactl):
    handle = routing.setup_capture_routing("RealSink")
    assert os.path.exists(routing._restore_path())
    routing.teardown_capture_routing(handle)
    assert not os.path.exists(routing._restore_path())


def test_restore_pending_routing_undoes_orphaned_setup(fake_pactl):
    handle = routing.setup_capture_routing("RealSink")
    handle.stop_sweep()  # this run's process is "gone" from here on
    fake_pactl.calls.clear()
    # Simulate the NEXT launch after a crash: a fresh process reads the
    # snapshot left on disk and must unwind it without ever having called
    # setup_capture_routing itself this run.
    routing.restore_pending_routing()
    unloaded = [c[1] for c in fake_pactl.calls if c[0] == "unload-module"]
    assert len(unloaded) == 2
    assert not os.path.exists(routing._restore_path())


def test_restore_pending_routing_noop_on_clean_start(fake_pactl):
    routing.restore_pending_routing()  # no snapshot -- must not raise or call pactl
    assert fake_pactl.calls == []


def test_set_duck_volume_resolves_loopback_index_once(fake_pactl):
    fake_pactl.sink_inputs = (
        'Sink Input #7\n\tmedia.name = "some other stream"\n'
        'Sink Input #9\n\tmedia.name = "loopback-1-2 output"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    try:
        fake_pactl.calls.clear()
        handle.set_duck_volume(0.3)
        assert ["set-sink-input-volume", "9", "30%"] in fake_pactl.calls
        # Resolved once and cached -- a second call must not re-query sink-inputs.
        fake_pactl.calls.clear()
        handle.set_duck_volume(0.5)
        assert not any(c[:2] == ["list", "sink-inputs"] for c in fake_pactl.calls)
        assert ["set-sink-input-volume", "9", "50%"] in fake_pactl.calls
    finally:
        routing.teardown_capture_routing(handle)


def test_ducker_ramps_toward_target_and_restores_on_close():
    from app.sysaudio.linux.ducking import LinuxSessionDucker

    class _RecordingHandle:
        def __init__(self):
            self.levels = []

        def set_duck_volume(self, level):
            self.levels.append(level)

    h = _RecordingHandle()
    d = LinuxSessionDucker(h)
    d.target = 0.3
    deadline = time.time() + 2.0
    while d.current > 0.32 and time.time() < deadline:
        time.sleep(0.02)
    assert d.current <= 0.32
    d.close()
    assert h.levels[-1] == 1.0  # restored to full on close


# --- locale + self-heal + native-fallback (2026-07-19 field fixes) ---------

def test_pactl_and_pw_pin_c_locale(monkeypatch):
    # Both subprocess wrappers must force LC_ALL=C so pactl never emits
    # localized listing headers the parsers can't read (proven on tr_TR).
    captured: dict[str, dict] = {}

    class _R:
        stdout = ""

    def fake_run(cmd, **kwargs):
        captured[cmd[0]] = kwargs
        return _R()

    monkeypatch.setattr(routing.subprocess, "run", fake_run)
    routing._pactl("info")
    routing._pw("pw-dump")
    assert captured["pactl"]["env"]["LC_ALL"] == "C"
    assert captured["pw-dump"]["env"]["LC_ALL"] == "C"


def _raise_on_move_factory(fp):
    """A `_pactl` replacement that raises CalledProcessError on move-sink-input
    (mimicking the ambiguous ENOENT) but delegates everything else to `fp`."""
    def _run(*args, timeout=5.0):
        if args and args[0] == "move-sink-input":
            raise subprocess.CalledProcessError(1, ["pactl", *args])
        return _FakePactl.__call__(fp, *args, timeout=timeout)
    return _run


def test_rebuild_on_vanished_capture_sink(fake_pactl, monkeypatch):
    # VoxisCapture module dies mid-session (pipewire-pulse restart). The sweep
    # must notice the sink is gone and reload both modules before enumerating.
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    handle = routing.setup_capture_routing("RealSink")
    try:
        old_sink_mod, old_loop_mod = handle._sink_module_id, handle._loopback_module_id
        snap_path = routing._restore_path()
        os.remove(snap_path)  # so the rebuild's rewrite is observable
        fake_pactl.sinks_short = "64\tRealSink\tPipeWire\tfoo\n"  # VoxisCapture gone
        fake_pactl.calls.clear()
        handle.sweep_other_apps()
        calls = fake_pactl.calls
        assert any(c[:2] == ["load-module", "module-null-sink"] for c in calls)
        assert any(c[:2] == ["load-module", "module-loopback"] for c in calls)
        assert handle._sink_module_id != old_sink_mod
        assert handle._loopback_module_id != old_loop_mod
        assert os.path.exists(snap_path)  # crash snapshot rewritten
    finally:
        routing.teardown_capture_routing(handle)


def test_duck_level_reapplied_after_rebuild(fake_pactl, monkeypatch):
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = 'Sink Input #9\n\tmedia.name = "loopback-1-2 output"\n'
    handle = routing.setup_capture_routing("RealSink")
    try:
        handle.set_duck_volume(0.3)
        fake_pactl.sinks_short = "64\tRealSink\tPipeWire\tfoo\n"  # sink vanishes
        fake_pactl.calls.clear()
        handle.sweep_other_apps()
        calls = fake_pactl.calls
        reload_at = next(i for i, c in enumerate(calls)
                         if c[:2] == ["load-module", "module-null-sink"])
        vol_at = next(i for i, c in enumerate(calls)
                      if c == ["set-sink-input-volume", "9", "30%"])
        assert vol_at > reload_at  # duck level re-applied AFTER the rebuild
    finally:
        routing.teardown_capture_routing(handle)


def test_move_falls_back_to_pw_metadata(fake_pactl, monkeypatch):
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = (
        'Sink Input #42\n\tSink: 64\n\tapplication.process.id = "5555"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    monkeypatch.setattr(routing, "_pactl", _raise_on_move_factory(fake_pactl))

    pw_calls: list[list[str]] = []
    pw_dump = json.dumps([
        {"id": 300, "type": "PipeWire:Interface:Node",
         "info": {"props": {"media.class": "Stream/Output/Audio",
                            "object.serial": 42}}},
        {"id": 77, "type": "PipeWire:Interface:Node",
         "info": {"props": {"node.name": "VoxisCapture", "object.serial": 99}}},
    ])

    def fake_pw(*args, timeout=5.0):
        pw_calls.append(list(args))
        return pw_dump if args and args[0] == "pw-dump" else ""

    monkeypatch.setattr(routing, "_pw", fake_pw)
    monkeypatch.setattr(routing.shutil, "which", lambda name: f"/usr/bin/{name}")

    handle.sweep_other_apps()
    assert ["pw-metadata", "300", "target.object", "99", "Spa:Id"] in pw_calls


def test_move_fallback_unavailable_stays_silent(fake_pactl, monkeypatch):
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = (
        'Sink Input #42\n\tSink: 64\n\tapplication.process.id = "5555"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    monkeypatch.setattr(routing, "_pactl", _raise_on_move_factory(fake_pactl))

    pw_calls: list[list[str]] = []
    monkeypatch.setattr(routing, "_pw",
                        lambda *a, **k: pw_calls.append(list(a)) or "")
    # pw-dump / pw-metadata absent -- native path must decline without calling _pw.
    monkeypatch.setattr(routing.shutil, "which",
                        lambda name: None if name.startswith("pw-") else f"/usr/bin/{name}")

    handle.sweep_other_apps()  # must not raise
    assert pw_calls == []


def test_module_owned_stream_never_swept(fake_pactl, monkeypatch):
    # The ambient loopback's own playback stream is module-owned (Owner
    # Module set). Sweeping it onto VoxisCapture would feed the monitor back
    # into itself and mute the speakers -- field bug 2026-07-19.
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = (
        'Sink Input #50\n'
        '\tOwner Module: 536870914\n'
        '\tSink: 64\n'
        '\tmedia.name = "loopback-1-2 output"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    try:
        moved = [c[1] for c in fake_pactl.calls if c[0] == "move-sink-input"]
        assert moved == []
    finally:
        routing.teardown_capture_routing(handle)


def _pw_dump_with_client(serial, sec_pid):
    """pw-dump JSON: one stream node (object.serial=serial) owned by a client
    whose kernel-verified pipewire.sec.pid is sec_pid."""
    return json.dumps([
        {"id": 300, "type": "PipeWire:Interface:Node",
         "info": {"props": {"media.class": "Stream/Output/Audio",
                            "object.serial": serial, "client.id": 20}}},
        {"id": 20, "type": "PipeWire:Interface:Client",
         "info": {"props": {"pipewire.sec.pid": sec_pid}}},
    ])


def test_own_pipewire_alsa_stream_without_pid_not_swept(fake_pactl, monkeypatch):
    # pipewire-alsa clients set NO application.process.id, so PID exclusion is
    # blind to our own Player stream (output device "pipewire") -- ownership
    # must be resolved via pw-dump's pipewire.sec.pid. Field bug 2026-07-19:
    # the TTS stream got swept onto VoxisCapture (self-translation feedback).
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = 'Sink Input #60\n\tSink: 64\n'
    monkeypatch.setattr(routing.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(routing, "_pw",
                        lambda *a, **k: _pw_dump_with_client(60, os.getpid()))
    handle = routing.setup_capture_routing("RealSink")
    try:
        moved = [c[1] for c in fake_pactl.calls if c[0] == "move-sink-input"]
        assert moved == []
    finally:
        routing.teardown_capture_routing(handle)


def test_foreign_stream_without_pid_swept_via_pw_dump(fake_pactl, monkeypatch):
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = 'Sink Input #61\n\tSink: 64\n'
    monkeypatch.setattr(routing.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(routing, "_pw",
                        lambda *a, **k: _pw_dump_with_client(61, 12345))
    handle = routing.setup_capture_routing("RealSink")
    try:
        moved = [c[1] for c in fake_pactl.calls if c[0] == "move-sink-input"]
        assert moved == ["61"]
    finally:
        routing.teardown_capture_routing(handle)


def test_unknown_ownership_skipped_and_logged_once(fake_pactl, monkeypatch, caplog):
    # No pw-dump on the box: an unverifiable no-pid stream must be left alone
    # (missing one app beats capturing our own TTS) and warned about ONCE.
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = 'Sink Input #62\n\tSink: 64\n'
    monkeypatch.setattr(routing.shutil, "which",
                        lambda name: None if name.startswith("pw-") else f"/usr/bin/{name}")
    handle = routing.setup_capture_routing("RealSink")
    try:
        with caplog.at_level(logging.WARNING, logger="voxis"):
            for _ in range(4):
                handle.sweep_other_apps()
        moved = [c[1] for c in fake_pactl.calls if c[0] == "move-sink-input"]
        assert moved == []
        warnings = [r for r in caplog.records
                    if "cannot verify ownership of sink-input 62" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        routing.teardown_capture_routing(handle)


def test_backsweep_moves_own_and_module_streams_off_capture(fake_pactl, monkeypatch):
    # pipewire-pulse's module-stream-restore re-targets streams by NAME onto
    # the sink a past mis-move left them on -- so our own TTS stream and the
    # loopback can materialize ON VoxisCapture at creation time. The sweep
    # must move those back to the real sink; a legit captured app stays put.
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    my_pid = str(os.getpid())
    fake_pactl.sink_inputs = (
        'Sink Input #70\n'
        '\tOwner Module: 536870914\n'
        '\tSink: 99\n'
        '\tmedia.name = "loopback-1-2 output"\n'
        'Sink Input #71\n'
        '\tSink: 99\n'
        f'\tapplication.process.id = "{my_pid}"\n'
        'Sink Input #72\n'
        '\tSink: 99\n'
        '\tapplication.process.id = "4444"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    try:
        back = [c for c in fake_pactl.calls
                if c[0] == "move-sink-input" and c[2] == "RealSink"]
        assert sorted(c[1] for c in back) == ["70", "71"]
        onto_capture = [c for c in fake_pactl.calls
                        if c[0] == "move-sink-input" and c[2] == routing.CAPTURE_SINK_NAME]
        assert onto_capture == []  # nothing on the real sink to sweep here
    finally:
        routing.teardown_capture_routing(handle)


def test_move_failure_logs_once_after_three_sweeps(fake_pactl, monkeypatch, caplog):
    monkeypatch.setattr(routing.RoutingHandle, "start_sweep", lambda self: None)
    fake_pactl.sink_inputs = (
        'Sink Input #42\n\tSink: 64\n\tapplication.process.id = "5555"\n'
    )
    handle = routing.setup_capture_routing("RealSink")
    monkeypatch.setattr(routing, "_pactl", _raise_on_move_factory(fake_pactl))
    monkeypatch.setattr(routing, "_pw", lambda *a, **k: "")  # native path also fails
    monkeypatch.setattr(routing.shutil, "which",
                        lambda name: None if name.startswith("pw-") else f"/usr/bin/{name}")

    with caplog.at_level(logging.WARNING, logger="voxis"):
        for _ in range(5):
            handle.sweep_other_apps()
    warnings = [r for r in caplog.records
                if "could not move sink-input 42" in r.getMessage()]
    assert len(warnings) == 1  # logged once at the 3rd sweep, never again
