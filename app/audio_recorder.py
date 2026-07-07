"""Optional dual-track session recorder.

Writes the SOURCE audio and the TRANSLATED audio as two SEPARATE WAV files
(never a single mix), so a user can compare the two, re-mix, or reuse the
translated track for dubbing / video editing — Ivo's localization request.

Opt-in (cfg['record_audio'], default OFF) on purpose: the source track captures
real human voice, a consent/biometric step up over a text transcript, whereas
the translated track is our own synthetic TTS. Recording only happens while a
session is active and the user has turned it on.

Streams straight to disk via stdlib `wave` (no whole-session memory buffer).
Thread-safe by track: source frames arrive on the capture-callback thread and
translated frames on the translator/stager thread, so each track owns its lock
and writer. A disk fault stops recording quietly — it must never break the live
translation session.
"""
import logging
import os
import threading
import time
import wave

import numpy as np

_log = logging.getLogger("voxis")


class _Track:
    """One mono PCM16 WAV, opened lazily on the first frame so a track that never
    receives audio (e.g. a silent leg) leaves no empty file."""

    def __init__(self, path: str, rate: int):
        self.path = path
        self._rate = int(rate)
        self._lock = threading.Lock()
        self._wav = None
        self.frames = 0

    def write(self, pcm: bytes):
        with self._lock:
            if self._wav is None:
                w = wave.open(self.path, "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self._rate)
                self._wav = w
            self._wav.writeframes(pcm)
            self.frames += len(pcm) // 2

    def close(self):
        with self._lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass
                self._wav = None


class DualTrackRecorder:
    def __init__(self, out_dir: str, source_rate: int, translated_rate: int = 24000,
                 tag: str = "video", on_status=None):
        os.makedirs(out_dir, exist_ok=True)
        base = "voxis_%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), tag)
        self._source = _Track(os.path.join(out_dir, base + "_source.wav"),
                              source_rate)
        self._translated = _Track(os.path.join(out_dir, base + "_translated.wav"),
                                  translated_rate)
        self._on_status = on_status
        self._active = True

    # --- taps (guarded; a fault disables recording, never the session) -------
    def feed_source(self, chunk: np.ndarray):
        """A float32 [-1, 1] mono capture chunk (the audio fed to the model)."""
        if not self._active or chunk is None or chunk.size == 0:
            return
        pcm = (np.clip(chunk, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        self._write(self._source, pcm)

    def feed_translated(self, data: bytes):
        """Translated TTS PCM16 bytes at translated_rate (24 kHz)."""
        if not self._active or not data:
            return
        self._write(self._translated, data)

    def _write(self, track: _Track, pcm: bytes):
        try:
            track.write(pcm)
        except Exception:
            if self._active:
                self._active = False
                _log.exception("audio recorder write failed — recording stopped")

    # --- lifecycle -----------------------------------------------------------
    def close(self):
        """Finalize both WAVs, drop any empty track, and surface the saved paths.
        Returns the list of files actually written."""
        self._active = False
        self._source.close()
        self._translated.close()
        saved = []
        for track in (self._source, self._translated):
            if track.frames > 0:
                saved.append(track.path)
            else:
                try:
                    os.remove(track.path)
                except OSError:
                    pass
        if saved and self._on_status is not None:
            try:
                self._on_status("audio saved: " + " | ".join(
                    os.path.basename(p) for p in saved))
            except Exception:
                pass
        return saved

    # _stop_all() finalizes components by calling .stop(); alias so the recorder
    # fits that loop alongside capture/player/translator.
    stop = close
