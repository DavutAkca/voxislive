"""First-run UI-language resolution (the 'every install booted in Turkish' fix).

Pins the two halves: the build seed must never carry the developer's language,
and startup must resolve an empty/unknown ui_language from the OS display
language while honoring an explicit user choice.
"""
from app import i18n
from app.config import DEFAULTS, sanitize_seed_config


def test_seed_never_carries_developer_language():
    seed = sanitize_seed_config({"ui_language": "tr", "quality_preset": "balanced"})
    assert seed["ui_language"] == ""          # "" = auto-detect at startup
    assert seed["quality_preset"] == "balanced"  # whitelisted keys still ride
    assert DEFAULTS["ui_language"] == ""


def test_resolve_language_contract():
    # Explicit, supported choice wins untouched.
    assert i18n.resolve_language("ro") == "ro"
    assert i18n.resolve_language("zh-Hant") == "zh-Hant"
    # Empty or unknown falls back to the OS language — always a supported key,
    # so set_language can never silently land on a missing block.
    assert i18n.resolve_language("") in i18n.STRINGS
    assert i18n.resolve_language("xx") in i18n.STRINGS


def test_detect_os_language_returns_supported():
    assert i18n.detect_os_language() in i18n.STRINGS
