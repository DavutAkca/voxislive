"""Tier A5 — single-use ephemeral Gemini session tokens (client leg, 1.0.38).

The server may answer /auth/session-key with a model-locked, uses:1 auth token
("auth_tokens/…") instead of the raw master key. These pin the client behaviors
that make that shippable and that a refactor could silently undo:

  * the token prefix is the ephemeral/raw discriminator (config.is_ephemeral_key);
  * LiveTranslator spends the initial token on the FIRST connect only, then
    fetches a fresh key via key_provider for every reconnect/rotation — on the
    v1alpha API surface for tokens, the default surface for raw keys;
  * a raw-key session is byte-identical to the old path (one cached client,
    provider never called) — the rollout flag can stay off with zero drift;
  * the prefetch cache never stores an ephemeral token (its 2-min new-session
    window is shorter than the cache TTL; a dead single-use token fails the
    first connect TERMINALLY).

No network anywhere: genai.Client is faked, HTTP is faked.
"""
import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest

import app.translator as gem
import app.voxis_client as vc
import app.webui as webui
from app.config import is_ephemeral_key
from app.engines import make_translator


def test_is_ephemeral_key_discriminates_by_resource_name():
    assert is_ephemeral_key("auth_tokens/abc123")
    assert not is_ephemeral_key("AIzaSyRawKey")
    assert not is_ephemeral_key("")
    assert not is_ephemeral_key(None)


def test_session_key_caps_include_ephemeral():
    # The cap is what tells the server this client can consume uses:1 tokens;
    # dropping it would silently freeze the rollout on raw keys.
    caps = set(vc.SESSION_KEY_CAPS.split(","))
    assert {"engine-routing", "ephemeral"} <= caps


# --- get_session_key: key_type parsing ---------------------------------------


class _FakeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _session_key_env(monkeypatch, body):
    monkeypatch.setattr(vc, "IS_OFFICIAL_RELEASE", True)
    monkeypatch.setattr(vc, "_valid_jwt", lambda: "tok")
    monkeypatch.setattr(vc, "_device_headers", lambda: {})
    seen = {}

    class _FakeHttp:
        def get(self, url, headers=None, params=None, timeout=None):
            seen["params"] = params
            return _FakeResp(200, body)

    monkeypatch.setattr(vc, "_http", _FakeHttp())
    return seen


def test_session_key_returns_key_type_ephemeral(monkeypatch):
    seen = _session_key_env(monkeypatch, {
        "key": "auth_tokens/t1", "engine": "gemini", "key_type": "ephemeral"})
    key, engine, *_mid, key_type, err = vc.get_session_key(
        target="el", caps=vc.SESSION_KEY_CAPS)
    assert (key, engine, key_type, err) == ("auth_tokens/t1", "gemini", "ephemeral", None)
    # The caps list grows as the client learns new tricks (cascade landed in
    # 1.0.39). What must never regress is that each capability is actually SENT:
    # the server gates every staged feature on the client claiming it, and a
    # dropped cap silently downgrades the user — an ephemeral-capable client
    # would be handed the raw master key, a cascade-capable one the paywall.
    caps = seen["params"]["caps"].split(",")
    assert "engine-routing" in caps
    assert "ephemeral" in caps
    assert "cascade-wall" in caps


def test_session_key_defaults_key_type_raw(monkeypatch):
    # Legacy/no-flag responses carry no key_type field → raw.
    _session_key_env(monkeypatch, {"key": "k", "engine": "gemini"})
    key, _engine, *_mid, key_type, err = vc.get_session_key()
    assert (key, key_type, err) == ("k", "raw", None)


# --- LiveTranslator: per-connect key lifecycle --------------------------------


class _FakeClient:
    """Stands in for genai.Client: records construction, hands out an async-CM
    connection whose object carries the api_key it was opened with."""

    instances: list = []

    def __init__(self, api_key=None, http_options=None):
        self.api_key = api_key
        self.http_options = http_options
        _FakeClient.instances.append(self)
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self._connect))

    def _connect(self, model=None, config=None):
        @contextlib.asynccontextmanager
        async def cm():
            yield SimpleNamespace(api_key=self.api_key)
        return cm()


@pytest.fixture
def fake_genai(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(gem.genai, "Client", _FakeClient)
    return _FakeClient


def _connect_once(tr):
    async def go():
        async with tr._session_cm() as conn:
            return conn
    return asyncio.run(go())


def _make(key, provider=None):
    return gem.LiveTranslator(key, "en", on_audio=lambda *a: None,
                              on_text=lambda *a: None, on_status=lambda *a: None,
                              key_provider=provider)


def test_raw_key_keeps_cached_client_and_never_calls_provider(fake_genai):
    calls = []
    tr = _make("AIzaSyRaw", provider=lambda: calls.append(1) or "auth_tokens/x")
    c1 = _connect_once(tr)
    c2 = _connect_once(tr)
    assert (c1.api_key, c2.api_key) == ("AIzaSyRaw", "AIzaSyRaw")
    assert len(fake_genai.instances) == 1          # one client, cached (old path)
    assert fake_genai.instances[0].http_options.api_version is None
    assert calls == []                             # provider untouched on raw keys


def test_ephemeral_key_spent_once_then_provider_feeds_reconnects(fake_genai):
    fetched = []

    def provider():
        fetched.append(1)
        return f"auth_tokens/next{len(fetched)}"

    tr = _make("auth_tokens/first", provider=provider)
    c1 = _connect_once(tr)
    assert c1.api_key == "auth_tokens/first"       # initial token serves connect #1
    assert fetched == []                           # no extra RTT at session start
    c2 = _connect_once(tr)
    c3 = _connect_once(tr)
    assert (c2.api_key, c3.api_key) == ("auth_tokens/next1", "auth_tokens/next2")
    # uses:1 → a fresh client per connect, always on the v1alpha token surface,
    # with the deflate-off transport setting preserved.
    assert len(fake_genai.instances) == 3
    for cl in fake_genai.instances:
        assert cl.http_options.api_version == "v1alpha"
        assert cl.http_options.async_client_args == {"compression": None}


def test_failed_connect_attempt_still_spends_the_token(monkeypatch):
    """A handshake that dies mid-dial may have consumed the uses:1 token — the
    retry must fetch a fresh one, never re-dial with the maybe-dead token."""
    fetched = []
    attempts = {"n": 0}

    class _FlakyClient(_FakeClient):
        def _connect(self, model=None, config=None):
            attempts["n"] += 1
            first_dial = attempts["n"] == 1

            @contextlib.asynccontextmanager
            async def cm():
                if first_dial:
                    raise ConnectionError("dropped mid-handshake")
                yield SimpleNamespace(api_key=self.api_key)
            return cm()

    _FakeClient.instances = []
    monkeypatch.setattr(gem.genai, "Client", _FlakyClient)
    tr = _make("auth_tokens/first",
               provider=lambda: fetched.append(1) or "auth_tokens/fresh")
    with pytest.raises(ConnectionError):
        _connect_once(tr)
    c2 = _connect_once(tr)
    assert fetched == [1]                          # retry went through the fountain
    assert c2.api_key == "auth_tokens/fresh"


def test_provider_falling_back_to_raw_settles_on_cached_client(fake_genai):
    """Rollout flag turned off mid-session: the provider starts answering with a
    raw key — later rotations must settle back onto the cached-client path."""
    tr = _make("auth_tokens/first", provider=lambda: "AIzaSyRawAgain")
    _connect_once(tr)                               # spends the token
    c2 = _connect_once(tr)                          # provider → raw key
    assert c2.api_key == "AIzaSyRawAgain"
    assert fake_genai.instances[-1].http_options.api_version is None
    n = len(fake_genai.instances)
    c3 = _connect_once(tr)                          # raw now → cached client
    assert c3.api_key == "AIzaSyRawAgain"
    assert len(fake_genai.instances) == n + 1       # built once, then cached
    _connect_once(tr)
    assert len(fake_genai.instances) == n + 1


def test_ephemeral_without_provider_fails_closed_after_first_connect(fake_genai):
    tr = _make("auth_tokens/only")
    _connect_once(tr)
    with pytest.raises(RuntimeError, match="key provider"):
        _connect_once(tr)


def test_make_translator_threads_provider_into_gemini():
    provider = lambda: "auth_tokens/x"
    tr = make_translator(
        {}, "en", engine="gemini", key="auth_tokens/first",
        on_audio=lambda *a: None, on_text=lambda *a: None,
        on_status=lambda *a: None, name="t", key_provider=provider)
    assert tr.key_provider is provider


# --- webui prefetch: ephemeral tokens are never cached -------------------------


class _SyncThread:
    """Runs the prefetch worker inline so the test is deterministic."""

    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def _bare_bridge():
    b = object.__new__(webui.Bridge)
    b.cfg = {"target_language_incoming": "el"}
    b._key_cache = {}
    b._key_cache_lock = threading.Lock()
    b._last_quota = None
    return b


def _prefetch_with(monkeypatch, response):
    monkeypatch.setattr(webui, "IS_OFFICIAL_RELEASE", True)
    monkeypatch.setattr(webui.threading, "Thread", _SyncThread)
    monkeypatch.setattr(vc, "get_session_key", lambda **kw: response)
    b = _bare_bridge()
    b._prefetch_session_key()
    return b


def test_prefetch_skips_ephemeral_tokens(monkeypatch):
    b = _prefetch_with(monkeypatch, (
        "auth_tokens/t", "gemini", "m", "balanced", {"remaining": 5.0}, None,
        "ephemeral", None))
    assert b._key_cache == {}                       # a single-use token must not sit
    assert b._last_quota == {"remaining": 5.0}      # quota snapshot still adopted


def test_prefetch_still_caches_raw_keys(monkeypatch):
    b = _prefetch_with(monkeypatch, (
        "k", "gemini", "m", "balanced", None, None, "raw", None))
    assert b._pop_prefetched_key("el") == ("gemini", "k", "m", "balanced", None)
