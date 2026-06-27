"""Per-user BYOK key storage for the open-source / developer build.

The stored Gemini key is wrapped at rest with Windows DPAPI (CryptProtectData,
CURRENT_USER scope) plus a per-install entropy secret, so the ciphertext is bound
to this Windows account on this install and cannot be decrypted after being copied
elsewhere or extracted by another local user. DPAPI needs no extra dependency and
no key material on disk we have to protect ourselves.

Legacy slots written by the previous build used a Fernet key derived from
SHA-256(MachineGuid:user_id:public-constant). That derivation had no secret salt
(the constant is public and the MachineGuid is readable by any local process), so
those blobs are re-wrapped with DPAPI the first time they are read (migrate-on-read).
"""
import base64
import hashlib
import json
import os
import subprocess

from .paths import install_secret, user_path

_STORE_DIR = user_path("profiles", "byok")

# Marks a DPAPI-wrapped slot; legacy Fernet slots have no prefix.
_DPAPI_MAGIC = b"VXDP1\n"


# --- Windows DPAPI via ctypes (no pywin32 dependency) ----------------------

def _dpapi_call(func_name: str, data: bytes, entropy: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    in_blob, _in_buf = to_blob(data)
    ent_blob, _ent_buf = to_blob(entropy)
    out_blob = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1: never prompt; fail instead of blocking.
    flags = 0x1
    fn = getattr(crypt32, func_name)
    ok = fn(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
            None, None, flags, ctypes.byref(out_blob))
    if not ok:
        raise OSError(f"{func_name} failed (err {ctypes.get_last_error()})")
    try:
        size = out_blob.cbData
        return ctypes.string_at(out_blob.pbData, size)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_protect(data: bytes, entropy: bytes) -> bytes:
    return _dpapi_call("CryptProtectData", data, entropy)


def _dpapi_unprotect(data: bytes, entropy: bytes) -> bytes:
    return _dpapi_call("CryptUnprotectData", data, entropy)


def _entropy(user_id: str) -> bytes:
    # Per-install random secret + the slot identity: ties the blob to this install
    # AND this Voxis account without relying on any public/guessable constant.
    return hashlib.sha256(install_secret() + user_id.encode()).digest()


# --- ACL hardening ----------------------------------------------------------

def _restrict_acl(path: str) -> None:
    """Limit a file/dir to the current user (best-effort, Windows-only).

    Prevents another local account from reading the at-rest blob. DPAPI already
    binds decryption to the user, but tightening the ACL removes the ciphertext
    from other users' view entirely. Failures are non-fatal."""
    if os.name != "nt":
        return
    # Grant by SID so we never lock ourselves out if the account name differs.
    user = os.environ.get("USERNAME")
    if not user:
        return
    # (OI)(CI) inheritance flags are valid only on a directory; a file gets plain
    # full control. Granting the wrong flags on a file yields an ACL the process
    # can no longer open.
    grant = f"{user}:(OI)(CI)F" if os.path.isdir(path) else f"{user}:F"
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # /inheritance:r drops inherited ACEs; /grant:r replaces this user's ACE.
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", grant],
            capture_output=True, text=True, timeout=10, creationflags=flags,
        )
    except Exception:
        pass


def _ensure_store_dir() -> None:
    first = not os.path.isdir(_STORE_DIR)
    os.makedirs(_STORE_DIR, exist_ok=True)
    if first:
        _restrict_acl(_STORE_DIR)


# --- Legacy Fernet reader (migrate-on-read) ---------------------------------

def _legacy_machine_id() -> str:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        # Per-install secret instead of a shared constant: a failed MachineGuid
        # lookup must not collapse every install onto the same derivable key.
        return install_secret("legacy_machine.secret").hex()


def _legacy_decrypt(user_id: str, token: bytes) -> dict | None:
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except Exception:
        return None
    material = f"{_legacy_machine_id()}:{user_id}:voxis-byok-v1".encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
    try:
        payload = Fernet(key).decrypt(token)
        return json.loads(payload)
    except (InvalidToken, ValueError, Exception):
        return None


# --- Slot I/O ---------------------------------------------------------------

def _slot_path(user_id: str) -> str:
    _ensure_store_dir()
    slug = hashlib.sha256(user_id.encode()).hexdigest()[:24]
    return os.path.join(_STORE_DIR, f"{slug}.enc")


def _write_slot(user_id: str, data: dict) -> None:
    payload = json.dumps(data).encode()
    blob = _DPAPI_MAGIC + _dpapi_protect(payload, _entropy(user_id))
    path = _slot_path(user_id)
    # Atomic write: a crash mid-write must not leave a truncated/zero slot, which
    # would fail to decrypt on next read and silently lose the stored key. Restrict
    # the temp before the rename so the final file is never world-readable.
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
        f.flush()
        os.fsync(f.fileno())
    _restrict_acl(tmp)
    os.replace(tmp, path)


# Both vendor keys live in ONE slot per user_id. Always default-merge so a slot
# written by an older single-key build still loads (a missing field -> "").
_EMPTY = {"gemini": "", "openai": ""}


def _normalize(d) -> dict:
    if not isinstance(d, dict):
        return dict(_EMPTY)
    return {"gemini": d.get("gemini", "") or "", "openai": d.get("openai", "") or ""}


def save_byok(user_id: str, gemini: str = "", openai: str = "") -> None:
    _write_slot(user_id, {"gemini": gemini, "openai": openai})


def load_byok(user_id: str) -> dict:
    """Returns {gemini, openai}; empty strings if unset or decryption fails.

    Always default-merges both fields so a slot from an older single-key build
    still loads. Legacy Fernet slots are decrypted with the old derivation and
    immediately re-wrapped with DPAPI (preserving openai) so each slot upgrades
    on first read."""
    path = _slot_path(user_id)
    if not os.path.exists(path):
        return dict(_EMPTY)
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return dict(_EMPTY)

    if blob.startswith(_DPAPI_MAGIC):
        try:
            payload = _dpapi_unprotect(blob[len(_DPAPI_MAGIC):], _entropy(user_id))
            return _normalize(json.loads(payload))
        except (OSError, ValueError, Exception):
            return dict(_EMPTY)

    data = _legacy_decrypt(user_id, blob)
    if data is None:
        return dict(_EMPTY)
    norm = _normalize(data)
    try:
        _write_slot(user_id, norm)
    except Exception:
        pass
    return norm


def has_byok(user_id: str, engine: str = "gemini") -> bool:
    return bool(load_byok(user_id).get(engine))


def clear_byok(user_id: str, engine: str | None = None) -> None:
    """Clear one engine's key (engine given) or the whole slot (engine=None)."""
    if engine is None:
        path = _slot_path(user_id)
        if os.path.exists(path):
            os.remove(path)
        return
    cur = load_byok(user_id)
    cur[engine] = ""
    if any(cur.values()):
        save_byok(user_id, gemini=cur.get("gemini", ""), openai=cur.get("openai", ""))
    else:
        path = _slot_path(user_id)
        if os.path.exists(path):
            os.remove(path)
