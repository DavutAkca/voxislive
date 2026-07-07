"""Dual-track audio recorder: WAV integrity, empty-track pruning, fault safety."""
import os
import wave

import numpy as np

from app.audio_recorder import DualTrackRecorder


def _read_wav(path):
    with wave.open(path, "rb") as w:
        return {
            "channels": w.getnchannels(),
            "width": w.getsampwidth(),
            "rate": w.getframerate(),
            "frames": w.getnframes(),
        }


def test_both_tracks_written_valid_wav(tmp_path):
    statuses = []
    rec = DualTrackRecorder(str(tmp_path), source_rate=16000,
                            translated_rate=24000, tag="video",
                            on_status=statuses.append)
    src = np.zeros(1600, dtype=np.float32)      # 0.1 s @ 16 kHz
    src[::2] = 0.5
    rec.feed_source(src)
    rec.feed_translated(b"\x10\x20" * 2400)     # 0.1 s @ 24 kHz PCM16
    saved = rec.close()

    assert len(saved) == 2
    src_wav = next(p for p in saved if p.endswith("_source.wav"))
    tr_wav = next(p for p in saved if p.endswith("_translated.wav"))
    assert _read_wav(src_wav) == {"channels": 1, "width": 2, "rate": 16000,
                                  "frames": 1600}
    assert _read_wav(tr_wav) == {"channels": 1, "width": 2, "rate": 24000,
                                 "frames": 2400}
    assert statuses and "audio saved" in statuses[-1]


def test_empty_track_pruned(tmp_path):
    rec = DualTrackRecorder(str(tmp_path), source_rate=16000)
    rec.feed_source(np.full(800, 0.3, dtype=np.float32))  # only source
    saved = rec.close()
    assert len(saved) == 1
    assert saved[0].endswith("_source.wav")
    # The never-written translated file must not be left behind as an empty stub.
    assert not any(p.endswith("_translated.wav") for p in os.listdir(tmp_path))


def test_no_audio_leaves_no_files(tmp_path):
    rec = DualTrackRecorder(str(tmp_path), source_rate=16000)
    assert rec.close() == []
    assert os.listdir(tmp_path) == []


def test_feed_after_close_is_ignored(tmp_path):
    rec = DualTrackRecorder(str(tmp_path), source_rate=16000)
    rec.feed_source(np.full(400, 0.2, dtype=np.float32))
    rec.close()
    # A late frame arriving after teardown must not reopen or crash.
    rec.feed_translated(b"\x00\x01" * 100)
    rec.feed_source(np.full(400, 0.2, dtype=np.float32))


def test_source_clips_out_of_range(tmp_path):
    rec = DualTrackRecorder(str(tmp_path), source_rate=8000)
    rec.feed_source(np.array([2.0, -2.0, 0.0], dtype=np.float32))  # clamp to ±full
    saved = rec.close()
    with wave.open(saved[0], "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert pcm[0] == 32767 and pcm[1] == -32767 and pcm[2] == 0
