import time

import numpy as np

from app.playback_sync import AdaptivePlaybackStager, time_compress_wsola


class _Ring:
    def __init__(self):
        self.fill = 0


class _Player:
    def __init__(self, rate=48000, input_rate=24000):
        self.rate = rate
        self.input_rate = input_rate
        self.tts = _Ring()
        self.fed = []

    def feed_tts_pcm16(self, data):
        self.fed.append(data)
        input_samples = len(data) // 2
        self.tts.fill += round(input_samples * self.rate / self.input_rate)


def _tone(seconds=1.0, rate=24000, frequency=440.0):
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return np.sin(2 * np.pi * frequency * t).astype(np.float32)


def _frequency(samples, rate):
    sounding = np.flatnonzero(np.abs(samples) > 1e-3)
    active = samples[sounding[0]:sounding[-1] + 1]
    crossings = np.flatnonzero(np.diff(np.signbit(active)))
    return len(crossings) * rate / (2 * len(active))


def test_speed_steps_follow_backlog():
    pick = AdaptivePlaybackStager.speed_for_backlog
    assert pick(0.0) == 1.0
    assert pick(2.99) == 1.0
    assert pick(3.0) == 1.12
    assert pick(5.99) == 1.12
    assert pick(6.0) == 1.25


def test_wsola_shortens_speech_without_raising_pitch():
    source = _tone(seconds=1.0)
    paced = time_compress_wsola(source, 1.25, 24000)

    assert 0.70 < len(paced) / len(source) < 0.90
    assert abs(_frequency(paced, 24000) - _frequency(source, 24000)) < 8.0
    # No extra unwritten hop may remain at the end of each WSOLA block.
    sounding = np.flatnonzero(np.abs(paced) > 1e-5)
    assert len(paced) - sounding[-1] < 8


def test_large_gemini_delta_is_split_and_accelerated():
    player = _Player()
    stager = AdaptivePlaybackStager(player)
    try:
        pcm = (np.clip(_tone(seconds=8.0), -1.0, 1.0) * 32767
               ).astype(np.int16).tobytes()
        stager.feed(pcm)

        deadline = time.monotonic() + 1.0
        while ((stager.speed != 1.25 or stager.sped_s <= 0.0 or not player.fed)
               and time.monotonic() < deadline):
            time.sleep(0.01)

        assert stager.speed == 1.25
        assert stager.sped_s > 0.0
        assert player.fed
        # The provider's eight-second callback must be split into pacing blocks,
        # not shoved into Player as one giant 1x delta.
        assert max(map(len, player.fed)) < len(pcm)
        assert player.tts.fill / player.rate <= stager.FEED_AHEAD_S + 0.6
    finally:
        stager.stop()


def test_stale_trim_keeps_newest_tail_of_one_large_delta():
    player = _Player()
    # Hold the worker outside its feed-ahead window so the pending accounting is
    # deterministic while the provider callback is being trimmed.
    player.tts.fill = player.rate * 3
    stager = AdaptivePlaybackStager(player)
    try:
        pcm = np.zeros(13 * 24000, dtype=np.int16).tobytes()
        stager.feed(pcm)

        assert stager.skipped_s == 9.0
        # Four newest pending seconds plus the three already in Player.
        assert stager.backlog_s == 7.0
    finally:
        stager.stop()
