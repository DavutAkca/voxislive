<!-- Reality-check of research/github-competitive-analysis.md vs actual app/ source. 22 audited: 11 PRESENT, 2 PARTIAL, 9 ABSENT. -->

# Voxis Capability Reality-Check (report claims vs real code)

# Voxis Competitive Gap Report — REALITY CHECK

> Reconciliation of `research/github-competitive-analysis.md` against the actual Voxis codebase. This document **corrects** the earlier analysis. Each row is backed by a per-capability code audit (file:line citations below).

## 1. Headline

The original gap report was **substantially inaccurate on the "missing features" axis**: of the items it flagged as gaps, **6 were already fully built and UI-reachable** (multi-language selection, SRT/VTT export, searchable transcript history, auto-update, the latency/quality preset selector, and Windows-only scope — which was *correctly* flagged but is not a "bug"). It also got one **advantage's encryption wrong** (BYOK is DPAPI, not Fernet — a *stronger* claim than written) and mis-scoped another (music-preserving M/S dubbing is real but gated behind the non-default `vbcable` backend). The report's **genuinely real gaps** are narrower than presented: **diarization, pluggable/offline backend, file/URL ingest, voice cloning, post-session LLM summary, CLI/headless, WER/BLEU harness, browser extension, "original-first" latency mask, two-pass VAD**, plus the partial **dual-column live captions**. Net: roughly **40% of the claimed gaps were phantom** — already shipped — and should be removed from the backlog before any roadmap planning.

## 2. Reality-check table

| Capability | Report claimed | Code reality (status) | UI-reachable? | Evidence (file:line) |
| --- | --- | --- | --- | --- |
| Multi-language coverage | GAP: UI exposes ~12 langs | **PRESENT** (79 langs) | Yes | `app/webui.py:39-48` LANGS (79 codes), `:257` shipped to UI; `app/web/index.html:2538-2539` both selectors populated, `:2377-2398` LANG_NAMES |
| Transcript SRT/VTT export | GAP: no subtitle export | **PRESENT** | Yes | `app/transcript_store.py:157` render_srt, `:173` render_vtt, `:191` export; `app/webui.py:830` export_session; `app/web/index.html:890-891` SRT/VTT buttons |
| Searchable transcript history | GAP: none | **PRESENT** | Yes | `app/transcript_store.py:73-115` save/list; `app/webui.py:806-849` Bridge; `app/web/index.html:870-893` modal + `#history-search`, `:613` history button |
| Auto-update | GAP: BROKEN | **PRESENT** (functional) | Yes | `app/updater.py:359` check, `:75` real pinned pubkey, `:398-430` SHA-256 verify; `app/webui.py:252,293,310-337`; `app/web/index.html:3064` banner |
| Latency/quality control | PARTIAL: dev-only presets | **PRESENT** (4 user modes) | Yes | `app/webui.py:268-276` official 4 choices; `app/web/index.html:685-686,2541,2591` `#quality` wired; `app/i18n.py:212-214` labels |
| macOS/Linux support | GAP: Windows-only | **PRESENT** (claim accurate) | n/a | `app/audio_io.py:299` WASAPI-only; `process_loopback.py`, `session_duck.py:12-13`, `win_audio.py:5` all comtypes/pycaw |
| Music-preserving M/S dubbing | ADVANTAGE | **PARTIAL** (gated to non-default backend) | No | `app/audio_io.py:497-501`, `mix_core.py:330-334`, `dsp.py:31-66` real M/S; but `pipeline.py:281-313` only on `vbcable`, `config.py:71` default=`driverless` |
| Dual-column live captions | GAP: none | **PARTIAL** (stacked, source not live; overlay target-only) | Yes | `app/web/index.html:2953` live `.tr`, `:2963-2970` stacked `.src`; `app/webui.py:159` source post-finalize; `:1106-1116` overlay single div |
| Speaker diarization | GAP: none | **ABSENT** (real gap) | No | `app/webui.py:157,179` pairs by order not speaker; `pipeline.py:164,372` only direction split; zero diariz/pyannote hits |
| Pluggable/offline backend | GAP: hardwired Gemini | **ABSENT** (real gap) | No | `app/translator.py:23` MODEL const, `:279` passed to live.connect, `:273` only genai client; no provider key |
| Voice cloning / per-speaker TTS | GAP: none | **ABSENT** (real gap) | No | `app/config.py:79` fixed GEMINI_VOICES; `translator.py:254-257` single prebuilt voice; `pipeline.py:214,392` one global voice |
| File/URL ingest | GAP: none | **ABSENT** (real gap) | No | `app/audio_io.py:265,317` live device only; zero ffmpeg/yt_dlp/soundfile hits; no file/URL UI control |
| Post-session LLM summary | GAP: none | **ABSENT** (real gap) | No | `app/transcript_store.py:149-191` only render/export; zero generate_content/recap/tldr hits |
| CLI / headless / SDK | GAP: none | **ABSENT** (real gap) | No | `main.py:143-144` sole entry → pywebview; `:119` no argparse; `:129` hard-blocks without WebView2 |
| WER/BLEU benchmark harness | GAP: none | **ABSENT** (real gap) | No | zero wer/bleu/jiwer/sacrebleu hits in app/; `scripts/` has no eval; no `tests/` dir |
| Chromium/Edge extension | GAP: none | **ABSENT** (real gap) | No | no `manifest.json` anywhere; zero manifest_version/chrome./browser.runtime hits; `client/` only has flutter/ |
| "Original-first" latency mask | GAP: none | **ABSENT** (real gap) | No | `app/pipeline.py:51-52` RTTEstimator delays original *backward*; `mix_core.py:173-178` DelayLine aligns, never substitutes |
| Two-pass WebRTC pre-gate VAD | GAP: Silero-only | **ABSENT** (real, low-impact) | No | `app/vad.py:24` only SileroVAD, `:81,93` single Silero per frame; zero webrtc hits in app/ |
| Driverless process-exclude loopback | ADVANTAGE | **PRESENT** (default path) | Yes | `app/process_loopback.py:109-131` ApplicationLoopback EXCLUDE; `pipeline.py:264-269` default, `:270-275` fallback needs echo gate |
| Session-volume ducking (no VB-CABLE) | ADVANTAGE | **PRESENT** (default path) | Yes | `app/session_duck.py:16-70` pycaw ISimpleAudioVolume, `:60-62` excludes own PID; `pipeline.py:235-261` default driverless |
| BYOK encrypted store + zero telemetry | ADVANTAGE (Fernet) | **PRESENT** (DPAPI, stronger) | Yes | `app/byok_store.py:3` DPAPI CryptProtectData; `voxis_client.py:418-421` report_usage gated off OSS; `webui.py:449-453` |
| Two-way Meeting (virtual mic) | ADVANTAGE | **PRESENT** | Yes | `app/pipeline.py:372` OutgoingPipeline, `:378-387` TTS→cable, `:549-557` two sessions; `web/index.html:645` Meeting tile |

## 3. ✅ FALSE GAPS — already built, drop from backlog

- **Multi-language (79, not 12).** Both translation-target selectors (`#hear` incoming, `#send` outgoing) are populated at runtime from `webui.py` `LANGS` (79 codes), each labeled via `LANG_NAMES` and wired back via `set_cfg`. The report appears to have miscounted the **16-entry `#uilang` interface-locale dropdown** (`index.html:783-798`) as the translation list. *No action.*
- **SRT/VTT (and TXT) export.** Fully implemented in `transcript_store.py` (`render_srt:157`, `render_vtt:173`, proper SRT-comma / VTT-dot timestamps, bilingual cues) and exposed via `Bridge.export_session` (`webui.py:830`, docstring: "available on every build") wired to History-panel buttons (`index.html:890-891`). *No action.*
- **Searchable transcript history.** Schema-v1 JSON persistence (`transcript_store.py:73-115`), `Bridge.list_sessions/load/delete` with path-traversal guards (`webui.py:806-849`), and a real History modal with a live `#history-search` that filters the session list *and* highlights matching turns (`index.html:870-893, 2780-2830`). 14 saved records on disk confirm it's live. *No action.* (Optional polish: extend list-level search past the 80-char preview to full turn bodies.)
- **Auto-update (NOT broken).** End-to-end: signature-verified manifest check at startup (`updater.py:359`), real pinned Ed25519 root (`:75`), download with Ed25519 + SHA-256 + TLS-pin verify (`:398-430`), clickable banner (`index.html:3064`), `apply_update` launches silent installer and exits (`webui.py:310-337`). The "BROKEN" label is false. *No action.* (Optional: populate `_TLS_SPKI_PINS` + ship an Authenticode cert to light up the 4th gate.)
- **Latency/quality selector is user-facing.** Official build exposes **Smooth / Fast / Callout / Saver** via `#quality` (`webui.py:268-276`, `index.html:685-686,2541,2591`, localized labels `i18n.py:212-214`). Not "developer-only." *No action.*

## 4. 🟡 PARTIAL — built but hidden/incomplete (cheap to finish)

- **Music-preserving M/S dubbing — real, but unreachable by default.** The M/S decomposition (`audio_io.py:497-501`, `mix_core.py:330-334`) and RBJ peaking-cut `DubbingDucker` (`dsp.py:31-66`) are genuine, but execute **only** on `capture_backend == "vbcable"` (`pipeline.py:281-313`); the shipped default is `driverless` (`config.py:71`) using coarse session-volume ducking, and **no UI/Bridge ever writes `capture_backend`** (grep: read-only at `config.py:71`, `pipeline.py:221,502`). **Small step:** either (a) add a settings toggle that writes `capture_backend='vbcable'` (the `capture_loopback`/`capture_vbcable` i18n strings already exist), or (b) port the M/S center-suppression onto the default driverless path so the advertised advantage is actually reachable.
- **Dual-column live captions — partial.** In-app transcript shows translated (live) + source, but **stacked vertically** (`.src` below `.turn`, `index.html:315,2963-2970`), and source is **not live** — withheld until the translation turn finalizes (`webui.py:159`). The floating overlay (`_OVERLAY_HTML`, `webui.py:1106-1116`) is **target-only, single-line**. **Small step:** (a) emit live source tokens from `_on_text` direction `'in'` instead of buffering; (b) restyle `.turn` to a 2-column grid (source left | target right) and/or add a second overlay div fed by a new `overlay_src_text()`.

## 5. ❌ REAL GAPS — confirmed missing (re-prioritized by impact)

1. **Pluggable / offline backend (highest leverage).** `translator.py:23` hardcodes `MODEL` and passes it straight into `genai.aio.live.connect` (`:279`); no provider abstraction, no model config key, no local ASR/MT/TTS anywhere — the app **cannot run without Gemini Live**. This is both a single-supplier risk (preview-model supply, per market-strategy memory) and a privacy/offline differentiator competitors offer. *Needs a `TranslatorBackend` protocol + a `translation_backend` config key + ≥1 offline engine (Whisper + local MT + Piper).*
2. **Speaker diarization.** No embedding/clustering, no per-utterance speaker tags (`webui.py:157,179` pair by ordering; `pipeline.py` split is direction-only). Named as a desired moat in market strategy. Competitors with diarization differentiate on multi-speaker meetings. *Streaming speaker-change/embedding stage feeding `transcript_store` + caption UI.*
3. **Voice cloning / per-speaker TTS.** Single global prebuilt voice (`config.py:79`, `translator.py:254-257`, `pipeline.py:214,392`). Depends on (2) and a cloning-capable TTS the current model doesn't expose. Premium dubbing differentiator.
4. **File/URL ingest (translate a recording/YouTube on demand).** Live WASAPI device only (`audio_io.py:265,317`); no decoder, no yt-dlp, no file/URL UI. High user-pull feature many competitors ship. *ffmpeg/yt-dlp → 16 kHz PCM16 → existing input queue + batch feed mode.*
5. **Post-session LLM summary.** No `generate_content`/recap path (`transcript_store.py:149-191`). Cheap, high-perceived-value meeting feature. *`summarize_session` Bridge method + History button.*
6. **CLI / headless / SDK mode.** Sole entry launches pywebview and hard-blocks without WebView2 (`main.py:129,143-144`). Blocks server/automation/integration use. *`argparse` branch → ModeController directly, bypass `_preflight_webview2`.*
7. **WER/BLEU/chrF benchmark harness.** None exist (no jiwer/sacrebleu, no `tests/`). Also a credibility/SEO asset and a regression guard against preview-model drift. *Offline `scripts/bench/` — fixtures + ground truth, re-transcribe TTS, score, baseline in CI.*
8. **Chromium/Edge extension.** No `manifest.json`/MV3 anywhere. Net-new build, not a wiring task; system-audio loopback already covers in-browser audio, so lower priority unless tab-scoped translation is strategic.
9. **"Original-first" latency mask.** Existing `DelayLine`/`RTTEstimator` do the *opposite* (hold original back to match translation, `pipeline.py:51-52`, `mix_core.py:173-178`). *Opt-in mode: original at unity on speech onset → crossfade to TTS on first chunk (`mark_tts` already detected at `pipeline.py:203-205`).*
10. **Two-pass WebRTC pre-gate VAD (low priority).** Single Silero stage (`vad.py:24,81,93`). Payoff is small — Silero is ~0.1 ms/frame on CPU. Minor optimization, not a correctness gap.

## 6. 🛡️ CONFIRMED ADVANTAGES (verified in code)

- **Driverless process-exclude WASAPI loopback** — true ApplicationLoopback `EXCLUDE_TARGET_PROCESS_TREE` on own PID (`process_loopback.py:109-131`), the **default** capture path (`pipeline.py:264-269`); own TTS excluded at OS level so it never re-translates itself (the classic fallback alone needs an echo gate).
- **Session-volume ducking without VB-CABLE** — `SessionDucker` via pycaw/`ISimpleAudioVolume`, excludes own PID (`session_duck.py:16-70`), the **default** runtime path (`pipeline.py:235-261`, `config.py:71`). No driver required.
- **Two-way Meeting into a virtual mic** — `OutgoingPipeline` runs a second Live session and injects translated TTS into VB-CABLE set as default input (`pipeline.py:372-387,521-557`); shipped Meeting tile (`index.html:645`); graceful listen-only fallback.
- **BYOK encrypted store + zero telemetry on OSS** — **correction: the cipher is Windows DPAPI** (`CryptProtectData`, CURRENT_USER + per-install entropy, `byok_store.py:3`), **not Fernet** (Fernet survives only as a legacy migrate-on-read decoder). Usage reporting hard-gated off before any network call on OSS (`voxis_client.py:418-421`); BYOK panel OSS-only (`webui.py:449-453`). The DPAPI binding is a **stronger** claim than the report's Fernet wording — update the marketing copy accordingly.

## 7. Corrected priority backlog (after removing already-done work)

**Drop entirely (phantom gaps):** multi-language, SRT/VTT export, searchable history, auto-update, latency/quality selector. These ship and are UI-reachable today.

**Tier 0 — finish the cheap partials (days):**
1. Expose `capture_backend='vbcable'` toggle (or default-path M/S) so **music-preserving dubbing** becomes reachable — turns an "advertised but hidden" advantage into a real one.
2. **Dual-column + live source captions** — emit live source tokens + 2-column grid + overlay source div.
3. **Post-session LLM summary** — one `summarize_session` Bridge method + History button; reuses existing genai client.

**Tier 1 — strategic moats (weeks):**
4. **Pluggable/offline backend** (`TranslatorBackend` protocol + config key + Whisper/local-MT/Piper) — de-risks single-supplier preview model and unlocks offline/privacy positioning.
5. **Speaker diarization** → feeds future **per-speaker/cloned voices** (items 2/3 in real-gaps) — the headline differentiator pair from the market-strategy memory.
6. **File/URL ingest** — broad user-pull, modest effort (decoder → existing queue + batch mode).

**Tier 2 — credibility & reach:**
7. **WER/BLEU harness** (`scripts/bench/`) — regression guard + SEO/credibility asset.
8. **CLI/headless mode** — unlocks automation/integration.
9. **"Original-first" latency mask** (opt-in) and **Chromium extension** — situational; defer unless validated by demand.

**Fix the copy, not the code:** correct `research/github-competitive-analysis.md` to (a) remove the 5 phantom gaps, (b) say **"DPAPI-encrypted (CURRENT_USER) BYOK store"** not Fernet, (c) re-label music-preserving dubbing as "implemented but behind the non-default vbcable backend," and (d) re-label auto-update from "BROKEN" to "functional."
