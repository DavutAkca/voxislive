"""voxis_client: local JWT claim/expiry logic and the proactive refresh path
(all network + disk I/O mocked)."""
import base64
import json
import time

import pytest

import app.voxis_client as vc


def _make_jwt(exp=None, **claims):
    if exp is not None:
        claims["exp"] = exp
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).rstrip(b"=")
    return (header + b"." + payload + b".sig").decode()


def test_jwt_claims_roundtrip():
    tok = _make_jwt(exp=123, id="user1")
    claims = vc._jwt_claims(tok)
    assert claims["id"] == "user1" and claims["exp"] == 123
    assert vc._jwt_claims("garbage") is None


def test_is_expired_boundaries():
    assert vc._is_expired(_make_jwt(exp=time.time() - 10)) is True
    assert vc._is_expired(_make_jwt(exp=time.time() + 3600)) is False
    # Missing/garbled exp → treated as not-expired (server decides).
    assert vc._is_expired(_make_jwt(id="x")) is False
    assert vc._is_expired("garbage") is False


class _FakeResp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


@pytest.fixture
def refresh_env(monkeypatch):
    """Official build + no throttle + captured set_jwt + fake HTTP."""
    calls = {"posts": [], "stored": []}
    monkeypatch.setattr(vc, "IS_OFFICIAL_RELEASE", True)
    monkeypatch.setattr(vc, "_last_refresh_attempt", 0.0)
    monkeypatch.setattr(vc, "set_jwt", lambda tok: calls["stored"].append(tok))

    class _FakeHttp:
        def post(self, url, headers=None, timeout=None, **kw):
            calls["posts"].append((url, headers))
            return calls["resp"]

    monkeypatch.setattr(vc, "_http", _FakeHttp())
    return calls


def test_refresh_skipped_when_expiry_far(refresh_env):
    tok = _make_jwt(exp=time.time() + 30 * 24 * 3600)  # 30 days out
    assert vc._maybe_refresh_jwt(tok) == tok
    assert refresh_env["posts"] == []


def test_refresh_renews_near_expiry(refresh_env):
    old = _make_jwt(exp=time.time() + 3600, id="u")  # inside the 3-day margin
    new = _make_jwt(exp=time.time() + 14 * 24 * 3600, id="u")
    refresh_env["resp"] = _FakeResp(200, {"token": new})
    out = vc._maybe_refresh_jwt(old)
    assert out == new
    assert refresh_env["stored"] == [new]
    url, headers = refresh_env["posts"][0]
    assert url.endswith("/dashboard/api/collections/users/auth-refresh")
    # PocketBase expects the RAW token — no Bearer prefix.
    assert headers["Authorization"] == old


def test_refresh_failure_keeps_current_token(refresh_env):
    old = _make_jwt(exp=time.time() + 3600)
    refresh_env["resp"] = _FakeResp(401, {}, text="unauthorized")
    assert vc._maybe_refresh_jwt(old) == old
    assert refresh_env["stored"] == []


def test_refresh_throttled_after_attempt(refresh_env, monkeypatch):
    old = _make_jwt(exp=time.time() + 3600)
    refresh_env["resp"] = _FakeResp(500)
    vc._maybe_refresh_jwt(old)
    assert len(refresh_env["posts"]) == 1
    # Second call inside the throttle window must not hit the network again.
    vc._maybe_refresh_jwt(old)
    assert len(refresh_env["posts"]) == 1


def test_refresh_disabled_on_oss_build(refresh_env, monkeypatch):
    monkeypatch.setattr(vc, "IS_OFFICIAL_RELEASE", False)
    old = _make_jwt(exp=time.time() + 60)
    assert vc._maybe_refresh_jwt(old) == old
    assert refresh_env["posts"] == []


# --- Device headers on /auth/session-key (one-free-tier-per-device) ---


def test_device_headers_sanitized(monkeypatch):
    import app.device_id as device_id
    monkeypatch.setattr(
        device_id, "fingerprint",
        lambda: {"primary": " guid-1 ", "secondary": "board|üuid\x01"})
    h = vc._device_headers()
    # Whitespace stripped; non-ASCII and non-printable bytes dropped so the
    # header can never make the HTTP request itself fail.
    assert h["X-Voxis-Device-Primary"] == "guid-1"
    assert h["X-Voxis-Device-Secondary"] == "board|uid"


def test_device_headers_omit_empty_components(monkeypatch):
    import app.device_id as device_id
    monkeypatch.setattr(
        device_id, "fingerprint", lambda: {"primary": "", "secondary": "hw"})
    h = vc._device_headers()
    assert "X-Voxis-Device-Primary" not in h
    assert h["X-Voxis-Device-Secondary"] == "hw"


def test_device_headers_fail_open(monkeypatch):
    import app.device_id as device_id

    def boom():
        raise RuntimeError("wmi unavailable")

    monkeypatch.setattr(device_id, "fingerprint", boom)
    assert vc._device_headers() == {}


def test_session_key_sends_device_headers(monkeypatch):
    monkeypatch.setattr(vc, "IS_OFFICIAL_RELEASE", True)
    monkeypatch.setattr(vc, "_valid_jwt", lambda: _make_jwt(exp=time.time() + 3600))
    monkeypatch.setattr(vc, "_device_headers", lambda: {"X-Voxis-Device-Primary": "g"})
    seen = {}

    class _FakeHttp:
        def get(self, url, headers=None, params=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            return _FakeResp(200, {"key": "k", "engine": "gemini"})

    monkeypatch.setattr(vc, "_http", _FakeHttp())
    key, engine, *_rest, err = vc.get_session_key(target="cs", caps="engine-routing")
    assert (key, engine, err) == ("k", "gemini", None)
    assert seen["url"].endswith("/auth/session-key")
    # Auth header intact, device fingerprint riding alongside it.
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    assert seen["headers"]["X-Voxis-Device-Primary"] == "g"
