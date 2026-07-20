"""config: merge/migrate semantics, engine/model resolution, atomic save/load."""
import json
import os

import pytest

import app.config as config


def test_merge_nested_dicts():
    base = {"a": 1, "d": {"x": 1, "y": 2}}
    over = {"d": {"y": 3}, "b": 2}
    out = config._merge(base, over)
    assert out == {"a": 1, "b": 2, "d": {"x": 1, "y": 3}}
    assert base["d"]["y"] == 2  # input untouched


def test_gate_params_strips_non_gate_keys():
    cfg = {"quality_preset": "max_savings"}
    p = config.gate_params(cfg)
    assert "gated" not in p
    assert config.stream_gated(cfg) is True
    assert config.stream_gated({"quality_preset": "balanced"}) is False


def test_unknown_preset_falls_back_to_balanced():
    p = config.gate_params({"quality_preset": "no_such_preset"})
    assert p == config.gate_params({"quality_preset": "balanced"})


def test_resolve_engine_rejects_unknown(monkeypatch):
    monkeypatch.delenv("VOXIS_ENGINE", raising=False)
    assert config.resolve_engine({"engine": "banana"}) == config.DEFAULT_ENGINE
    assert config.resolve_engine({"engine": "openai"}) == "openai"


def test_resolve_model_env_override(monkeypatch):
    monkeypatch.setenv("VOXIS_MODEL", "custom-live-model")
    assert config.resolve_model({"engine": "gemini"}) == "custom-live-model"
    monkeypatch.delenv("VOXIS_MODEL")
    assert config.resolve_model({"engine": "gemini", "model": "cfg-model"}) == "cfg-model"


def test_route_engine_oss_is_gemini_only(monkeypatch):
    monkeypatch.delenv("VOXIS_ENGINE", raising=False)
    monkeypatch.setattr(config, "IS_OFFICIAL_RELEASE", False)
    assert config.route_engine({}, "en") == config.ENGINE_GEMINI


def test_route_engine_official_routes_by_target(monkeypatch):
    monkeypatch.delenv("VOXIS_ENGINE", raising=False)
    monkeypatch.setattr(config, "IS_OFFICIAL_RELEASE", True)
    assert config.route_engine({}, "en") == config.ENGINE_OPENAI
    assert config.route_engine({}, "sw") == config.ENGINE_GEMINI  # not in OpenAI set
    assert config.route_engine({}, "zh-Hant") == config.ENGINE_GEMINI  # pinned


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def test_save_load_roundtrip(tmp_config):
    cfg = dict(config.DEFAULTS)
    cfg["target_language_incoming"] = "de"
    cfg["allow_multiple_instances"] = True
    config.save_config(cfg)
    loaded = config.load_config()
    assert loaded["target_language_incoming"] == "de"
    assert loaded["allow_multiple_instances"] is True
    assert loaded["config_version"] == config.CONFIG_VERSION


def test_multiple_instances_are_opt_in():
    assert config.DEFAULTS["allow_multiple_instances"] is False


def test_corrupt_config_falls_back_to_defaults(tmp_config):
    with open(tmp_config, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    loaded = config.load_config()
    assert loaded["engine"] == config.DEFAULT_ENGINE


def test_non_object_root_falls_back(tmp_config):
    with open(tmp_config, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    loaded = config.load_config()
    assert isinstance(loaded, dict)
    assert loaded["config_version"] == config.CONFIG_VERSION


def test_migration_stamps_version_and_persists(tmp_config):
    old = {"target_language_incoming": "fr"}  # pre-versioned file
    with open(tmp_config, "w", encoding="utf-8") as f:
        json.dump(old, f)
    loaded = config.load_config()
    assert loaded["config_version"] == config.CONFIG_VERSION
    assert loaded["target_language_incoming"] == "fr"
    # Migration is persisted once, so a reload sees the stamped file.
    with open(tmp_config, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["config_version"] == config.CONFIG_VERSION


def test_save_config_leaves_no_temp_files(tmp_config):
    config.save_config(dict(config.DEFAULTS))
    leftovers = [n for n in os.listdir(os.path.dirname(tmp_config))
                 if n.endswith(".tmp")]
    assert leftovers == []


def test_sanitize_seed_config_strips_secrets_and_machine_state():
    # A realistic dirty developer config: secrets, machine-specific device names,
    # window geometry, recovery state, custom hotkeys, first-run flags.
    dirty = dict(config.DEFAULTS)
    dirty["qwen_key"] = "sk-EXAMPLE-not-a-real-dashscope-key"
    dirty["openai_key"] = "sk-proj-EXAMPLE-not-a-real-openai-key"
    dirty["gemini_key"] = "AIza-EXAMPLE-not-a-real-gemini-key"
    dirty["devices"] = {"headphones_output": "Dev Speakers (USB)",
                        "microphone": "Dev Mic (Realtek)"}
    dirty["window"] = {"w": 1367, "h": 934, "x": 650, "y": 299, "max": False}
    dirty["_pending_default_restore"] = {"id": "CABLE Input"}
    dirty["hotkeys"] = {"video": "ctrl+alt+9"}
    dirty["onboarding_done"] = True
    dirty["update_check_url"] = "http://dev.local/update"
    # Whitelisted product choices must survive.
    dirty["target_language_incoming"] = "de"
    dirty["quality_preset"] = "turbo"
    dirty["engine"] = "openai"

    seed = config.sanitize_seed_config(dirty)

    # Secrets and unknown keys are gone by construction.
    for gone in ("qwen_key", "openai_key", "gemini_key", "window",
                 "_pending_default_restore", "onboarding_done", "update_check_url"):
        assert gone not in seed
    # Machine-specific dicts fall back to the generic DEFAULTS (self-healing seed).
    assert seed["devices"] == config.DEFAULTS["devices"]
    assert seed["hotkeys"] == config.DEFAULTS["hotkeys"]
    # Deliberate product choices are preserved.
    assert seed["target_language_incoming"] == "de"
    assert seed["quality_preset"] == "turbo"
    assert seed["engine"] == "openai"
    # No API-key-shaped value survives anywhere in the serialized seed.
    blob = json.dumps(seed)
    assert "sk-" not in blob and "AIza" not in blob
    # Version is stamped current so a freshly-seeded user runs no migration.
    assert seed["config_version"] == config.CONFIG_VERSION


def test_sanitize_seed_config_ignores_absent_whitelist_keys():
    # max_ambient_delay_ms is whitelisted but not in DEFAULTS; absent input must
    # not inject a key, present input must pass through.
    assert "max_ambient_delay_ms" not in config.sanitize_seed_config(dict(config.DEFAULTS))
    seed = config.sanitize_seed_config({"max_ambient_delay_ms": 400})
    assert seed["max_ambient_delay_ms"] == 400
