# Voxis — Developer / BYOK Setup

> This guide is for the **open-source GitHub build**. You run Voxis from source and
> supply your **own Google Gemini API key** (BYOK — Bring Your Own Key). No account,
> no server, no telemetry — the developer build never contacts the Voxis backend.
>
> Looking for the one-click installer with a membership? That is the official build
> from **voxislive.com** — end-user install docs are published there.

---

## 1. Prerequisites

- **Windows 10 (2004 / May 2020) or newer**, 64-bit.
- **Python 3.11–3.13** (64-bit; the shipped builds use 3.13). Python 3.14 is not
  supported yet — numpy / onnxruntime have no stable cp314 wheels at the pinned versions.
- A **Google Gemini API key** — free from <https://aistudio.google.com/>.
- Headphones; a microphone too if you want Meeting mode.

---

## 2. Get the code and install dependencies

```powershell
git clone https://github.com/DavutAkca/voxislive.git
cd voxislive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 3. Provide your Gemini key (BYOK)

The developer build (`IS_OFFICIAL_RELEASE = False`, the default) resolves the key
locally — it is **never** fetched from a server. The engine reads the key from the
encrypted BYOK store; supply it through the app:

**In the app (the supported path).** Launch Voxis, open **Settings → BYOK**, paste
your Gemini key, and save. It is stored encrypted at rest with **Windows DPAPI**
(CryptProtectData, CURRENT_USER scope — bound to your Windows account) plus a
per-install entropy secret, as a `.enc` file in a local `developer` slot under
`profiles/byok/`. On the next session start the engine loads the key from there.
(Legacy slots from an older build used a Fernet key and are re-wrapped with DPAPI
the first time they are read.)

> **About `.env`:** a `.env` in the repo root is loaded at startup (`load_dotenv`),
> but the engine does **not** read `GEMINI_API_KEY` from the environment — the key
> path is the encrypted BYOK store above, so a bare `GEMINI_API_KEY=...` line is not
> consumed. `.env` is used only for runtime variables such as the persisted
> `VOXIS_JWT_TOKEN`. Use the in-app BYOK panel to provide your Gemini key.

> The BYOK panel is only visible in the developer build. The official build hides it
> and uses the server-issued session key instead.

---

## 4. Run

```powershell
python main.py
```

List your audio devices any time:

```powershell
python -m app.audio_io
```

When run from source, runtime data (`config.json`, `profiles/`, `transcripts/`,
`.env`) stays in the repo root — handy for development. In a frozen build it moves
to `%APPDATA%\Voxis` (see `app/paths.py`).

---

## 5. Modes & hotkeys

| Mode | What it does | Hotkey |
| --- | --- | --- |
| Video / Game | One-way incoming translation; original audio is ducked | `Ctrl+Alt+1` |
| Meeting | Two-way; needs a virtual mic (VB-CABLE) for the outgoing direction | `Ctrl+Alt+2` |
| Stop | Stops the active mode | `Ctrl+Alt+0` |
| Overlay | Toggles the subtitle overlay | `Ctrl+Alt+O` |

**Capture backends:** `driverless` (default — WASAPI process-exclude loopback, no
install) or `vbcable`. Set via `capture_backend` in `config.json`.

---

## 6. Meeting mode (VB-CABLE)

Two-way translation needs a virtual microphone so a meeting app can select your
translated voice. Install **VB-CABLE** (free, one-time) and point your meeting app's
microphone at **CABLE Output**. Full step-by-step is in the main
[README → Meeting mode setup](../README.md#meeting-mode-setup-two-way-translation).
Without a virtual cable, Meeting mode runs in **listen-only** automatically.

---

## 7. Building your own executable (optional)

`app/build_official.py` is the production pipeline: it runs PyInstaller (one-folder),
bundles `web/`, `models/`, and `assets/`, then compiles an installer with **Inno Setup**
(`installer/voxis.iss`) into `production_release/`.

```powershell
.\.venv\Scripts\python.exe app\build_official.py
```

- Requires **PyInstaller** (`pip install pyinstaller`) and, for the `setup.exe`,
  **[Inno Setup 6](https://jrsoftware.org/isdl.php)** on `PATH` or in its default
  location. Without Inno Setup the script falls back to producing a `.zip` bundle.
- **The committed source is never mutated to pick a flavor.** The pipeline asserts
  `config.py` is flavor-neutral, then writes an `OFFICIAL` marker file into the frozen
  bundle's `_internal/` directory; that marker is what selects the SaaS flavor at
  runtime (`app/config._resolve_official_release`). The flavor is a property of the
  built artifact, not of how it was launched — so there is nothing to restore and the
  git tree stays clean.
- This produces the **official (SaaS) flavor**. The open-source / BYOK flavor is the
  default: an unmodified build ships no `OFFICIAL` marker, so it stays BYOK with no
  source change needed.

---

## 8. Build flavors

| | Official SaaS `.exe` | Open-source / developer (this guide) |
| --- | --- | --- |
| API key | Server-issued per session | Your own (BYOK) |
| Auth | PocketBase sign-in | None — local, offline |
| Telemetry / billing | Usage heartbeat | Fully disabled |
| Translation settings | Locked to best defaults | All presets exposed |

Selected at build time by `IS_OFFICIAL_RELEASE` (env override
`VOXIS_OFFICIAL_RELEASE=1/0`, default `False`).

---

## See also

- [README.md](../README.md) — architecture, module map, configuration reference.
- [voxislive.com](https://voxislive.com) — end-user installer guide (published on the site).
