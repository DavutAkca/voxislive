"""HTTP client for the Voxis auth-core service.

Used by the Python audio engine and the webui bridge:
    * Register      → POST /auth/register
    * Login         → POST /auth/login   (auth-core proxies PocketBase)
    * Verify        → POST /auth/verify
    * Quota         → GET  /auth/quota
    * Usage report  → POST /usage/report (fire-and-forget on session end)
    * Funnel event  → POST /usage/event  (fire-and-forget activation milestones)

Return convention: (result, error_message) tuple - never (None, None).

The JWT is wrapped at rest with Windows DPAPI (CURRENT_USER) and stored under the
user data dir with a current-user-only ACL; a legacy cleartext .env token is
imported once then re-wrapped. User-facing errors stay generic ("could not reach
the server") - URL / proxy / TLS detail is logged locally only.
"""
import hashlib
import logging
import os
import threading
import time
from typing import Optional

import requests

from . import APP_VERSION
from .config import IS_OFFICIAL_RELEASE
from .i18n import t
from .paths import client_channel, user_path

# Distribution channel ("store" | "desktop"), resolved once and sent with each
# usage heartbeat so the backend can attribute minutes by source.
_CLIENT_CHANNEL: str = client_channel()

# Legacy cleartext store, imported once then superseded by the DPAPI token below.
_ENV_PATH: str = user_path(".env")
# DPAPI-wrapped JWT at rest; binary blob with a current-user-only ACL.
_JWT_PATH: str = user_path("jwt.dat")
_BASE_URL: str = os.getenv("VOXIS_API_URL", "https://voxislive.com")
_TIMEOUT: int  = 10
# DPAPI entropy: ties the wrapped token to this Voxis client at rest.
_JWT_ENTROPY: bytes = b"voxis-jwt-v1"

# One shared HTTP session: connection keep-alive means the 6-second usage
# heartbeat reuses its TCP+TLS connection instead of a full handshake per beat
# (less client CPU, less server load, fewer transient failures on weak links).
# requests.Session is thread-safe for our usage (no cookies; urllib3 pools are
# locked internally) — heartbeat workers and UI threads may share it.
_http = requests.Session()

# Proactive JWT refresh via PocketBase's auth-refresh (exposed behind Caddy's
# /dashboard/* -> :8090 route; PB expects the RAW token in Authorization, no
# Bearer prefix). Without this, a token expiring mid-session silently stops
# usage reporting while translation keeps running, and the user is bounced to
# the login form on next launch. Refresh starts inside _REFRESH_MARGIN_S of
# expiry and is throttled; every failure is non-fatal (the current token is
# kept until it genuinely expires — the server stays the authority).
_REFRESH_URL: str = _BASE_URL + "/dashboard/api/collections/users/auth-refresh"
_REFRESH_MARGIN_S: float = 3 * 24 * 3600.0   # start refreshing 3 days early
_REFRESH_THROTTLE_S: float = 3600.0          # at most one attempt per hour
_last_refresh_attempt: float = 0.0

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


def _maybe_refresh_jwt(token: str) -> str:
    """Best-effort proactive token refresh when expiry is near.

    Returns the (possibly renewed) token. Strictly non-destructive: any failure
    — network, 401 from PB, unexpected body — keeps the current token, which
    remains valid until its own exp. Throttled so the rare synchronous HTTP
    call cannot become a per-request tax."""
    global _last_refresh_attempt
    if not IS_OFFICIAL_RELEASE:
        return token
    claims = _jwt_claims(token)
    exp = (claims or {}).get("exp")
    try:
        exp = float(exp)
    except (TypeError, ValueError):
        return token  # no readable exp — nothing to anticipate
    now = time.time()
    if exp - now > _REFRESH_MARGIN_S:
        return token
    if now - _last_refresh_attempt < _REFRESH_THROTTLE_S:
        return token
    _last_refresh_attempt = now
    try:
        # PocketBase auth header is the raw token (no Bearer prefix).
        resp = _http.post(_REFRESH_URL, headers={"Authorization": token},
                          timeout=_TIMEOUT)
        if resp.status_code == 200:
            new_token = (resp.json() or {}).get("token")
            if new_token and new_token != token:
                set_jwt(new_token)
                _log.info("jwt refreshed (exp was %.0f h away)", (exp - now) / 3600)
                return new_token
        else:
            _log_detail("jwt_refresh http %d" % resp.status_code,
                        RuntimeError(resp.text[:200]))
    except (requests.RequestException, ValueError) as exc:
        _log_detail("jwt_refresh", exc)
    return token


def _valid_jwt() -> Optional[str]:
    """Return the stored JWT, proactively clearing it when locally expired.
    Near-expiry tokens are renewed in-line (throttled, best-effort)."""
    token = get_jwt()
    if not token:
        return None
    if _is_expired(token):
        clear_jwt()
        return None
    return _maybe_refresh_jwt(token)


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
        resp = _http.post(
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
        resp = _http.post(
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
        resp = _http.post(
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
        resp = _http.get(
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


def _device_headers() -> dict:
    """Raw device identifiers sent with /auth/session-key so the server can
    enforce one free tier per machine at issuance (it peppers + SHA-256-hashes
    them; raw values are never stored — see device_id). Best-effort: any
    failure returns {} and the server fails open, so this can never block a
    session start."""
    try:
        from . import device_id
        fp = device_id.fingerprint()
    except Exception:
        return {}
    headers = {}
    for name, key in (("X-Voxis-Device-Primary", "primary"),
                      ("X-Voxis-Device-Secondary", "secondary")):
        # Header values must be printable ASCII; fingerprints are in practice,
        # so strip anything else rather than risk the whole request failing.
        v = (fp.get(key) or "").encode("ascii", "ignore").decode()
        v = "".join(ch for ch in v if ch.isprintable()).strip()
        if v:
            headers[name] = v
    return headers


# Capability list a routing-aware client sends with /auth/session-key. The
# "ephemeral" cap (Tier A5, 1.0.38+) tells the server this client can consume a
# single-use, model-locked Gemini auth token instead of the raw master key;
# whether one is actually issued is the server-side gemini_ephemeral setting
# (staged rollout), so sending the cap is always safe.
#
# "cascade" cap (1.0.39+) tells the server this client can RUN the free-tier
# cascade (cloud text + a local voice). The server must never answer an older
# client with engine="cascade": it would not recognise the name, fall back to
# Gemini, and a spent free account would silently get the PAID engine. Sending
# the cap is what makes the free tier safe to enable server-side.
SESSION_KEY_CAPS = "engine-routing,ephemeral,cascade"


def get_session_key(target=None, caps=None, engine=None, mode=None) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[dict], Optional[str], Optional[str], Optional[str]]:
    """SaaS execution path: retrieves a server-issued translation key. With
    caps='engine-routing' the server picks the engine by TARGET language and also
    returns {engine, model, quality, quota} — plus {workspace} on Qwen (DashScope
    keys are workspace-scoped; the id builds the MAAS WS host). Since the server
    verifies the token inline on a cold cache, this single call is a complete
    session start — no separate /auth/verify round-trip is needed.

    With the "ephemeral" cap (SESSION_KEY_CAPS) the Gemini key may instead be a
    single-use auth token ("auth_tokens/…"); the server discriminates via
    key_type ("ephemeral" | "raw"). A response without the field (legacy path,
    Qwen/OpenAI engines) is always a raw key.

    Returns (key, engine, model, quality, quota, workspace, key_type, error);
    `quota` is the license snapshot dict when the server provided one
    (routing-aware responses only). 401/402 are mapped to localized messages by
    STATUS CODE (never by sniffing the server's English error string). Never
    embedded in the client build."""
    if not IS_OFFICIAL_RELEASE:
        return None, None, None, None, None, None, None, "SaaS keys are disabled in developer builds."
    token = _valid_jwt()
    if not token:
        return None, None, None, None, None, None, None, t("st_not_signed_in")
    params = {}
    if caps:
        params["caps"] = caps
    if target:
        params["target"] = target
    if engine:
        # Explicit engine request (beta opt-in). The server honors it only for
        # beta-flagged accounts; anyone else gets normal routing / a refusal.
        params["engine"] = engine
    if mode:
        # The server refuses to cascade a MEETING: the other party would hear a
        # synthetic voice speaking as the user. It cannot infer the mode from the
        # key request, so the client has to say which one it is starting.
        params["mode"] = mode
    try:
        resp = _http.get(
            f"{_BASE_URL}/auth/session-key",
            headers=_auth_headers() | _device_headers(),
            params=params,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        _log_detail("get_session_key", exc)
        return None, None, None, None, None, None, None, _net_error()
    if resp.status_code == 200:
        d = resp.json()
        quota = d.get("quota") if isinstance(d.get("quota"), dict) else None
        if quota is not None and d.get("cascade_daily_minutes") is not None:
            # Carried on the quota snapshot so the free-tier chip can name the
            # real allowance instead of hard-coding a number the server owns.
            quota["cascade_daily_minutes"] = d.get("cascade_daily_minutes")
        return (d.get("key"), d.get("engine", "gemini"), d.get("model"),
                d.get("quality"), quota, d.get("workspace"),
                d.get("key_type") or "raw", None)
    if resp.status_code == 401:
        clear_jwt()
        return None, None, None, None, None, None, None, t("st_not_signed_in")
    if resp.status_code == 402:
        return None, None, None, None, None, None, None, t("err_quota_exhausted")
    if resp.status_code == 503:
        # Distinguishable "engine unavailable" → caller falls back to Gemini.
        try:
            eng = resp.json().get("engine")
        except Exception:
            eng = None
        return None, eng, None, None, None, None, None, "engine unavailable"
    return None, None, None, None, None, None, None, _core_error(resp)


def _post_usage(session_id: str, delta_minutes: float, source: str, engine: str) -> str:
    """One POST /usage/report attempt. Returns "ok" | "quota" | "reauth" | "fail".

    "reauth" is an internal signal for a 401: the server's token cache (5-min TTL)
    expired mid-session, not a real sign-out — /usage/report only reads the cache
    (tc.Get); only /auth/verify repopulates it (tc.Set). The caller re-verifies and
    retries rather than discarding the token.
    """
    try:
        resp = _http.post(
            f"{_BASE_URL}/usage/report",
            headers=_auth_headers(),
            json={
                "session_id":    session_id,
                "delta_minutes": round(delta_minutes, 4),
                "source":        source,
                "client":        _CLIENT_CHANNEL,
                "engine":        engine,
                "app_version":   APP_VERSION,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code == 401:
            return "reauth"
        if resp.status_code == 402:
            return "quota"
        return "ok" if resp.status_code == 204 else "fail"
    except requests.RequestException as exc:
        _log_detail("report_usage", exc)
        return "fail"


def report_usage(session_id: str, delta_minutes: float, source: str, engine: str = "gemini") -> str:
    """Reports session usage to auth-core. Fire-and-forget.

    Returns one of:
      * "ok"    — accepted (HTTP 204), quota still positive.
      * "quota" — accepted but the license is now exhausted (HTTP 402). The
                  caller should stop the running session: the server is not in
                  the audio path, so the mid-session cutoff happens client-side.
      * "fail"  — not reported (disabled build, no token, transport error, or any
                  other non-204/402 status). Best-effort; never raises.

    A 401 is NOT treated as a sign-out: it almost always means the server's
    5-minute token cache expired mid-session (only /auth/verify refreshes it, and
    the client verifies just once at session start). We re-verify to repopulate
    that cache and retry the report once; the JWT is cleared only if the re-verify
    itself returns a genuine 401 (verify_session handles that). Without this, every
    session longer than the cache TTL silently stopped billing and signed the user
    out.
    """
    if not IS_OFFICIAL_RELEASE:
        return "fail"
    token = _valid_jwt()
    if not token or delta_minutes <= 0:
        return "fail"
    status = _post_usage(session_id, delta_minutes, source, engine)
    if status == "reauth":
        info, _ = verify_session()
        if info is None:
            # verify_session already cleared the JWT on a real 401; any other
            # failure (network, 402) just means this report is best-effort lost.
            return "fail"
        status = _post_usage(session_id, delta_minutes, source, engine)
        if status == "reauth":
            return "fail"
    return status


def report_usage_async(session_id: str, delta_minutes: float, source: str,
                       engine: str = "gemini", on_quota_exceeded=None):
    """Fire the usage report on a worker thread. When the server signals quota
    exhaustion (402) and on_quota_exceeded is provided, invoke it so the caller
    can tear the session down. The callback runs on this worker thread."""
    def _run():
        status = report_usage(session_id, delta_minutes, source, engine)
        if status == "quota" and on_quota_exceeded is not None:
            try:
                on_quota_exceeded()
            except Exception:
                pass

    threading.Thread(
        target=_run,
        daemon=True,
        name="voxis-usage-report",
    ).start()


def _post_event(event: str, session_id: Optional[str], meta: Optional[dict]) -> bool:
    """One POST /usage/event attempt. Best-effort; True on 2xx/204."""
    try:
        resp = _http.post(
            f"{_BASE_URL}/usage/event",
            headers=_auth_headers(),
            json={
                "event":       event,
                "session_id":  session_id or "",
                "client":      _CLIENT_CHANNEL,
                "app_version": APP_VERSION,
                "meta":        meta or {},
            },
            timeout=_TIMEOUT,
        )
        return resp.status_code in (200, 201, 204)
    except requests.RequestException as exc:
        _log_detail("report_event", exc)
        return False


def report_event(event: str, session_id: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Report a lightweight activation-funnel milestone (app_launched,
    session_start / session_live / session_error, capture_lost) to auth-core.
    Fire-and-forget; never raises.

    Hard-gated off on the OSS/BYOK build (no network off-box), mirroring
    report_usage / send_report so the "zero telemetry" claim holds. Carries NO
    transcript, audio or PII — only the milestone name, a correlation session id,
    the client channel, app version, and a small non-sensitive meta dict (mode,
    engine, capture backend, coarse error class). A missing JWT is a no-op: the
    funnel is per-user, attributed server-side from the bearer token.
    """
    if not IS_OFFICIAL_RELEASE:
        return
    if not _valid_jwt():
        return
    _post_event(event, session_id, meta)


def report_event_async(event: str, session_id: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Fire report_event on a daemon worker so the audio/UI thread never blocks on
    the network (report_event may refresh the JWT). Instant no-op on OSS."""
    if not IS_OFFICIAL_RELEASE:
        return
    threading.Thread(
        target=report_event, args=(event, session_id, meta),
        daemon=True, name="voxis-event",
    ).start()


def _device_hash() -> str:
    """Stable, one-way per-device id for anonymous app_opened dedup. Hashes the
    raw hardware fingerprint so no raw identifier ever leaves the device — the
    server only sees an opaque hash, preserving the 'no PII' guarantee."""
    try:
        from . import device_id
        fp = device_id.fingerprint()
    except Exception:
        return ""
    parts = "|".join(f"{k}={fp[k]}" for k in sorted(fp) if fp.get(k))
    if not parts:
        return ""
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


def report_app_opened() -> None:
    """Anonymous top-of-funnel milestone, fired on every app open BEFORE login.
    app_launched only fires once a user is authenticated, so it cannot see people
    who open the app and never sign in — the biggest suspected activation leak.
    No JWT required: the server attributes this by a hashed device id, not a user
    (a bearer is still sent when one exists, linking the open to the account).
    Hard-gated off on the OSS/BYOK build (zero telemetry). Carries no PII —
    only a one-way device hash."""
    if not IS_OFFICIAL_RELEASE:
        return
    _post_event("app_opened", None, {"device": _device_hash()})


def report_app_opened_async() -> None:
    """Fire report_app_opened on a daemon worker — the device fingerprint runs
    blocking WMI queries, so it must never touch the UI thread. No-op on OSS."""
    if not IS_OFFICIAL_RELEASE:
        return
    threading.Thread(target=report_app_opened, daemon=True, name="voxis-open").start()


def send_report(payload: dict) -> dict:
    """Submit a user-initiated problem report to POST /report.

    Returns a result dict:
      * {"ok": True, "ticket": str, "deduped": bool}     - stored (201)
      * {"ok": False, "retryable": True}                 - network/5xx; caller may queue
      * {"ok": False, "retryable": False, "error": str}  - rejected (400/413/429/disabled)

    Anonymous is allowed; a valid JWT, when present, only attributes the report to
    a user. Hard-gated off on the OSS/BYOK build (no network off-box), mirroring
    report_usage so the "sends no usage data" claim holds.
    """
    if not IS_OFFICIAL_RELEASE:
        return {"ok": False, "retryable": False, "error": "disabled"}
    headers = {"Content-Type": "application/json"}
    token = _valid_jwt()
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        resp = _http.post(
            f"{_BASE_URL}/report",
            headers=headers,
            json=payload,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        _log_detail("send_report", exc)
        return {"ok": False, "retryable": True}
    if resp.status_code == 201:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return {"ok": True, "ticket": data.get("ticket", ""), "deduped": bool(data.get("deduped"))}
    if resp.status_code in (400, 413, 429):
        return {"ok": False, "retryable": False, "error": "http_%d" % resp.status_code}
    # 5xx or any other unexpected status: transient, let the caller queue + retry.
    return {"ok": False, "retryable": True}


_load_stored_jwt()
