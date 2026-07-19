"""Linux virtual microphone for Meeting mode's outgoing leg (VB-CABLE
equivalent, chosen 2026-07-19 -- see linux/PLAN.md Faz 5).

Windows routes OutgoingPipeline's Player straight to VB-CABLE's Input device
(a real, separately-addressable PortAudio device -- no special handling
needed). Linux has no such pre-existing virtual cable; "VoxisMic" (a plain
null-sink) is the equivalent, but getting Player's stream onto it safely is
less direct than picking a device index.

THE SYSTEM DEFAULT SOURCE IS NEVER CHANGED (mirrors routing.py's "never touch
default sink" rule for the capture side -- see that module's "KESIN KARAR
v2"). Confirmed empirically (linux/phase5_source_autoconnect_test.py,
2026-07-19): changing the default SOURCE drags an already-open InputStream
exactly like changing the default SINK dragged Player in Faz 3's first
(abandoned) design -- if OutgoingPipeline's own mic `Capture` ever reads via
`device=None` and the default source were switched to VoxisMic.monitor for a
conferencing app's convenience, our own mic capture would follow it too,
capturing our own TTS output as if it were the user's voice.

Instead: create VoxisMic (not defaulted), let Player open normally
(device=None, lands on whatever real default happens to be), then explicitly
`pactl move-sink-input` it onto VoxisMic exactly once. Proved stable over a
5+ second running window with no drift back
(linux/phase5_stable_move_test.py) -- the drift Faz 3 hit was specifically
tied to a LATER default-sink change re-triggering WirePlumber's autoconnect
policy; with no default change ever happening here, the one-time move holds.
The conferencing app must be pointed at "VoxisMic Monitor" manually in its
OWN microphone selector -- this matches the documented DoD and mirrors how
VB-CABLE is used on Windows too (system default switch there is a
convenience layer on top of manual selection, not a replacement for it).
"""
import json
import os
import shutil
import subprocess

MIC_SINK_NAME = "VoxisMic"


def _restore_path() -> str:
    from ... import paths  # noqa: PLC0415 -- deferred, only needed off the hot path
    return paths.user_path("linux_virtual_mic_restore.json")


def _write_snapshot(sink_module_id: str) -> None:
    tmp = _restore_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sink_module_id": sink_module_id}, f)
        os.replace(tmp, _restore_path())
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _clear_snapshot() -> None:
    try:
        os.remove(_restore_path())
    except OSError:
        pass


def restore_pending_virtual_mic() -> None:
    """Undoes a virtual-mic setup left behind by a crash/kill of a previous
    run -- an orphaned VoxisMic just idles otherwise (system default was
    never touched, so nothing else is affected). Called alongside
    `routing.restore_pending_routing()` from
    `sysaudio.restore_pending_ducking()`; must never raise on a clean start or
    an already-gone module."""
    path = _restore_path()
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f) or {}
    except (OSError, ValueError):
        _clear_snapshot()
        return
    mod_id = info.get("sink_module_id")
    if mod_id:
        try:
            _pactl("unload-module", str(mod_id))
        except Exception:
            pass  # already gone -- fine
    _clear_snapshot()


def _pactl(*args, timeout: float = 5.0) -> str:
    # pactl localizes its full-listing headers under a non-English locale (e.g.
    # "Sink Input #" becomes a translated string on tr_TR), which silently
    # breaks the header parser below -- pin LC_ALL=C (shared parsing contract
    # with routing.py). Field-proven 2026-07-19.
    out = subprocess.run(["pactl", *args], capture_output=True, text=True,
                         timeout=timeout, check=True,
                         env={**os.environ, "LC_ALL": "C"})
    return out.stdout.strip()


def _pw(*args, timeout: float = 5.0) -> str:
    """Runs a pw-* command (`pw-dump`) -- same env pin as `_pactl` (uniform
    locale contract, shared with routing.py). Test seam, monkeypatched like
    `_pactl`."""
    out = subprocess.run(list(args), capture_output=True, text=True,
                         timeout=timeout, check=True,
                         env={**os.environ, "LC_ALL": "C"})
    return out.stdout.strip()


def _own_pipewire_serials(own_pid: str) -> set[str]:
    """Node object.serials (== pactl sink-input index -- pipewire-pulse
    manager.c: index == serial, the same identity routing.py's `_move_native`
    relies on) whose owning client's kernel-verified `pipewire.sec.pid`
    matches our own pid.

    PipeWire-ALSA clients -- which is what our own sounddevice-based Players
    actually are on a PipeWire system -- set NO `application.process.id` at
    all (confirmed empirically on a live VM, 2026-07-19: a real `pactl list
    sink-inputs` dump for a running Player carried zero `application.*`
    properties whatsoever). PID-only matching in `_own_sink_input_ids` below
    therefore silently sees none of our own streams -- the exact bug already
    found and fixed in routing.py's `_classify_stream`; this mirrors that fix
    here. Best-effort: returns an empty set if `pw-dump` is absent or its
    output can't be parsed (falls back to whatever the PID path found)."""
    if shutil.which("pw-dump") is None:
        return set()
    try:
        objs = json.loads(_pw("pw-dump"))
    except Exception:
        return set()
    own_client_ids = set()
    for obj in objs:
        if not str(obj.get("type", "")).endswith("Client"):
            continue
        props = (obj.get("info") or {}).get("props") or {}
        if str(props.get("pipewire.sec.pid", "")) == own_pid:
            own_client_ids.add(obj.get("id"))
    serials: set[str] = set()
    for obj in objs:
        if not str(obj.get("type", "")).endswith("Node"):
            continue
        props = (obj.get("info") or {}).get("props") or {}
        serial = props.get("object.serial")
        if serial is not None and props.get("client.id") in own_client_ids:
            serials.add(str(serial))
    return serials


def _own_sink_input_ids(own_pid: str) -> set[str]:
    """Indices of sink-inputs belonging to OUR OWN process: the union of
    PID-tagged sink-inputs from the FULL `pactl list sink-inputs` output
    (native pulse clients) and pw-dump-resolved ones (PipeWire-ALSA clients,
    see `_own_pipewire_serials` -- this is the path our own Players actually
    take)."""
    ids: set[str] = set()
    idx = None
    pid = None

    def _flush():
        if idx is not None and pid == own_pid:
            ids.add(idx)

    for raw in _pactl("list", "sink-inputs").splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            _flush()
            idx = line.split("#", 1)[1].strip()
            pid = None
        elif line.startswith("application.process.id"):
            pid = line.split("=", 1)[1].strip().strip('"')
    _flush()
    ids |= _own_pipewire_serials(own_pid)
    return ids


def snapshot_own_streams() -> set[str]:
    """Baseline of our own sink-input indices, taken BEFORE constructing the
    outgoing Player -- `pin_newest_own_stream` diffs against this to find the
    stream Player just opened."""
    return _own_sink_input_ids(str(os.getpid()))


class VirtualMicHandle:
    """Opaque handle returned by `create_virtual_mic`; pass to
    `teardown_virtual_mic` to unwind it."""

    def __init__(self, sink_module_id: str):
        self.sink_name = MIC_SINK_NAME
        self._sink_module_id = sink_module_id


def create_virtual_mic(sink_name: str = MIC_SINK_NAME) -> VirtualMicHandle:
    """Creates the dedicated virtual-mic null-sink. Creating it opens no
    sink-input of our own, so call order relative to `snapshot_own_streams`
    doesn't matter in practice -- called first here simply to mirror
    `routing.setup_capture_routing`'s shape."""
    if shutil.which("pactl") is None:
        raise RuntimeError("pactl not found -- is PipeWire/PulseAudio installed?")
    mod_id = _pactl("load-module", "module-null-sink", f"sink_name={sink_name}",
                    "sink_properties=device.description=Voxis_Virtual_Mic")
    handle = VirtualMicHandle(mod_id)
    _write_snapshot(mod_id)
    return handle


def pin_newest_own_stream(before_ids: set[str], sink_name: str = MIC_SINK_NAME) -> str | None:
    """Moves whichever of OUR OWN sink-inputs appeared after `before_ids` was
    snapshotted (via `snapshot_own_streams`, called before Player opened) onto
    `sink_name`. Returns the moved index, or None if nothing new was found --
    callers should treat that as a soft failure (log and continue; the
    session still works, just without the virtual-mic redirect)."""
    own_pid = str(os.getpid())
    after_ids = _own_sink_input_ids(own_pid)
    new_ids = after_ids - before_ids
    if not new_ids:
        return None
    idx = sorted(new_ids, key=int)[-1]  # the just-created stream
    _pactl("move-sink-input", idx, sink_name)
    return idx


def teardown_virtual_mic(handle: VirtualMicHandle | None) -> None:
    """Unwinds a `create_virtual_mic` result. No-op for None. Best-effort -- a
    module already gone (e.g. PipeWire restarted mid-session) is not an
    error."""
    if handle is None:
        return
    try:
        _pactl("unload-module", handle._sink_module_id)
    except Exception:
        pass
    _clear_snapshot()
