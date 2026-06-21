"""Headless benchmark runner: feed audio clips through the real Voxis translator
and capture the translation text, the model's source transcription, and the
onset->first-audio latency — then score.py turns those into BLEU/chrF/WER.

This DOES hit Gemini Live: it needs a valid BYOK key and network, and it consumes
billed minutes. It is a dev/CI tool, never shipped to end users.

Key resolution order: --key  >  $VOXIS_BENCH_KEY  >  local BYOK store ("developer").

Fixtures manifest (JSONL), one clip per line:
  {"id":"c1","audio":"fixtures/clip1.wav","target_lang":"en",
   "reference":"<ground-truth translation>","source_ref":"<ground-truth source text>"}

Usage:
  python scripts/bench/run_session.py fixtures/manifest.jsonl -o results.jsonl
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


def _resolve_key(cli_key: str | None) -> str:
    if cli_key:
        return cli_key
    env = os.environ.get("VOXIS_BENCH_KEY")
    if env:
        return env
    try:
        from app import byok_store
        rec = byok_store.load_byok("developer")
        k = rec.get("gemini") if isinstance(rec, dict) else rec
        if k:
            return k
    except Exception:
        pass
    raise SystemExit("No Gemini key. Pass --key, set VOXIS_BENCH_KEY, or store a BYOK key.")


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
    key, err = voxis_client.get_session_key()
    if err or not key:
        raise SystemExit(f"session-key failed: {err}")
    print("Production session key acquired (not printed).")
    return key


def _load_pcm16_16k(path: str) -> bytes:
    """Read any wav/flac, downmix to mono, resample to 16 kHz, return PCM16 bytes."""
    import soundfile as sf
    from app.audio_io import _make_resampler

    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)  # mono
    if sr != SR:
        x = _make_resampler(sr, SR)(np.ascontiguousarray(x))
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


def run_clip(clip: dict, api_key: str, *, voice: str = "Aoede", drain_s: float = 8.0) -> dict:
    from app.translator import LiveTranslator

    heard: list[str] = []   # on_text("in", ...)  -> source transcription
    trans: list[str] = []   # on_text("out", ...) -> translation
    first_audio = {"t": None}
    started = {"t": None}
    lock = threading.Lock()

    def on_text(direction: str, text: str):
        with lock:
            (heard if direction == "in" else trans).append(text)

    def on_audio(_data: bytes):
        with lock:
            if first_audio["t"] is None:
                first_audio["t"] = time.monotonic()

    def on_status(_msg: str):
        pass

    tr = LiveTranslator(
        api_key, clip["target_lang"],
        on_audio=on_audio, on_text=on_text, on_status=on_status,
        rotate_minutes=13, name="bench", voice=voice, temperature=0.3,
    )
    tr.start()
    tr.wait_ready(timeout=15)

    pcm = _load_pcm16_16k(str(ROOT / clip["audio"]) if not os.path.isabs(clip["audio"]) else clip["audio"])
    started["t"] = time.monotonic()
    # Feed at realtime so the measured first-audio latency is realistic.
    step = FRAME * 2  # bytes per frame (int16)
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
        "target_lang": clip["target_lang"],
        "reference": clip.get("reference", ""),
        "hypothesis": " ".join(trans).strip(),
        "source_ref": clip.get("source_ref", ""),
        "source_heard": " ".join(heard).strip(),
        "latency_s": lat,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Voxis over a fixtures manifest and capture results.")
    ap.add_argument("manifest", help="JSONL of clips (id, audio, target_lang, reference, source_ref)")
    ap.add_argument("-o", "--out", default="results.jsonl", help="output results JSONL")
    ap.add_argument("--key", help="Gemini API key (else env VOXIS_BENCH_KEY or BYOK store)")
    ap.add_argument("--prod", action="store_true",
                    help="use the production server-issued key (needs VOXIS_OFFICIAL_RELEASE=1 + VOXIS_EMAIL/VOXIS_PW)")
    ap.add_argument("--voice", default="Aoede")
    args = ap.parse_args()

    key = _resolve_prod_key() if args.prod else _resolve_key(args.key)
    clips = [json.loads(l) for l in open(args.manifest, encoding="utf-8") if l.strip()]
    print(f"Running {len(clips)} clip(s) through Gemini Live...")
    with open(args.out, "w", encoding="utf-8") as out:
        for i, clip in enumerate(clips, 1):
            print(f"  [{i}/{len(clips)}] {clip.get('id')} -> {clip['target_lang']}")
            try:
                rec = run_clip(clip, key, voice=args.voice)
            except Exception as e:
                print(f"    FAILED: {e}")
                continue
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
    print(f"Wrote {args.out}. Score it:  python scripts/bench/score.py {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
