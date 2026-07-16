# Voxis Live

**[English](README.md)** | **[Türkçe](README.tr.md)** | **[Deutsch]**

![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/python-3.11--3.13-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue)

> Echtzeit-Sprachübersetzung für Windows — übersetze jedes Video, Spiel oder Meeting und höre es live in deiner eigenen Sprache.
>
> Marke: **Voxis** · Website: **[voxislive.com](https://voxislive.com)**

**📖 Anleitung:** [Entwickler / BYOK-Setup](docs/INSTALL_BYOK.md) — die Endnutzer-App wird über den **Microsoft Store** ausgeliefert; Setup-Doku unter [voxislive.com](https://voxislive.com).

> [!WARNING]
> **Laden Sie nur von [voxislive.com](https://voxislive.com), dem Microsoft-Store-Eintrag oder diesem Repository (`github.com/DavutAkca/voxislive`) herunter.** Kopien dieses Repositorys wurden auf anderen GitHub-Konten gefunden, einige leiten zu Installern auf fremden Seiten weiter — diese sind **nicht offiziell** und können schädlich sein. Dieses Projekt verteilt niemals einen Installer über eine Drittanbieter-Seite oder ein anderes GitHub-Konto. Wenn Sie eine verdächtige Kopie finden, melden Sie sie bitte: [support@voxislive.com](mailto:support@voxislive.com).

---

## Überblick

Voxis erfasst dein Windows-Systemaudio (ein Video, ein Spiel, die Gegenseite eines Anrufs), streamt es an Googles Übersetzungsmodell **Gemini Live** und spielt eine gesprochene Übersetzung in deiner Zielsprache ab — noch während gesprochen wird.

Es verwendet `gemini-3.5-live-translate-preview`, ein Modell für **native simultane Sprache-zu-Sprache-Übersetzung**: Es übersetzt fortlaufend, während die sprechende Person redet, und balanciert dabei selbstständig Qualität gegen Synchronität aus, indem es einige Sekunden zurückbleibt (so wie es auch ein menschlicher Simultandolmetscher tut). Es gibt keine separate Kette aus Sprache-zu-Text → Übersetzung → Text-zu-Sprache; Audio geht hinein, übersetztes Audio kommt heraus.

Zwei Betriebsmodi:

- **Video / Spiel** — einseitige eingehende Übersetzung; das Originalaudio wird abgesenkt, während die Übersetzung spricht.
- **Meeting** — zweiseitig: Die Stimme der Gegenseite wird in deine Sprache übersetzt (auf deine Kopfhörer), und deine Stimme wird in deren Sprache übersetzt und als virtuelles Mikrofon in den Anruf eingespeist.

Jede Sitzung kann als **TXT / SRT / VTT** (zweisprachige Untertitel) gespeichert und exportiert werden, und vergangene Sitzungen bleiben im In-App-Verlaufspanel durchsuchbar.

---

## Funktionsweise

```
Windows audio ──► Capture ──► Silero VAD gate ──► Gemini Live (translate) ──► Player ──► Headphones
                (loopback /     (filters non-                                 (limiter,
                 VB-CABLE)        speech)                                      stereo mix)
```

- **Capture** — zwei Pfade:
  - *Treiberlos* (Standard, keine Installation): WASAPI-Process-Exclude-Loopback (Windows 10 2004+) liest den Systemmix und schließt die eigene Ausgabe von Voxis aus, sodass Voxis niemals seine eigene Stimme erneut übersetzt. Andere Apps werden direkt an der Quelle über die Windows-Sitzungslautstärke-API abgesenkt.
  - *VB-CABLE*: Das Audio wird vor den Lautsprechern abgegriffen, sodass die Engine echtes DSP anwenden kann — eine M/S-Mitten-Unterdrückung senkt den ursprünglichen Dialog ab, während die Stereo-Musik erhalten bleibt, und eine fraktionale Verzögerungsleitung richtet das Original per RTT auf die Übersetzung aus.
- **VAD gate** — Silero VAD v5 (ONNX, CPU) filtert Musik/Rauschen heraus, sodass nur Sprache die Cloud erreicht.
- **Übersetzung** — ein `LiveTranslator`-Thread hält eine Gemini-Live-WebSocket-Sitzung und streamt 16-kHz-PCM hinein und 24-kHz-übersetztes Audio heraus.
- **Wiedergabe** — ein Stereo-Mixer mit einem vorausschauenden Brickwall-Limiter; die Übersetzung sitzt in der Phantommitte.

---

## Schnellstart (Entwickler-Build)

```powershell
git clone https://github.com/DavutAkca/voxislive.git
cd voxislive
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Python 3.11–3.13 (64-bit).** Python 3.14 wird noch nicht unterstützt: für numpy / onnxruntime gibt es bei den gepinnten Versionen keine stabilen cp314-Wheels, daher würde `pip install` fehlschlagen.

Ausführen:

```powershell
python main.py            # GUI
```

Der Open-Source-Build ist **BYOK** (bring your own key). Öffne beim ersten Start
**Einstellungen → API-Schlüssel** und füge deinen Gemini-Schlüssel
(<https://aistudio.google.com/>) ein; er wird **verschlüsselt** unter
`profiles/byok` gespeichert (über Windows DPAPI, an dein Windows-Konto gebunden),
nie in einer Klartext-`.env`. Vollständige Anleitung: [docs/INSTALL_BYOK.md](docs/INSTALL_BYOK.md).

Liste deine Audiogeräte jederzeit mit `python -m app.audio_io` auf.

---

## Build-Varianten — `IS_OFFICIAL_RELEASE`

Voxis wird in zwei Varianten ausgeliefert, die zur Build-Zeit über `IS_OFFICIAL_RELEASE` ausgewählt werden (Umgebungsvariable `VOXIS_OFFICIAL_RELEASE=1/0`, Standard `False`).

| | Offizielle SaaS-`.exe` (`True`) | Open Source / Entwickler (`False`) |
| --- | --- | --- |
| API-Schlüssel | Pro Sitzung vom Server abgerufen; keine Schlüssel-UI | Eigener Schlüssel (BYOK), in den Einstellungen eingegeben |
| Übersetzungs-Engine | Google Gemini Live + OpenAI, je nach Zielsprache geroutet | Nur Google Gemini Live |
| Authentifizierung | Anmeldung (PocketBase) | Keine — lokal, offline |
| Telemetrie / Abrechnung | Nutzungs-Heartbeat an den Server | Vollständig deaktiviert |
| Übersetzungseinstellungen | Auf die besten Simultan-Standardwerte festgelegt | Alle Einstellungen zum Feinabstimmen verfügbar |

`start.bat` setzt `VOXIS_OFFICIAL_RELEASE` nicht, sodass ein Start aus dem Quellcode standardmäßig den BYOK-/Entwickler-Pfad verwendet (dein eigener Schlüssel — kein Server, keine Authentifizierung). Die offizielle SaaS-`.exe` wird separat von `release.py` erzeugt, dessen Build-Schritt den `OFFICIAL`-Marker in das eingefrorene Bundle schreibt.

**Netzwerk-Oberfläche des Open-Source-Builds.** Ein eingefrorener (frozen) Entwickler-Build trägt keinen `OFFICIAL`-Marker, fällt daher auf BYOK zurück und macht **von sich aus keine ausgehenden Aufrufe**: Registrierung, Anmeldung, Verifizierung, Kontingent, das serverseitige Abrufen des Sitzungsschlüssels, der Nutzungs-Heartbeat und sämtliche Telemetrie sind deaktiviert oder fest auf lokale Mock-Antworten verdrahtet. Das einzige Netzwerk, das er berührt, ist der Gemini-Live-WebSocket, den dein eigener Schlüssel öffnet. Es gibt keinen In-App-Auto-Updater (er wurde entfernt; die offizielle App aktualisiert sich über den Microsoft Store). Das öffentliche Repository wird durch ein Release-Hygiene-Gate (`scripts/check_release_hygiene.py`, in CI und einen Pre-Push-Hook eingebunden) frei von Closed-Core-Pfaden und Live-Secrets gehalten.

---

## Einrichtung des Meeting-Modus (zweiseitige Übersetzung)

**Ziel:** Du sprichst Türkisch → die Gegenseite hört Englisch; die Gegenseite spricht Englisch → du hörst Türkisch.

Die beiden Richtungen haben unterschiedliche Anforderungen:

| Richtung | Was sie tut | Anforderung |
| --- | --- | --- |
| **Eingehend** (du hörst sie in deiner Sprache) | Hört das Systemaudio ab, übersetzt es und gibt es auf deinen Kopfhörern wieder | **Keine zusätzliche Installation** |
| **Ausgehend** (deine Stimme geht übersetzt hinaus) | Übersetzt dein Mikrofon und speist ein virtuelles Mikrofon | **Ein virtuelles Mikrofon (VB-CABLE) ist erforderlich** |

> Unter Windows ist die einzige Möglichkeit, ein „Mikrofon" bereitzustellen, das eine Meeting-App (Teams/Zoom/Meet) auswählen kann, ein virtueller Audiotreiber — deshalb benötigt die ausgehende Richtung VB-CABLE. Ohne einen solchen laufen Meetings automatisch im **Nur-Zuhören**-Modus (du verstehst sie; deine Stimme geht unübersetzt hinaus).

### 1. VB-CABLE installieren (einmalig, kostenlos)
1. Lade es von <https://vb-audio.com/Cable/> herunter.
2. Entpacken → Rechtsklick auf `VBCABLE_Setup_x64.exe` → **Als Administrator ausführen** → **Install Driver** → **Neustart**.
3. Es erscheinen zwei Geräte: **CABLE Input** (Wiedergabe) und **CABLE Output** (Aufnahme).

### 2. Voxis konfigurieren
- Stelle die Sprachen im Panel ein: **Ich höre: Türkisch**, **Für andere: Englisch**.
- Einstellungen → **Ausgabegerät**: deine echten Kopfhörer · **Mikrofon**: dein echtes Mikrofon — das, in das du sprichst; Voxis hört hier zu.
- **Das virtuelle Kabel wird automatisch erkannt.** Beim Start findet Voxis ein installiertes Kabel (VB-CABLE / VB-Audio / VoiceMeeter) und richtet das Meeting-Routing selbst ein — kein Bearbeiten von `config.json`.

### 3. Die Meeting-App konfigurieren (Teams / Zoom / Meet)
- Stelle das **Mikrofon** auf **„CABLE Output (VB-Audio Virtual Cable)"** ein — die *Aufnahme*-Seite des Kabels (`CABLE Output`, **nicht** `CABLE Input`). Das ist das Mikrofon der Meeting-App, nicht das in Voxis gewählte echte Mikrofon: Voxis schreibt dein übersetztes Englisch in das Kabel, und die Meeting-App liest es hier zurück.
- Wenn mehrere virtuelle Kabel installiert sind (z. B. VB-Audio Point, VoiceMeeter), wähle das Paar **VB-Audio Virtual Cable** — das verdrahtet Voxis standardmäßig automatisch.
- Belasse Lautsprecher/Ausgabe auf deinen eigenen Kopfhörern.

### 4. Verwenden
Starte Voxis → **Meeting**-Modus (`Ctrl+Alt+2`). Sprich Türkisch → es geht als Englisch hinaus; sie sprechen Englisch → du hörst Türkisch.

---

## Latenz & Simultanübersetzung

Die Ende-zu-Ende-Verzögerung beträgt ungefähr **die Satzlänge plus einige Sekunden** — diese Verzögerung ist die vom Übersetzungsmodell vorgesehene *Ear-Voice-Span* (es wartet auf genügend Kontext, um korrekt zu übersetzen, genau wie es ein menschlicher Dolmetscher tut) und ist **vom Client aus nicht einstellbar**. Es gibt keine „schneller"-Einstellung auf Google-Seite, und dies ist das neueste/einzige Übersetzungsmodell.

Was Voxis auf der Client-Seite *tatsächlich* optimiert: Es füttert das Modell mit einem kontinuierlichen Stream (das dokumentierte native Setup des Modells — es wird keine clientseitige Endpoint-Konfiguration gesendet), wärmt die Verbindung vor der Erfassung vor, sodass der erste Satz den kalten Handshake überspringt, deaktiviert die WebSocket-Komprimierung, hält einen kleinen Eingabepuffer nach dem Drop-Oldest-Prinzip vor und führt VAD auf der CPU aus. Das trimmt die steuerbaren Ränder — nicht die grundlegende Verzögerung des Modells.

---

## Konfigurationsreferenz

`config.json` (per .gitignore ausgeschlossen; Standardwerte liegen in `app/config.py`):

| Key | Bedeutung |
| --- | --- |
| `target_language_incoming` / `target_language_outgoing` | Deine Sprache / die Sprache der Gegenseite |
| `capture_backend` | `"driverless"` (WASAPI-Loopback) oder `"vbcable"` |
| `original_audio` | `"duck"` · `"mute_during_speech"` · `"mix"` |
| `duck_gain` | Originalpegel, während die Übersetzung spricht (0–1) |
| `quality_preset` | `max_quality` · `balanced` · `max_savings` · `turbo` |
| `gemini_voice` / `gemini_temperature` | Vorgefertigte Stimme · Sampling-Temperatur |
| `tts_volume` | Wiedergabelautstärke der Übersetzung |
| `session_rotate_minutes` | Rotation der Live-Sitzung (vor dem 15-Minuten-Limit) |

**Qualitäts-Presets** werden auf das lokale VAD-Gate abgebildet, das den kontinuierlichen Stream an das Modell formt. `max_savings` („Saver") gated den Stream — nur Sprache wird gesendet, Stille-Lücken werden verworfen — um weniger abgerechnete Minuten zu verbrauchen. Der offizielle Build zeigt vier benutzerfreundliche Optionen (**Smooth** = `balanced`, **Fast** = `turbo`, **Callout** = `callout`, **Saver** = `max_savings`); der Entwickler-Build legt die vollständige Preset-Liste offen (`max_quality`, `balanced`, `max_savings`, `turbo`).

Das Übersetzungsmodell ist ein nativer Simultandolmetscher, daher sendet der Client keine Endpoint-Konfiguration — er füttert einen kontinuierlichen Stream und überlässt das Endpointing dem Modell selbst.

**Oberflächensprachen** (die App-UI) umfassen **16 Sprachen** — gesetzt über `ui_language`. **Übersetzungs-Zielsprachen** (wohin das Modell übersetzt) sind davon unabhängig und umfassen **79 Sprachen** (`tr, en, es, fr, de, it, pt, ru, ar, zh-Hans, ja, ko, hi, …`), gesetzt über `target_language_incoming` / `target_language_outgoing`.

---

## Architektur (Modulübersicht)

| Module | Verantwortlichkeit |
| --- | --- |
| `app/config.py` | Laden/Speichern der Konfiguration, `DEFAULTS`, `QUALITY_PRESETS`, `IS_OFFICIAL_RELEASE`, Gate-Helfer |
| `app/audio_io.py` | Geräteerkennung, Loopback-Erfassung, `Player` (Stereo-Mix + Limiter), Erkennung des virtuellen Kabels |
| `app/process_loopback.py` | Process-Exclude-WASAPI-Loopback (treiberlos) |
| `app/session_duck.py` | Absenkung auf Quellenebene über die Windows-Sitzungslautstärke-API |
| `app/vad.py` | Silero VAD (CPU) + `SpeechGate` |
| `app/translator.py` | `LiveTranslator` — Gemini-Live-Sitzung, native Simultanübersetzung, Rotation |
| `app/pipeline.py` | `IncomingPipeline`, `OutgoingPipeline`, `ModeController` |
| `app/mix_core.py` / `app/dsp.py` | Vorausschauender Limiter, Verzögerungsleitung, M/S-Mitten-Unterdrückung |
| `app/byok_store.py` | DPAPI-verschlüsselte lokale Schlüsselspeicherung (Entwickler-Build) |
| `app/voxis_client.py` | HTTP-Client für den Auth-Core (offizieller Build) |
| `app/webui.py` + `app/web/index.html` | pywebview-Bridge + Single-File-UI |

Ein optionales `premium/`-Paket (Open-Core-Hook, per .gitignore ausgeschlossen) kann eine ONNX-basierte Gesangs-/Instrumententrennung bereitstellen; fehlt es, wird die deterministische M/S-Mitten-Unterdrückung als Fallback verwendet.

Das SaaS-Backend (ein Go-+-PocketBase-Dienst hinter Caddy auf `voxislive.com`) gibt Schlüssel pro Sitzung aus und erfasst die Nutzung; der Open-Source-Build kontaktiert es niemals.

---

## Fehlerbehebung

| Symptom | Ursache | Lösung |
| --- | --- | --- |
| `API key not valid` | Ungültiger/leerer Schlüssel (BYOK) oder Ausführen des Dev-Builds ohne Schlüssel | Gib in den Einstellungen einen vollständigen Gemini-Schlüssel ein oder starte mit `VOXIS_OFFICIAL_RELEASE=1`, um den Server-Schlüssel zu verwenden |
| Meeting ist nur Zuhören | Kein virtuelles Mikrofon installiert | Installiere VB-CABLE (siehe oben) |
| `PaError -9999` | Veraltete WASAPI-Geräteliste | Trenne das USB-Audiogerät und stecke es wieder ein, dann neu starten |
| Keine Übersetzungsausgabe wird geroutet | Ausgabe auf ein virtuelles Kabel gesetzt (Rückkopplungsschleife) | Richte `headphones_output` auf dein echtes Gerät |

---

## Lizenz — PolyForm Noncommercial 1.0.0

Lizenziert unter der **PolyForm Noncommercial License 1.0.0**; vollständiger Text in [LICENSE](LICENSE).

- ✅ Kostenlos nutzbar für persönliche, Hobby-, Forschungs- und nicht-kommerzielle Zwecke.
- ❌ Kommerzielle Nutzung, Weiterverkauf, White-Label und gewinnorientierte Bereitstellung sind untersagt.

**Kommerzielle Lizenzierung** (kommerzielle Produkte, SaaS, White-Label): **<https://voxislive.com/licensing>**.

Beiträge sind willkommen — indem du einen Pull Request öffnest, erklärst du dich damit einverstanden, dass dein Beitrag unter denselben Lizenzbedingungen steht und mit Namensnennung in die Projekthistorie aufgenommen werden darf.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/DavutAkca/voxislive/issues)
- **Kommerzielle Anfragen:** <https://voxislive.com/licensing>

*Voxis Live — Echtzeit-Sprachübersetzung, simultan.*
