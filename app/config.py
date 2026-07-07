"""Runtime configuration: load/save config.json, build-flavor constants, and
quality/profile presets that drive the VAD gate."""
import json
import logging
import os
import shutil
import tempfile

from .paths import bundled_default_config, is_frozen, official_marker, user_path

# User-writable config. Frozen builds land in Program Files (read-only for a
# standard user), so this resolves to %APPDATA%\Voxis; from source it stays in
# the repo root. See app/paths.py.
CONFIG_PATH = user_path("config.json")

# Build-flavor flag (set at compile time for the official voxislive.com .exe).
# True  → SaaS-only flow: BYOK fields are hidden, login routes to the cloud, the
#         server-side key is the only execution path.
# False → Open-source / developer flow: BYOK input is exposed in Settings so the
#         user can supply their own Gemini key.
def _resolve_official_release() -> bool:
    # A frozen build is a signed artifact: its flavor must be a property of the
    # bundle, not of the launching environment. Honoring VOXIS_OFFICIAL_RELEASE
    # in a frozen build would let any caller flip a signed SaaS exe into BYOK (or
    # vice versa) just by exporting an env var, so the override is restricted to
    # source/dev runs and a frozen build trusts ONLY the embedded OFFICIAL marker.
    if not is_frozen():
        env = os.environ.get("VOXIS_OFFICIAL_RELEASE")
        if env is not None:
            return env.strip().lower() in ("1", "true", "yes", "on")
        return False
    try:
        return os.path.exists(official_marker())
    except Exception:
        return False


IS_OFFICIAL_RELEASE: bool = _resolve_official_release()

# The Gemini Live translate model. A preview model can be retired on a few months'
# notice, so the name is a config key (not a hardcoded constant) and can be swapped
# without a client release: edit config.json's "model", or set the VOXIS_MODEL env
# var for an emergency ops override (see resolve_model).
GEMINI_LIVE_MODEL = "gemini-3.5-live-translate-preview"

# Second translation engine (OpenAI). Like the Gemini model, the OpenAI model
# name is a config key so a retired preview can be swapped without a client
# release. See PLAN/OPENAI_ENGINE_INTEGRATION.md.
OPENAI_TRANSLATE_MODEL = "gpt-realtime-translate"
# Third engine (Qwen3.5-LiveTranslate) — BETA, per-user server-gated. Same
# swap-without-release pattern as the other two. The intl account requires the
# workspace-scoped MAAS endpoint (see app/qwen_translator.py).
QWEN_TRANSLATE_MODEL = "qwen3.5-livetranslate-flash-realtime"
QWEN_WORKSPACE = "ws-o9euzpyp254xo4es"
ENGINE_GEMINI = "gemini"
ENGINE_OPENAI = "openai"
ENGINE_QWEN = "qwen"
VALID_ENGINES = (ENGINE_GEMINI, ENGINE_OPENAI, ENGINE_QWEN)
DEFAULT_ENGINE = ENGINE_GEMINI

# OpenAI gpt-realtime-translate validated OUTPUT (target) languages (13), per
# OpenAI's current official docs. English is #1 of the supported set.
OPENAI_OUTPUT_LANGS = ["en", "es", "pt", "fr", "de", "it", "ru", "ja", "ko", "zh", "hi", "id", "vi"]
# Default per-language routing set = exactly OpenAI's documented 13. Server-/config-
# overridable via cfg["openai_langs"]; anything NOT in this set routes to Gemini
# (79-lang catch-all). (tr/ar/pl were previously added on an "A/B-confirmed" note
# that OpenAI's official docs do not corroborate — removed pending real evidence.)
DEFAULT_OPENAI_LANGS = list(OPENAI_OUTPUT_LANGS)

# Qwen3.5-LiveTranslate synthesizes translated SPEECH for these 29 targets; its
# other ~31 supported targets are TEXT-ONLY (captions, no voice) — routing one of
# those to Qwen yields subtitles with no translated audio. Source: Alibaba Model
# Studio doc for qwen3.5-livetranslate-flash-realtime (verified 2026-07-05).
# Server-/config-overridable via cfg["qwen_audio_langs"] since the tier can shift
# with model updates. Base ISO codes (matched via _norm_lang).
QWEN_AUDIO_LANGS = ["zh", "en", "ar", "de", "fr", "es", "pt", "id", "it", "ko",
                    "ru", "th", "vi", "ja", "tr", "hi", "ms", "nl", "ur", "nb",
                    "sv", "da", "he", "fi", "pl", "is", "cs", "fil", "fa"]

DEFAULTS = {
    "engine": DEFAULT_ENGINE,
    "model": GEMINI_LIVE_MODEL,
    "openai_model": OPENAI_TRANSLATE_MODEL,
    "openai_langs": DEFAULT_OPENAI_LANGS,
    "qwen_model": QWEN_TRANSLATE_MODEL,
    # Beta engine (Qwen) opt-in + its knobs. The Settings tab only renders when
    # the server marks the account beta-eligible; "enabled" is the user's own
    # switch. vad_ms=0 leaves segmentation to the model; 500 is the validated
    # sweet spot (sandbox TEST_REPORT_2026-07-04).
    "beta": {
        "enabled": False,
        "source_lang": "auto",
        "clone": "off",
        "hotwords": "",
        "vad_ms": 500,
    },
    "target_language_incoming": "tr",
    "target_language_outgoing": "en",
    "devices": {
        "system_capture": "CABLE Output (VB-Audio Virtual Cable)",
        "meeting_mic_playback": "CABLE Input (VB-Audio Virtual Cable)",
        "meeting_virtual_mic": "CABLE Output (VB-Audio Virtual Cable)",
        "headphones_output": "",
        "microphone": "",
    },
    "original_audio": "duck",
    "show_subtitles": True,
    "ui_language": "tr",
    "tts_volume": 1.0,
    "overlay_enabled": False,
    "obs_subtitle_enabled": False,
    "branding_badge_enabled": True,
    "meeting_consent_ack": False,
    "hotkeys": {
        "video": "ctrl+alt+1",
        "meeting": "ctrl+alt+2",
        "stop": "ctrl+alt+0",
        "overlay": "ctrl+alt+o",
    },
    "duck_gain": 0.30,
    "session_rotate_minutes": 13,
    "quality_preset": "max_quality",
    "active_profile": "custom",
    "gemini_voice": "Aoede",
    "gemini_temperature": 0.3,
    "capture_backend": "driverless",
    # Where session transcripts are saved. Empty = the built-in default
    # (Documents\Voxis\Transcripts on a frozen build); a non-empty path overrides.
    "transcript_dir": "",
    # Opt-in dual-track audio export: save the source + translated audio as two
    # separate WAV files beside the transcript. OFF by default — the source track
    # captures real human voice (a consent step up over a text transcript).
    "record_audio": False,
    # Qwen audio-output tier (see QWEN_AUDIO_LANGS); server-overridable so a model
    # update that adds/removes voiced languages doesn't need a client release.
    "qwen_audio_langs": list(QWEN_AUDIO_LANGS),
    "ui_theme": "dark",
    # Bumped when a load-time migration is added; see _migrate / load_config.
    "config_version": 2,
}

CONFIG_VERSION = DEFAULTS["config_version"]

GEMINI_VOICES = ["Aoede", "Kore", "Puck", "Charon", "Fenrir", "Leda", "Orus", "Zephyr",
                 "Sulafat", "Laomedeia", "Achernar", "Despina", "Erinome", "Gacrux",
                 "Vindemiatrix"]

# Quality presets map to the local VAD gate parameters that shape the continuous
# stream fed to the native simultaneous translate model.
# preroll  = leading audio retained so the first word is not clipped (PAST audio — zero forward latency);
# hangover = trailing audio retained so a pause inside a sentence does not close the gate;
# threshold / min_speech_ms = Silero speech-probability gate and minimum onset burst.
# gated = omit-silence stream policy (the end-user "Saver" option) — only speech is sent,
#         so fewer audio minutes are billed; the other presets stream continuously.
# `turbo` is the most aggressive low-latency gate profile.
QUALITY_PRESETS = {
    "max_quality": {"threshold": 0.30, "min_speech_ms": 80,  "hangover_ms": 1200, "preroll_ms": 800},
    "balanced":    {"threshold": 0.45, "min_speech_ms": 120, "hangover_ms": 1000, "preroll_ms": 600},
    "max_savings": {"threshold": 0.60, "min_speech_ms": 220, "hangover_ms": 600,  "preroll_ms": 350, "gated": True},
    "turbo":       {"threshold": 0.40, "min_speech_ms": 100, "hangover_ms": 500, "preroll_ms": 500},
    # Gaming "Callout" — lowest-latency terse comms: quick onset, short hangover so
    # a callout closes promptly. Ungated (callouts are short; gating clips lead-ins).
    "callout":     {"threshold": 0.38, "min_speech_ms": 70,  "hangover_ms": 300, "preroll_ms": 300},
}

# Keys that live in a preset but are NOT SpeechGate(**kwargs) constructor args —
# stripped by gate_params so the dict can be splatted into SpeechGate.
#   gated = omit-silence stream policy.
_NON_GATE_KEYS = ("gated",)

PROFILES = {
    "meeting":    {"quality_preset": "balanced",    "original_audio": "duck", "duck_gain": 0.30},
    "film":       {"quality_preset": "max_quality", "original_audio": "duck", "duck_gain": 0.30},
    "conference": {"quality_preset": "max_quality", "original_audio": "duck", "duck_gain": 0.20},
}


def _preset(cfg: dict) -> dict:
    return QUALITY_PRESETS.get(cfg.get("quality_preset", "balanced"),
                               QUALITY_PRESETS["balanced"])


def gate_params(cfg: dict) -> dict:
    """Local SpeechGate kwargs for the active preset (server-only keys stripped
    so the dict can be splatted into SpeechGate(**gate_params(cfg)))."""
    return {k: v for k, v in _preset(cfg).items() if k not in _NON_GATE_KEYS}


def stream_gated(cfg: dict) -> bool:
    """True when the active preset gates the stream — only speech is sent, silence
    gaps are omitted, so fewer audio minutes are billed. This is the end-user
    'Tasarruf' (savings) option; the default smooth stream sends silence too."""
    return bool(_preset(cfg).get("gated", False))


def resolve_engine(cfg: dict) -> str:
    """Active translation engine. Precedence: VOXIS_ENGINE env (ops override) >
    config.json "engine" > default. An unknown value falls back to the default
    so a bad config can never select a non-existent backend."""
    eng = (os.environ.get("VOXIS_ENGINE", "").strip().lower()
           or cfg.get("engine") or DEFAULT_ENGINE)
    return eng if eng in VALID_ENGINES else DEFAULT_ENGINE


def resolve_model(cfg: dict, engine: str | None = None) -> str:
    """Model name to connect with for the given engine (defaults to the active
    engine). Each engine keeps its own env override + config key + built-in
    default, so a retired preview can be swapped without shipping a new client.
    The Gemini branch is byte-for-byte the original logic (VOXIS_MODEL parity)."""
    engine = engine or resolve_engine(cfg)
    if engine == ENGINE_OPENAI:
        return (os.environ.get("VOXIS_OPENAI_MODEL", "").strip()
                or cfg.get("openai_model") or OPENAI_TRANSLATE_MODEL)
    if engine == ENGINE_QWEN:
        return (os.environ.get("VOXIS_QWEN_MODEL", "").strip()
                or cfg.get("qwen_model") or QWEN_TRANSLATE_MODEL)
    return os.environ.get("VOXIS_MODEL", "").strip() or cfg.get("model") or GEMINI_LIVE_MODEL


def parse_hotwords(text: str) -> dict:
    """Beta hotword box: `term=translation` lines -> Qwen corpus.phrases dict.
    A bare `term` pins the term to itself (proper-noun passthrough). Capped at
    50 pairs by the engine (server limit)."""
    phrases: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        term, _, tr = line.partition("=")
        term, tr = term.strip(), tr.strip()
        if term:
            phrases[term] = tr or term
    return phrases


def _norm_lang(code: str) -> str:
    """Normalize a BCP-47 target to OpenAI's base code for routing: pt-BR/pt-PT ->
    pt, zh-Hans -> zh. Traditional Chinese (zh-hant) is kept distinct so it can be
    pinned to Gemini."""
    if not code:
        return ""
    c = code.strip().lower()
    return "zh-hant" if c == "zh-hant" else c.split("-")[0]


def openai_route_langs(cfg: dict) -> list:
    """Target languages routed to OpenAI (lower-cased). Server-/config-overridable
    via cfg['openai_langs']; defaults to the validated 13 + tr/ar/pl."""
    v = cfg.get("openai_langs")
    src = v if isinstance(v, list) and v else DEFAULT_OPENAI_LANGS
    return [str(s).strip().lower() for s in src]


def qwen_audio_langs(cfg: dict) -> list:
    """Target languages (base codes) for which Qwen produces translated SPEECH.
    Config-/server-overridable via cfg['qwen_audio_langs']; defaults to the
    documented 29-language audio tier."""
    v = cfg.get("qwen_audio_langs")
    src = v if isinstance(v, list) and v else QWEN_AUDIO_LANGS
    return [str(s).strip().lower() for s in src]


def qwen_can_voice(cfg: dict, target: str) -> bool:
    """True when the Qwen beta engine can synthesize a translated VOICE for this
    target. A text-only target must fall back to the standard engine, or the user
    is left with subtitles and no audio (the class of bug behind the 1.0.24 field
    report). Czech (cs) IS voiced, so this does not change the EN->CS path."""
    return _norm_lang(target) in qwen_audio_langs(cfg)


def route_engine(cfg: dict, target: str) -> str:
    """Pick the engine for a TARGET language: OpenAI for its (config-listed) outputs
    (faster + cheaper), Gemini for everything else (the 79-language catch-all).
    VOXIS_ENGINE env forces one engine (ops/dev override). OpenAI is an
    OFFICIAL-build feature: the open-source / BYOK build is Gemini-only."""
    forced = os.environ.get("VOXIS_ENGINE", "").strip().lower()
    if forced in VALID_ENGINES:
        return forced
    if not IS_OFFICIAL_RELEASE:
        return ENGINE_GEMINI
    return ENGINE_OPENAI if _norm_lang(target) in openai_route_langs(cfg) else ENGINE_GEMINI


def apply_profile(cfg: dict, profile: str):
    if profile in PROFILES:
        cfg.update(PROFILES[profile])
    cfg["active_profile"] = profile


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


_log = logging.getLogger("voxis.config")


def _logfile() -> str:
    return user_path("voxis.log")


def _log_failure(msg: str, exc: Exception) -> None:
    """Append a config failure to the user-dir logfile so a bad seed/migration is
    diagnosable instead of silently masked by a DEFAULTS fallback."""
    _log.warning("%s: %s", msg, exc)
    try:
        import datetime
        line = f"{datetime.datetime.now().isoformat()} config {msg}: {exc!r}" + chr(10)
        with open(_logfile(), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _seed_from_bundle() -> bool:
    """On first run of a frozen build, seed the user config from the production
    config.json shipped inside the bundle (no-op from source / if absent).
    Returns True on a successful copy."""
    src = bundled_default_config()
    if src and os.path.abspath(src) != os.path.abspath(CONFIG_PATH) and os.path.exists(src):
        try:
            shutil.copy2(src, CONFIG_PATH)
            return True
        except OSError as exc:
            # Do not silently fall back to DEFAULTS: a failed seed means the user
            # loses the shipped production config, which is worth recording.
            _log_failure("seed_from_bundle failed", exc)
    return False


def _sanitize_devices(cfg: dict) -> bool:
    """Blank device names that do not resolve on this machine so a config carried
    over from another PC (older builds seeded %APPDATA% from the developer's own
    config.json) self-heals to the system default instead of hard-failing session
    start. Returns True if anything changed.

    Fully guarded: any enumeration fault, or an empty device list, leaves the
    config untouched — a selection is never wiped on a transient PortAudio glitch.
    Only the driverless playback target and the mic are validated; the VB-CABLE
    meeting fields legitimately stay set before the cable is installed (meeting
    mode is gated separately) so they are left alone."""
    devs = cfg.get("devices")
    if not isinstance(devs, dict):
        return False
    try:
        import sounddevice as sd  # noqa: PLC0415
        devices = sd.query_devices()
    except Exception as exc:
        _log_failure("device sanitize: enumeration failed", exc)
        return False
    if not len(devices):
        return False
    has_out = any(d.get("max_output_channels", 0) > 0 for d in devices)
    has_in = any(d.get("max_input_channels", 0) > 0 for d in devices)
    from .audio_io import find_device  # noqa: PLC0415
    changed = False
    for field, kind, has_kind in (("headphones_output", "output", has_out),
                                  ("microphone", "input", has_in)):
        name = devs.get(field, "")
        if not name or not has_kind:
            continue
        try:
            find_device(name, kind)  # raises ValueError when absent
        except ValueError:
            devs[field] = ""
            changed = True
            _log.info("device sanitize: '%s' (%s) not present -> system default",
                      name, kind)
        except Exception as exc:
            # A deeper PortAudio fault is not proof the device is gone — leave it.
            _log_failure(f"device sanitize: probe failed for {field}", exc)
    return changed


def _migrate(cfg: dict) -> dict:
    """Forward-migrate an older on-disk config to CONFIG_VERSION. Each step is
    idempotent so a partial/older file converges to the current shape."""
    version = cfg.get("config_version", 0)
    if version >= CONFIG_VERSION:
        cfg["config_version"] = CONFIG_VERSION
        return cfg
    # v0 -> v1: pre-versioned configs gain the stamp (no field rewrites needed).
    # v<2 -> v2: drop device names that no longer resolve on this machine. Older
    # builds inherited the developer's device names via the bundled seed, so a
    # user on different audio hardware hit a find_device ValueError that aborted
    # session start; blanking them falls back to the system default.
    if version < 2:
        _sanitize_devices(cfg)
    cfg["config_version"] = CONFIG_VERSION
    return cfg


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        _seed_from_bundle()
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                # Valid JSON but not an object (e.g. "[]"/"null" from a corrupt or
                # hand-edited file): _merge would raise AttributeError. Treat it as
                # corrupt and fall back to DEFAULTS like a parse failure.
                raise ValueError("config root is not a JSON object")
        except (OSError, ValueError) as exc:
            _log_failure("load_config read/parse failed", exc)
            return _migrate(dict(DEFAULTS))
        # Read the true on-disk version BEFORE merging: _merge would otherwise let
        # the DEFAULTS stamp mask a pre-versioned file and skip its migration.
        raw_version = raw.get("config_version", 0) if isinstance(raw, dict) else 0
        cfg = _merge(DEFAULTS, raw)
        cfg["config_version"] = raw_version
        cfg = _migrate(cfg)
        # Persist once when a migration ran so the repair (e.g. blanked devices)
        # sticks and the UI reflects it, instead of re-healing on every launch.
        if raw_version < CONFIG_VERSION:
            try:
                save_config(cfg)
            except OSError as exc:
                _log_failure("persist migration failed", exc)
        return cfg
    return _migrate(dict(DEFAULTS))


# --- Build seed sanitization (used by app/build_official.py) -----------------
# Production-meaningful keys that are safe to carry from the developer's working
# config.json into the shipped seed (build → _internal/config.json → every user's
# %APPDATA%\Voxis on first run, see _seed_from_bundle). Everything NOT on this
# list falls back to its DEFAULTS value, so secrets (qwen_key / any *_key),
# machine-specific device names, window geometry, hotkey customizations,
# _pending_* recovery state and the beta/qwen opt-in can never leak into the
# bundle. This is the allowlist half of P0 #8; the build script adds a
# fail-closed secret scan on top.
#   capture_backend is included but is auto-resolved by premium on the official
#   build (pipeline.resolve_capture_backend), falling back to driverless, so a
#   seeded "vbcable" is harmless on a machine without a virtual cable.
SEED_WHITELIST = (
    "target_language_incoming",
    "target_language_outgoing",
    "quality_preset",
    "original_audio",
    "duck_gain",
    "tts_volume",
    "show_subtitles",
    "ui_language",
    "ui_theme",
    "engine",
    "model",
    "openai_model",
    "openai_langs",
    "qwen_model",
    "max_ambient_delay_ms",
    "capture_backend",
)


def sanitize_seed_config(cfg: dict) -> dict:
    """Build a clean production seed from a developer's working config.

    Returns DEFAULTS overlaid with ONLY the whitelisted, production-meaningful
    keys present in `cfg`. Anything else — secrets, PII, machine-specific state —
    is dropped by construction (it never enters the returned dict). config_version
    comes from DEFAULTS so a freshly-seeded user is already at the current version
    and runs no first-launch migration. See P0 #8 and SEED_WHITELIST."""
    seed = dict(DEFAULTS)
    if isinstance(cfg, dict):
        for key in SEED_WHITELIST:
            if key in cfg:
                seed[key] = cfg[key]
    return seed


def save_config(cfg: dict):
    # Atomic write: a crash / power loss mid-dump must not leave a truncated
    # config.json (which load_config would then discard, silently resetting all
    # user settings). Write a sibling temp file, fsync, then atomically replace.
    #
    # The temp file gets a UNIQUE name (mkstemp) rather than a fixed
    # "config.json.tmp": rapid slider drags fan out concurrent save threads
    # (pywebview dispatches each JS api call on its own thread), and a shared
    # temp name made them collide on Windows — one thread's open/replace hit the
    # other's open handle (WinError 32 / Errno 13), surfacing a bogus
    # "cannot write to disk" error. A per-call temp name removes the collision.
    d = os.path.dirname(CONFIG_PATH) or "."
    fd, tmp = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        # Don't leave the unique temp behind if the write/replace failed.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
