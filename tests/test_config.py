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
    config.save_config(cfg)
    loaded = config.load_config()
    assert loaded["target_language_incoming"] == "de"
    assert loaded["config_version"] == config.CONFIG_VERSION


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
