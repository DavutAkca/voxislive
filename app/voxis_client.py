"""HTTP client for the Voxis auth-core service.

Used by the Python audio engine and the webui bridge:
    * Register      → POST /auth/register
    * Login         → POST /auth/login   (auth-core proxies PocketBase)
    * Verify        → POST /auth/verify
    * Quota         → GET  /auth/quota
    * Usage report  → POST /usage/report (fire-and-forget on session end)

Return convention: (result, error_message) tuple - never (None, None).

The JWT is wrapped at rest with Windows DPAPI (CURRENT_USER) and stored under the
user data dir with a current-user-only ACL; a legacy cleartext .env token is
imported once then re-wrapped. User-facing errors stay generic ("could not reach
the server") - URL / proxy / TLS detail is logged locally only.
"""
import logging
import os
import threading
import time
from typing import Optional

import requests

from .config import IS_OFFICIAL_RELEASE
from .i18n import t
from .paths import user_path

# Legacy cleartext store, imported once then superseded by the DPAPI token below.
_ENV_PATH: str = user_path(".env")
# DPAPI-wrapped JWT at rest; binary blob with a current-user-only ACL.
_JWT_PATH: str = user_path("jwt.dat")
_BASE_URL: str = os.getenv("VOXIS_API_URL", "https://voxislive.com")
_TIMEOUT: int  = 10
# DPAPI entropy: ties the wrapped token to this Voxis client at rest.
_JWT_ENTROPY: bytes = b"voxis-jwt-v1"

_log = logging.getLogger("voxis.client")
_lock: threading.Lock = threading.Lock()
_jwt: Optional[str] = None


def _logfile() -> str:
    return user_path("voxis.log")


def _log_detail(where: str, exc: Exception) -> None:
    """Record full network/error detail locally so the UI message can stay
    generic (no URL/proxy/TLS leakage into the user-facing string)."""
    _log.warning("%s: %s", where, exc)
    try:
        import datetime
        ts = datetime.datetime.now().isoformat()
        with open(_logfile(), "a", encoding="utf-8") as f:
            f.write(ts + " client " + where + ": " + repr(exc) + chr(10))
    except OSError:
        pass


def _dpapi_call(func_name: str, data: bytes) -> bytes:
    """CryptProtectData / CryptUnprotectData via ctypes (no pywin32 dependency)."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    in_blob, _in = to_blob(data)
    ent_blob, _ent = to_blob(_JWT_ENTROPY)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    fn = getattr(crypt32, func_name)
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1: never prompt; fail instead.
    if not fn(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
              None, None, 0x1, ctypes.byref(out_blob)):
        raise OSError(func_name + " failed (err " + str(ctypes.get_last_error()) + ")")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _restrict_acl(path: str) -> None:
    """Tighten a file to the current user only (best-effort, Windows-only)."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", user + ":F"],
            capture_output=True, text=True, timeout=10, creationflags=flags,
        )
    except Exception:
        pass


def _read_env_jwt() -> Optional[str]:
    try:
        with open(_ENV_PATH, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("VOXIS_JWT_TOKEN="):
                    token = line.strip().split("=", 1)[1].strip()
                    return token or None
    except OSError:
        pass
    return None


def _store_jwt(token: str) -> None:
    """Persist the JWT via DPAPI with a current-user-only ACL. Falls back to a
    tightened cleartext file only if DPAPI is unavailable (non-Windows)."""
    if not token:
        for fp in (_JWT_PATH, _ENV_PATH):
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except OSError:
                pass
        return
    try:
        blob = _dpapi_call("CryptProtectData", token.encode())
        with open(_JWT_PATH, "wb") as f:
            f.write(blob)
        _restrict_acl(_JWT_PATH)
        # Drop any superseded cleartext token left by an older build.
        if os.path.exists(_ENV_PATH):
            _write_env("VOXIS_JWT_TOKEN", "")
    except Exception as exc:
        _log_detail("store_jwt dpapi", exc)
        _write_env("VOXIS_JWT_TOKEN", token)
        _restrict_acl(_ENV_PATH)


def _load_stored_jwt():
    """Load the JWT: prefer the DPAPI blob; import a legacy .env token once and
    re-wrap it, so a previously cleartext token upgrades on first run."""
    global _jwt
    try:
        if os.path.exists(_JWT_PATH):
            with open(_JWT_PATH, "rb") as f:
                blob = f.read()
            _jwt = _dpapi_call("CryptUnprotectData", blob).decode()
            return
    except Exception as exc:
        _log_detail("load_jwt dpapi", exc)
    legacy = _read_env_jwt()
    if legacy:
        _jwt = legacy
        _store_jwt(legacy)


def set_jwt(token: str):
    global _jwt
    with _lock:
        _jwt = token
    _store_jwt(token)


def get_jwt() -> Optional[str]:
    with _lock:
        return _jwt


def clear_jwt():
    global _jwt
    with _lock:
        _jwt = None
    _store_jwt("")


def _write_env(key: str, value: str):
    env_path = _ENV_PATH
    lines: list[str] = []
    try:
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    for i, ln in enumerate(lines):
        if ln.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _jwt_claims(token: str) -> Optional[dict]:
    import base64
    import json
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None


def _is_expired(token: str) -> bool:
    """Local exp check (clients only read the claim; the signature is verified
    server-side). A missing/garbled exp is treated as not-yet-expired so the
    server still makes the final call rather than us discarding a usable token."""
    claims = _jwt_claims(token)
    if not claims:
        return False
    exp = claims.get("exp")
    try:
        # 30 s skew: refresh slightly early instead of sending a dead token.
        return bool(exp) and time.time() >= float(exp) - 30
    except (TypeError, ValueError):
        return False


def _valid_jwt() -> Optional[str]:
    """Return the stored JWT, proactively clearing it when locally expired."""
    token = get_jwt()
    if not token:
        return None
    if _is_expired(token):
        clear_jwt()
        return None
    return token


def _auth_headers() -> dict:
    return {"Authorization": "Bearer " + (get_jwt() or ""), "Content-Type": "application/json"}


def user_id_from_jwt() -> Optional[str]:
    """Decodes the user id from the JWT payload locally.

    Used to index the BYOK store independently of license/quota state — the
    server has already verified the signature, so a local read is safe.
    """
    token = get_jwt()
    if not token:
        return None
    claims = _jwt_claims(token)
    return (claims.get("id") if claims else None) or None


def _net_error() -> str:
    """Generic, localized 'could not reach the server' message. Detail is logged
    locally — never embed the base URL, proxy, or TLS internals in the UI text.

    Uses the i18n table when the key exists; otherwise a built-in fallback keyed
    off the active UI language so we never leak the raw key name into the UI.
    (Maintainer: promote these strings into app/i18n.py STRINGS as
    'st_server_unreachable'.)"""
    import app.i18n as _i18n
    if "st_server_unreachable" in _i18n.STRINGS.get(_i18n._current, {}):
        return t("st_server_unreachable")
    fallback = {
        "tr": "Sunucuya ulaşılamadı. İnternet bağlantını kontrol et.",
        "en": "Could not reach the server. Check your connection.",
    }
    return fallback.get(_i18n._current, fallback["en"])


def _core_error(resp: requests.Response) -> str:
    """Server-supplied message for a non-2xx (already sanitized by auth-core);
    falls back to a status code, never a transport-level detail."""
    try:
        body = resp.json()
        return body.get("error") or body.get("message") or f"HTTP {resp.status_code}"
    except Exception:
        return f"HTTP {resp.status_code}"


def auth_register(email: str, password: str, name: str = "") -> tuple[Optional[str], Optional[str]]:
    """Registers via auth-core (creates PocketBase user, Stripe customer, free license).

    Returns (jwt_token, None) on success or (None, error_message)."""
    if not IS_OFFICIAL_RELEASE:
        return None, "Registration is disabled in developer builds."
    try:
        from . import device_id
        device = device_id.fingerprint()
    except Exception:
        device = {"primary": "", "secondary": ""}
    try:
        resp = requests.post(
            f"{_BASE_URL}/auth/register",
            json={
                "email": email, "password": password, "name": name,
                "device_primary": device.get("primary", ""),
                "device_secondary": device.get("secondary", ""),
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        _log_detail("auth_register", exc)
        return None, _net_error()

    if resp.status_code == 201:
        token = resp.json().get("token")
        if token:
            set_jwt(token)
        return token, None

    return None, _core_error(resp)


def pb_login(email: str, password: str) -> tuple[Optional[str], Optional[str]]:
    """Logs in via auth-core (PocketBase proxy). Returns (jwt_token, error)."""
    if not IS_OFFICIAL_RELEASE:
        return None, "Login is disabled in developer builds."
    try:
        resp = requests.post(
            f"{_BASE_URL}/auth/login",
            json={"email": email, "password": password},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        _log_detail("pb_login", exc)
        return None, _net_error()

    if resp.ok:
        token = resp.json().get("token")
        if token:
            set_jwt(token)
        return token, None

    return None, _core_error(resp)


def verify_session() -> tuple[Optional[dict], Optional[str]]:
    """Verifies the stored JWT against auth-core and returns (quota_dict, error).

    Returns (dict, None) on success. Returns (None, message) on every failure —
    callers receive the exact server-side reason rather than a generic None so
    they can surface actionable messages to the user.
    """
    if not IS_OFFICIAL_RELEASE:
        return {"unlimited": True, "remaining": 999999.0, "allowed_minutes": 999999.0, "used_minutes": 0.0}, None
    token = _valid_jwt()
    if not token:
        return None, t("st_not_signed_in")
    try:
        resp = requests.post(
            f"{_BASE_URL}/auth/verify",
            headers=_auth_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json(), None
        if resp.status_code == 401:
            clear_jwt()
            return None, t("st_not_signed_in")
        # 402 quota exceeded, 403 no license, 502 PB unavailable, etc.
        return None, _core_error(resp)
    except requests.RequestException as exc:
        _log_detail("verify_session", exc)
        return None, _net_error()


def get_quota() -> Optional[dict]:
    """Fetches the cached quota (verify_session must have been called this session)."""
    if not IS_OFFICIAL_RELEASE:
        return {"unlimited": True, "remaining": 999999.0, "allowed_minutes": 999999.0, "used_minutes": 0.0}
    token = _valid_jwt()
    if not token:
        return None
    try:
        resp = requests.get(
            f"{_BASE_URL}/auth/quota",
            headers=_auth_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        # 401 on quota is not treated as a definitive sign-out: the session-key
        # endpoint is the authoritative auth check and will clear the JWT if needed.
        return None
    except requests.RequestException as exc:
        _log_detail("get_quota", exc)
        return None


def get_session_key() -> tuple[Optional[str], Optional[str]]:
    """SaaS execution path: retrieves the server-side Gemini key. Never embedded
    in the client build."""
    if not IS_OFFICIAL_RELEASE:
        return None, "SaaS keys are disabled in developer builds."
    token = _valid_jwt()
    if not token:
        return None, t("st_not_signed_in")
    try:
        resp = requests.get(
            f"{_BASE_URL}/auth/session-key",
            headers=_auth_headers(),
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        _log_detail("get_session_key", exc)
        return None, _net_error()
    if resp.status_code == 200:
        return resp.json().get("key"), None
    if resp.status_code == 401:
        clear_jwt()
    return None, _core_error(resp)


def report_usage(session_id: str, delta_minutes: float, source: str) -> bool:
    """Reports session usage to auth-core. Fire-and-forget."""
    if not IS_OFFICIAL_RELEASE:
        return False
    token = _valid_jwt()
    if not token or delta_minutes <= 0:
        return False
    try:
        resp = requests.post(
            f"{_BASE_URL}/usage/report",
            headers=_auth_headers(),
            json={
                "session_id":    session_id,
                "delta_minutes": round(delta_minutes, 4),
                "source":        source,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 401:
            clear_jwt()
        return resp.status_code == 204
    except requests.RequestException as exc:
        _log_detail("report_usage", exc)
        return False


def report_usage_async(session_id: str, delta_minutes: float, source: str):
    t = threading.Thread(
        target=report_usage,
        args=(session_id, delta_minutes, source),
        daemon=True,
        name="voxis-usage-report",
    )
    t.start()


_load_stored_jwt()
