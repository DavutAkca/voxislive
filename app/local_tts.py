"""Local neural TTS for the cascade free tier (sherpa-onnx / Piper VITS).

Voice models are per-language, ~60 MB each, and NEVER bundled: they download
hash-verified on first use (the vad.py pattern) into the user-writable data
root — the MSIX install dir is read-only, %APPDATA%\\Voxis is not. A missing
or failed voice degrades soft: the cascade session continues captions-only.

Registry entries are added only with a pinned SHA-256 of the voice's .onnx
(computed from a verified local download). A language absent here simply has
no free-tier voice yet.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request

import numpy as np

from .paths import user_path

_ASSET_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{name}.tar.bz2"
_DOWNLOAD_TIMEOUT = 30  # socket-level; an unreachable CDN can't hang session start

# lang -> (sherpa release asset, sha256 of the model .onnx inside it).
#
# Entries are produced by scripts/gen_tts_registry.py, which downloads each
# candidate and refuses it unless the model card grants commercial use, the
# voice actually speaks its own script, and it synthesizes faster than
# real time. Add nothing here by hand: a Piper card names the *dataset*, and
# several otherwise-fine voices turned out to be CC BY-NC (Hindi pratham,
# Serbian serbski_institut) or barred from products outright (Georgian natia).
#
# Voice picks: owner A/B 2026-07-12 (tr=fahrettin — "parlak"); the rest are
# the default medium voice for the locale, or the best that cleared the gates.
VOICES: dict[str, tuple[str, str]] = {
    "tr": ("vits-piper-tr_TR-fahrettin-medium",
           "36e899b5e448e726cd742578fd9d1c0bdd57a0bd8f83297b0e0046c0d69d97a2"),
    "en": ("vits-piper-en_US-amy-medium",
           "fbaa8e36d8f26fe6f3ebb65cab461e629d8b37a5b7c5fb78fb64317db73e1c25"),
    "de": ("vits-piper-de_DE-thorsten-medium",
           "2d98b40baac55e4206f64114110a3474807e26440b80d465cd202af1e95be5e0"),
    "cs": ("vits-piper-cs_CZ-jirka-medium",
           "e22a799812a7088b533dc363845ddc56bca78e8c89c44ba0b9fcad7567ea38c3"),
    "ro": ("vits-piper-ro_RO-mihai-medium",
           "83c35f4d5bdd286f0143e876bfdc964c94ed95dc33b36a1d80a7214e24871432"),
    "ru": ("vits-piper-ru_RU-irina-medium",
           "44c31fdb9de753d25380b558effb84f250d98acb2cc0971fea78ec40eda98cdd"),
    "es": ("vits-piper-es_ES-davefx-medium",
           "6826f9ac3fb4aac5c89c597f76ff32e84e7a0dea9469a6f18d1d387b696163eb"),
    "fr": ("vits-piper-fr_FR-siwis-medium",
           "b015076c2bedcff22fbedd5da6ffb49b7d0abb2e3bdc51e1a7906e0000e411ed"),
    "ca": ("vits-piper-ca_ES-upc_ona-medium",
           "b64de8ad74b5960c00d405f21121f44010eaece98bfd86eb585704539f06ad83"),
    "da": ("vits-piper-da_DK-talesyntese-medium",
           "33a51b3246c74cfe7530388117189e7d414ea1159f2e14996dab0ee810a273f1"),
    "el": ("vits-piper-el_GR-rapunzelina-low",
           "7824cd5c9912e8aab9fce653f7af9d609c4e2e1d6e4c81d823699afa6d1b1537"),
    "eu": ("vits-piper-eu_ES-antton-medium",
           "a7264bb829cbcd5fb4538133ac203f57624fd72087f08d896a5a9d713954ae00"),
    "fa": ("vits-piper-fa_IR-amir-medium",
           "ec6cf5d89067fcf72c410206d42b83ebaf95ee8219ecd169c85c6868a2ab977c"),
    "fi": ("vits-piper-fi_FI-harri-medium",
           "d6a42383b3c3386dd867ea5c5ed45904f7d08e50436a5cb114c0c2c520b6b696"),
    "hu": ("vits-piper-hu_HU-anna-medium",
           "d369ccfeafdaafd1098b34eb47784566a313bd72fa9dac2028f81587d6463598"),
    "kk": ("vits-piper-kk_KZ-raya-x_low",
           "98d1717f6ea66c69d3f926116fc2df1dcff462fff0842df0b6ff7abbf82f1064"),
    "lv": ("vits-piper-lv_LV-aivars-medium",
           "00e00fda15d4aedb5c50f19c91e3d36dac3004a053b57df7d3e3761b8dd5a9c6"),
    "nb": ("vits-piper-no_NO-talesyntese-medium",
           "8027a2c33f6dc30e137a0f7f15022c07ef2d0e56300a5dfc29e38408970a0ddf"),
    "ne": ("vits-piper-ne_NP-google-medium",
           "d67e7a670fb98c07cfd5027efbde5a06cff8ee2c2cff83231aafb9b938de3eb4"),
    "nl": ("vits-piper-nl_NL-pim-medium",
           "369448594e2e3ace75688aaa551a79ef6e09f5d0f0946d6b115cc6b8daf7a95d"),
    "pl": ("vits-piper-pl_PL-gosia-medium",
           "f22aeb41672b3fb71ff1003863c890e5d2a3632c123b7bc1cac9907119d6cb56"),
    # Voxis offers pt, pt-BR and pt-PT as three targets. The regions get their
    # own voice; bare "pt" follows the larger population rather than answering
    # an unqualified pick with a Lisbon accent.
    "pt": ("vits-piper-pt_BR-faber-medium",
           "1eecd74d1984c73922033629de08974a4cf878f0b4b150e78146331d3d37a053"),
    "pt-br": ("vits-piper-pt_BR-faber-medium",
              "1eecd74d1984c73922033629de08974a4cf878f0b4b150e78146331d3d37a053"),
    "pt-pt": ("vits-piper-pt_PT-tugao-medium",
              "0d922da6f6fd87f981bb05fa8f698a1af6fc5c9366c212cdeb36a0f04c3c056d"),
    "sk": ("vits-piper-sk_SK-lili-medium",
           "4184b6990b4cd832ed7889abbeaff217d96cb1eb168c29231168b0d8c4848993"),
    "sq": ("vits-piper-sq_AL-edon-medium",
           "0a77467127bf3016a2b74f1958be97596dad4e82d745aebf1bd216418bada142"),
    "sv": ("vits-piper-sv_SE-nst-medium",
           "5de1daa67ae8e52af806bdf2ea027fa03a3a5407ec0d0c744d61aae10a7123b3"),
    "uk": ("vits-piper-uk_UA-lada-x_low",
           "eb3d4b60e41b8af92ae6e208d66a55e564a8eb1e0f4eca0f00bea85ccc8eeb65"),
    "ur": ("vits-piper-ur_PK-fasih-medium",
           "07c397de680fcd8140269ce8519cc62c158b2f44979a9415065bb261b6fdff35"),
    "sl": ("vits-piper-sl_SI-artur-medium",
           "7e0c703d8cd2577e669e1dcb95d1cf47b06a8abfdd08c50890508d69f6528c70"),
    "vi": ("vits-piper-vi_VN-vais1000-medium",
           "df1512ef3265609f147ae23726b8c8867c6d28e60acb9ffca3545e11783b809f"),
    # One zh voice serves both Voxis targets: chaowen reads Traditional as well
    # as Simplified (verified). Not huayan, which sounds fine but grants
    # nothing — its card says "Unknown" and its dataset repo is now a 404.
    "zh": ("vits-piper-zh_CN-chaowen-medium",
           "1cb646ea826a6ee73e329ea06319fb8d459da8a35be6c1ba30210387deb3bc55"),
    # These two cards say only "See URL"; the URL was read by hand (2026-07-13)
    # and does grant commercial use, which the script cannot determine itself.
    # it: dataset is CC0 (huggingface.co/datasets/paolapersico1/Voice-Dataset-Italian)
    # is: Talrómur 1 is CC BY 4.0 (repository.clarin.is .../20.500.12537/104)
    "it": ("vits-piper-it_IT-paola-medium",
           "477ab68d8021543decc9e067d12c5fddbc777d81b7b221e233d3c14719a1a6b9"),
    "is": ("vits-piper-is_IS-salka-medium",
           "b57de082c3243b23c21ff37ef391c2654441d2a9a9c4fe88ce2cfffb812cf2e8"),
    # Deliberately voiceless — each was checked and cannot ship (2026-07-13):
    #   sr, ka  - the only Piper voice for each forbids commercial use.
    #   hi, ml  - every voice traces to IIT Madras IndicTTS, which is a signed
    #             per-user agreement, not a public grant (the two other Hindi
    #             voices are outright CC BY-NC-SA).
    #   ar      - dataset repo carries no licence file at all.
    #   sw      - Lanfrica states no licence for the corpus.
    #   id      - the model card points at an unrelated Malayalam corpus, so
    #             the voice's real provenance is unknown.
    #   ja, ko, th, he, bn, ta, te, ... - Piper publishes no voice at all.
}


class VoiceUnavailable(RuntimeError):
    """No registered/usable voice for this language — run captions-only."""


def voice_available(lang: str) -> bool:
    return _norm(lang) in VOICES


def _norm(lang: str) -> str:
    # An exact tag beats its base language, so regions that genuinely differ
    # (pt-BR vs pt-PT) can each hold a voice, while tags that only differ in
    # script (zh-Hans/zh-Hant) still fall back to the one shared base entry.
    tag = (lang or "").lower()
    return tag if tag in VOICES else tag.split("-")[0]


def _tts_root() -> str:
    return user_path("tts_models")


def _voice_dir(asset: str) -> str:
    return os.path.join(_tts_root(), asset)


def _find_onnx(voice_dir: str) -> str | None:
    try:
        for f in sorted(os.listdir(voice_dir)):
            if f.endswith(".onnx"):
                return os.path.join(voice_dir, f)
    except OSError:
        pass
    return None


def _download_voice(asset: str, sha256: str, on_status=None) -> str:
    """Fetch + extract a voice, verify the model hash, atomically move into
    place. Any failure leaves no partial dir behind (a truncated voice must
    fail HERE, loudly — not as a cryptic sherpa load error mid-session)."""
    root = _tts_root()
    os.makedirs(root, exist_ok=True)
    if on_status:
        on_status(f"TTS voice download: {asset} (~60 MB)")
    req = urllib.request.Request(_ASSET_URL.format(name=asset),
                                 headers={"User-Agent": "voxis"})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp, \
            tempfile.NamedTemporaryFile(dir=root, suffix=".tar.bz2",
                                        delete=False) as tmp:
        shutil.copyfileobj(resp, tmp, length=1 << 20)
        tmp_path = tmp.name
    stage = tempfile.mkdtemp(dir=root, prefix=".stage-")
    try:
        with tarfile.open(tmp_path, "r:bz2") as tf:
            tf.extractall(stage, filter="data")
        src = os.path.join(stage, asset)
        onnx = _find_onnx(src)
        if onnx is None:
            raise RuntimeError(f"voice archive {asset} contains no .onnx")
        digest = hashlib.sha256(open(onnx, "rb").read()).hexdigest()
        if digest != sha256:
            raise RuntimeError(
                f"voice {asset} hash mismatch (got {digest[:16]}…, expected "
                f"{sha256[:16]}…) — refusing to use an unverified model.")
        dest = _voice_dir(asset)
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(src, dest)
        return dest
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def ensure_voice(lang: str, on_status=None) -> str:
    """Voice dir for `lang`, downloading on first use. Raises VoiceUnavailable
    when the language has no registered voice or the download/verify fails."""
    key = _norm(lang)
    if key not in VOICES:
        raise VoiceUnavailable(f"no free-tier voice for '{lang}'")
    asset, sha = VOICES[key]
    d = _voice_dir(asset)
    if _find_onnx(d):
        return d
    try:
        return _download_voice(asset, sha, on_status=on_status)
    except Exception as e:
        raise VoiceUnavailable(f"voice download failed for '{lang}': {e}") from e


def build_tts(voice_dir: str, num_threads: int = 2):
    """Load a sherpa OfflineTts from an extracted voice dir.

    Two kinds of Piper voice ship in this release and they configure
    differently: most phonemize through espeak-ng (data_dir, no lexicon),
    while the Chinese ones carry a character lexicon plus text-normalization
    FSTs and fail to load without them. Deciding from the files on disk keeps
    one code path for both. gen_tts_registry.py imports this so the sweep
    cannot drift from what the app actually runs.
    """
    import sherpa_onnx  # lazy: OSS builds without the wheel never pay for it
    lexicon = os.path.join(voice_dir, "lexicon.txt")
    fsts = [os.path.join(voice_dir, n) for n in ("date.fst", "number.fst",
                                                 "phone.fst")]
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=_find_onnx(voice_dir),
                lexicon=lexicon if os.path.exists(lexicon) else "",
                tokens=os.path.join(voice_dir, "tokens.txt"),
                data_dir=os.path.join(voice_dir, "espeak-ng-data"),
            ),
            provider="cpu",
            num_threads=num_threads,
        ),
        rule_fsts=",".join(f for f in fsts if os.path.exists(f)),
        max_num_sentences=1,
    )
    return sherpa_onnx.OfflineTts(cfg)


class LocalTTS:
    """One loaded Piper voice. Synth is CPU-cheap (measured RTF ~0.06-0.4)."""

    def __init__(self, lang: str, on_status=None, num_threads: int = 2):
        voice_dir = ensure_voice(lang, on_status=on_status)
        self._tts = build_tts(voice_dir, num_threads)
        # Warm up off the session path: first generate pays JIT/alloc costs.
        self._tts.generate(".", sid=0, speed=1.0)

    def synth(self, text: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        """Returns (float32 mono samples, sample_rate)."""
        audio = self._tts.generate(text, sid=0, speed=speed)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)
