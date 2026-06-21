"""Build a small real-speech benchmark set from FLEURS (Google, CC-BY-4.0).

FLEURS is parallel: the same FLoRes sentence is recorded in every language and
shares an `id`. So FLEURS[tr] audio + FLEURS[en] transcription for the same id =
(real Turkish speech, ground-truth English reference translation) — exactly what
the benchmark needs, no manual labeling.

Audio is taken with decode=False (raw wav bytes written straight to disk), so this
needs neither torch nor torchcodec. Writes fixtures/fleurs/*.wav + manifest.jsonl.

  python scripts/bench/fetch_fleurs.py --n 8 --src tr_tr --tgt en_us
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from datasets import Audio, load_dataset

OUT_DIR = Path(__file__).resolve().parent / "fixtures" / "fleurs"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8, help="number of matched clips")
    ap.add_argument("--src", default="tr_tr", help="FLEURS source config (audio side)")
    ap.add_argument("--tgt", default="en_us", help="FLEURS target config (reference translation)")
    ap.add_argument("--tgt-lang", default="en", help="Voxis target_language code for the manifest")
    ap.add_argument("--scan", type=int, default=400, help="max target rows to scan for ids")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Scanning {args.tgt} transcriptions for reference translations...")
    tgt = load_dataset("google/fleurs", args.tgt, split="test", streaming=True)
    tgt = tgt.cast_column("audio", Audio(decode=False))  # we only read text; skip audio decode
    ref_by_id: dict[int, str] = {}
    for i, ex in enumerate(tgt):
        if i >= args.scan:
            break
        ref_by_id[ex["id"]] = ex.get("raw_transcription") or ex.get("transcription") or ""
    print(f"  collected {len(ref_by_id)} target sentences")

    print(f"Streaming {args.src} audio, matching ids...")
    src = load_dataset("google/fleurs", args.src, split="test", streaming=True)
    src = src.cast_column("audio", Audio(decode=False))  # raw wav bytes, no torchcodec

    manifest_path = OUT_DIR / "manifest.jsonl"
    written = 0
    with open(manifest_path, "w", encoding="utf-8") as mf:
        for ex in src:
            if written >= args.n:
                break
            sid = ex["id"]
            ref = ref_by_id.get(sid)
            if not ref:
                continue
            audio = ex["audio"]
            raw = audio.get("bytes")
            if not raw:
                continue
            wav_path = OUT_DIR / f"fleurs_{args.src}_{sid}.wav"
            wav_path.write_bytes(raw)  # FLEURS audio is already a 16 kHz wav
            rec = {
                "id": f"fleurs_{sid}",
                "audio": str(wav_path.relative_to(Path(__file__).resolve().parents[2])).replace("\\", "/"),
                "target_lang": args.tgt_lang,
                "reference": ref.strip(),
                "source_ref": (ex.get("raw_transcription") or ex.get("transcription") or "").strip(),
            }
            mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            print(f"  [{written}/{args.n}] id={sid}  TR: {rec['source_ref'][:45]}...  -> EN: {ref[:45]}...")

    print(f"\nWrote {written} clips + {manifest_path}")
    if written == 0:
        print("No id overlap found — try a larger --scan.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
