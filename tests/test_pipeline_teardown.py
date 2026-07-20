"""pipeline._stop_all must JOIN the translator thread so a late receiver
callback can't leak a ghost turn into the next session (audit P2-4/P2-7)."""
import threading
import time
import types

import pytest

import app.pipeline as pipeline
from app.config import ENGINE_GEMINI


class _FakeTranslator:
    """A real thread that exits once stop() is signalled — mimics a translator
    whose receiver could still fire on_text until the thread actually ends."""

    def __init__(self):
        self._stop = threading.Event()
        self.stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            time.sleep(0.02)

    def stop(self):
        self.stopped = True
        self._stop.set()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self, timeout=None):
        self._thread.join(timeout)


def test_stop_all_joins_translator_thread():
    tr = _FakeTranslator()
    pipe = types.SimpleNamespace(_source=None, capture=None, _stager=None,
                                 player=None, translator=tr)
    assert tr.is_alive()
    pipeline._stop_all(pipe)
    assert tr.stopped                     # stop() was called
    assert not tr.is_alive()              # and _stop_all waited for the thread


def test_stop_all_tolerates_non_thread_translator():
    # A translator without join/is_alive (or None components) must not crash.
    calls = []
    tr = types.SimpleNamespace(stop=lambda: calls.append("stop"))
    pipe = types.SimpleNamespace(_source=None, capture=None, _stager=None,
                                 player=None, translator=tr)
    pipeline._stop_all(pipe)              # must not raise
    assert calls == ["stop"]


class _Resource:
    def __init__(self, *args, **kwargs):
        self.rate = 48000
        self.stopped = False
        self.tts_gain = 1.0

    def stop(self):
        self.stopped = True


class _Stager(_Resource):
    pass


def _incoming_cfg():
    return {
        "devices": {"headphones_output": ""},
        "target_language_incoming": "ru",
        "speaker_labels": False,
        "tts_volume": 1.0,
        "max_ambient_delay_ms": 400,
    }


def test_incoming_resolver_failure_closes_the_open_player(monkeypatch):
    made = []

    def player(*args, **kwargs):
        made.append(_Resource())
        return made[-1]

    monkeypatch.setattr(pipeline, "find_device", lambda *a, **k: 1)
    monkeypatch.setattr(pipeline, "resolve_name", lambda *a, **k: "Headphones")
    monkeypatch.setattr(pipeline, "Player", player)

    with pytest.raises(RuntimeError, match="resolver failed"):
        pipeline.IncomingPipeline(
            _incoming_cfg(), lambda target: (_ for _ in ()).throw(
                RuntimeError("resolver failed")), "video", lambda *a: None,
            lambda *a: None)
    assert made and made[0].stopped


def test_incoming_translator_failure_closes_player_and_stager(monkeypatch):
    player = _Resource()
    stagers = []
    monkeypatch.setattr(pipeline, "find_device", lambda *a, **k: 1)
    monkeypatch.setattr(pipeline, "resolve_name", lambda *a, **k: "Headphones")
    monkeypatch.setattr(pipeline, "Player", lambda *a, **k: player)

    def stager(*args, **kwargs):
        stagers.append(_Stager())
        return stagers[-1]

    monkeypatch.setattr(pipeline, "AdaptivePlaybackStager", stager)
    monkeypatch.setattr(
        pipeline, "make_translator",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("translator failed")))
    with pytest.raises(RuntimeError, match="translator failed"):
        pipeline.IncomingPipeline(
            _incoming_cfg(), lambda target: (ENGINE_GEMINI, "key", "model"),
            "video", lambda *a: None, lambda *a: None)
    assert player.stopped
    assert stagers and stagers[0].stopped


def test_outgoing_resolver_failure_closes_player_and_virtual_mic(monkeypatch):
    player = _Resource()
    torn_down = []
    monkeypatch.setattr(pipeline, "find_device", lambda *a, **k: 1)
    monkeypatch.setattr(pipeline, "Player", lambda *a, **k: player)
    monkeypatch.setattr(pipeline.sysaudio, "make_virtual_mic", lambda: "mic-handle")
    monkeypatch.setattr(pipeline.sysaudio, "snapshot_own_audio_streams", lambda: set())
    monkeypatch.setattr(
        pipeline.sysaudio, "pin_newest_own_stream_to_mic", lambda *a: 1)
    monkeypatch.setattr(
        pipeline.sysaudio, "teardown_virtual_mic", torn_down.append)
    cfg = {
        "devices": {"microphone": "", "meeting_mic_playback": ""},
        "target_language_outgoing": "en",
    }
    with pytest.raises(RuntimeError, match="resolver failed"):
        pipeline.OutgoingPipeline(
            cfg, lambda target: (_ for _ in ()).throw(
                RuntimeError("resolver failed")), lambda *a: None,
            lambda *a: None)
    assert player.stopped
    assert torn_down == ["mic-handle"]


@pytest.mark.parametrize("pipeline_cls", [
    pipeline.IncomingPipeline,
    pipeline.OutgoingPipeline,
])
def test_io_does_not_start_when_translator_never_becomes_ready(pipeline_cls):
    starts = []
    pipe = types.SimpleNamespace(
        translator=types.SimpleNamespace(wait_ready=lambda timeout: False),
        player=types.SimpleNamespace(start=lambda: starts.append("player")),
        capture=types.SimpleNamespace(start=lambda: starts.append("capture")),
        monitor_player=None,
    )
    with pytest.raises(RuntimeError):
        pipeline_cls.start_io(pipe)
    assert starts == []
