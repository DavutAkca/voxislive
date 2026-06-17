"""Best-effort device fingerprint for free-tier abuse detection (Phase 1).

Collects two independent, stable hardware identifiers and returns them raw. The
server peppers + SHA-256-hashes them and stores only the hash (raw values are
never persisted).

This is one weak signal, not DRM or an entitlement check: a determined user can
change every value here. The goal is only to flag the common "new email = fresh
free tier on the same machine" pattern. Real abuse gating MUST live server-side
(rate limits, payment, account review) — never trust this fingerprint alone. All
collection degrades to an empty component on any error, never an exception.
"""

# Placeholder / OEM-default values that are not unique to a machine.
_BAD = {
    "", "0", "none", "default string", "to be filled by o.e.m.",
    "system serial number", "n/a", "not applicable", "invalid",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "00000000-0000-0000-0000-000000000000",
}

# Cache: the values never change within a process and collection is non-trivial.
_cache: dict | None = None


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() in _BAD else v


def _machine_guid() -> str:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        return ""


def _wmi_props() -> dict:
    """Read baseboard serial + system UUID over WMI via the COM SWbemLocator.

    Using the in-process WMI provider avoids spawning powershell.exe twice per
    call: that double spawn cost multiple seconds, depended on powershell being
    on PATH, and looked like script-execution behavior to some AV heuristics.
    comtypes is already bundled, so this needs no extra dependency. COM is
    initialized for this call and uninitialized afterward so we never alter the
    apartment state of the caller's thread.
    """
    out = {"board": "", "uuid": ""}
    try:
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            import comtypes.client
            locator = comtypes.client.CreateObject("WbemScripting.SWbemLocator")
            svc = locator.ConnectServer(".", r"root\cimv2")

            def first(wql: str, prop: str) -> str:
                for obj in svc.ExecQuery(wql):
                    val = getattr(obj, prop, None)
                    if val:
                        return str(val)
                return ""

            out["board"] = first("SELECT SerialNumber FROM Win32_BaseBoard", "SerialNumber")
            out["uuid"] = first("SELECT UUID FROM Win32_ComputerSystemProduct", "UUID")
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass
    return out


def _registry_hw() -> dict:
    """SMBIOS values from the registry — a powershell-free fallback when WMI is
    unavailable. The registry exposes product/manufacturer (not the board serial),
    which is weaker but still machine-stable."""
    out = {"board": "", "uuid": ""}
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS"
        ) as k:
            def rv(name: str) -> str:
                try:
                    return str(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    return ""
            man = rv("SystemManufacturer")
            prod = rv("SystemProductName")
            out["board"] = "|".join(p for p in (man, prod) if p)
    except Exception:
        pass
    return out


def fingerprint() -> dict:
    """Return {'primary', 'secondary'} raw identifiers (either may be empty).

    primary   = Windows MachineGuid (survives reboots; changes on OS reinstall).
    secondary = baseboard serial + system UUID (survive OS reinstall; very stable).
    Two independent components let the server tolerate a partial hardware change.
    """
    global _cache
    if _cache is not None:
        return dict(_cache)

    primary = _clean(_machine_guid())
    hw = _wmi_props()
    if not (hw["board"] or hw["uuid"]):
        hw = _registry_hw()
    board = _clean(hw.get("board", ""))
    uuid = _clean(hw.get("uuid", ""))
    secondary = "|".join(p for p in (board, uuid) if p)

    _cache = {"primary": primary, "secondary": secondary}
    return dict(_cache)
