"""Platform dispatch for the OS audio backend (capture / ducking / endpoints).

Faz 0 seam for the Linux port. `pipeline.py` and `main.py` obtain their capture,
ducker and default-endpoint operations through here instead of importing the
Windows-native modules (`process_loopback`, `session_duck`, `win_audio`)
directly.

- **On Windows** every accessor returns the exact same implementation as before,
  constructed identically — behaviour is byte-for-byte unchanged (the Windows
  modules are untouched; only the import site moved behind this factory).
- **On any other platform** the audio accessors raise `AudioBackendUnavailable`
  until a native backend lands (Linux/PipeWire = Faz 3+). Callers gate on
  `is_supported()` and decline a session with a friendly status rather than
  crash. The startup restore helpers no-op off Windows so launch never crashes
  on the unconditional restore path in `main.py`.

The native Linux implementations (PipeWire capture, per-stream ducking,
null-sink endpoints) will plug in here without any further change to the call
sites — see linux/architecture-port-map.md.
"""
import shutil
import sys


class AudioBackendUnavailable(RuntimeError):
    """No OS capture/duck/endpoint layer exists on this platform yet.

    Callers should surface a "not supported on this OS" status instead of
    letting this propagate as a crash."""

    def __init__(self, platform: str | None = None):
        self.platform = platform or sys.platform
        super().__init__(f"No OS audio backend for platform '{self.platform}'")


def is_supported() -> bool:
    """True when a native OS audio backend exists for the running platform.

    Windows: always. Linux: when the PipeWire/Pulse-compat tooling this
    backend shells out to (`pactl`, `parec`) is actually on PATH (Faz 3 —
    driverless-equivalent path only, see `supports_endpoints` for what is
    still Windows-only). Evaluated at call time (not import time) so
    behaviour tracks the real platform and tests can exercise both branches."""
    if sys.platform == "win32":
        return True
    if sys.platform.startswith("linux"):
        return shutil.which("pactl") is not None and shutil.which("parec") is not None
    return False


def supports_endpoints() -> bool:
    """True only where default-ENDPOINT switching (vbcable/meeting-mode default
    input/output swap) is implemented -- Windows only. Deliberately separate
    from `is_supported()`: Linux's Option-A routing (Faz 3) never changes the
    system default sink at all (see linux/audio-pipewire.md "KESIN KARAR v2"),
    so it needs no endpoint-switching equivalent yet (Faz 5)."""
    return sys.platform == "win32"


def _require() -> None:
    if not is_supported():
        raise AudioBackendUnavailable(sys.platform)


# --- audio backend accessors --------------------------------------------

def make_ducker(routing_handle=None):
    """Construct the session-volume ducker for the current platform.

    Windows: `session_duck.SessionDucker` (COM/pycaw), per-app source-level
    ducking; `routing_handle` is ignored (no such concept exists there).
    Linux: `sysaudio.linux.ducking.LinuxSessionDucker`, ramping the ONE
    ambient loopback stream `routing_handle` (from `make_capture_routing`)
    set up -- Option-A funnels every "other app" through that single point,
    so there is nothing to enumerate."""
    _require()
    if sys.platform == "win32":
        from ..session_duck import SessionDucker
        return SessionDucker()
    from .linux.ducking import LinuxSessionDucker  # noqa: PLC0415
    return LinuxSessionDucker(routing_handle)


def make_process_loopback(on_chunk, *, rate: int = 16000, routing_handle=None):
    """Process-exclude system loopback capture (excludes our own output).

    Windows: `process_loopback.ProcessExcludeLoopback` (WASAPI
    ApplicationLoopback); `routing_handle` is ignored. Linux:
    `sysaudio.linux.capture.PipeWireCapture`, reading
    `routing_handle.capture_monitor` (from `make_capture_routing` -- REQUIRED
    on Linux, since there is no "the current default" to fall back to)."""
    _require()
    if sys.platform == "win32":
        from ..process_loopback import ProcessExcludeLoopback
        return ProcessExcludeLoopback(on_chunk, rate=rate)
    from .linux.capture import PipeWireCapture  # noqa: PLC0415
    return PipeWireCapture(on_chunk, routing_handle.capture_monitor, rate=rate)


def make_loopback_capture(on_chunk, *, prefer_name=None, on_status=None):
    """Classic loopback capture fallback (used when process-exclude is unavailable).

    Windows: `audio_io.LoopbackCapture` (pyaudiowpatch WASAPI loopback). No
    Linux equivalent exists yet -- `PipeWireCapture` has no analogous "classic"
    degraded mode, so this raises on any non-Windows platform even where
    `is_supported()` is True."""
    _require()
    if sys.platform != "win32":
        raise AudioBackendUnavailable(sys.platform)
    from ..audio_io import LoopbackCapture
    return LoopbackCapture(on_chunk, prefer_name=prefer_name, on_status=on_status)


def make_capture_routing(real_sink: str | None = None):
    """Linux-only Option-A capture routing setup: creates the dedicated
    VoxisCapture sink + ducked loopback + continuous sweep of other apps'
    streams onto it (see linux/audio-pipewire.md "KESIN KARAR v2"). Returns a
    routing handle to pass into `make_ducker`/`make_process_loopback`.

    Windows: returns None (no equivalent construct -- ducking and capture are
    independent constructs there, this call is simply skipped by callers)."""
    _require()
    if sys.platform == "win32":
        return None
    from .linux import routing  # noqa: PLC0415
    if real_sink is None:
        import os  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        real_sink = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True,
            timeout=5, check=True, env={**os.environ, "LC_ALL": "C"}).stdout.strip()
    return routing.setup_capture_routing(real_sink)


def teardown_capture_routing(handle) -> None:
    """Unwinds a `make_capture_routing` result. No-op for None (Windows, or a
    Linux setup that never completed)."""
    if handle is None:
        return
    from .linux import routing  # noqa: PLC0415
    routing.teardown_capture_routing(handle)


def make_virtual_mic():
    """Linux-only: creates the dedicated "VoxisMic" virtual-mic sink for
    Meeting mode's outgoing leg (see linux/sysaudio/linux/virtual_mic.py).
    Windows: returns None -- OutgoingPipeline targets VB-CABLE's real Input
    device directly there, no equivalent construct needed."""
    _require()
    if sys.platform == "win32":
        return None
    from .linux import virtual_mic  # noqa: PLC0415
    return virtual_mic.create_virtual_mic()


def snapshot_own_audio_streams():
    """Linux-only baseline for `pin_newest_own_stream_to_mic`, taken BEFORE
    constructing the outgoing Player. Windows: returns None (unused there)."""
    if sys.platform != "win32" and is_supported():
        from .linux import virtual_mic  # noqa: PLC0415
        return virtual_mic.snapshot_own_streams()
    return None


def pin_newest_own_stream_to_mic(before, handle) -> str | None:
    """Linux-only: moves whichever sink-input appeared after `before` (the
    outgoing Player, just constructed) onto the virtual mic `handle`. Returns
    the moved index, or None if nothing new was found or on Windows/no
    handle (no-op there -- Player already targets the real VB-CABLE device)."""
    if sys.platform == "win32" or handle is None:
        return None
    from .linux import virtual_mic  # noqa: PLC0415
    return virtual_mic.pin_newest_own_stream(before or set(), handle.sink_name)


def teardown_virtual_mic(handle) -> None:
    """Unwinds a `make_virtual_mic` result. No-op for None."""
    if handle is None:
        return
    from .linux import virtual_mic  # noqa: PLC0415
    virtual_mic.teardown_virtual_mic(handle)


def endpoints():
    """The default-endpoint management module (get_default / set_default /
    find_endpoint_id / restore). Windows only -- see `supports_endpoints`.

    Windows: the `win_audio` module (IPolicyConfigVista COM). Returned as a
    module so existing call sites keep using `wa.get_default(...)` etc."""
    if sys.platform != "win32":
        raise AudioBackendUnavailable(sys.platform)
    from .. import win_audio
    return win_audio


# --- startup restore helpers (safe no-op where not applicable) -------------

def restore_pending_ducking() -> None:
    """Undo session-audio state left behind by a crashed previous run. No-op
    on any other platform. Called unconditionally at launch (main.py), so it
    MUST NOT import a platform-specific module before checking `sys.platform`.

    Windows: restores per-app session volumes a crash left ducked
    (`session_duck.restore_pending`). Linux: removes an orphaned
    VoxisCapture/loopback pair a crash left loaded
    (`linux.routing.restore_pending_routing`) -- lower stakes than Windows
    since Option-A never touches the default sink, but still worth cleaning up
    so a stale virtual sink doesn't accumulate in the user's audio menu."""
    if sys.platform == "win32":
        from ..session_duck import restore_pending
        restore_pending()
    elif sys.platform.startswith("linux"):
        from .linux import routing  # noqa: PLC0415
        from .linux import virtual_mic  # noqa: PLC0415
        routing.restore_pending_routing()
        virtual_mic.restore_pending_virtual_mic()


def restore_endpoints(saved) -> None:
    """Restore default endpoints snapshotted before a crash/kill. No-op off
    Windows (same unconditional-at-launch contract as above) -- endpoint
    switching is a Windows-only concept, see `supports_endpoints`."""
    if sys.platform != "win32":
        return
    from .. import win_audio
    win_audio.restore(saved)
