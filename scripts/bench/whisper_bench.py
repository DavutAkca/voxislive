"""Head-to-head baseline: run the SAME fixtures through faster-whisper (the engine
class every Whisper-based OSS competitor — WhisperLive, Synthalingua, RTranslator,
etc. — is built on) so score.py compares it to Voxis on identical audio + refs.

Whisper's `translate` task is X->English, matching the tr->en fixtures. This is an
OFFLINE/batch run, so latency is NOT a live-latency comparison (left null); the
honest comparison here is translation/recognition QUALITY on the same clips.

  python scripts/bench/whisper_bench.py scripts/bench/fixtures/fleurs/manifest.jsonl \
      -o scripts/bench/results_whisper.jsonl --model large-v3
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("-o", "--out", default="results_whisper.jsonl")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--compute", default="int8", help="int8 (fast CPU) / float16 / float32")
    args = ap.parse_args()

    from faster_whisper import WhisperModel
    print(f"Loading faster-whisper {args.model} ({args.compute}, CPU)... (first run downloads the model)")
    model = WhisperModel(args.model, device="cpu", compute_type=args.compute)

    clips = [json.loads(line) for line in open(args.manifest, encoding="utf-8")
             if line.strip()]
    print(f"Running {len(clips)} clip(s)...")
    with open(args.out, "w", encoding="utf-8") as out:
        for i, clip in enumerate(clips, 1):
            wav = str(ROOT / clip["audio"])
            t0 = time.monotonic()
            seg_en, _ = model.transcribe(wav, task="translate", language="tr", beam_size=5)
            en = " ".join(s.text.strip() for s in seg_en).strip()
            seg_tr, _ = model.transcribe(wav, task="transcribe", language="tr", beam_size=5)
            tr = " ".join(s.text.strip() for s in seg_tr).strip()
            proc = round(time.monotonic() - t0, 2)
            rec = {
                "id": clip.get("id"),
                "target_lang": clip["target_lang"],
                "reference": clip.get("reference", ""),
                "hypothesis": en,
                "source_ref": clip.get("source_ref", ""),
                "source_heard": tr,
                "latency_s": None,            # batch run — not a live-latency number
                "proc_s": proc,               # wall-clock to translate+transcribe the clip
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            print(f"  [{i}/{len(clips)}] {clip.get('id')}  ({proc}s)  EN: {en[:60]}...")
    print(f"Wrote {args.out}.  Score:  python scripts/bench/score.py {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
