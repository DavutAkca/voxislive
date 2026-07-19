"""Faz 3 PipeWireCapture (app/sysaudio/linux/capture.py).

Pins the on_chunk/failed/dropped contract shared with Windows
ProcessExcludeLoopback, using a fake subprocess so these run without a real
PipeWire/parec on the dev machine. Real hardware capture is proved separately
against an actual RPi5 (see linux/phase3_capture_poc.sh /
linux/phase3_subprocess_capture_test.py, 2026-07-19).
"""
import threading
import time

import numpy as np
import pytest

from app.sysaudio.linux import capture as pw_capture


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._closed = threading.Event()

    def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        self._closed.wait(2.0)
        return b""


class _FakeProc:
    def __init__(self, chunks):
        self.stdout = _FakeStdout(chunks)

    def terminate(self):
        self.stdout._closed.set()

    def wait(self, timeout=None):
        pass


def test_default_monitor_source_no_pactl(monkeypatch):
    monkeypatch.setattr(pw_capture.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        pw_capture.default_monitor_source()


def test_missing_binary_sets_failed(monkeypatch):
    # No monkeypatching -- runs against whatever is really on this machine.
    # On Windows: no pactl/parec at all, so Popen raises FileNotFoundError.
    # On real Linux: pactl exists but "bogus.monitor" is not a real source, so
    # _source_exists() catches it before parec is ever spawned (parec itself
    # would NOT fail on a bad name -- see _source_exists' docstring). Either
    # way .failed must surface instead of the read thread dying silently.
    received = []
    cap = pw_capture.PipeWireCapture(received.append, "bogus.monitor")
    cap.start()
    for _ in range(50):
        if cap.failed:
            break
        time.sleep(0.05)
    assert cap.failed
    cap.stop()  # must not raise/hang even though nothing ever started


def test_delivers_chunks_and_clean_stop(monkeypatch):
    # 800 Hz-ish synthetic PCM16 mono chunks at 16 kHz, split into ~20 ms blocks
    # matching the real block size the read loop pulls.
    rate = 16000
    block_samples = rate // 50
    t = np.arange(0, 1.0, 1 / rate)
    tone = (0.3 * np.sin(2 * np.pi * 800 * t) * 32767).astype(np.int16)
    raw = tone.tobytes()
    block_bytes = block_samples * 2
    chunks = [raw[i:i + block_bytes] for i in range(0, len(raw), block_bytes)]

    monkeypatch.setattr(pw_capture, "_source_exists", lambda name: True)
    monkeypatch.setattr(pw_capture.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(chunks))

    received = []
    cap = pw_capture.PipeWireCapture(received.append, "FakeSpeaker.monitor", rate=rate)
    cap.start()
    deadline = time.time() + 2.0
    while sum(len(x) for x in received) < len(tone) and time.time() < deadline:
        time.sleep(0.02)
    cap.stop()

    assert not cap.failed
    assert cap.dropped == 0
    got = np.concatenate(received) if received else np.zeros(0, dtype=np.float32)
    assert len(got) >= block_samples  # at least got real audio through
    assert np.max(np.abs(got)) > 0.1  # not silence


def test_dropped_counts_on_overflow(monkeypatch):
    # A consumer that never drains lets the bounded queue fill past _QUEUE_MAX;
    # deque(maxlen=...) evicts silently, so `dropped` is the only signal.
    rate = 16000
    block_bytes = (rate // 50) * 2
    n_blocks = pw_capture.PipeWireCapture._QUEUE_MAX + 20
    chunks = [b"\x00\x01" * (block_bytes // 2) for _ in range(n_blocks)]
    monkeypatch.setattr(pw_capture, "_source_exists", lambda name: True)
    monkeypatch.setattr(pw_capture.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(chunks))

    gate = threading.Event()
    cap = pw_capture.PipeWireCapture(lambda x: gate.wait(), "FakeSpeaker.monitor", rate=rate)
    cap.start()
    time.sleep(0.5)  # let the read loop race far ahead of the blocked consumer
    gate.set()
    cap.stop()
    assert cap.dropped > 0
