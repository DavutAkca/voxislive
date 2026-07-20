"""Mid-session engine failover.

When DashScope runs out of balance it rejects with a terminal 'arrearage' error.
The server cannot see that — the Qwen key is still configured, so /auth/session-key
keeps routing every voiced target to Qwen — so without a client-side substitution
the whole 29-language Qwen tier goes dark, one dead session at a time.

These pin the two things that make the swap work and that a refactor could
silently undo: the send path must indirect through the pipeline (not be bound to
the translator instance), and _give_up must route every abandon-path through
on_fatal.
"""
import pytest

from app import pipeline as P
from app.base_translator import BaseTranslator, is_terminal_error
from app.config import ENGINE_GEMINI, ENGINE_OPENAI, ENGINE_QWEN
from app.qwen_translator import _TERMINAL_PHRASES


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
    def __init__(self, *args, **kwargs):
        self.stopped = False
        self.cleared = 0

    def clear(self):
        self.cleared += 1

    def stop(self):
        self.stopped = True


class _FakePlayer:
    def __init__(self):
        self.cleared = 0

    def clear_tts(self):
        self.cleared += 1


class _FakeSource:
    def __init__(self, rate=24000):
        self.rate = rate

    def set_send_rate(self, rate):
        self.rate = rate


@pytest.fixture
def pipe(monkeypatch):
    """An IncomingPipeline carrying only the state the failover touches."""
    built = []

    def fake_make_translator(cfg, target, *, engine, key, model, on_audio, on_text,
                             on_status, name, noise_reduction=None, on_fatal=None,
                             key_provider=None):
        built.append({"engine": engine, "key": key, "on_fatal": on_fatal,
                      "key_provider": key_provider})
        return _FakeTr(engine)

    monkeypatch.setattr(P, "make_translator", fake_make_translator)

    p = P.IncomingPipeline.__new__(P.IncomingPipeline)
    p.cfg = {"target_language_incoming": "tr"}
    p._engine = ENGINE_QWEN
    p._failover_done = False
    p.translator = _FakeTr(ENGINE_QWEN)
    p._stager = _FakeStager()
    p.player = _FakePlayer()
    p._tts_sink = lambda d: None
    p._on_text = lambda *a: None
    p.statuses = []
    p._on_status = p.statuses.append
    p.built = built

    def resolve(target, force_gemini=False):
        assert force_gemini, "failover must ask for Gemini explicitly"
        return ENGINE_GEMINI, "gem-key", "gemini-model"

    p._resolve = resolve
    return p


@pytest.mark.parametrize("msg", [
    "Arrearage: Your account is in arrears, please recharge.",
    "quota exceeded for model qwen3.5-livetranslate-flash-realtime",
    "billing account not active",
])
def test_out_of_balance_is_terminal(msg):
    """An exhausted DashScope balance must be classed terminal — that is what
    triggers the failover instead of an endless reconnect."""
    assert is_terminal_error(Exception(msg), _TERMINAL_PHRASES)


def test_transient_error_is_not_terminal():
    assert not is_terminal_error(Exception("connection reset by peer"), _TERMINAL_PHRASES)


def test_failover_swaps_engine_and_redirects_audio(pipe):
    dead = pipe.translator
    stager = pipe._stager
    # The capture binds exactly this indirection (see IncomingPipeline.send_fn).
    def send(pcm):
        pipe.translator.send_pcm16(pcm)
    send(b"before")

    assert pipe._failover_to_gemini(Exception("Arrearage")) is True

    send(b"after")
    live = pipe.translator
    assert pipe._engine == ENGINE_GEMINI
    assert live is not dead and live.engine == ENGINE_GEMINI
    assert live.started and dead.stopped
    # The whole point: audio follows the swap without rebuilding the capture.
    assert live.sent == [b"after"]
    assert dead.sent == [b"before"]
    # Pending audio from the dead provider is cleared, while the shared
    # adaptive playback worker remains available to catch Gemini up too.
    assert stager.cleared == 1 and not stager.stopped
    assert pipe._stager is stager
    assert pipe.statuses  # user is told, once


def test_gemini_is_the_last_resort(pipe):
    """The replacement gets no on_fatal, and a second failure is not retried —
    otherwise a total outage would loop instead of surfacing."""
    assert pipe._failover_to_gemini(Exception("Arrearage")) is True
    assert pipe.built[-1]["on_fatal"] is None
    assert pipe._failover_to_gemini(Exception("boom")) is False


def test_failover_forwards_key_provider(pipe):
    """The SaaS resolver hangs the Gemini key fountain off the resolve fn; the
    failover replacement must inherit it so an ephemeral-token session can still
    refresh its key across rotations after the swap."""
    def provider():
        return "auth_tokens/next"
    pipe._resolve.gemini_key_provider = provider
    assert pipe._failover_to_gemini(Exception("Arrearage")) is True
    assert pipe.built[-1]["key_provider"] is provider


def test_no_failover_when_already_on_gemini(pipe):
    pipe._engine = ENGINE_GEMINI
    assert pipe._failover_to_gemini(Exception("boom")) is False


def test_failover_declines_when_no_gemini_key(pipe):
    def resolve(target, force_gemini=False):
        raise RuntimeError("no key")

    pipe._resolve = resolve
    assert pipe._failover_to_gemini(Exception("Arrearage")) is False
    assert pipe.translator.engine == ENGINE_QWEN  # left as-is, caller surfaces the error


class _Dummy(BaseTranslator):
    TERMINAL_PHRASES = _TERMINAL_PHRASES

    def __init__(self, on_status, on_fatal=None):
        super().__init__("k", "en", lambda *a: None, lambda *a: None, on_status,
                         rotate_minutes=1, name="dummy", on_fatal=on_fatal)

    async def _connect(self):
        pass

    async def _open_session(self, conn):
        pass

    async def _run_session(self):
        return False


def test_handled_fatal_stays_silent():
    """A successful substitution must not show the user a connection error."""
    statuses = []
    d = _Dummy(statuses.append, on_fatal=lambda e: True)
    d._give_up(Exception("Arrearage"))
    assert statuses == []


@pytest.mark.parametrize("on_fatal", [
    None,                       # nobody listening — the old behaviour
    lambda e: False,            # substitution declined
    lambda e: 1 / 0,            # broken handler must not swallow the failure
])
def test_unhandled_fatal_still_surfaces(on_fatal):
    statuses = []
    d = _Dummy(statuses.append, on_fatal=on_fatal)
    d._give_up(Exception("Arrearage"))
    assert len(statuses) == 1


def test_the_dead_engine_stops_talking(pipe):
    """Qwen buffers seconds ahead of the source (that is what the stager is for),
    and the player's ring holds up to 45 s. Failing over without emptying it
    leaves the DEAD engine's voice reading stale sentences over live captions,
    with Gemini queued behind the backlog — the failure reads as a broken product
    rather than a recovered one."""
    P._swap_to_gemini(pipe, "target_language_incoming", "in", Exception("arrearage"))
    assert pipe.player.cleared == 1


def test_openai_failover_retargets_capture_to_gemini_16khz(pipe, monkeypatch):
    monkeypatch.setattr(P, "AdaptivePlaybackStager", _FakeStager)
    pipe._engine = ENGINE_OPENAI
    pipe._stager = None
    pipe._source = _FakeSource(24000)
    assert pipe._failover_to_gemini(Exception("quota")) is True
    assert pipe._source.rate == 16000
    # Incoming OpenAI starts without a pacing worker; Gemini needs one.
    assert pipe._stager is not None
