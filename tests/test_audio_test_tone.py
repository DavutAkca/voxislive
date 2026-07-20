"""The output diagnostic tone must target the requested device safely."""

import numpy as np

from app import audio_io


class _OutputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.written = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def write(self, data):
        self.written = data.copy()


def test_tone_targets_device_and_is_faded(monkeypatch):
    made = []
    monkeypatch.setattr(
        audio_io.sd,
        "query_devices",
        lambda device: {"default_samplerate": 48_000, "max_output_channels": 2},
    )
    monkeypatch.setattr(
        audio_io.sd,
        "OutputStream",
        lambda **kwargs: made.append(_OutputStream(**kwargs)) or made[-1],
    )

    audio_io.play_test_tone(12, duration=0.1)

    stream = made[0]
    assert stream.kwargs["device"] == 12
    assert stream.kwargs["samplerate"] == 48_000
    assert stream.written.shape == (4_800, 2)
    assert np.max(np.abs(stream.written)) <= 0.081
    assert stream.written[0, 0] == 0.0
    assert abs(stream.written[-1, 0]) < 1e-6
