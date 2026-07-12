"""Taste spent → the session DOWNGRADES; it does not die.

Cutting a session off mid-film is how a paywall earns resentment instead of a
sale. When the one-time Pro taste runs out the server answers with the cascade,
and the translator is swapped under a live session: capture, VAD and the player
keep running, and only the VOICE changes — which is the pitch.

Pinned here: the send path must keep indirecting through pipe.translator (a
refactor that re-binds it to the instance would keep feeding the dead engine),
the swap happens at most once, and anything the server does not answer with a
cascade for must fall back to the hard stop.
"""
import pytest

from app import pipeline as P
from app.config import ENGINE_CASCADE, ENGINE_GEMINI, ENGINE_QWEN


class _FakeTr:
    def __init__(self, engine):
        self.engine = engine
        self.sent = []
        self.started = False
        self.stopped = False

    def send_pcm16(self, pcm):
        self.sent.append(pcm)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _FakeStager:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakePlayer:
    def __init__(self):
        self.cleared = 0

    def clear_tts(self):
        self.cleared += 1


@pytest.fixture
def pipe(monkeypatch):
    built = []

    def fake_make_translator(cfg, target, *, engine, key, model, on_audio, on_text,
                             on_status, name, noise_reduction=None, on_fatal=None,
                             key_provider=None):
        built.append({"engine": engine, "key": key, "on_fatal": on_fatal})
        return _FakeTr(engine)

    monkeypatch.setattr(P, "make_translator", fake_make_translator)

    p = P.IncomingPipeline.__new__(P.IncomingPipeline)
    p.cfg = {"target_language_incoming": "tr"}
    p._engine = ENGINE_QWEN
    p._downgraded = False
    p.translator = _FakeTr(ENGINE_QWEN)
    p._stager = _FakeStager()
    p._tts_sink = lambda d: None
    p._on_text = lambda *a: None
    p._on_status = lambda *a: None
    p.player = _FakePlayer()
    p.built = built
    p._resolve = lambda target, force_gemini=False: (ENGINE_CASCADE, "key", "model")
    return p


def test_the_dead_engine_stops_talking(pipe):
    """The bug the owner heard: the swap happened, and the user went on listening
    to the OLD voice reading OLD sentences.

    Qwen's speech runs longer than the source, so it buffers seconds ahead, and
    the player's ring holds up to 45 s. Swapping the translator without emptying
    that ring leaves the dead engine's backlog playing over captions that have
    moved on, with the new engine queued politely behind it. It sounds exactly
    like a broken product, and no test caught it because the swap itself was
    perfect.
    """
    P._swap_to_cascade(pipe, "target_language_incoming", "in")
    assert pipe.player.cleared == 1


def test_downgrade_swaps_engine_and_keeps_the_session_alive(pipe):
    dead = pipe.translator
    stager = pipe._stager
    # Exactly the indirection the capture binds (IncomingPipeline send path).
    send = lambda pcm: pipe.translator.send_pcm16(pcm)

    assert P._swap_to_cascade(pipe, "target_language_incoming", "in") is True

    assert pipe._engine == ENGINE_CASCADE
    assert pipe.translator is not dead
    assert pipe.translator.started and dead.stopped
    # Qwen's WSOLA pacing must be retired before the swap: the cascade self-times.
    assert stager.stopped and pipe._stager is None

    send(b"\x01\x02")
    assert pipe.translator.sent == [b"\x01\x02"]   # audio reaches the NEW engine
    assert dead.sent == []                          # and none reaches the dead one


def test_cascade_gets_no_on_fatal(pipe):
    # The free tier is the floor. A failure there must surface, not loop looking
    # for something cheaper.
    P._swap_to_cascade(pipe, "target_language_incoming", "in")
    assert pipe.built[0]["on_fatal"] is None


def test_downgrade_happens_at_most_once(pipe):
    assert P._swap_to_cascade(pipe, "target_language_incoming", "in") is True
    assert P._swap_to_cascade(pipe, "target_language_incoming", "in") is False
    assert len(pipe.built) == 1


def test_no_cascade_offered_falls_back_to_the_hard_stop(pipe):
    # A paid account out of minutes, a disabled cascade, or a spent daily cap:
    # the server answers with something else, and the caller must stop instead.
    pipe._resolve = lambda target, force_gemini=False: (ENGINE_GEMINI, "k", "m")
    assert P._swap_to_cascade(pipe, "target_language_incoming", "in") is False
    assert pipe._engine == ENGINE_QWEN     # untouched
    assert pipe.built == []


def test_a_server_error_falls_back_to_the_hard_stop(pipe):
    def boom(target, force_gemini=False):
        raise RuntimeError("402")

    pipe._resolve = boom
    assert P._swap_to_cascade(pipe, "target_language_incoming", "in") is False


def test_meeting_is_never_downgraded(monkeypatch):
    # The other party would hear a synthetic voice speaking AS the user. That is
    # exactly what the free tier must never do, so Meeting keeps the hard stop.
    ctl = P.ModeController.__new__(P.ModeController)
    ctl.mode = "meeting"
    called = []
    monkeypatch.setattr(P, "_swap_to_cascade", lambda *a: called.append(a) or True)
    assert P.ModeController.downgrade_to_cascade(ctl) is False
    assert called == []
