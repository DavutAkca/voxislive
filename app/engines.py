"""Translation-engine factory.

`make_translator` builds the translator thread for an ALREADY-RESOLVED engine.
Live routing is NOT decided here: on the SaaS build the server picks the engine
per target (/auth/session-key), and BYOK/OSS is Gemini-only. The shipped policy
is Qwen primary for its voiced targets with Gemini the 79-language catch-all;
this module just constructs whatever engine it was handed and tags it `.engine`.

Heavy vendor runtimes (google.genai / websockets) are imported lazily inside the
factory so cold app startup is not slowed by an engine that won't be used.
"""
from __future__ import annotations

from .config import ENGINE_CASCADE, ENGINE_QWEN, resolve_model


def make_translator(cfg, target_lang, *, engine, key, model=None,
                    on_audio, on_text, on_status, name,
                    on_fatal=None, key_provider=None, beta_active=False):
    """Build the translator thread for an ALREADY-RESOLVED engine + key + model.
    The caller resolves per target (locally for BYOK, server-side for SaaS) so the
    capture send-rate can match. Returns an object honoring the translator
    contract, tagged with `.engine`.

    on_fatal is invoked if the engine gives up mid-session (see
    BaseTranslator._give_up) so the caller can substitute another engine. It is
    attached after construction rather than threaded through all three subclass
    signatures — nothing reads it until the reconnect loop is abandoned.

    key_provider (Gemini only) fetches a fresh api key per reconnect once a
    single-use ephemeral token has been spent (see LiveTranslator); the other
    engines receive raw multi-use keys and ignore it.

    beta_active gates Qwen voice-cloning: cfg["beta"]["clone"] is honored ONLY
    when this is True (a genuine, server-authorized beta session — webui sets
    resolve.beta_active there). It is False for standard-routed Qwen so a stale
    beta.clone left in config.json by an old Beta-tab soak cannot silently turn
    per-speaker cloning on (which mis-genders male source as a female voice)."""
    if not key:
        raise RuntimeError(f"no API key for engine '{engine}'")
    model = model or resolve_model(cfg, engine)

    if engine == ENGINE_CASCADE:
        # Free-tier half-cascade: the cloud leg is the SAME Live translate
        # model/key (resolve_model falls through to the Gemini branch for
        # unknown engines), so key/ephemeral/rotation infra is shared.
        from .cascade_translator import CascadeTranslator  # lazy
        tr = CascadeTranslator(
            key, target_lang,
            on_audio=on_audio, on_text=on_text, on_status=on_status,
            rotate_minutes=cfg.get("session_rotate_minutes", 13), name=name,
            model=model, voice=cfg.get("gemini_voice", "Aoede"),
            temperature=float(cfg.get("gemini_temperature", 0.3)),
            key_provider=key_provider)
        tr.on_fatal = on_fatal
        return tr

    if engine == ENGINE_QWEN:
        # Qwen is now the server-routed PRIMARY engine (per-target), not only the
        # Beta opt-in. Its knobs still live under cfg["beta"], but voice-cloning
        # must NOT ride along on the standard route: clone is gated on
        # beta_active (a genuine server-authorized beta session) so a stale
        # beta.clone from an old soak can't silently mis-gender speakers.
        from .qwen_translator import QwenTranslator  # lazy: keep websockets off cold start
        from .config import QWEN_WORKSPACE, parse_hotwords  # noqa: PLC0415
        beta = cfg.get("beta") or {}
        clone = beta.get("clone", "off") if beta_active else "off"
        tr = QwenTranslator(
            key, target_lang,
            on_audio=on_audio, on_text=on_text, on_status=on_status,
            rotate_minutes=cfg.get("qwen_rotate_minutes", 25), name=name,
            model=model,
            source_lang=beta.get("source_lang", "auto"),
            clone=clone,
            hotwords=parse_hotwords(beta.get("hotwords", "")),
            vad_silence_ms=int(beta.get("vad_ms", 500)),
            # DashScope intl keys are workspace-scoped (the WS host carries the
            # workspace id) — a key from a different Model Studio account needs
            # its own workspace here, or the handshake 401s.
            workspace=cfg.get("qwen_workspace") or QWEN_WORKSPACE)
        tr.engine = engine
        tr.on_fatal = on_fatal
        return tr

    from .translator import LiveTranslator  # lazy: keep google.genai off cold start
    tr = LiveTranslator(
        key, target_lang,
        on_audio=on_audio, on_text=on_text, on_status=on_status,
        rotate_minutes=cfg.get("session_rotate_minutes", 13), name=name,
        voice=cfg.get("gemini_voice", "Aoede"),
        temperature=float(cfg.get("gemini_temperature", 0.3)),
        model=model, key_provider=key_provider)
    tr.engine = engine
    tr.on_fatal = on_fatal
    return tr
