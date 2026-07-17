"""Offline quality scorer for Voxis translation runs.

Reads a JSONL of per-utterance records produced by run_session.py and reports:
  * BLEU  (sacrebleu, 13a tok)   — translation adequacy/fluency vs a reference
  * chrF  (sacrebleu)            — character-level; the RELIABLE metric for
                                   morphologically rich targets like Turkish,
                                   where word-BLEU under-credits valid inflection
  * WER   (jiwer)               — how well the model HEARD the source (its input
                                   transcription vs the ground-truth transcript)
  * latency stats (mean/p50/p95) over per-utterance onset->first-audio seconds

Pure offline: no API key, no network. Run it on a results file or with --selftest.

Record schema (one JSON object per line):
  {
    "id": "fleurs_tr_0001",
    "target_lang": "en",
    "reference":   "<ground-truth translation in target language>",
    "hypothesis":  "<Voxis translation = on_text('out') text>",
    "source_ref":  "<optional: ground-truth source transcript>",
    "source_heard":"<optional: Voxis input transcription = on_text('in') text>",
    "latency_s":   1.42
  }
reference + hypothesis drive BLEU/chrF; source_ref + source_heard drive WER;
latency_s drives the latency summary. Missing fields are skipped, not fatal.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata


def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, NFKC. Applied before WER
    so casing/punctuation differences are not counted as recognition errors."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def score(records: list[dict]) -> dict:
    import sacrebleu

    hyps = [r["hypothesis"] for r in records if r.get("hypothesis") and r.get("reference")]
    refs = [r["reference"] for r in records if r.get("hypothesis") and r.get("reference")]

    out: dict = {"n_translation": len(hyps)}
    if hyps:
        out["bleu"] = round(sacrebleu.corpus_bleu(hyps, [refs]).score, 2)
        out["chrf"] = round(sacrebleu.corpus_chrf(hyps, [refs]).score, 2)

    # WER on the ASR side (what the model heard vs ground truth), normalized.
    asr = [(r["source_ref"], r["source_heard"]) for r in records
           if r.get("source_ref") and r.get("source_heard")]
    if asr:
        import jiwer
        ref_n = [_norm(a) for a, _ in asr]
        hyp_n = [_norm(b) for _, b in asr]
        out["n_asr"] = len(asr)
        out["wer"] = round(jiwer.wer(ref_n, hyp_n) * 100, 2)

    lats = [float(r["latency_s"]) for r in records if isinstance(r.get("latency_s"), (int, float))]
    if lats:
        out["latency"] = {
            "n": len(lats),
            "mean_s": round(sum(lats) / len(lats), 3),
            "p50_s": round(_percentile(lats, 0.50), 3),
            "p95_s": round(_percentile(lats, 0.95), 3),
        }
    return out


def _print_report(res: dict, label: str) -> None:
    print(f"\n=== Voxis quality report: {label} ===")
    if "bleu" in res:
        print(f"  Translation (n={res['n_translation']}):  BLEU {res['bleu']}   chrF {res['chrf']}"
              f"   <- chrF is the trustworthy one for Turkish")
    else:
        print("  Translation: no (reference, hypothesis) pairs found")
    if "wer" in res:
        print(f"  ASR/heard   (n={res['n_asr']}):  WER {res['wer']}%   (lower = heard the source better)")
    if "latency" in res:
        L = res["latency"]
        print(f"  Latency     (n={L['n']}):  mean {L['mean_s']}s   p50 {L['p50_s']}s   p95 {L['p95_s']}s")
    print()


def _check_regression(res: dict, baseline: dict, max_chrf_drop: float,
                      max_p95_rise: float) -> list[str]:
    """Compare a fresh result against a stored baseline. Returns a list of breach
    messages (empty = pass): chrF must not fall more than max_chrf_drop points,
    and p95 latency must not rise more than max_p95_rise seconds. Metrics missing
    on either side are skipped, not failed."""
    breaches: list[str] = []
    base_chrf, cur_chrf = baseline.get("chrf"), res.get("chrf")
    if base_chrf is not None and cur_chrf is not None:
        drop = base_chrf - cur_chrf
        if drop > max_chrf_drop:
            breaches.append(
                f"chrF regressed {drop:.2f} pts (baseline {base_chrf} -> {cur_chrf}, "
                f"allowed drop {max_chrf_drop})")
    base_p95 = (baseline.get("latency") or {}).get("p95_s")
    cur_p95 = (res.get("latency") or {}).get("p95_s")
    if base_p95 is not None and cur_p95 is not None:
        rise = cur_p95 - base_p95
        if rise > max_p95_rise:
            breaches.append(
                f"p95 latency rose {rise:.3f}s (baseline {base_p95}s -> {cur_p95}s, "
                f"allowed rise {max_p95_rise}s)")
    return breaches


_SELFTEST = [
    {"id": "t1", "target_lang": "en",
     "reference": "the cat is sleeping on the couch",
     "hypothesis": "the cat sleeps on the sofa",
     "source_ref": "kedi koltukta uyuyor", "source_heard": "kedi koltukta uyuyor", "latency_s": 1.2},
    {"id": "t2", "target_lang": "en",
     "reference": "i will call you tomorrow morning",
     "hypothesis": "i will call you tomorrow morning",
     "source_ref": "seni yarin sabah ararim", "source_heard": "seni yarin sabah arrim", "latency_s": 1.8},
    {"id": "t3", "target_lang": "en",
     "reference": "the meeting was postponed to friday",
     "hypothesis": "the meeting got moved to friday",
     "source_ref": "toplanti cumaya ertelendi", "source_heard": "toplanti cumaya ertelendi", "latency_s": 1.5},
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Score Voxis translation runs (BLEU/chrF/WER/latency).")
    ap.add_argument("results", nargs="?", help="JSONL of run records (omit with --selftest)")
    ap.add_argument("--selftest", action="store_true", help="run a built-in example and exit")
    ap.add_argument("--json", action="store_true", help="emit the raw metrics as JSON too")
    ap.add_argument("--check", metavar="BASELINE",
                    help="compare metrics against a baseline JSON and exit non-zero on regression")
    ap.add_argument("--max-chrf-drop", type=float, default=2.0,
                    help="max allowed chrF drop vs baseline before failing (default 2.0)")
    ap.add_argument("--max-p95-rise", type=float, default=1.0,
                    help="max allowed p95 latency rise (s) vs baseline before failing (default 1.0)")
    ap.add_argument("--write-baseline", metavar="PATH",
                    help="write the computed metrics to PATH as a baseline JSON and exit")
    args = ap.parse_args()

    if args.selftest:
        records = _SELFTEST
        label = "selftest"
    elif args.results:
        with open(args.results, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        label = args.results
    else:
        ap.error("pass a results JSONL path or --selftest")
        return 2

    res = score(records)
    _print_report(res, label)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))

    if args.write_baseline:
        with open(args.write_baseline, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(f"baseline written: {args.write_baseline}")
        return 0

    if args.check:
        with open(args.check, encoding="utf-8") as f:
            baseline = json.load(f)
        breaches = _check_regression(res, baseline, args.max_chrf_drop, args.max_p95_rise)
        if breaches:
            print("REGRESSION - bench gate FAILED:")
            for b in breaches:
                print(f"  - {b}")
            return 1
        print(f"bench gate PASSED (chrF drop <= {args.max_chrf_drop} pts, "
              f"p95 rise <= {args.max_p95_rise}s vs {args.check})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
