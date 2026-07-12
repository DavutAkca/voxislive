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
# Voice picks: owner A/B 2026-07-12 (tr=fahrettin — "parlak"; rest defaults).
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
    # TODO(cascade phase 2): remaining ~32 covered languages via
    # scripts/gen_tts_registry.py (download -> hash -> append here).
}


class VoiceUnavailable(RuntimeError):
    """No registered/usable voice for this language — run captions-only."""


def voice_available(lang: str) -> bool:
    return _norm(lang) in VOICES


def _norm(lang: str) -> str:
    # Registry keys are base languages; pt-BR/pt-PT style tags share one voice.
    return (lang or "").split("-")[0].lower()


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


class LocalTTS:
    """One loaded Piper voice. Synth is CPU-cheap (measured RTF ~0.06-0.4)."""

    def __init__(self, lang: str, on_status=None, num_threads: int = 2):
        voice_dir = ensure_voice(lang, on_status=on_status)
        import sherpa_onnx  # lazy: OSS builds without the wheel never pay for it
        onnx = _find_onnx(voice_dir)
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=onnx,
                    lexicon="",
                    tokens=os.path.join(voice_dir, "tokens.txt"),
                    data_dir=os.path.join(voice_dir, "espeak-ng-data"),
                ),
                provider="cpu",
                num_threads=num_threads,
            ),
            max_num_sentences=1,
        )
        self._tts = sherpa_onnx.OfflineTts(cfg)
        # Warm up off the session path: first generate pays JIT/alloc costs.
        self._tts.generate(".", sid=0, speed=1.0)

    def synth(self, text: str, speed: float = 1.0) -> tuple[np.ndarray, int]:
        """Returns (float32 mono samples, sample_rate)."""
        audio = self._tts.generate(text, sid=0, speed=speed)
        return np.asarray(audio.samples, dtype=np.float32), int(audio.sample_rate)
