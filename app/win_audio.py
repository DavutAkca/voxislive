"""Windows default audio endpoint management.

Switches the system default output to the virtual cable when a mode starts and
restores the previous endpoint on stop. Uses the undocumented but de-facto
standard IPolicyConfigVista COM interface (the Sound control panel uses the
same call).
"""
import threading
from ctypes import POINTER, c_void_p
from ctypes.wintypes import INT, LPCWSTR

import comtypes
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown, CoCreateInstance

_CLSID_PolicyConfigVistaClient = GUID("{294935CE-F637-4E7C-A41B-AB255460B862}")


class IPolicyConfigVista(IUnknown):
    _iid_ = GUID("{568b9108-44bf-40b4-9006-86afe5b5a620}")
    # Only SetDefaultEndpoint is invoked; the others are present to preserve
    # the vtable ordering.
    _methods_ = (
        COMMETHOD([], HRESULT, "GetMixFormat",
                  (["in"], LPCWSTR), (["out"], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, "GetDeviceFormat",
                  (["in"], LPCWSTR), (["in"], INT), (["out"], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, "SetDeviceFormat",
                  (["in"], LPCWSTR), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "GetProcessingPeriod",
                  (["in"], LPCWSTR), (["in"], INT),
                  (["out"], POINTER(c_void_p)), (["out"], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, "SetProcessingPeriod",
                  (["in"], LPCWSTR), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "GetShareMode",
                  (["in"], LPCWSTR), (["out"], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, "SetShareMode",
                  (["in"], LPCWSTR), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "GetPropertyValue",
                  (["in"], LPCWSTR), (["in"], c_void_p), (["out"], POINTER(c_void_p))),
        COMMETHOD([], HRESULT, "SetPropertyValue",
                  (["in"], LPCWSTR), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], LPCWSTR, "wszDeviceId"), (["in"], INT, "eRole")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility",
                  (["in"], LPCWSTR), (["in"], INT)),
    )


# Roles passed to SetDefaultEndpoint: console / multimedia / communications.
_ROLES = (0, 1, 2)

# COM is managed once per bridge thread rather than re-initialized on every
# call. Tracks only threads where *we* owned the CoInitialize so shutdown_com
# never tears down an apartment some other component (e.g. pywebview) set up.
_com_seen: set[int] = set()      # threads where _ensure_com already ran
_com_owned: set[int] = set()     # subset we must CoUninitialize at shutdown
_com_lock = threading.Lock()


def _ensure_com():
    tid = threading.get_ident()
    with _com_lock:
        if tid in _com_seen:
            return
        _com_seen.add(tid)
        try:
            comtypes.CoInitialize()
            _com_owned.add(tid)
        except OSError:
            # Already initialized on this thread by another component; do not
            # claim ownership so we never uninitialize their apartment.
            pass


def shutdown_com():
    """Uninitializes the COM apartment for the calling thread at shutdown.

    Pairs one-for-one with the _ensure_com call that owned the init, so the
    bridge thread does not leak an apartment across the process lifetime."""
    tid = threading.get_ident()
    with _com_lock:
        _com_seen.discard(tid)
        if tid not in _com_owned:
            return
        _com_owned.discard(tid)
    try:
        comtypes.CoUninitialize()
    except Exception:
        pass


def list_endpoints() -> list[tuple[str, str]]:
    """Returns the active endpoints as (id, friendly_name) tuples (input + output)."""
    _ensure_com()
    from pycaw.pycaw import AudioUtilities

    out = []
    for d in AudioUtilities.GetAllDevices():
        try:
            if d.id and d.FriendlyName and "Active" in str(d.state):
                out.append((d.id, d.FriendlyName))
        except Exception:
            continue
    return out


def find_endpoint_id(name_substr: str) -> str:
    # Reject an empty/blank query: "" is a substring of every name, so it would
    # silently resolve to the first active endpoint (an arbitrary device) instead
    # of signalling that the caller's config field is unset.
    if not name_substr or not name_substr.strip():
        raise ValueError("Audio endpoint name is empty")
    for dev_id, name in list_endpoints():
        if name_substr.lower() in name.lower():
            return dev_id
    raise ValueError(f"Audio endpoint not found: '{name_substr}'")


def get_default(kind: str) -> tuple[str, str]:
    """Returns (id, friendly_name) of the current default device. kind ∈ {'output','input'}."""
    _ensure_com()
    from pycaw.pycaw import AudioUtilities

    dev = AudioUtilities.GetSpeakers() if kind == "output" else AudioUtilities.GetMicrophone()
    dev_id = dev.id if hasattr(dev, "id") else dev.GetId()
    name = next((n for i, n in list_endpoints() if i == dev_id), "")
    return dev_id, name


def _is_capture_endpoint(device_id: str) -> bool:
    """True if device_id is a capture (input) endpoint, so a rollback targets the
    right data flow even before the device becomes the default."""
    try:
        from pycaw.pycaw import AudioUtilities, EDataFlow

        capture = AudioUtilities.GetAllDevices(data_flow=EDataFlow.eCapture.value)
        return any(getattr(d, "id", None) == device_id for d in capture)
    except Exception:
        return False


def _prior_default_for(device_id: str) -> str:
    """Returns the current default of the same data flow (render/capture) as the
    target, so a rollback restores the correct endpoint."""
    kind = "input" if _is_capture_endpoint(device_id) else "output"
    try:
        return get_default(kind)[0]
    except Exception:
        return ""


def set_default(device_id: str):
    """Sets the device as default for all three roles (console / multimedia / communications).

    Best-effort with rollback: if any role fails, the roles already switched are
    reverted to the prior default of the same data flow and the original error is
    re-raised, so a partial switch never leaves the system half-changed (which
    the caller would otherwise persist as the recovery snapshot)."""
    _ensure_com()
    pc = CoCreateInstance(_CLSID_PolicyConfigVistaClient,
                          interface=IPolicyConfigVista, clsctx=CLSCTX_ALL)
    prior = _prior_default_for(device_id)
    applied: list[int] = []
    for role in _ROLES:
        try:
            pc.SetDefaultEndpoint(device_id, role)
            applied.append(role)
        except Exception:
            if prior:
                for done in applied:
                    try:
                        pc.SetDefaultEndpoint(prior, done)
                    except Exception:
                        pass
            raise


def restore(saved: dict):
    """Restores devices previously snapshotted in _pending_default_restore.

    Each data flow is restored independently: if one fails (e.g. the saved output
    endpoint was unplugged mid-session) the other is still attempted, so a single
    transient error can never leave the system default microphone stranded on the
    virtual cable. Any failure is re-raised after both are tried so the caller
    keeps the recovery snapshot and retries on the next stop/launch."""
    errors: list[str] = []
    for role in ("output", "input"):
        dev = saved.get(role)
        if not dev:
            continue
        try:
            set_default(dev)
        except Exception as exc:
            errors.append(f"{role}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
