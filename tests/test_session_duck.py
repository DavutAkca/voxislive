"""Crash-safe duck restore: snapshot file lifecycle + restore_pending matching."""
import json
import os

import pytest

import app.session_duck as sd


class _FakeVol:
    def __init__(self, level):
        self.level = level

    def GetMasterVolume(self):
        return self.level

    def SetMasterVolume(self, v, ctx):
        self.level = v


class _FakeProc:
    def __init__(self, pid, name):
        self.pid = pid
        self._name = name

    def name(self):
        return self._name


class _FakeSession:
    def __init__(self, pid, name, vol):
        self.Process = _FakeProc(pid, name)
        self.SimpleAudioVolume = vol


@pytest.fixture
def snap_path(tmp_path, monkeypatch):
    path = str(tmp_path / "duck_restore.json")
    monkeypatch.setattr(sd, "_RESTORE_PATH", path)
    return path


def _install_sessions(monkeypatch, sessions):
    monkeypatch.setattr(sd.AudioUtilities, "GetAllSessions",
                        staticmethod(lambda: sessions))


def test_snapshot_write_and_clear(snap_path):
    sd._write_snapshot({"123": {"exe": "chrome.exe", "level": 0.8}})
    with open(snap_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["pids"]["123"]["level"] == 0.8
    sd._clear_snapshot()
    assert not os.path.exists(snap_path)


def test_restore_by_pid_raises_ducked_volume(snap_path, monkeypatch):
    vol = _FakeVol(0.24)  # left ducked by a crash (base was 0.8)
    _install_sessions(monkeypatch, [_FakeSession(123, "chrome.exe", vol)])
    sd._write_snapshot({"123": {"exe": "chrome.exe", "level": 0.8}})
    sd.restore_pending()
    assert vol.level == pytest.approx(0.8)
    assert not os.path.exists(snap_path)  # consumed


def test_restore_by_exe_when_pid_changed(snap_path, monkeypatch):
    vol = _FakeVol(0.3)
    _install_sessions(monkeypatch, [_FakeSession(999, "game.exe", vol)])
    sd._write_snapshot({"123": {"exe": "GAME.EXE", "level": 1.0}})
    sd.restore_pending()
    assert vol.level == pytest.approx(1.0)


def test_restore_never_lowers_a_user_fixed_volume(snap_path, monkeypatch):
    vol = _FakeVol(1.0)  # user already put it back up
    _install_sessions(monkeypatch, [_FakeSession(123, "chrome.exe", vol)])
    sd._write_snapshot({"123": {"exe": "chrome.exe", "level": 0.5}})
    sd.restore_pending()
    assert vol.level == pytest.approx(1.0)  # untouched (raise-only rule)


def test_restore_with_no_snapshot_is_noop(snap_path, monkeypatch):
    _install_sessions(monkeypatch, [])
    sd.restore_pending()  # must not raise
    assert not os.path.exists(snap_path)


def test_corrupt_snapshot_is_discarded(snap_path, monkeypatch):
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    _install_sessions(monkeypatch, [])
    sd.restore_pending()
    assert not os.path.exists(snap_path)
