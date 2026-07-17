# Voxis translation benchmark harness

Measures translation/recognition quality and latency so a preview-model regression
(or a backend swap) is caught with a number instead of a vibe. Two steps:

```
# 1) Run clips through the real translator (needs a BYOK key + network; uses minutes)
python scripts/bench/run_session.py scripts/bench/fixtures/manifest.example.jsonl -o results.jsonl

# 2) Score the captured results (pure offline, no key)
python scripts/bench/score.py results.jsonl
# quick smoke test with no data:
python scripts/bench/score.py --selftest
```

## What it reports

| Metric | Tool | Meaning | Good |
| --- | --- | --- | --- |
| **BLEU** | sacrebleu | word-overlap translation quality | higher |
| **chrF** | sacrebleu | character-level quality — **the metric to trust for Turkish** (word-BLEU under-credits valid inflection) | higher |
| **WER** | jiwer | how well the model *heard* the source (its input transcription vs ground truth) | lower |
| **latency** | — | onset → first translated audio, mean/p50/p95 | lower |

> For a publishable quality number, add **COMET** (`pip install unbabel-comet`) — a neural
> metric that correlates best with human judgment. It is heavier (downloads a model), so
> it is intentionally left out of the default path; wire it into `score.py` when you want it.

## Fixtures

A fixture is a clip with ground truth. Manifest is JSONL, one clip per line:

```json
{"id":"c1","audio":"scripts/bench/fixtures/c1.wav","target_lang":"en","reference":"the cat is on the couch","source_ref":"kedi koltukta"}
```

`reference` = the correct translation, `source_ref` = the correct source transcript.
`audio` is any wav/flac (auto-downmixed + resampled to 16 kHz).

Where to get real fixtures (audio + transcript + reference translation, many language pairs):

- **FLEURS** (`google/fleurs` on HuggingFace) — 102 languages, read speech, has transcripts; pair with a reference translation set.
- **CoVoST 2** — speech-to-text translation, 21→en and en→15, has source transcript + target translation.
- **CVSS** — speech-to-**speech** translation corpus (closest to what Voxis does).

Start small: 20–50 clips per language pair is enough to track regressions. Keep the set fixed so numbers are comparable across runs; commit the manifest, not the audio.

## Notes / caveats

- `run_session.py` feeds audio at **realtime** so the latency number is realistic; pass a
  short `drain` tail so the simultaneous model finishes the last utterance.
- It consumes **billed Gemini minutes** — it is a dev/CI tool, not user-facing.
- The translation hypothesis is captured from the model's own `output_audio_transcription`
  (`on_text('out')`) — i.e. the text it speaks — so BLEU/chrF score the translation directly,
  no re-transcription error. To also measure the TTS→intelligibility loop, re-transcribe the
  output audio with Whisper and score that separately.
- Regression gate (implemented): a committed baseline lives at `scripts/bench/baseline.json`
  (production-representative, from `results_prod.jsonl`). After a fresh run, gate it:

  ```
  python scripts/bench/score.py results.jsonl --check scripts/bench/baseline.json \
      --max-chrf-drop 2.0 --max-p95-rise 1.0
  ```

  It exits non-zero if chrF fell more than `--max-chrf-drop` points or p95 latency rose more
  than `--max-p95-rise` s vs the baseline — the regression guard the competitive analysis flagged.
  This CANNOT auto-run in CI unbudgeted: `run_session.py` needs a BYOK key + network + billed
  minutes, so wire it as a manual/scheduled dispatch that captures a results file first, then runs
  this gate on it. Regenerate the baseline when the shipped routing/config changes:

  ```
  python scripts/bench/score.py results.jsonl --write-baseline scripts/bench/baseline.json
  ```
