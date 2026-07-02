"""Windows session-level ducking — no external program or driver required.

Uses the same ISimpleAudioVolume API as the Sound Mixer to attenuate other
applications (browser, game, etc.) at their source while excluding our own
process. The original levels are restored when the translation stops. Combined
with loopback capture this produces a real dubbing feel without VB-CABLE.

Crash safety: Windows persists per-app mixer levels across app AND system
restarts, so a hard kill mid-duck would leave every other app quiet forever.
The original levels are therefore snapshotted to a sidecar file while ducking
is active; restore_pending() (called at next launch) puts them back and only
then deletes the snapshot.
"""
import json
import os
import threading
import time

import comtypes
from pycaw.pycaw import AudioUtilities

from .paths import user_path

# Sidecar snapshot of pre-duck session volumes, kept only while a duck is in
# force. Owned exclusively by this module (never config.json) so the ducker
# thread cannot race the bridge's config writes.
_RESTORE_PATH = user_path("duck_restore.json")

# GetAllSessions is a full COM enumeration (session manager + process probes).
# The ramp loop ticks every 50 ms; re-enumerating on each tick is pure waste,
# so the session list is cached and refreshed at most this often. New sessions
# are still ducked within one cache window.
_SESSIONS_TTL = 1.0


def _write_snapshot(entries: dict) -> None:
    """Persist {pid: {exe, level}} atomically (tmp + replace)."""
    tmp = _RESTORE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pids": entries}, f)
        os.replace(tmp, _RESTORE_PATH)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _clear_snapshot() -> None:
    try:
        if os.path.exists(_RESTORE_PATH):
            os.remove(_RESTORE_PATH)
    except OSError:
        pass


def restore_pending() -> None:
    """Undo a duck left behind by a crash/kill of a previous run.

    Matches stored sessions by pid first, then by executable name (the app may
    have been restarted since). Volumes are only ever RAISED back to the stored
    base — if the user already fixed a level by hand we never lower it again.
    The snapshot is removed after one pass either way; a transient COM failure
    just leaves it for the next launch. Runs on its own thread + COM apartment."""
    try:
        if not os.path.exists(_RESTORE_PATH):
            return
        with open(_RESTORE_PATH, "r", encoding="utf-8") as f:
            entries = (json.load(f) or {}).get("pids") or {}
    except (OSError, ValueError):
        _clear_snapshot()
        return
    if not entries:
        _clear_snapshot()
        return
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return  # keep the snapshot; retry next launch
    by_pid: dict[int, list] = {}
    by_exe: dict[str, list] = {}
    for s in sessions:
        try:
            pid = s.Process.pid if s.Process else 0
            if pid <= 0:
                continue
            by_pid.setdefault(pid, []).append(s)
            name = (s.Process.name() or "").lower()
            if name:
                by_exe.setdefault(name, []).append(s)
        except Exception:
            continue
    for pid_s, info in entries.items():
        try:
            level = float(info.get("level", 1.0))
        except (TypeError, ValueError):
            continue
        exe = str(info.get("exe") or "").lower()
        try:
            targets = by_pid.get(int(pid_s), [])
        except (TypeError, ValueError):
            targets = []
        if not targets and exe:
            targets = by_exe.get(exe, [])
        for s in targets:
            try:
                vol = s.SimpleAudioVolume
                # Raise-only: restore the base if the session still looks ducked.
                if float(vol.GetMasterVolume()) < level - 0.01:
                    vol.SetMasterVolume(max(0.0, min(1.0, level)), None)
            except Exception:
                continue
    _clear_snapshot()
    try:
        comtypes.CoUninitialize()
    except Exception:
        pass


class SessionDucker:
    """Smoothly ramps other-app session volumes toward `target` (0..1)."""

    def __init__(self):
        self.target = 1.0
        self.current = 1.0
        self._orig: dict = {}  # pid -> (SimpleAudioVolume, original level)
        self._exe: dict = {}   # pid -> executable name (for the crash snapshot)
        self._sessions = None
        self._sessions_at = 0.0
        self._snapshot_dirty = False
        self._snapshot_at = 0.0
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
                self._apply(self.current, refresh=True)
                last_apply = time.time()
            elif self.current >= 0.985 and self._orig:
                self._restore()
            self._maybe_persist()
            time.sleep(0.05)
        self._restore()

    def _get_sessions(self, refresh: bool = False):
        """Cached session list; a full COM enumeration at most every
        _SESSIONS_TTL seconds (or on demand) instead of every 50 ms ramp tick."""
        now = time.time()
        if (self._sessions is None or refresh
                or now - self._sessions_at > _SESSIONS_TTL):
            try:
                self._sessions = AudioUtilities.GetAllSessions()
                self._sessions_at = now
            except Exception:
                pass  # keep the last known list; retry next tick
        return self._sessions or []

    def _apply(self, level: float, refresh: bool = False):
        me = os.getpid()
        live: set[int] = set()
        for s in self._get_sessions(refresh=refresh):
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
                    try:
                        self._exe[pid] = s.Process.name() if s.Process else ""
                    except Exception:
                        self._exe[pid] = ""
                    self._snapshot_dirty = True
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
            self._exe.pop(pid, None)
            self._snapshot_dirty = True
            try:
                vol.SetMasterVolume(base, None)
            except Exception:
                pass

    def _maybe_persist(self):
        """Write the crash snapshot when the captured set changed (throttled).
        Runs only on the ducker thread, so no locking is needed."""
        if not self._snapshot_dirty:
            return
        now = time.time()
        if now - self._snapshot_at < 1.0:
            return
        self._snapshot_at = now
        self._snapshot_dirty = False
        if self._orig:
            _write_snapshot({
                str(pid): {"exe": self._exe.get(pid, ""), "level": base}
                for pid, (_vol, base) in self._orig.items()
            })
        else:
            _clear_snapshot()

    def _restore(self):
        for vol, base in list(self._orig.values()):
            try:
                vol.SetMasterVolume(base, None)
            except Exception:
                pass
        self._orig.clear()
        self._exe.clear()
        self.current = 1.0
        # Levels are back — the crash snapshot is no longer needed.
        self._snapshot_dirty = False
        _clear_snapshot()

    def close(self):
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
