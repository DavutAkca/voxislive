"""Linux ambient ducking: ramps the Option-A loopback's volume toward a
target, API-compatible with Windows `session_duck.SessionDucker` (`.target`,
`.current`, `.close()`).

Simpler than Windows by construction: `routing.setup_capture_routing` already
funnels every "other app" through ONE loopback stream (VoxisCapture.monitor ->
real sink), so there is only ever one volume to control -- no per-session COM
enumeration, no pid tracking, no crash snapshot (a leftover duck level is
already handled by `routing.restore_pending_routing`, which removes the whole
loopback module rather than needing to restore its volume).
"""
import threading
import time


class LinuxSessionDucker:
    """Smoothly ramps the ambient loopback's volume toward `target` (0..1)."""

    # Windows' SessionDucker attenuates the SAME mix its loopback capture then
    # reads back (both live at the OS mixer), so pipeline.py compensates by
    # dividing the captured level back up. Option-A's capture point
    # (VoxisCapture.monitor) sits UPSTREAM of the ducked forward -- ducking
    # only ever touches the copy sent to real hardware for the user's ears,
    # never what gets translated -- so that compensation must NOT be applied
    # here (pipeline.py checks this flag, defaulting True for any ducker that
    # doesn't set it, which preserves Windows' behavior unchanged).
    duck_affects_capture = False

    def __init__(self, routing_handle):
        self._handle = routing_handle
        self.target = 1.0
        self.current = 1.0
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="linux-ducker")
        self._thread.start()

    def _loop(self):
        while self._run:
            tgt = max(0.0, min(1.0, float(self.target)))
            if abs(self.current - tgt) > 0.02:
                # Same fast-attack/slow-release curve as Windows SessionDucker:
                # the translation should land cleanly, but the original should
                # ease back up rather than snap.
                step = 0.25 if tgt < self.current else 0.12
                self.current += max(min(tgt - self.current, step), -step)
                self._handle.set_duck_volume(self.current)
            time.sleep(0.05)
        self._handle.set_duck_volume(1.0)

    def close(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
