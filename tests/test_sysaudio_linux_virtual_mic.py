"""Faz 5 virtual mic (app/sysaudio/linux/virtual_mic.py).

Pins the pactl call sequence + crash-safety snapshot contract using a fake
`pactl` runner, so these run without real PipeWire on the dev machine. Real
hardware behavior (the one-time move staying stable with no default-source
change, confirmed against a real RPi5) is proved separately -- see
linux/phase5_source_autoconnect_test.py, linux/phase5_stable_move_test.py,
linux/phase5_virtual_mic_real_test.py, 2026-07-19.
"""
import json
import os

import pytest

from app.sysaudio.linux import virtual_mic


class _FakePactl:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.sink_inputs = ""  # `pactl list sink-inputs` (full) response
        self.pw_dump_objs: list[dict] = []  # `pw-dump` (JSON) response
        self._next_module_id = 100

    def __call__(self, *args, timeout=5.0):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["load-module", "module-null-sink"]:
            mid = str(self._next_module_id); self._next_module_id += 1
            return mid
        if args == ["list", "sink-inputs"]:
            return self.sink_inputs
        return ""

    def pw(self, *args, timeout=5.0):
        self.calls.append(list(args))
        return json.dumps(self.pw_dump_objs)


@pytest.fixture
def fake_pactl(monkeypatch, tmp_path):
    fp = _FakePactl()
    monkeypatch.setattr(virtual_mic, "_pactl", fp)
    monkeypatch.setattr(virtual_mic, "_pw", fp.pw)
    monkeypatch.setattr(virtual_mic.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(virtual_mic, "_restore_path", lambda: str(tmp_path / "restore.json"))
    return fp


def _sink_input_block(idx: str, pid: str) -> str:
    return f'Sink Input #{idx}\n\tapplication.process.id = "{pid}"\n'


def _sink_input_block_no_pid(idx: str) -> str:
    """A PipeWire-ALSA sink-input (e.g. our own sounddevice Player) -- carries
    no application.process.id header at all, confirmed empirically 2026-07-19."""
    return f'Sink Input #{idx}\n\tnode.name = "alsa_playback.python3"\n'


def _pw_node(serial: int, client_id: int) -> dict:
    return {"type": "PipeWire:Interface:Node",
           "info": {"props": {"object.serial": serial, "client.id": client_id}}}


def _pw_client(client_id: int, pid: str) -> dict:
    return {"id": client_id, "type": "PipeWire:Interface:Client",
           "info": {"props": {"pipewire.sec.pid": pid}}}


def test_create_virtual_mic_requires_pactl(monkeypatch):
    monkeypatch.setattr(virtual_mic.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        virtual_mic.create_virtual_mic()


def test_create_virtual_mic_loads_module_and_snapshots(fake_pactl):
    handle = virtual_mic.create_virtual_mic()
    assert handle.sink_name == virtual_mic.MIC_SINK_NAME
    assert ["load-module", "module-null-sink",
           f"sink_name={virtual_mic.MIC_SINK_NAME}",
           "sink_properties=device.description=Voxis_Virtual_Mic"] in fake_pactl.calls
    assert os.path.exists(virtual_mic._restore_path())
    virtual_mic.teardown_virtual_mic(handle)
    assert not os.path.exists(virtual_mic._restore_path())


def test_pin_newest_own_stream_finds_the_diff(fake_pactl):
    my_pid = str(os.getpid())
    # Baseline: one pre-existing stream of ours (e.g. the incoming Player).
    fake_pactl.sink_inputs = _sink_input_block("10", my_pid)
    before = virtual_mic.snapshot_own_streams()
    assert before == {"10"}

    # After constructing the outgoing Player: a second one appears.
    fake_pactl.sink_inputs = (_sink_input_block("10", my_pid)
                              + _sink_input_block("11", my_pid)
                              + _sink_input_block("12", "9999"))  # someone else's
    moved = virtual_mic.pin_newest_own_stream(before)
    assert moved == "11"
    assert ["move-sink-input", "11", virtual_mic.MIC_SINK_NAME] in fake_pactl.calls


def test_pin_newest_own_stream_finds_pipewire_alsa_stream_with_no_pid(fake_pactl):
    """The real-world case (confirmed on a live VM, 2026-07-19): a
    sounddevice/PortAudio Player on PipeWire sets NO application.process.id at
    all, so PID matching alone sees nothing -- ownership must be resolved via
    pw-dump's pipewire.sec.pid instead (mirrors routing.py's fix)."""
    my_pid = str(os.getpid())
    # Baseline: one pre-existing stream, belonging to some OTHER app (real PID).
    fake_pactl.sink_inputs = _sink_input_block("10", "9999")
    before = virtual_mic.snapshot_own_streams()
    assert before == set()

    # After constructing the outgoing Player: a PID-less PipeWire-ALSA stream
    # appears. Only pw-dump can attribute it to us.
    fake_pactl.sink_inputs = (_sink_input_block("10", "9999")
                              + _sink_input_block_no_pid("11"))
    fake_pactl.pw_dump_objs = [_pw_node(11, client_id=5), _pw_client(5, my_pid)]

    moved = virtual_mic.pin_newest_own_stream(before)
    assert moved == "11"
    assert ["move-sink-input", "11", virtual_mic.MIC_SINK_NAME] in fake_pactl.calls


def test_pin_newest_own_stream_ignores_other_apps_pipewire_alsa_stream(fake_pactl):
    """A PID-less stream owned by a DIFFERENT client pid must never be picked
    up as ours -- pw-dump attribution must match pid exactly, not just fill in
    any PID-less gap."""
    my_pid = str(os.getpid())
    fake_pactl.sink_inputs = ""
    before = virtual_mic.snapshot_own_streams()
    assert before == set()

    fake_pactl.sink_inputs = _sink_input_block_no_pid("11")
    fake_pactl.pw_dump_objs = [_pw_node(11, client_id=5), _pw_client(5, "4242")]
    assert my_pid != "4242"

    moved = virtual_mic.pin_newest_own_stream(before)
    assert moved is None
    assert not any(c[0] == "move-sink-input" for c in fake_pactl.calls)


def test_pin_newest_own_stream_returns_none_when_nothing_new(fake_pactl):
    my_pid = str(os.getpid())
    fake_pactl.sink_inputs = _sink_input_block("10", my_pid)
    before = virtual_mic.snapshot_own_streams()
    # No new stream appeared (same state) -- e.g. Player failed to open.
    moved = virtual_mic.pin_newest_own_stream(before)
    assert moved is None
    assert not any(c[0] == "move-sink-input" for c in fake_pactl.calls)


def test_teardown_noop_for_none(fake_pactl):
    virtual_mic.teardown_virtual_mic(None)  # must not raise
    assert fake_pactl.calls == []


def test_restore_pending_virtual_mic_undoes_orphaned_setup(fake_pactl):
    handle = virtual_mic.create_virtual_mic()
    fake_pactl.calls.clear()
    # Simulate the NEXT launch after a crash.
    virtual_mic.restore_pending_virtual_mic()
    unloaded = [c[1] for c in fake_pactl.calls if c[0] == "unload-module"]
    assert unloaded == [handle._sink_module_id]
    assert not os.path.exists(virtual_mic._restore_path())


def test_restore_pending_virtual_mic_noop_on_clean_start(fake_pactl):
    virtual_mic.restore_pending_virtual_mic()  # no snapshot -- must not raise
    assert fake_pactl.calls == []
