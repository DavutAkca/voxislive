"""Windows session-level ducking — no external program or driver required.

Uses the same ISimpleAudioVolume API as the Sound Mixer to attenuate other
applications (browser, game, etc.) at their source while excluding our own
process. The original levels are restored when the translation stops. Combined
with loopback capture this produces a real dubbing feel without VB-CABLE.
"""
import os
import threading
import time

import comtypes
from pycaw.pycaw import AudioUtilities


class SessionDucker:
    """Smoothly ramps other-app session volumes toward `target` (0..1)."""

    def __init__(self):
        self.target = 1.0
        self.current = 1.0
        self._orig: dict = {}  # pid -> (SimpleAudioVolume, original level)
        self._run = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ducker")
        self._thread.start()

    def _loop(self):
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        last_apply = 0.0
        while self._run:
            tgt = max(0.0, min(1.0, float(self.target)))
            if abs(self.current - tgt) > 0.02:
                # Fast attack so the translation lands cleanly, slower release.
                step = 0.25 if tgt < self.current else 0.12
                self.current += max(min(tgt - self.current, step), -step)
                self._apply(self.current)
                last_apply = time.time()
            elif self.current < 0.985 and time.time() - last_apply > 1.0:
                # Catch sessions that started after the duck began.
                self._apply(self.current)
                last_apply = time.time()
            elif self.current >= 0.985 and self._orig:
                self._restore()
            time.sleep(0.05)
        self._restore()

    def _apply(self, level: float):
        me = os.getpid()
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            return
        live: set[int] = set()
        for s in sessions:
            try:
                pid = s.Process.pid if s.Process else 0
                # Skip system sounds and our own translation output.
                if pid in (0, me):
                    continue
                live.add(pid)
                if pid not in self._orig:
                    # A failed QueryInterface here means the session went away
                    # mid-enumeration; skip it rather than caching a dead handle.
                    vol = s.SimpleAudioVolume
                    self._orig[pid] = (vol, float(vol.GetMasterVolume()))
                vol, base = self._orig[pid]
                vol.SetMasterVolume(max(0.0, min(1.0, base * level)), None)
            except Exception:
                continue
        self._prune(live)

    def _prune(self, live: set[int]):
        # Drop sessions whose pid has departed so dead COM handles do not
        # accumulate over a long ducking run. Restore the base level first while
        # the handle may still be valid, then release the reference.
        for pid in [p for p in self._orig if p not in live]:
            vol, base = self._orig.pop(pid)
            try:
                vol.SetMasterVolume(base, None)
            except Exception:
                pass

    def _restore(self):
        for vol, base in list(self._orig.values()):
            try:
                vol.SetMasterVolume(base, None)
            except Exception:
                pass
        self._orig.clear()
        self.current = 1.0

    def close(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
