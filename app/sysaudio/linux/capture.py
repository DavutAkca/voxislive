"""PipeWire/Pulse-compat system-audio capture: reads a named monitor source as
16 kHz mono float32, matching the same on_chunk contract as Windows
`process_loopback.ProcessExcludeLoopback`.

Self-exclude architecture (proved via linux/phase3_capture_poc.sh, 2026-07-19):
capture the monitor of the system's REAL output sink as-is (everything a
normal app plays there is fair game to translate); Voxis's own TTS is routed
to a SEPARATE, dedicated sink that this class never captures from -- so
exclusion is structural (graph separation), not PID/energy-based. The
dedicated-output-sink creation + forwarding to real hardware is a companion
piece (not yet built) that plugs in via `pipeline.py`'s Player construction;
this module only covers the capture half.

Implementation note: PortAudio's Pulse host API does not enumerate individual
monitor sources as selectable devices -- it only exposes fixed "pulse"/
"default" pseudo-devices, and targeting one specific monitor requires a
process-wide `PULSE_SOURCE` env var plus a full PortAudio reinit
(`sd._terminate()/_initialize()`, proved in
linux/phase3_dynamic_source_switch_test.py). Since `pipeline.py` constructs
`Player` (a live sounddevice OutputStream) BEFORE calling
`sysaudio.make_process_loopback`, that reinit would risk tearing down the
already-open playback stream. A `parec` subprocess sidesteps this entirely:
capture becomes a fully independent OS process with no shared PortAudio
state, proved side-by-side with a real `Player` stream in
linux/phase3_subprocess_capture_test.py with zero interference.
"""
import collections
import os
import shutil
import subprocess
import threading

import numpy as np

RATE = 16000


def _source_exists(name: str) -> bool:
    """Checks `pactl list sources short` for an exact name match.

    Required because `parec -d <nonexistent-name>` does NOT fail -- it silently
    falls back to some other source and streams pure silence forever, with no
    error and no stderr output (confirmed on a real RPi5, 2026-07-19: a typo'd
    or torn-down monitor name looks identical to "no one is talking" instead of
    a capture failure). Validating the name up front turns that into an
    immediate, visible `.failed` instead of silent dead air."""
    try:
        # LC_ALL=C for a uniform locale contract with routing.py (this
        # tab-separated `short` output is not itself localized).
        out = subprocess.run(["pactl", "list", "sources", "short"],
                            capture_output=True, text=True, timeout=5, check=True,
                            env={**os.environ, "LC_ALL": "C"})
    except Exception:
        return True  # can't verify -- don't block capture on a pactl hiccup
    names = {line.split("\t")[1] for line in out.stdout.splitlines() if "\t" in line}
    return name in names


def default_monitor_source(sink_name: str | None = None) -> str:
    """Resolves the monitor source name for `sink_name` (or the system default
    sink when omitted). Raises RuntimeError if pactl is unavailable or no
    default sink is configured."""
    if shutil.which("pactl") is None:
        raise RuntimeError("pactl not found -- is PipeWire/PulseAudio installed?")
    if sink_name is None:
        out = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                             text=True, timeout=5, check=True,
                             env={**os.environ, "LC_ALL": "C"})
        sink_name = out.stdout.strip()
    if not sink_name:
        raise RuntimeError("no default sink configured")
    return f"{sink_name}.monitor"


class PipeWireCapture:
    """Yields a named PipeWire/Pulse monitor source as 16 kHz mono float32.

    API mirrors `ProcessExcludeLoopback`: `.rate`, `.dropped`, `.failed`,
    `.start()`, `.stop()`, and an `on_chunk(np.float32)` callback. Capture and
    consumer processing run on separate threads (same split as
    ProcessExcludeLoopback/audio_io.Capture) so a slow consumer cannot block
    the subprocess read loop; the queue is bounded drop-oldest to cap latency.
    """

    rate = RATE
    _QUEUE_MAX = 64

    def __init__(self, on_chunk, monitor_source: str, rate: int = RATE):
        self.rate = int(rate)
        self._monitor = monitor_source
        self._on_chunk = on_chunk
        self._run = False
        self._err: Exception | None = None
        self.dropped = 0  # chunks lost to drop-oldest (telemetry)
        self._proc: subprocess.Popen | None = None
        self._queue: collections.deque = collections.deque(maxlen=self._QUEUE_MAX)
        self._has_data = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="pw-capture-read")
        self._processor = threading.Thread(target=self._process_loop, daemon=True,
                                           name="pw-capture-proc")

    @property
    def failed(self) -> bool:
        """True once the capture subprocess failed to launch or died from an
        unexpected fault (not a normal stop), or the consumer faulted
        persistently. Polled by the session liveness gate so a dead capture
        stops billing instead of accruing silent dead air."""
        return self._err is not None

    def _read_loop(self):
        frame_bytes = 2  # s16le mono
        block = max(1, self.rate // 50) * frame_bytes  # ~20 ms blocks
        if not _source_exists(self._monitor):
            self._err = RuntimeError(f"monitor source not found: {self._monitor!r}")
            self._has_data.set()
            return
        try:
            self._proc = subprocess.Popen(
                ["parec", "-d", self._monitor, "--rate", str(self.rate),
                 "--format=s16le", "--channels=1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._err = e
            self._has_data.set()
            return
        try:
            while self._run:
                buf = self._proc.stdout.read(block)
                if not buf:
                    # EOF: the subprocess exited. A normal stop() terminates it
                    # (and clears _run first), so only flag a fault if we were
                    # still expected to be running.
                    if self._run:
                        self._err = RuntimeError(
                            "parec capture ended unexpectedly (device gone?)")
                    break
                x = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                if len(self._queue) == self._QUEUE_MAX:
                    self.dropped += 1  # deque(maxlen) evicts silently -- count it
                self._queue.append(x)
                self._has_data.set()
        finally:
            self._has_data.set()  # wake the processor so it can exit

    def _process_loop(self):
        # Same fault policy as ProcessExcludeLoopback._processor: a single bad
        # chunk is transient; a long back-to-back run means the pipeline
        # behind us is dead -- record it so `failed` surfaces and billing stops.
        fails = 0
        while self._run or self._queue:
            if not self._queue:
                self._has_data.wait(0.05)
                self._has_data.clear()
                continue
            try:
                x = self._queue.popleft()
            except IndexError:
                continue
            try:
                self._on_chunk(x)
                fails = 0
            except Exception as e:
                fails += 1
                if fails == 1 or fails % 200 == 0:
                    print(f"[pipewire-capture] consumer fault #{fails}: {e!r}")
                if fails >= 50 and self._err is None:
                    self._err = e

    def start(self):
        if self._run:
            return
        # Thread objects are single-use: recreate after any prior run so a
        # restart never hits "threads can only be started once".
        if not self._reader.is_alive():
            self._err = None
            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name="pw-capture-read")
        if not self._processor.is_alive():
            self._processor = threading.Thread(target=self._process_loop, daemon=True,
                                               name="pw-capture-proc")
        self._run = True
        self._reader.start()
        self._processor.start()

    def stop(self):
        self._run = False
        if self._proc is not None:
            # Unblocks the reader's blocking stdout.read() promptly instead of
            # waiting for the next scheduled block to arrive.
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._has_data.set()
        if self._reader.is_alive():
            self._reader.join(timeout=1.5)
        if self._proc is not None:
            try:
                self._proc.wait(timeout=1.5)
            except Exception:
                pass
            self._proc = None
        if self._processor.is_alive():
            self._processor.join(timeout=1.5)
