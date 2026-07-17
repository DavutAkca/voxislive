"""Qwen voice-cloning must ride ONLY a genuine beta session.

Field bug (Ivo Kapec, 2026-07-17): a stale `beta.clone="always"` left in
config.json by an old Beta-tab soak silently turned per-speaker voice cloning on
for the now-standard Qwen route, which mis-genders male source speakers as a
female voice. `make_translator` used to read cfg["beta"]["clone"]
unconditionally; it now gates it on `beta_active`, which webui sets True only on
a real server-authorized beta resolver. These pin that gate so a refactor can't
quietly re-open the hole.
"""
import json

from app.config import ENGINE_QWEN
from app.engines import make_translator


def _make(cfg, *, beta_active):
    return make_translator(
        cfg, "cs", engine=ENGINE_QWEN, key="dummy-key", model="test-model",
        on_audio=lambda *_: None, on_text=lambda *_: None,
        on_status=lambda *_: None, name="t", beta_active=beta_active)


def test_stale_clone_ignored_on_standard_route():
    # config carries clone=always (the soak leftover) but this is NOT a beta
    # session → clone must be forced off.
    cfg = {"beta": {"enabled": True, "clone": "always"}}
    tr = _make(cfg, beta_active=False)
    assert tr.clone == "off"
    # And the wire config must not carry any voice/clone fields.
    session = json.loads(tr._session_update())["session"]
    assert "voice" not in session
    assert "enable_voice_clone" not in session
    assert "voice_clone_options" not in session


def test_clone_honored_on_genuine_beta_session():
    cfg = {"beta": {"enabled": True, "clone": "always"}}
    tr = _make(cfg, beta_active=True)
    assert tr.clone == "always"
    session = json.loads(tr._session_update())["session"]
    assert session["voice"] == "default"          # cloning REQUIRES voice=default
    assert session["enable_voice_clone"] is True
    assert session["voice_clone_options"] == {"frequency": "always"}


def test_default_is_off_even_in_beta():
    # No clone key at all → off regardless of beta_active.
    tr = _make({"beta": {"enabled": True}}, beta_active=True)
    assert tr.clone == "off"
    assert "enable_voice_clone" not in json.loads(tr._session_update())["session"]
