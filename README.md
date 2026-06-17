# Voxis Live

**[English]** | **[Türkçe](README.tr.md)** | **[Deutsch](README.de.md)**

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11--3.13-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)

> Real-time voice translation for Windows — translate any video, game, or meeting and hear it in your own language, live.
>
> Brand: **Voxis** · Site: **[voxislive.com](https://voxislive.com)**

**📖 Guide:** [Developer / BYOK setup](docs/INSTALL_BYOK.md) — end-user (setup.exe) docs live at [voxislive.com](https://voxislive.com).

---

## Overview

Voxis captures your Windows system audio (a video, a game, the other side of a call), streams it to Google's **Gemini Live** translation model, and plays back a spoken translation in your target language — while it is still being spoken.

It uses `gemini-3.5-live-translate-preview`, a **native simultaneous speech-to-speech** model: it translates continuously as the speaker talks and self-balances quality versus sync, staying a few seconds behind (the way a human simultaneous interpreter does). There is no separate speech-to-text → translate → text-to-speech chain; audio goes in, translated audio comes out.

Two operating modes:

- **Video / Game** — one-way incoming translation; the original audio is ducked while the translation speaks.
- **Meeting** — two-way: the other party's voice is translated into your language (to your headphones), and your voice is translated into their language and fed into the call as a virtual microphone.

---

## How it works

```
Windows audio ──► Capture ──► Silero VAD gate ──► Gemini Live (translate) ──► Player ──► Headphones
                (loopback /     (filters non-                                 (limiter,
                 VB-CABLE)        speech)                                      stereo mix)
```

- **Capture** — two paths:
  - *Driverless* (default, no install): WASAPI process-exclude loopback (Windows 10 2004+) reads the system mix and excludes Voxis's own output, so it never re-translates its own voice. Other apps are ducked at the source via the Windows session-volume API.
  - *VB-CABLE*: the audio is intercepted before the speakers, so the engine can apply real DSP — M/S center-suppression ducks the original dialogue while preserving stereo music, and a fractional delay line RTT-aligns the original with the translation.
- **VAD gate** — Silero VAD v5 (ONNX, CPU) filters out music/noise so only speech reaches the cloud.
- **Translation** — a `LiveTranslator` thread holds a Gemini Live WebSocket session and streams 16 kHz PCM in, 24 kHz translated audio out.
- **Playback** — a stereo mixer with a look-ahead brick-wall limiter; the translation sits in the phantom center.

---

## Quick start (developer build)

```powershell
git clone https://github.com/DavutAkca/voxislive.git
cd voxislive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Python 3.11–3.13 (64-bit).** Python 3.14 is not supported yet: numpy / onnxruntime have no stable cp314 wheels at the pinned versions, so `pip install` would fail.

Run it:

```powershell
python main.py            # GUI
```

The open-source build is **BYOK** (bring your own key). On first launch open
**Settings → API key** and paste your Gemini key (from
<https://aistudio.google.com/>); it is stored **encrypted** under `profiles/byok`
(via Windows DPAPI, bound to your Windows account), never in a plaintext `.env`. Full walkthrough:
[docs/INSTALL_BYOK.md](docs/INSTALL_BYOK.md).

List your audio devices any time with `python -m app.audio_io`.

---

## Build flavors — `IS_OFFICIAL_RELEASE`

Voxis ships in two flavors, selected at build time by `IS_OFFICIAL_RELEASE` (env var `VOXIS_OFFICIAL_RELEASE=1/0`, default `False`).

| | Official SaaS `.exe` (`True`) | Open-source / developer (`False`) |
| --- | --- | --- |
| API key | Fetched from the server per session; no key UI | Your own key (BYOK), entered in Settings |
| Auth | Sign in (PocketBase) | None — local, offline |
| Telemetry / billing | Usage heartbeat to the server | Fully disabled |
| Translation settings | Locked to the best simultaneous defaults | All settings exposed for tuning |

`start.bat` leaves `VOXIS_OFFICIAL_RELEASE` unset, so a launch from source defaults to the BYOK / developer path (your own key — no server, no auth). The official SaaS `.exe` is produced separately by `release.py`, whose build step writes the `OFFICIAL` marker into the frozen bundle.

**Network surface of the open-source build.** A frozen developer build carries no `OFFICIAL` marker, so it resolves to BYOK and makes **no outbound calls of its own**: registration, login, verification, quota, server session-key fetch, usage heartbeat, and all telemetry are bypassed or hard-gated to local mock responses. The only network it touches is the Gemini Live WebSocket your own key opens. The auto-update check runs in frozen builds only and depends on `update_check_url`, which is empty by default. The public repo is kept free of any closed-core path or live secret by a release-hygiene gate (`scripts/check_release_hygiene.py`, wired into CI and a pre-push hook).

---

## Meeting mode setup (two-way translation)

**Goal:** you speak Turkish → the other side hears English; the other side speaks English → you hear Turkish.

The two directions have different requirements:

| Direction | What it does | Requirement |
| --- | --- | --- |
| **Incoming** (you hear them in your language) | Listens to system audio, translates, plays to your headphones | **No extra install** |
| **Outgoing** (your voice goes out translated) | Translates your mic, feeds a virtual microphone | **A virtual microphone (VB-CABLE) is required** |

> On Windows the only way to present a "microphone" that a meeting app (Teams/Zoom/Meet) can select is a virtual audio driver — so the outgoing direction needs VB-CABLE. Without one, meetings run in **listen-only** mode automatically (you understand them; your voice goes out untranslated).

### 1. Install VB-CABLE (one-time, free)
1. Download from <https://vb-audio.com/Cable/>.
2. Unzip → right-click `VBCABLE_Setup_x64.exe` → **Run as administrator** → **Install Driver** → **reboot**.
3. Two devices appear: **CABLE Input** (playback) and **CABLE Output** (recording).

### 2. Configure Voxis
- Set the languages in the panel: **I hear: Turkish**, **To others: English**.
- Settings → **Output device**: your real headphones · **Microphone**: your real mic — the one you speak into; Voxis listens here.
- **The virtual cable is auto-detected.** On launch Voxis finds an installed cable (VB-CABLE / VB-Audio / VoiceMeeter) and wires the meeting routing itself — no `config.json` editing.

### 3. Configure the meeting app (Teams / Zoom / Meet)
- Set the **microphone** to **"CABLE Output (VB-Audio Virtual Cable)"** — the *recording* side of the cable (`CABLE Output`, **not** `CABLE Input`). This is the meeting app's mic, not the real mic you picked in Voxis: Voxis writes your translated English into the cable and the meeting app reads it back from here.
- If more than one virtual cable is installed (e.g. VB-Audio Point, VoiceMeeter), pick the **VB-Audio Virtual Cable** pair — that is what Voxis auto-wires by default.
- Leave speaker/output as your own headphones.

### 4. Use it
Start Voxis → **Meeting** mode (`Ctrl+Alt+2`). Speak Turkish → it goes out as English; they speak English → you hear Turkish.

---

## Latency & simultaneous translation

The end-to-end delay is roughly **the sentence length plus a few seconds** — that lag is the translation model's designed *ear-voice span* (it waits for enough context to translate correctly, exactly as a human interpreter does) and is **not tunable from the client**. There is no Google-side "go faster" setting, and this is the latest/only translate model.

What Voxis *does* optimize on the client side: it feeds the model a continuous stream (the model's documented native setup — no client-side endpointing config is sent), warms the connection before capture so the first sentence skips the cold handshake, disables WebSocket compression, keeps a small drop-oldest input buffer, and runs VAD on the CPU. These trim the controllable edges — not the model's core lag.

---

## Configuration reference

`config.json` (gitignored; defaults live in `app/config.py`):

| Key | Meaning |
| --- | --- |
| `target_language_incoming` / `target_language_outgoing` | Your language / the other party's language |
| `capture_backend` | `"driverless"` (WASAPI loopback) or `"vbcable"` |
| `original_audio` | `"duck"` · `"mute_during_speech"` · `"mix"` |
| `duck_gain` | Original level while the translation speaks (0–1) |
| `quality_preset` | `max_quality` · `balanced` · `max_savings` · `turbo` |
| `gemini_voice` / `gemini_temperature` | Prebuilt voice · sampling temperature |
| `tts_volume` | Translation playback volume |
| `session_rotate_minutes` | Live session rotation (before the 15-min ceiling) |

**Quality presets** map to the local VAD gate that shapes the continuous stream sent to the model. `max_savings` ("Saver") gates the stream — only speech is sent, silence gaps are dropped — to use fewer billed minutes. The official build surfaces three friendly options (**Smooth** = `balanced`, **Fast** = `turbo`, **Saver** = `max_savings`); the developer build exposes the full preset list (`max_quality`, `balanced`, `max_savings`, `turbo`).

The translate model is a native simultaneous interpreter, so the client sends no endpointing configuration — it feeds a continuous stream and lets the model own its own endpointing.

**Interface languages** (the app UI) are **Turkish and English only** — set via `ui_language`. **Translation target languages** (what the model translates *into*) are independent and cover: `tr, en, de, fr, es, it, pt, ru, ar, ja, ko, zh-Hans` (set via `target_language_incoming` / `target_language_outgoing`).

---

## Architecture (module map)

| Module | Responsibility |
| --- | --- |
| `app/config.py` | Config load/save, `DEFAULTS`, `QUALITY_PRESETS`, `IS_OFFICIAL_RELEASE`, gate helpers |
| `app/audio_io.py` | Device discovery, loopback capture, `Player` (stereo mix + limiter), virtual-cable detection |
| `app/process_loopback.py` | Process-exclude WASAPI loopback (driverless) |
| `app/session_duck.py` | Source-level ducking via the Windows session-volume API |
| `app/vad.py` | Silero VAD (CPU) + `SpeechGate` |
| `app/translator.py` | `LiveTranslator` — Gemini Live session, native simultaneous translation, rotation |
| `app/pipeline.py` | `IncomingPipeline`, `OutgoingPipeline`, `ModeController` |
| `app/mix_core.py` / `app/dsp.py` | Look-ahead limiter, delay line, M/S center-suppression |
| `app/byok_store.py` | DPAPI-encrypted local key storage (developer build) |
| `app/voxis_client.py` | Auth-core HTTP client (official build) |
| `app/webui.py` + `app/web/index.html` | pywebview bridge + single-file UI |

An optional `premium/` package (open-core hook, gitignored) can provide ONNX vocal/instrument separation; when absent, the deterministic M/S center-suppression fallback is used.

The SaaS backend (`backend/auth-core/`, Go + PocketBase, behind Caddy on `voxislive.com`) issues per-session keys and records usage; the open-source build never contacts it.

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `API key not valid` | Invalid/empty key (BYOK), or running the dev build without a key | Enter a full Gemini key in Settings, or launch with `VOXIS_OFFICIAL_RELEASE=1` to use the server key |
| Meeting is listen-only | No virtual microphone installed | Install VB-CABLE (see above) |
| `PaError -9999` | Stale WASAPI device list | Unplug/replug the USB audio device, restart |
| No translation output is routed | Output set to a virtual cable (feedback loop) | Point `headphones_output` at your real device |

---

## License — PolyForm Noncommercial 1.0.0

Licensed under the **PolyForm Noncommercial License 1.0.0**; full text in [LICENSE](LICENSE).

- ✅ Free to use for personal, hobby, research, and non-commercial purposes.
- ❌ Commercial use, resale, white-label, and revenue-generating deployments are prohibited.

**Commercial licensing** (commercial products, SaaS, white-label): **<https://voxislive.com/licensing>**.

Contributions are welcome — by opening a pull request you agree your contribution is licensed under the same terms and may be incorporated with attribution in the project history.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/DavutAkca/voxislive/issues)
- **Commercial inquiries:** <https://voxislive.com/licensing>

*Voxis Live — real-time, simultaneous voice translation.*
