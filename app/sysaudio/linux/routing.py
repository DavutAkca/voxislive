"""Session-lifetime audio graph routing for the Linux "Option A" architecture
(the VB-CABLE-equivalent path, chosen 2026-07-19 -- see linux/audio-pipewire.md
"KESIN KARAR" and linux/PLAN.md Faz 3).

Shape:
    other apps ──(moved here explicitly)──► VoxisCapture (null-sink)
                                                  │
                                                  ├─ .monitor ──► captured (16 kHz mono, Gemini)
                                                  └─ .monitor ──(ducked loopback)──► real hardware
    Voxis's own TTS (Player) ─────────────────────────────────────────────────► real hardware (device=None, untouched)

Self-exclude is structural: capture only ever reads VoxisCapture's monitor,
which sits upstream of the point where Voxis's own TTS enters the mix, so the
TTS can never loop back into what gets translated.

THE SYSTEM DEFAULT SINK IS NEVER CHANGED (load-bearing -- see history below).
Other apps' sink-inputs are moved onto VoxisCapture explicitly via
`pactl move-sink-input`, both at session start (existing streams) and
continuously for the session's duration (a background sweep thread catches
streams that start later -- they briefly touch the real sink first, since it
stays default, before being moved within one poll tick).

Architecture history (why NOT promote VoxisCapture to default): the first
design promoted VoxisCapture to system default so other apps would follow it
automatically, with Player pinned to the real sink via a PULSE_SINK env var +
PortAudio reinit (proved to work in isolation --
linux/phase3_player_pin_test.py). It broke under the FULL routing flow on a
real RPi5 (2026-07-19): `pactl set-default-sink VoxisCapture` itself causes
WirePlumber to re-link ALREADY-CONNECTED nodes with `node.autoconnect=true`
(which is what Player's PortAudio/Pulse-compat stream carries) onto the new
default -- regardless of the PULSE_SINK hint used when it was created, and
regardless of an explicit `pactl move-sink-input`/`pw-metadata target.node`
issued afterward (both were reverted, apparently by the same continuous
policy). Confirmed via a debug capture: Player's own sink-input measurably
migrated onto the throwaway sink the instant the default changed
(linux/phase3_debug_sweep.py, linux/phase3_repin_test.py, linux/phase3_
pwmeta_pin_test.py). The flipped design (never touch default; explicitly move
OTHER streams instead) was verified end-to-end
(linux/phase3_flipped_test.py): "other app" tone measured ~1.2% of the TTS
tone's energy in VoxisCapture's monitor -- clean self-exclude, and Player
needs zero Linux-specific handling at all.
"""
import json
import logging
import os
import shutil
import subprocess
import threading
import time

_log = logging.getLogger("voxis")

CAPTURE_SINK_NAME = "VoxisCapture"

SWEEP_INTERVAL = 0.3  # seconds between checks for newly-started "other app" streams


def _restore_path() -> str:
    from ... import paths  # noqa: PLC0415 -- deferred, only needed off the hot path
    return paths.user_path("linux_routing_restore.json")


def _write_snapshot(sink_module_id: str, loopback_module_id: str) -> None:
    tmp = _restore_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sink_module_id": sink_module_id,
                      "loopback_module_id": loopback_module_id}, f)
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


def restore_pending_routing() -> None:
    """Undoes a routing setup left behind by a crash/kill of a previous run.

    Lower stakes than it would be if the default sink were ever changed (it
    never is -- see module docstring): an orphaned VoxisCapture/loopback pair
    just idles, using a little memory, while all NEW app audio keeps going
    straight to the real sink as normal. Still worth cleaning up so a stale
    virtual sink doesn't accumulate in the user's audio menu across restarts.
    Called unconditionally at launch (see `sysaudio.restore_pending_ducking`),
    so it must never raise on a clean start or an already-gone module."""
    path = _restore_path()
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f) or {}
    except (OSError, ValueError):
        _clear_snapshot()
        return
    for mod_id in (info.get("loopback_module_id"), info.get("sink_module_id")):
        if not mod_id:
            continue
        try:
            _pactl("unload-module", str(mod_id))
        except Exception:
            pass  # already gone -- fine
    _clear_snapshot()


def _pactl(*args, timeout: float = 5.0) -> str:
    # pactl localizes its full-listing headers under a non-English locale (e.g.
    # "Sink Input #" becomes a translated string on tr_TR), which silently
    # breaks the header parsers below -- pin LC_ALL=C. Field-proven 2026-07-19.
    out = subprocess.run(["pactl", *args], capture_output=True, text=True,
                         timeout=timeout, check=True,
                         env={**os.environ, "LC_ALL": "C"})
    return out.stdout.strip()


def _pw(*args, timeout: float = 5.0) -> str:
    """Runs an arbitrary pw-* command (`pw-dump`, `pw-metadata`) -- the native
    PipeWire fallback for a `pactl move-sink-input` that fails ambiguously.
    Same env pin as `_pactl` (uniform locale contract); first arg is the
    binary name. This is the test seam, monkeypatched exactly like `_pactl`."""
    out = subprocess.run([*args], capture_output=True, text=True,
                         timeout=timeout, check=True,
                         env={**os.environ, "LC_ALL": "C"})
    return out.stdout.strip()


def _sink_ids() -> dict[str, str]:
    """Sink NAME -> numeric id map via `pactl list sinks short` (the id
    sink-inputs report themselves against, e.g. "Sink: 64", is numeric --
    never the name)."""
    ids: dict[str, str] = {}
    for line in _pactl("list", "sinks", "short").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            ids[parts[1]] = parts[0]
    return ids


def _sink_id(sink_name: str) -> str | None:
    return _sink_ids().get(sink_name)


def _stream_records() -> list[dict]:
    """Every sink-input from the FULL `pactl list sink-inputs` output as
    {idx, sink, pid, module_owned} (the `short` format has none of the
    exclusion columns). `pid` stays None for pipewire-alsa clients -- they set
    no application.process.id (unlike pulse clients), so PID-based exclusion
    alone cannot see them; `module_owned` marks streams created by a
    pipewire-pulse module (Owner Module set), e.g. the ambient loopback's own
    playback stream."""
    recs: list[dict] = []
    idx = sink = pid = None
    module_owned = False

    def _flush():
        if idx is not None:
            recs.append({"idx": idx, "sink": sink, "pid": pid,
                         "module_owned": module_owned})

    for raw in _pactl("list", "sink-inputs").splitlines():
        line = raw.strip()
        if line.startswith("Sink Input #"):
            _flush()
            idx = line.split("#", 1)[1].strip()
            sink = None
            pid = None
            module_owned = False
        elif line.startswith("Owner Module:"):
            module_owned = line.split(":", 1)[1].strip() not in ("n/a", "")
        elif line.startswith("Sink:"):
            sink = line.split(":", 1)[1].strip()
        elif line.startswith("application.process.id"):
            pid = line.split("=", 1)[1].strip().strip('"')
    _flush()
    return recs


def _sink_inputs_on(sink_name: str, *, exclude_pid: str | None = None) -> list[str]:
    """Sink-input indices currently attached to `sink_name` (compat helper)."""
    sink_id = _sink_id(sink_name)
    if sink_id is None:
        return []
    return [r["idx"] for r in _stream_records()
            if r["sink"] == sink_id and r["pid"] != exclude_pid]


def _load_capture_modules(real_sink: str) -> tuple[str, str]:
    """Loads the VoxisCapture null-sink + its ducked loopback back into
    `real_sink`, returning the two module ids. Shared by
    `setup_capture_routing` and `RoutingHandle._rebuild_capture_sink` so the
    initial build and the mid-session rebuild stay in lockstep."""
    sink_module_id = _pactl(
        "load-module", "module-null-sink", f"sink_name={CAPTURE_SINK_NAME}",
        "sink_properties=device.description=Voxis_Capture")
    loopback_module_id = _pactl(
        "load-module", "module-loopback",
        f"source={CAPTURE_SINK_NAME}.monitor", f"sink={real_sink}",
        "latency_msec=20")
    return sink_module_id, loopback_module_id


class RoutingHandle:
    """Opaque handle returned by `setup_capture_routing`; pass to
    `teardown_capture_routing` to unwind it."""

    def __init__(self, real_sink: str, sink_module_id: str, loopback_module_id: str):
        self.real_sink = real_sink
        self.capture_monitor = f"{CAPTURE_SINK_NAME}.monitor"
        self._sink_module_id = sink_module_id
        self._loopback_module_id = loopback_module_id
        self._loopback_sink_input_idx: str | None = None
        self._own_pid = str(os.getpid())
        self._sweep_run = False
        self._sweep_thread: threading.Thread | None = None
        self._last_duck_level = 1.0        # re-applied after a rebuild
        self._rebuild_failed_logged = False  # log-once guard for rebuild failure
        self._move_fail_counts: dict[str, int] = {}  # consecutive move failures per idx
        self._move_fail_logged: set[str] = set()     # indexes already warned about
        self._stream_class: dict[str, str] = {}      # idx -> "own"|"other"|"unknown"
        self._unknown_logged: set[str] = set()       # unknown-ownership warned once
        self._backswept_logged: set[str] = set()     # backsweep logged once per idx

    def _resolve_loopback_sink_input(self) -> str | None:
        if self._loopback_sink_input_idx is not None:
            return self._loopback_sink_input_idx
        out = _pactl("list", "sink-inputs")
        idx = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Sink Input #"):
                idx = line.split("#", 1)[1].strip()
            elif 'media.name = "loopback' in line and idx is not None:
                self._loopback_sink_input_idx = idx
                return idx
        return None

    def set_duck_volume(self, level: float) -> None:
        """Sets the ambient loopback's volume (0.0-1.0). Used by
        `ducking.LinuxSessionDucker` to ramp toward a target level -- the
        single-stream analog of Windows' per-app SessionDucker (Option A funnels
        every "other app" through one loopback stream, so there is only ever
        one volume to control instead of enumerating N sessions)."""
        self._last_duck_level = level  # remembered so a rebuild can re-apply it
        idx = self._resolve_loopback_sink_input()
        if idx is None:
            return  # loopback not up yet / already torn down -- no-op
        pct = max(0, min(100, round(level * 100)))
        try:
            _pactl("set-sink-input-volume", idx, f"{pct}%")
        except Exception:
            pass

    def _rebuild_capture_sink(self) -> None:
        """Re-creates VoxisCapture after it vanished mid-session. Modules loaded
        via `pactl load-module` live inside pipewire-pulse and die silently when
        it restarts (or if unloaded externally); `move-sink-input` then fails
        with the SAME ENOENT it gives a missing stream, so the sweep can't tell
        the two apart -- rebuild pre-emptively. Best-effort: never raises from
        the sweep thread; logs once per vanish event, and (on a failing reload)
        once until a rebuild finally succeeds, so it can't spam every tick."""
        try:
            sink_id, loopback_id = _load_capture_modules(self.real_sink)
        except Exception:
            if not self._rebuild_failed_logged:
                _log.warning("linux routing: VoxisCapture rebuild failed "
                             "(pactl gone?) -- other-app audio not captured")
                self._rebuild_failed_logged = True
            return
        self._sink_module_id = sink_id
        self._loopback_module_id = loopback_id
        self._loopback_sink_input_idx = None  # the cached loopback index is stale
        self._rebuild_failed_logged = False
        self.set_duck_volume(self._last_duck_level)  # re-apply the last duck level
        _write_snapshot(sink_id, loopback_id)
        _log.warning("linux routing: VoxisCapture vanished (pipewire-pulse "
                     "restart?) -- rebuilt")

    def _move_native(self, idx: str) -> bool:
        """Native PipeWire fallback for a `pactl move-sink-input` that failed:
        re-target the stream node directly via `pw-metadata target.object`,
        which WirePlumber honors (field-proven 2026-07-19). The pulse
        sink-input index equals the PipeWire node's `object.serial`
        (pipewire-pulse manager.c: index == serial), so `idx` maps to a
        pw-dump node by `object.serial`. Returns True on success, False if the
        tooling is absent, the nodes can't be resolved, or anything raises."""
        if shutil.which("pw-dump") is None or shutil.which("pw-metadata") is None:
            return False
        try:
            nodes = json.loads(_pw("pw-dump"))
            stream_node_id = None
            capture_serial = None
            for obj in nodes:
                if not str(obj.get("type", "")).endswith("Node"):
                    continue
                props = (obj.get("info") or {}).get("props") or {}
                if (props.get("media.class") == "Stream/Output/Audio"
                        and props.get("object.serial") == int(idx)):
                    stream_node_id = obj.get("id")
                if props.get("node.name") == CAPTURE_SINK_NAME:
                    capture_serial = props.get("object.serial")
            if stream_node_id is None or capture_serial is None:
                return False
            _pw("pw-metadata", str(stream_node_id), "target.object",
                str(capture_serial), "Spa:Id")
            return True
        except Exception:
            return False

    def _classify_stream(self, idx: str) -> str:
        """Ownership of a stream that carries no application.process.id
        (pipewire-alsa clients don't set it): resolve the owning client's
        kernel-verified `pipewire.sec.pid` via pw-dump. Returns "own" /
        "other" / "unknown" (no pw-dump, or the node can't be resolved).
        Cached per index -- pruned when the stream disappears."""
        cached = self._stream_class.get(idx)
        if cached is not None:
            return cached
        verdict = "unknown"
        if shutil.which("pw-dump") is not None:
            try:
                objs = json.loads(_pw("pw-dump"))
                client_id = None
                for obj in objs:
                    if not str(obj.get("type", "")).endswith("Node"):
                        continue
                    props = (obj.get("info") or {}).get("props") or {}
                    if props.get("object.serial") == int(idx):
                        client_id = props.get("client.id")
                        break
                for obj in (objs if client_id is not None else []):
                    if obj.get("id") == client_id and \
                            str(obj.get("type", "")).endswith("Client"):
                        cp = (obj.get("info") or {}).get("props") or {}
                        sec_pid = str(cp.get("pipewire.sec.pid", ""))
                        verdict = "own" if sec_pid == self._own_pid else "other"
                        break
            except Exception:
                verdict = "unknown"
        self._stream_class[idx] = verdict
        return verdict

    def _backsweep_wrong_side(self, records: list[dict], cap_id: str) -> set[str]:
        """Moves streams that must never sit on VoxisCapture back to the real
        sink: module-owned streams (the ambient loopback's own output --
        leaving it there feeds the monitor back into itself and nothing ever
        reaches the speakers) and our own TTS stream (feedback: Voxis would
        translate its own voice). They land there via a past mis-move that
        pipewire-pulse's module-stream-restore remembered -- it re-targets a
        stream by name the moment it is created, before any sweep runs."""
        seen: set[str] = set()
        for rec in records:
            if rec["sink"] != cap_id:
                continue
            idx = rec["idx"]
            seen.add(idx)
            ours = rec["pid"] == self._own_pid or (
                rec["pid"] is None and self._classify_stream(idx) == "own")
            if not (rec["module_owned"] or ours):
                continue
            try:
                _pactl("move-sink-input", idx, self.real_sink)
                if idx not in self._backswept_logged:
                    _log.warning("linux routing: moved own/module stream %s "
                                 "off %s back to %s (stream-restore relic)",
                                 idx, CAPTURE_SINK_NAME, self.real_sink)
                    self._backswept_logged.add(idx)
            except Exception:
                pass
        return seen

    def sweep_other_apps(self) -> None:
        """Moves any OTHER app's sink-input on the real sink onto VoxisCapture.
        Idempotent -- safe to call repeatedly. Called once at setup for
        pre-existing streams, then repeatedly by the background sweep thread to
        catch streams that start mid-session (they land on the real sink first,
        since it is never un-defaulted, then get moved here within one
        `SWEEP_INTERVAL`).

        Never touches: module-owned streams (the ducked loopback's own output
        -- moving it onto the sink it monitors is a feedback loop that mutes
        the speakers) and our own streams, including pipewire-alsa ones that
        carry no application.process.id (ownership then resolved via pw-dump's
        `pipewire.sec.pid`; unresolvable streams are left alone -- missing one
        app beats capturing our own TTS). Also self-heals a vanished capture
        sink (pipewire-pulse restart), falls back to a native pw-metadata
        re-target when pactl's move fails, and moves own/module streams BACK
        off VoxisCapture (stream-restore re-targets them there by name)."""
        ids = _sink_ids()
        if CAPTURE_SINK_NAME not in ids:
            self._rebuild_capture_sink()
            ids = _sink_ids()
            if CAPTURE_SINK_NAME not in ids:
                return  # rebuild failed -- nothing to move onto; retry next tick
        cap_id = ids[CAPTURE_SINK_NAME]
        real_id = ids.get(self.real_sink)
        records = _stream_records()
        seen: set[str] = set()
        for rec in records:
            if rec["sink"] != real_id:
                continue
            idx = rec["idx"]
            seen.add(idx)
            if rec["module_owned"] or rec["pid"] == self._own_pid:
                continue
            if rec["pid"] is None:
                verdict = self._classify_stream(idx)
                if verdict == "own":
                    continue
                if verdict == "unknown":
                    if idx not in self._unknown_logged:
                        _log.warning("linux routing: cannot verify ownership "
                                     "of sink-input %s (no pw-dump?) -- "
                                     "leaving it unmoved", idx)
                        self._unknown_logged.add(idx)
                    continue
            try:
                _pactl("move-sink-input", idx, CAPTURE_SINK_NAME)
                moved = True
            except subprocess.CalledProcessError:
                # ENOENT is ambiguous (stream vs. target sink) -- decide on the
                # return code alone, never on localized stderr text, then try
                # the native path before giving up.
                moved = self._move_native(idx)
            except Exception:
                moved = False
            if moved:
                self._move_fail_counts.pop(idx, None)
                self._move_fail_logged.discard(idx)
            else:
                n = self._move_fail_counts.get(idx, 0) + 1
                self._move_fail_counts[idx] = n
                if n >= 3 and idx not in self._move_fail_logged:
                    _log.warning("linux routing: could not move sink-input %s "
                                 "onto %s (pactl + pw-metadata both failed)",
                                 idx, CAPTURE_SINK_NAME)
                    self._move_fail_logged.add(idx)
        seen |= self._backsweep_wrong_side(records, cap_id)
        # Prune per-stream state for indexes no longer present.
        for gone in [i for i in self._move_fail_counts if i not in seen]:
            self._move_fail_counts.pop(gone, None)
            self._move_fail_logged.discard(gone)
        for gone in [i for i in self._stream_class if i not in seen]:
            self._stream_class.pop(gone, None)
        self._unknown_logged &= seen
        self._backswept_logged &= seen

    def _sweep_loop(self):
        while self._sweep_run:
            self.sweep_other_apps()
            time.sleep(SWEEP_INTERVAL)

    def start_sweep(self) -> None:
        if self._sweep_thread is not None:
            return
        self._sweep_run = True
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True,
                                              name="linux-routing-sweep")
        self._sweep_thread.start()

    def stop_sweep(self) -> None:
        self._sweep_run = False
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=1.5)
            self._sweep_thread = None


def setup_capture_routing(real_sink: str, *, duck_level: float = 1.0) -> RoutingHandle:
    """Creates the dedicated VoxisCapture sink and a ducked loopback forwarding
    its monitor back into `real_sink`, moves any streams already on
    `real_sink` (other than our own) onto it, and starts a background sweep
    to catch streams that start later. The system default sink is NEVER
    changed -- see module docstring for why."""
    if shutil.which("pactl") is None:
        raise RuntimeError("pactl not found -- is PipeWire/PulseAudio installed?")

    sink_module_id, loopback_module_id = _load_capture_modules(real_sink)

    handle = RoutingHandle(real_sink, sink_module_id, loopback_module_id)
    handle.set_duck_volume(duck_level)
    handle.sweep_other_apps()   # pre-existing streams
    handle.start_sweep()        # streams that start mid-session
    _write_snapshot(sink_module_id, loopback_module_id)
    return handle


def teardown_capture_routing(handle: RoutingHandle) -> None:
    """Unwinds a routing set up by `setup_capture_routing`: stops the sweep,
    removes the loopback and VoxisCapture. Best-effort -- a module already
    gone (e.g. PipeWire restarted mid-session) is not an error."""
    handle.stop_sweep()
    for mod_id in (handle._loopback_module_id, handle._sink_module_id):
        try:
            _pactl("unload-module", mod_id)
        except Exception:
            pass
    _clear_snapshot()
