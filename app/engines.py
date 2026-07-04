"""Translation-engine factory + per-target routing.

Engine is chosen PER TARGET LANGUAGE (config.route_engine): OpenAI for the
languages it supports (faster + cheaper), Gemini for the rest (79-lang catch-all).
The caller passes a `keys` dict {"gemini": ..., "openai": ...}; if the routed
engine has no key available, we fall back to whichever engine does. See
PLAN/OPENAI_ENGINE_INTEGRATION.md.

Heavy vendor runtimes (google.genai / websockets) are imported lazily inside the
factory so cold app startup is not slowed by an engine that won't be used.
"""
from __future__ import annotations

from .config import ENGINE_GEMINI, ENGINE_OPENAI, ENGINE_QWEN, resolve_model, route_engine


def resolve_engine_for_target(cfg, keys, target_lang):
    """Engine for this target: route by language, then fall back to whichever
    engine actually has a key if the preferred one is unavailable."""
    engine = route_engine(cfg, target_lang)
    if not (keys or {}).get(engine):
        other = ENGINE_GEMINI if engine == ENGINE_OPENAI else ENGINE_OPENAI
        if (keys or {}).get(other):
            engine = other
    return engine


def make_translator(cfg, target_lang, *, engine, key, model=None,
                    on_audio, on_text, on_status, name, noise_reduction=None):
    """Build the translator thread for an ALREADY-RESOLVED engine + key + model.
    The caller resolves per target (locally for BYOK, server-side for SaaS) so the
    capture send-rate can match. Returns an object honoring the translator
    contract, tagged with `.engine`."""
    if not key:
        raise RuntimeError(f"no API key for engine '{engine}'")
    model = model or resolve_model(cfg, engine)

    if engine == ENGINE_QWEN:
        # BETA engine: reachable only through the server-gated Beta opt-in (the
        # language router never selects it), so Gemini/OpenAI paths are
        # untouched. Its knobs live under cfg["beta"].
        from .qwen_translator import QwenTranslator  # lazy: keep websockets off cold start
        from .config import parse_hotwords  # noqa: PLC0415
        beta = cfg.get("beta") or {}
        tr = QwenTranslator(
            key, target_lang,
            on_audio=on_audio, on_text=on_text, on_status=on_status,
            rotate_minutes=cfg.get("qwen_rotate_minutes", 25), name=name,
            model=model,
            source_lang=beta.get("source_lang", "auto"),
            clone=beta.get("clone", "off"),
            hotwords=parse_hotwords(beta.get("hotwords", "")),
            vad_silence_ms=int(beta.get("vad_ms", 500)))
        tr.engine = engine
        return tr

    if engine == ENGINE_OPENAI:
        from .openai_translator import OpenAITranslator  # lazy: keep websockets off cold start
        tr = OpenAITranslator(
            key, target_lang,
            on_audio=on_audio, on_text=on_text, on_status=on_status,
            # OpenAI realtime caps at ~60 min, so rotate near 55 (its own default)
            # instead of Gemini's 13-min cadence — 4x less reconnect churn. Separate
            # key so the Gemini interval stays independent.
            rotate_minutes=cfg.get("openai_rotate_minutes", 55), name=name,
            model=model, noise_reduction=noise_reduction)
        tr.engine = engine
        return tr

    from .translator import LiveTranslator  # lazy: keep google.genai off cold start
    tr = LiveTranslator(
        key, target_lang,
        on_audio=on_audio, on_text=on_text, on_status=on_status,
        rotate_minutes=cfg.get("session_rotate_minutes", 13), name=name,
        voice=cfg.get("gemini_voice", "Aoede"),
        temperature=float(cfg.get("gemini_temperature", 0.3)),
        model=model)
    tr.engine = engine
    return tr
