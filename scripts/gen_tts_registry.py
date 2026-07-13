"""Build the cascade free-tier voice registry (app/local_tts.py VOICES).

A registry entry is a promise: this asset exists, this hash is what a clean
download yields, this voice may legally ship in a commercial product, and it
actually speaks the language. Hand-maintaining that is how a silent or
non-commercial voice reaches a paying build, so this script proves all four
and prints entries ready to paste.

Each candidate is downloaded once, then gated:
  1. LICENSE   - the MODEL_CARD must not be non-commercial. Voxis is a paid
                 product; a CC BY-NC voice in the free tier is a legal defect,
                 not a quality one. Unknown licenses are reported, never
                 auto-accepted.
  2. SPEECH    - the voice must synthesize a probe sentence *in its own
                 script* to non-silent audio. A Piper lexicon fed a script it
                 does not know (Traditional Chinese into a zh_CN voice) emits
                 silence, which would ship as a mute free tier.
  3. SPEED     - real-time factor on CPU, since the cascade synthesizes while
                 the user listens. A voice slower than the audio it produces
                 is unusable regardless of how good it sounds.

The first candidate per language that passes all three wins; the rest are
fallbacks. Failures are reported, not silently skipped.

  python scripts/gen_tts_registry.py                 # all languages
  python scripts/gen_tts_registry.py --langs zh,pt-BR
  python scripts/gen_tts_registry.py --keep-cache    # reuse tarballs
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.local_tts import (_ASSET_URL, _find_onnx,  # noqa: E402  single source
                          build_tts)

# Voices whose real licence is NOT in their Piper model card and forbids us.
# The card only names the dataset; the grant lives upstream, so the keyword
# gate cannot see these. Findings are recorded here so nobody re-derives them.
KNOWN_BLOCKED: dict[str, str] = {
    # RHVoice doc/en/License.md: "It is prohibited to ... use this voice by
    # Organizations and manufacturers for inclusion in their products."
    # Piper's card for it says only "See URL". Georgian has no other Piper
    # voice, so ka simply has no free-tier voice.
    "vits-piper-ka_GE-natia-medium":
        "RHVoice Natia: personal use only, barred for products",
}

# Ordered preference per Voxis target language. Later entries are fallbacks
# used only when an earlier one fails a gate. Quality is 'medium' wherever
# Piper publishes one -- 'low'/'x_low' sound thin and 'high' is slower on CPU
# for no gain the free tier can spend.
CANDIDATES: dict[str, list[str]] = {
    "ar": ["ar_JO-kareem-medium"],
    "ca": ["ca_ES-upc_ona-medium", "ca_ES-upc_pau-x_low"],
    "da": ["da_DK-talesyntese-medium"],
    "el": ["el_GR-rapunzelina-low"],          # no medium published
    "eu": ["eu_ES-antton-medium", "eu_ES-maider-medium"],
    "fa": ["fa_IR-amir-medium", "fa_IR-gyro-medium", "fa_IR-ganji-medium"],
    "fi": ["fi_FI-harri-medium"],
    "hi": ["hi_IN-pratham-medium", "hi_IN-rohan-medium", "hi_IN-priyamvada-medium"],
    "hu": ["hu_HU-anna-medium", "hu_HU-berta-medium", "hu_HU-imre-medium"],
    "id": ["id_ID-news_tts-medium"],
    "is": ["is_IS-salka-medium", "is_IS-bui-medium", "is_IS-steinn-medium"],
    "it": ["it_IT-paola-medium", "it_IT-riccardo-x_low"],
    "ka": ["ka_GE-natia-medium"],
    "kk": ["kk_KZ-issai-high", "kk_KZ-raya-x_low"],   # no medium published
    "lv": ["lv_LV-aivars-medium"],
    "ml": ["ml_IN-meera-medium", "ml_IN-arjun-medium"],
    "ne": ["ne_NP-google-medium", "ne_NP-chitwan-medium"],
    "nl": ["nl_NL-pim-medium", "nl_NL-ronnie-medium", "nl_NL-alex-medium"],
    "nb": ["no_NO-talesyntese-medium"],       # Voxis says nb, Piper says no_NO
    "pl": ["pl_PL-gosia-medium", "pl_PL-darkman-medium"],
    # pt-BR and pt-PT are separate Voxis targets; collapsing them to one voice
    # would answer an explicit pt-PT pick with a Brazilian accent.
    "pt-BR": ["pt_BR-faber-medium", "pt_BR-cadu-medium", "pt_BR-jeff-medium"],
    "pt-PT": ["pt_PT-tugao-medium"],
    "sk": ["sk_SK-lili-medium"],
    "sl": ["sl_SI-artur-medium"],
    "sq": ["sq_AL-edon-medium"],
    "sr": ["sr_RS-serbski_institut-medium"],
    "sv": ["sv_SE-nst-medium", "sv_SE-lisa-medium", "sv_SE-alma-medium"],
    "sw": ["sw_CD-lanfrica-medium"],
    "uk": ["uk_UA-ukrainian_tts-medium", "uk_UA-lada-x_low"],
    "ur": ["ur_PK-fasih-medium"],
    "vi": ["vi_VN-vais1000-medium", "vi_VN-25hours_single-low"],
    # chaowen first: huayan speaks just as well but its card says "Unknown"
    # and its dataset repo is now a 404, so there is no grant to rely on.
    "zh": ["zh_CN-chaowen-medium", "zh_CN-huayan-medium"],
}

# Probe sentences must be written in the script the model will actually meet
# in production -- that is the whole point of the SPEECH gate.
PROBES: dict[str, str] = {
    # The eight already in VOICES -- kept so --audit-shipped can re-check them.
    "tr": "Merhaba, bu Türkçe için kısa bir ses testidir.",
    "en": "Hello, this is a short voice test in English.",
    "de": "Hallo, dies ist ein kurzer Sprachtest auf Deutsch.",
    "cs": "Ahoj, toto je krátký hlasový test v češtině.",
    "ro": "Bună, acesta este un scurt test de voce în română.",
    "ru": "Привет, это короткий голосовой тест на русском языке.",
    "es": "Hola, esta es una breve prueba de voz en español.",
    "fr": "Bonjour, ceci est un court test vocal en français.",

    "ar": "مرحبا، هذا اختبار قصير للصوت باللغة العربية.",
    "ca": "Hola, aquesta és una prova curta de veu en català.",
    "da": "Hej, dette er en kort stemmetest på dansk.",
    "el": "Γεια σας, αυτή είναι μια σύντομη δοκιμή φωνής στα ελληνικά.",
    "eu": "Kaixo, hau euskarazko ahots proba labur bat da.",
    "fa": "سلام، این یک آزمایش کوتاه صدا به زبان فارسی است.",
    "fi": "Hei, tämä on lyhyt äänitesti suomeksi.",
    "hi": "नमस्ते, यह हिंदी में एक छोटा आवाज़ परीक्षण है।",
    "hu": "Helló, ez egy rövid hangteszt magyarul.",
    "id": "Halo, ini adalah tes suara singkat dalam bahasa Indonesia.",
    "is": "Halló, þetta er stutt raddprófun á íslensku.",
    "it": "Ciao, questa è una breve prova vocale in italiano.",
    "ka": "გამარჯობა, ეს არის ხმის მოკლე ტესტი ქართულად.",
    "kk": "Сәлем, бұл қазақ тіліндегі қысқа дауыс сынағы.",
    "lv": "Sveiki, šis ir īss balss tests latviešu valodā.",
    "ml": "നമസ്കാരം, ഇത് മലയാളത്തിലെ ഒരു ചെറിയ ശബ്ദ പരീക്ഷണമാണ്.",
    "ne": "नमस्ते, यो नेपालीमा एउटा छोटो आवाज परीक्षण हो।",
    "nl": "Hallo, dit is een korte stemtest in het Nederlands.",
    "nb": "Hei, dette er en kort stemmetest på norsk.",
    "pl": "Cześć, to jest krótki test głosu po polsku.",
    "pt-BR": "Olá, este é um breve teste de voz em português.",
    "pt-PT": "Olá, este é um breve teste de voz em português.",
    "sk": "Ahoj, toto je krátky test hlasu v slovenčine.",
    "sl": "Pozdravljeni, to je kratek glasovni test v slovenščini.",
    "sq": "Përshëndetje, ky është një test i shkurtër i zërit në shqip.",
    "sr": "Здраво, ово је кратак тест гласа на српском.",
    "sv": "Hej, det här är ett kort rösttest på svenska.",
    "sw": "Habari, huu ni mtihani mfupi wa sauti kwa Kiswahili.",
    "uk": "Привіт, це короткий тест голосу українською.",
    "ur": "سلام، یہ اردو میں ایک مختصر آواز کا امتحان ہے۔",
    "vi": "Xin chào, đây là một bài kiểm tra giọng nói ngắn bằng tiếng Việt.",
    "zh": "你好，这是一段简短的中文语音测试。",
}

# Extra probes that answer a specific doubt rather than gating an entry.
# zh-Hant is a distinct Voxis target but shares the zh key, so the Simplified
# voice will be handed Traditional characters in production -- prove it copes.
EXTRA_PROBES: dict[str, list[tuple[str, str]]] = {
    "zh": [("zh-Hant", "你好，這是一段簡短的中文語音測試。")],
}

_NC_MARKERS = ("non-commercial", "noncommercial", "by-nc", "cc-by-nc",
               "nc-sa", "nc 4.0", "research only", "research purposes only")
# Matched against the card's license VALUE only, with word boundaries -- a
# bare "mit" substring hides inside "submitted", and a loose match here grants
# a licence the dataset never gave.
_PERMISSIVE_RE = re.compile(
    r"\b(cc0|public domain|mit|apache(-| )?2|bsd|unlicense|"
    r"cc[- ]by([- ]sa)?([- ]?[0-9.]+)?|creative commons attribution)\b"
    # Cards often give the licence as a bare URL rather than a name. The NC
    # check runs first, so a by-nc-sa URL is already blocked before here.
    r"|creativecommons\.org/(licenses|publicdomain)/", re.I)
# A voice that cannot keep up with its own audio makes the free tier stutter.
MAX_RTF = 0.6
MIN_RMS = 0.005          # below this the "speech" is silence or a click
MIN_DURATION_RATIO = 0.4  # seconds of audio per 10 chars, at minimum


def _log(msg: str) -> None:
    print(msg, flush=True)


def fetch(asset: str, cache: str) -> str:
    """Download the tarball into `cache` (reused if already there)."""
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, asset + ".tar.bz2")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    url = _ASSET_URL.format(name=asset)
    req = urllib.request.Request(url, headers={"User-Agent": "voxis"})
    tmp = path + ".part"
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f, length=1 << 20)
    os.replace(tmp, path)
    return path


def unpack(tar_path: str, asset: str, dest_root: str) -> str:
    """Extract and return the voice dir, mirroring local_tts._download_voice."""
    with tarfile.open(tar_path, "r:bz2") as tf:
        tf.extractall(dest_root, filter="data")
    return os.path.join(dest_root, asset)


# Cards that name no licence, resolved by reading the upstream source by hand
# (2026-07-13). The credits file is the attribution artifact, so "See URL" in
# it would satisfy nobody -- these are what actually grants us the voice.
RESEARCHED: dict[str, tuple[str, str]] = {
    "vits-piper-en_US-amy-medium":
        ("CC BY-SA 4.0", "https://github.com/MycroftAI/mimic3-voices"),
    "vits-piper-ru_RU-irina-medium":
        ("GPL-2.0 (RHVoice; not one of its non-commercial voices)",
         "https://github.com/RHVoice/RHVoice"),
    "vits-piper-it_IT-paola-medium":
        ("CC0 1.0",
         "https://huggingface.co/datasets/paolapersico1/Voice-Dataset-Italian"),
    "vits-piper-is_IS-salka-medium":
        ("CC BY 4.0 (Talrómur 1)",
         "https://repository.clarin.is/repository/xmlui/handle/20.500.12537/104"),
}


def read_license(voice_dir: str) -> tuple[str, str, str]:
    """(verdict, license_value, dataset_url) from the voice's MODEL_CARD.

    Piper's card records the *dataset's* license, and for many voices that is
    literally "Unknown" or "See URL" -- a pointer, not a grant. So the verdict
    is three-way and only an explicit permissive grant is auto-accepted:
    'blocked' (non-commercial, must not ship), 'permissive' (safe), 'review'
    (the card does not say; a human must read the dataset URL). Guessing here
    is how a CC BY-NC voice ends up in a paid product."""
    text = ""
    for name in sorted(os.listdir(voice_dir)):
        if name.lower().startswith(("model_card", "license", "readme")):
            try:
                with open(os.path.join(voice_dir, name), encoding="utf-8",
                          errors="replace") as f:
                    text += f.read() + "\n"
            except OSError:
                pass
    if not text.strip():
        return "review", "no MODEL_CARD in archive", ""

    m = re.search(r"^\s*\*?\s*Licen[sc]e\s*:\s*(.+)$", text, re.I | re.M)
    value = m.group(1).strip() if m else ""
    u = re.search(r"^\s*\*?\s*URL\s*:\s*(\S+)", text, re.I | re.M)
    url = u.group(1).strip() if u else ""

    low = (value + " " + text).lower()
    if any(k in low for k in _NC_MARKERS):
        return "blocked", value or "NC marker in card", url
    if _PERMISSIVE_RE.search(value):
        return "permissive", value, url
    return "review", value or "(no license line)", url


def speak(voice_dir: str, text: str, threads: int = 2):
    """Synthesize `text` through the app's own loader; returns
    (samples, rate, synth_seconds). Using build_tts rather than a copy of the
    sherpa config is the point: a voice that needs a config the app does not
    build must fail HERE, not in a user's session."""
    import numpy as np
    tts = build_tts(voice_dir, threads)
    tts.generate(".", sid=0, speed=1.0)  # warm up off the measured path
    t0 = time.perf_counter()
    audio = tts.generate(text, sid=0, speed=1.0)
    elapsed = time.perf_counter() - t0
    return (np.asarray(audio.samples, dtype="float32"),
            int(audio.sample_rate), elapsed)


def _probe_worker(voice_dir: str, text: str) -> int:
    """Synthesize once and print metrics as JSON. Runs as its own process.

    espeak-ng initializes ONCE per process and pins the first voice's
    data_dir. This sweep stages each voice to a temp dir and deletes it after
    measuring, so a second voice in the same process would inherit a data_dir
    that no longer exists and synthesize silence -- every voice after the
    first would be rejected for a fault of the harness. (The app is unaffected:
    its voice dirs persist and each ships a complete espeak-ng-data, so the
    pinned dir still phonemizes later languages correctly -- measured, 1.3%
    duration drift vs a fresh process. Do not "fix" that as a bug.)"""
    import json
    import numpy as np
    samples, rate, elapsed = speak(voice_dir, text)
    dur = len(samples) / float(rate) if rate else 0.0
    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
    print("VOXIS_PROBE " + json.dumps(
        {"dur": dur, "rms": rms, "elapsed": elapsed}))
    return 0


def check_speech(voice_dir: str, lang: str, text: str) -> dict:
    import json
    import subprocess
    proc = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--probe", voice_dir, text],
        capture_output=True, text=True, encoding="utf-8", timeout=300)
    line = next((l for l in proc.stdout.splitlines()
                 if l.startswith("VOXIS_PROBE ")), None)
    if line is None:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
        return {"ok": False, "rms": 0.0, "dur": 0.0, "rtf": float("inf"),
                "why": f"synth crashed: {tail[0][:80]}"}
    m = json.loads(line[len("VOXIS_PROBE "):])
    dur, rms, elapsed = m["dur"], m["rms"], m["elapsed"]
    rtf = (elapsed / dur) if dur > 0 else float("inf")
    expected = MIN_DURATION_RATIO * (len(text) / 10.0)
    ok = rms >= MIN_RMS and dur >= expected and rtf <= MAX_RTF
    why = []
    if rms < MIN_RMS:
        why.append(f"silent (rms={rms:.4f})")
    if dur < expected:
        why.append(f"too short ({dur:.2f}s < {expected:.2f}s expected)")
    if rtf > MAX_RTF:
        why.append(f"too slow (rtf={rtf:.2f})")
    return {"ok": ok, "rms": rms, "dur": dur, "rtf": rtf, "why": ", ".join(why)}


def evaluate(lang: str, asset: str, cache: str, work: str) -> dict:
    # CANDIDATES holds the locale-voice-quality tail; the release asset, the
    # archive's inner dir and the registry key are all the prefixed full name.
    if not asset.startswith("vits-piper-"):
        asset = "vits-piper-" + asset
    res = {"lang": lang, "asset": asset, "ok": False}
    if asset in KNOWN_BLOCKED:
        res["license"] = "blocked"
        res["error"] = f"NON-COMMERCIAL (upstream): {KNOWN_BLOCKED[asset]}"
        return res
    tar = fetch(asset, cache)
    res["mb"] = os.path.getsize(tar) / 1e6
    stage = tempfile.mkdtemp(dir=work, prefix="v-")
    try:
        vdir = unpack(tar, asset, stage)
        onnx = _find_onnx(vdir)
        if not onnx:
            res["error"] = "archive contains no .onnx"
            return res
        with open(onnx, "rb") as f:
            res["sha256"] = hashlib.sha256(f.read()).hexdigest()

        verdict, value, url = read_license(vdir)
        res.update({"license": verdict, "license_value": value,
                    "dataset_url": url})
        if verdict == "blocked":
            # Non-commercial: no amount of audio quality makes this shippable.
            res["error"] = f"NON-COMMERCIAL licence: {value}"
            return res

        probe = check_speech(vdir, lang, PROBES[lang])
        res.update({k: probe[k] for k in ("rms", "dur", "rtf")})
        if not probe["ok"]:
            res["error"] = probe["why"]
            return res

        res["extra"] = []
        for label, text in EXTRA_PROBES.get(lang, []):
            res["extra"].append((label, check_speech(vdir, lang, text)))

        # A 'review' voice speaks fine but has no written grant. It is a
        # candidate, not an entry -- main() keeps it out of the paste block.
        res["ok"] = True
        return res
    except Exception as e:  # a bad archive must not abort the whole sweep
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def write_credits(path: str, cache: str, work: str) -> int:
    """Emit the attribution file for every voice currently in VOICES.

    Most of these are CC-BY or CC-BY-SA: they permit commercial use *on
    condition of attribution*. Shipping them with no credit is the one way a
    permissive licence still gets breached, so the list is generated from the
    registry rather than hand-kept -- a voice added without a credit line is
    the failure mode this closes."""
    from app.local_tts import VOICES
    rows = []
    for lang, (asset, _sha) in sorted(VOICES.items()):
        tar = fetch(asset, cache)
        stage = tempfile.mkdtemp(dir=work, prefix="c-")
        try:
            vdir = unpack(tar, asset, stage)
            _verdict, value, url = read_license(vdir)
            value, url = RESEARCHED.get(asset, (value, url))
            rows.append((lang, asset, value or "-", url or "-"))
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Free-tier voice credits\n\n")
        f.write("Voxis's free tier speaks with neural voices from the Piper "
                "project, fetched on first use from the sherpa-onnx model "
                "release. Each voice is used under the licence below; several "
                "are Creative Commons Attribution licences, which is why this "
                "file exists.\n\n")
        f.write("Generated by `scripts/gen_tts_registry.py --credits`. "
                "Do not edit by hand.\n\n")
        f.write("| Language | Voice | Licence | Source |\n")
        f.write("| --- | --- | --- | --- |\n")
        for lang, asset, value, url in rows:
            voice = asset.removeprefix("vits-piper-")
            f.write(f"| `{lang}` | {voice} | {value} | {url} |\n")
    _log(f"wrote {path} ({len(rows)} voices)")
    return 0


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--probe":
        return _probe_worker(sys.argv[2], sys.argv[3])
    ap = argparse.ArgumentParser()
    ap.add_argument("--credits", metavar="PATH", nargs="?",
                    const=os.path.join("docs", "VOICE_CREDITS.md"),
                    help="write the attribution file for the current registry "
                         "and exit (no speech tests)")
    ap.add_argument("--langs", help="comma-separated subset of languages")
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(),
                                                    "voxis-tts-cache"))
    ap.add_argument("--keep-cache", action="store_true",
                    help="keep downloaded tarballs for a re-run")
    ap.add_argument("--audit-shipped", action="store_true",
                    help="re-check the voices already in VOICES: licence, "
                         "speech, and whether the pinned hash still matches")
    args = ap.parse_args()

    if args.credits:
        work = tempfile.mkdtemp(prefix="voxis-tts-credits-")
        try:
            return write_credits(args.credits, args.cache, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if args.audit_shipped:
        from app.local_tts import VOICES
        table = {lang: [asset] for lang, (asset, _sha) in VOICES.items()}
        pinned = {lang: sha for lang, (_a, sha) in VOICES.items()}
    else:
        table, pinned = CANDIDATES, {}

    langs = ([l.strip() for l in args.langs.split(",")] if args.langs
             else list(table))
    work = tempfile.mkdtemp(prefix="voxis-tts-work-")
    accepted, rejected = {}, []
    try:
        for lang in langs:
            for asset in table[lang]:
                _log(f"[{lang}] {asset} ...")
                r = evaluate(lang, asset, args.cache, work)
                if not r["ok"]:
                    _log(f"  REJECT {r['error']}")
                    rejected.append(r)
                    continue
                extra = "".join(
                    f"  +{lbl}: {'ok' if e['ok'] else 'FAIL ' + e['why']}"
                    for lbl, e in r["extra"])
                _log(f"  OK  rtf={r['rtf']:.2f} {r['dur']:.1f}s "
                     f"{r['mb']:.0f}MB  licence={r['license']}"
                     f" ({r['license_value'][:40]}){extra}")
                if lang in pinned and pinned[lang] != r["sha256"]:
                    _log(f"  !! PINNED HASH MISMATCH: registry has "
                         f"{pinned[lang][:16]}…, download is "
                         f"{r['sha256'][:16]}…")
                # Keep the first candidate that speaks, but keep LOOKING for a
                # licensed one: a good voice with no grant is not shippable, and
                # the next candidate often has the same quality with a real
                # licence (zh huayan "Unknown" vs zh chaowen CC0).
                if lang not in accepted or (
                        r["license"] == "permissive"
                        and accepted[lang]["license"] != "permissive"):
                    accepted[lang] = r
                if r["license"] == "permissive":
                    break
                _log("  (speaks, but no licence — trying the next candidate)")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep_cache:
            shutil.rmtree(args.cache, ignore_errors=True)

    clear = {k: v for k, v in accepted.items() if v["license"] == "permissive"}
    review = {k: v for k, v in accepted.items() if v["license"] == "review"}

    _log("\n" + "=" * 72)
    _log("Entries with an explicit permissive licence — safe to paste:\n")
    for lang in sorted(clear):
        r = clear[lang]
        _log(f'    "{lang}": ("{r["asset"]}",\n'
             f'           "{r["sha256"]}"),')

    if review:
        _log("\n" + "-" * 72)
        _log("Speaks fine, but the model card states NO usable licence.")
        _log("Voxis is a paid product: read the dataset URL before shipping.\n")
        for lang in sorted(review):
            r = review[lang]
            _log(f'  {lang:6} {r["asset"]:36} card="{r["license_value"][:22]}"'
                 f'  {r["dataset_url"]}')

    _log(f"\nSpeech+speed OK: {len(accepted)}/{len(langs)}  "
         f"(licence clear {len(clear)}, needs review {len(review)})")
    if rejected:
        _log("\nRejected:")
        for r in rejected:
            _log(f"  {r['lang']:6} {r['asset']:36} {r.get('error','')}")
    for lang, r in sorted(accepted.items()):
        for lbl, e in r.get("extra", []):
            if not e["ok"]:
                _log(f"\nWARNING [{lang}] probe '{lbl}' failed: {e['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
