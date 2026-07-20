"""Meeting confidence monitor mirrors outgoing translation without replacing it."""

from app.pipeline import OutgoingPipeline


class _Sink:
    def __init__(self):
        self.chunks = []

    def feed_tts_pcm16(self, data):
        self.chunks.append(data)


def test_outgoing_translation_is_mirrored_to_monitor():
    pipe = OutgoingPipeline.__new__(OutgoingPipeline)
    pipe.player = _Sink()
    pipe.monitor_player = _Sink()

    pipe._feed_translated_audio(b"translated")

    assert pipe.player.chunks == [b"translated"]
    assert pipe.monitor_player.chunks == [b"translated"]


def test_outgoing_translation_still_reaches_call_without_monitor():
    pipe = OutgoingPipeline.__new__(OutgoingPipeline)
    pipe.player = _Sink()
    pipe.monitor_player = None

    pipe._feed_translated_audio(b"translated")

    assert pipe.player.chunks == [b"translated"]
