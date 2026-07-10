"""Headless benchmark runner: feed audio clips through the real Voxis translator
and capture the translation text, the model's source transcription, and the
onset->first-audio latency — then score.py turns those into BLEU/chrF/WER.

This DOES hit the live engines: it needs a valid key and network, and it
consumes billed minutes. It is a dev/CI tool, never shipped to end users.

Engines: --engine gemini (default) | openai | qwen — built through the same
engines.make_translator factory the app uses, so the bench measures the real
production stack per engine. Input is fed at each engine's ingest rate
(OpenAI 24 kHz, Gemini/Qwen 16 kHz) in realtime.

Key resolution order (per engine):
  gemini: --key > $VOXIS_BENCH_KEY       > BYOK store "developer" slot
  openai: --key > $VOXIS_BENCH_OPENAI_KEY > BYOK store "openai" slot
  qwen:   --key > $VOXIS_BENCH_QWEN_KEY  > config.json "qwen_key"

Fixtures manifest (JSONL), one clip per line:
  {"id":"c1","audio":"fixtures/clip1.wav","target_lang":"en",
   "reference":"<ground-truth translation>","source_ref":"<ground-truth source text>"}

Usage:
  python scripts/bench/run_session.py fixtures/manifest.jsonl -o results.jsonl
  python scripts/bench/run_session.py fixtures/manifest.jsonl --engine openai -o results_oai.jsonl
  python scripts/bench/score.py results.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

# Make the repo importable when run as a script.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

FRAME_MS = 20
SR = 16000
FRAME = SR * FRAME_MS // 1000  # 320 samples
# Matches pipeline._tts_sink's audible gate: an initial near-silent padding
# chunk (OpenAI pads its stream) must not count as "first audio" or the
# latency numbers flatter the padded engine.
AUDIBLE_PEAK = 512
OUT_RATE = 24000  # all three engines emit 24 kHz PCM16


def _resolve_key(cli_key: str | None, engine: str) -> str:
    if cli_key:
        return cli_key
    env_names = {"gemini": "VOXIS_BENCH_KEY", "openai": "VOXIS_BENCH_OPENAI_KEY",
                 "qwen": "VOXIS_BENCH_QWEN_KEY"}
    env = os.environ.get(env_names[engine])
    if env:
        return env
    try:
        if engine == "qwen":
            from app.config import load_config
            k = (load_config() or {}).get("qwen_key")
        else:
            from app import byok_store
            rec = byok_store.load_byok("developer")
            k = rec.get(engine) if isinstance(rec, dict) else rec
        if k:
            return k
    except Exception:
        pass
    raise SystemExit(f"No {engine} key. Pass --key, set ${env_names[engine]}, "
                     "or store one (BYOK store / config.json).")


def _resolve_prod_key() -> str:
    """Fetch the PRODUCTION server-issued Gemini key (SaaS path) to A/B its tier
    vs the local BYOK key. Needs the env override + the owner's credentials, all
    supplied by the caller's own `!` shell so no secret is relayed:
        VOXIS_OFFICIAL_RELEASE=1 VOXIS_EMAIL=... VOXIS_PW=... ... --prod
    Never prints the key."""
    if os.environ.get("VOXIS_OFFICIAL_RELEASE") not in ("1", "true", "yes", "on"):
        raise SystemExit("--prod needs VOXIS_OFFICIAL_RELEASE=1 (source override) so login/session-key are enabled.")
    email, pw = os.environ.get("VOXIS_EMAIL"), os.environ.get("VOXIS_PW")
    if not (email and pw):
        raise SystemExit("--prod needs VOXIS_EMAIL and VOXIS_PW env vars.")
    from app import voxis_client
    _, err = voxis_client.pb_login(email, pw)
    if err:
        raise SystemExit(f"login failed: {err}")
    key, *_mid, err = voxis_client.get_session_key()
    if err or not key:
        raise SystemExit(f"session-key failed: {err}")
    print("Production session key acquired (not printed).")
    return key


def _load_pcm16(path: str, rate: int) -> bytes:
    """Read any wav/flac, downmix to mono, resample to `rate`, return PCM16 bytes."""
    import soundfile as sf
    from app.audio_io import _make_resampler

    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)  # mono
    if sr != rate:
        x = _make_resampler(sr, rate)(np.ascontiguousarray(x))
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


def _bench_cfg(engine: str) -> dict:
    """Minimal config for engines.make_translator — DEFAULTS so the bench runs
    exactly the app's production translator settings per engine."""
    from app.config import DEFAULTS
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy, no shared mutables
    if engine == "qwen":
        # The bench measures the ENGINE, not the beta voice-clone extras: keep
        # the same defaults the spike validated (auto source, clone off, 500 ms).
        cfg["beta"] = {"enabled": True, "source_lang": "auto", "clone": "off",
                       "hotwords": "", "vad_ms": 500}
        # DashScope keys are workspace-scoped; a key from another Model Studio
        # account needs its own ws-… id (else the built-in default is used).
        ws = os.environ.get("VOXIS_BENCH_QWEN_WS", "").strip()
        if ws:
            cfg["qwen_workspace"] = ws
    return cfg


def run_clip(clip: dict, api_key: str, *, engine: str = "gemini",
             voice: str = "Aoede", drain_s: float = 8.0) -> dict:
    from app.engines import make_translator

    heard: list[str] = []   # on_text("in", ...)  -> source transcription
    trans: list[str] = []   # on_text("out", ...) -> translation
    first_audio = {"t": None}
    started = {"t": None}
    audio_bytes = {"n": 0}          # audible translated audio received
    statuses: list[str] = []
    lock = threading.Lock()

    def on_text(direction: str, text: str):
        with lock:
            (heard if direction == "in" else trans).append(text)

    def on_audio(data: bytes):
        a = np.frombuffer(data, dtype=np.int16)
        audible = a.size > 0 and int(np.abs(a).max()) > AUDIBLE_PEAK
        with lock:
            if audible:
                audio_bytes["n"] += len(data)
                if first_audio["t"] is None:
                    first_audio["t"] = time.monotonic()

    def on_status(msg: str):
        statuses.append(str(msg))

    cfg = _bench_cfg(engine)
    cfg["gemini_voice"] = voice
    tr = make_translator(cfg, clip["target_lang"], engine=engine, key=api_key,
                         on_audio=on_audio, on_text=on_text, on_status=on_status,
                         name="bench")
    tr.start()
    tr.wait_ready(timeout=15)

    in_rate = 24000 if engine == "openai" else SR
    frame = in_rate * FRAME_MS // 1000
    pcm = _load_pcm16(str(ROOT / clip["audio"]) if not os.path.isabs(clip["audio"]) else clip["audio"],
                      in_rate)
    source_s = len(pcm) / (in_rate * 2)
    started["t"] = time.monotonic()
    # Feed at realtime so the measured first-audio latency is realistic.
    step = frame * 2  # bytes per frame (int16)
    for off in range(0, len(pcm), step):
        tr.send_pcm16(pcm[off:off + step])
        time.sleep(FRAME_MS / 1000.0)
    # Let the simultaneous tail finish translating after the audio ends.
    time.sleep(drain_s)
    try:
        tr.stop()
    except Exception:
        pass

    lat = None
    if first_audio["t"] is not None and started["t"] is not None:
        lat = round(first_audio["t"] - started["t"], 3)
    return {
        "id": clip.get("id"),
        "engine": engine,
        "target_lang": clip["target_lang"],
        "reference": clip.get("reference", ""),
        "hypothesis": " ".join(trans).strip(),
        "source_ref": clip.get("source_ref", ""),
        "source_heard": " ".join(heard).strip(),
        "latency_s": lat,
        # Voiced coverage: seconds of AUDIBLE translated speech received vs the
        # source clip's length — the dead-air detector (an engine can emit fine
        # text but little/no voiced audio, which is what the user hears).
        "audio_s": round(audio_bytes["n"] / (OUT_RATE * 2), 2),
        "source_s": round(source_s, 2),
        "status_tail": statuses[-3:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Voxis over a fixtures manifest and capture results.")
    ap.add_argument("manifest", help="JSONL of clips (id, audio, target_lang, reference, source_ref)")
    ap.add_argument("-o", "--out", default="results.jsonl", help="output results JSONL")
    ap.add_argument("--engine", default="gemini", choices=["gemini", "openai", "qwen"],
                    help="translation engine to benchmark (default gemini)")
    ap.add_argument("--key", help="API key for the chosen engine (else env / stored key)")
    ap.add_argument("--prod", action="store_true",
                    help="use the production server-issued key (needs VOXIS_OFFICIAL_RELEASE=1 + VOXIS_EMAIL/VOXIS_PW)")
    ap.add_argument("--voice", default="Aoede")
    args = ap.parse_args()

    key = _resolve_prod_key() if args.prod else _resolve_key(args.key, args.engine)
    clips = [json.loads(l) for l in open(args.manifest, encoding="utf-8") if l.strip()]
    print(f"Running {len(clips)} clip(s) through engine={args.engine}...")
    with open(args.out, "w", encoding="utf-8") as out:
        for i, clip in enumerate(clips, 1):
            print(f"  [{i}/{len(clips)}] {clip.get('id')} -> {clip['target_lang']}")
            try:
                rec = run_clip(clip, key, engine=args.engine, voice=args.voice)
            except Exception as e:
                print(f"    FAILED: {e}")
                continue
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
    print(f"Wrote {args.out}. Score it:  python scripts/bench/score.py {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
