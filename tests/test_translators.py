"""Characterization tests for the translation-engine session machines.

These lock the *external contract* that must survive the BaseTranslator
consolidation (P0 #6): the drop-oldest queue, carryover ordering across a
rotation, terminal-vs-transient error classification, when `_ready` fires, and
the reconnect/rotation/terminal outcomes of the `_main` loop — all driven with
in-memory fakes, no network. They must pass identically before and after the
refactor.
"""
import asyncio
import time

import pytest

import app.translator as gem
import app.qwen_translator as qwen

ALL_MODULES = (gem, qwen)
ALL_CLASSES = (gem.LiveTranslator, qwen.QwenTranslator)


def _noop(*a, **k):
    pass


def _make(cls, on_status=_noop, on_audio=_noop, on_text=_noop, target="en"):
    return cls("k", target, on_audio=on_audio, on_text=on_text, on_status=on_status)


# --- shared helpers that move into the base class ---------------------------

@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_put_nowait_drops_oldest_and_counts(cls):
    tr = _make(cls)
    tr._queue = asyncio.Queue(maxsize=50)
    for i in range(50):
        tr._queue.put_nowait(bytes([i % 256]) * 4)
    before = gem._USAGE["dropped_frames"]
    tr._put_nowait(b"NEWEST-FRAME")
    assert tr._queue.qsize() == 50                    # still bounded
    assert gem._USAGE["dropped_frames"] == before + 1  # loss is counted
    # The OLDEST frame (index 0) was evicted; the newest is retained.
    drained = []
    while not tr._queue.empty():
        drained.append(tr._queue.get_nowait())
    assert bytes([0]) * 4 not in drained
    assert b"NEWEST-FRAME" in drained


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_carryover_snapshot_and_reinject_preserve_order(cls):
    tr = _make(cls)
    tr._queue = asyncio.Queue(maxsize=50)
    frames = [b"a", b"b", b"c"]
    for f in frames:
        tr._queue.put_nowait(f)
    tr._snapshot_carryover()
    assert tr._carryover == frames          # oldest-first snapshot
    assert tr._queue.empty()                # queue drained into carryover
    tr._reinject_carryover()
    assert tr._carryover == []              # consumed
    out = [tr._queue.get_nowait() for _ in range(3)]
    assert out == frames                     # order preserved into next session


def test_terminal_error_classification_gemini():
    assert gem._is_terminal_error(RuntimeError("Invalid API key"))
    assert gem._is_terminal_error(RuntimeError("Permission denied for this key"))
    assert gem._is_terminal_error(RuntimeError("resource_exhausted"))
    assert not gem._is_terminal_error(RuntimeError("connection reset by peer"))
    assert not gem._is_terminal_error(RuntimeError("429 rate limit"))  # transient


def test_terminal_error_classification_qwen():
    assert qwen._is_terminal_error(RuntimeError("Arrearage: account in debt"))
    assert qwen._is_terminal_error(RuntimeError("AccessDenied"))
    assert not qwen._is_terminal_error(RuntimeError("InvalidParameter: bad lang"))


@pytest.mark.parametrize("cls", ALL_CLASSES)
def test_terminal_error_prefers_structured_code(cls):
    mod = {gem.LiveTranslator: gem, qwen.QwenTranslator: qwen}[cls]

    class _E(Exception):
        def __init__(self, code):
            self.code = code

    assert mod._is_terminal_error(_E(403))
    assert not mod._is_terminal_error(_E(429))  # rate-limit is transient
    assert not mod._is_terminal_error(_E(500))


# --- Qwen-specific pure logic ----------------------------------------------

def test_qwen_delta_cumulative_to_increments():
    tr = _make(qwen.QwenTranslator)
    # Cumulative stream: each event repeats the full text so far.
    assert tr._delta("_out_acc", "Hel") == "Hel"
    assert tr._delta("_out_acc", "Hello") == "lo"
    assert tr._delta("_out_acc", "Hello") == ""        # no growth → nothing new
    # A shorter, unrelated string after a reset is emitted whole.
    assert tr._delta("_out_acc", "Bye") == " Bye"


def test_qwen_duplicate_audio_detection():
    import numpy as np
    tr = _make(qwen.QwenTranslator)

    def pcm(seed, n=400):
        rng = np.arange(n) + seed
        return (np.sin(rng) * 8000).astype(np.int16).tobytes()

    a = pcm(0)
    b = pcm(9999)
    # First delta: nothing to compare against.
    assert tr._detect_dup_audio(a) is None
    # Distinct next delta → not a duplicate.
    assert tr._detect_dup_audio(b) is None
    # Exact repeat of the previous delta.
    assert tr._detect_dup_audio(b) == "exact-repeat"
    # Cumulative: previous audio + more.
    assert tr._detect_dup_audio(b) == "exact-repeat"   # b again resets prev=b
    assert tr._detect_dup_audio(b + a) == "cumulative-prefix"
    # Overlap tail: a shorter chunk that is a prefix of the previous one.
    assert tr._detect_dup_audio(b) == "overlap-tail"
    assert tr._dup_audio_count == 4
    assert tr._dup_audio_warned is True


def test_qwen_silence_never_flagged_as_duplicate():
    tr = _make(qwen.QwenTranslator)
    silence = b"\x00\x00" * 400
    assert tr._detect_dup_audio(silence) is None
    assert tr._detect_dup_audio(silence) is None   # identical silence is normal
    assert tr._dup_audio_count == 0


def test_qwen_constructor_normalizes_target_and_clamps_knobs():
    tr = qwen.QwenTranslator("k", "zh-Hans", on_audio=_noop, on_text=_noop,
                             on_status=_noop, clone="bogus", vad_silence_ms=250)
    assert tr.target_lang == "zh"          # BCP-47 → base code
    assert tr.clone == "off"               # invalid clone mode clamped
    assert tr.vad_silence_ms == 250


# --- driven _main loop: Qwen websocket family -------------------------------

class _FakeWS:
    """Minimal async websocket: yields seeded messages then blocks until the
    task is cancelled (mimics a live-but-idle socket)."""

    def __init__(self, incoming=()):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._incoming:
            return self._incoming.pop(0)
        while True:
            await asyncio.sleep(0.02)


def _run_ws_translator(cls, connect_impl, ready_msg=None):
    events = []

    class _Driven(cls):
        async def _connect(self):
            return await connect_impl()

    tr = _Driven("k", "en", on_audio=_noop, on_text=_noop,
                 on_status=lambda s: events.append(s))
    tr.start()
    return tr, events


def test_ws_main_sets_ready_only_after_session_event():
    # Qwen connects, then sets _ready only when the server confirms the
    # session (session.created/updated) — NOT on the bare socket open.
    ws = _FakeWS(['{"type":"session.updated"}'])

    async def _connect():
        return ws

    tr, events = _run_ws_translator(qwen.QwenTranslator, _connect)
    try:
        assert tr.wait_ready(5.0)
    finally:
        tr.stop()
        tr.join(timeout=5.0)
    assert not tr.is_alive()
    assert ws.closed


def test_ws_main_terminal_error_breaks_without_retry():
    connects = []
    ws = _FakeWS(['{"type":"error","error":"unauthorized"}'])

    async def _connect():
        connects.append(1)
        return ws

    tr, events = _run_ws_translator(qwen.QwenTranslator, _connect)
    tr.join(timeout=6.0)
    assert not tr.is_alive()
    assert len(connects) == 1  # terminal → no reconnect spin


def test_no_output_watchdog_self_heals_by_reconnecting():
    # Input transcription is flowing but the engine emits NO output — the
    # Beta-off→Gemini "translation stops" failure. The watchdog must escalate
    # from warn-only to a forced reconnect (self-heal), not sit dead. Thresholds
    # shrunk so the stall trips in one sender tick.
    connects = []
    first = _FakeWS(['{"type":"conversation.item.input_audio_transcription.'
                     'completed","transcript":"hola"}'])

    class _Driven(qwen.QwenTranslator):
        NO_OUTPUT_WARN_SECONDS = 0.05
        NO_OUTPUT_ROTATE_SECONDS = 0.1
        INPUT_RECENT_SECONDS = 100.0

        async def _connect(self):
            connects.append(1)
            # Only the first socket carries input; the healed session is idle, so
            # the watchdog disarms and does not spin further reconnects.
            return first if len(connects) == 1 else _FakeWS()

    events = []
    tr = _Driven("k", "en", on_audio=_noop, on_text=_noop,
                 on_status=lambda s: events.append(s))
    tr.start()
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and len(connects) < 2:
            time.sleep(0.05)
        assert len(connects) >= 2  # the stall forced a reconnect
        # Status text is localized (st_noout_reconnect) — pin via the i18n key.
        from app.i18n import t
        expected = t("st_noout_reconnect", name=tr.name,
                     s=int(_Driven.NO_OUTPUT_ROTATE_SECONDS))
        assert any(e == expected for e in events)
    finally:
        tr.stop()
        tr.join(timeout=5.0)
    assert not tr.is_alive()


def test_ws_main_transient_error_retries_then_succeeds():
    calls = {"n": 0}
    ws = _FakeWS(['{"type":"session.updated"}'])

    async def _connect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("temporary reset")
        return ws

    tr, events = _run_ws_translator(qwen.QwenTranslator, _connect)
    try:
        assert tr.wait_ready(8.0)   # succeeds on the 2nd attempt after backoff
        assert calls["n"] == 2
    finally:
        tr.stop()
        tr.join(timeout=6.0)
    assert not tr.is_alive()


# --- driven _main loop: Gemini SDK family -----------------------------------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeSession:
    def __init__(self, responses=()):
        self._responses = list(responses)
        self.sent = []

    async def send_realtime_input(self, audio=None):
        self.sent.append(audio)

    async def receive(self):
        for r in self._responses:
            yield r
        while True:
            await asyncio.sleep(0.02)


class _FakeConnectCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        outer = self

        class _Live:
            def connect(self, model=None, config=None):
                s = outer._sessions.pop(0) if outer._sessions else _FakeSession()
                return _FakeConnectCM(s)

        self.aio = _Obj(live=_Live())


def _patch_gemini_client(monkeypatch, sessions):
    monkeypatch.setattr(gem.genai, "Client",
                        lambda **kw: _FakeClient(sessions))


def test_gemini_main_sets_ready_on_connect(monkeypatch):
    # Gemini sets _ready right after the connect context opens (no separate
    # server 'session live' event), unlike the websocket engines.
    _patch_gemini_client(monkeypatch, [_FakeSession()])
    tr = gem.LiveTranslator("k", "en", on_audio=_noop, on_text=_noop,
                            on_status=_noop)
    tr.start()
    try:
        assert tr.wait_ready(5.0)
    finally:
        tr.stop()
        tr.join(timeout=5.0)
    assert not tr.is_alive()


def test_gemini_goaway_rotates_and_keeps_resume_handle(monkeypatch):
    sru = _Obj(resumable=True, new_handle="H1")
    resp_resume = _Obj(session_resumption_update=sru, go_away=None,
                       server_content=None)
    resp_goaway = _Obj(session_resumption_update=None, go_away=_Obj(),
                       server_content=None)
    s1 = _FakeSession([resp_resume, resp_goaway])
    s2 = _FakeSession()
    _patch_gemini_client(monkeypatch, [s1, s2])

    tr = gem.LiveTranslator("k", "en", on_audio=_noop, on_text=_noop,
                            on_status=_noop)
    tr.start()
    try:
        # The GoAway on session 1 forces a seamless rotation; the resume handle
        # captured from the resumption update survives into session 2.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and tr._resume_handle != "H1":
            time.sleep(0.02)
        assert tr._resume_handle == "H1"
    finally:
        tr.stop()
        tr.join(timeout=5.0)
    assert not tr.is_alive()
