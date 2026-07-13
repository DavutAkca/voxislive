"""The taste wall: when the free tier's Pro minutes run out, the session stops
GRACEFULLY and a card offers the free voice — it does not die mid-word, and it
does not swap engines under a live session.

The hot swap this replaces failed in the field in a way no assertion caught: the
UI said "Pro voice" while Piper was speaking, because two engines inside one
session require the interface and the audio to update in lockstep. The owner's
design (2026-07-13) makes that state unrepresentable — one session, one engine —
so what these tests pin is the ROUTING: who gets the wall card, who gets the
hard stop, and that the Pro voice is allowed to finish its sentence first.
"""
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.webui import Bridge  # noqa: E402


def _bridge(quota, mode="video", engine="gemini"):
    """A Bridge carrying only what _on_quota_exceeded touches.

    `engine` is the engine of the LIVE session — the discriminator that tells the
    taste wall (Pro voice ran out) apart from the daily wall (the free voice ran
    out of today's minutes). Defaults to the paid engine, i.e. the taste case.
    """
    b = Bridge.__new__(Bridge)
    b._last_quota = quota
    b._key_cache_lock = threading.Lock()
    b._key_cache = {}
    b._key_epoch = 0
    b._last_error_code = None
    b._session_error = False
    b.events = []
    b._put_event = b.events.append
    b.statuses = []
    b._emit_status = lambda msg, level="info": b.statuses.append((msg, level))
    b.stopped = []
    b.stop = lambda: b.stopped.append(True)
    b.drained = []
    b._drain_tts = lambda timeout=8.0: b.drained.append(True)
    b.controller = types.SimpleNamespace(mode=mode, incoming=lambda: None,
                                         current_engine=lambda: engine)
    return b


def _fired(b):
    return [e[0] for e in b.events]


def test_free_tier_gets_the_wall_card_not_an_error():
    b = _bridge({"cascade_ready": True, "tier": "free"})
    Bridge._on_quota_exceeded(b)
    assert ("taste_wall", {"mode": "video"}) in b.events
    assert "quota_wall" not in _fired(b)     # the hard paywall stays down
    assert b.stopped and b.drained           # sentence finished, then stopped
    assert b._session_error is False         # the taste ending is not a failure
    assert b._last_error_code is None


def test_the_sentence_finishes_before_the_card_appears():
    # Drain, then stop, then the card. Any other order either clips the paid
    # voice mid-word or shows a decision card over a session that is still talking.
    b = _bridge({"cascade_ready": True})
    order = []
    b._drain_tts = lambda timeout=8.0: order.append("drain")
    b.stop = lambda: order.append("stop")
    b._put_event = lambda ev: order.append(ev[0])
    Bridge._on_quota_exceeded(b)
    assert order[-3:] == ["drain", "stop", "taste_wall"]


def test_paid_account_out_of_minutes_hits_the_paywall():
    # The server never marks a customer cascade_ready; out of minutes means
    # "buy more", not a quiet demotion to the robot voice.
    b = _bridge({"cascade_ready": False, "tier": "paid"})
    Bridge._on_quota_exceeded(b)
    assert "quota_wall" in _fired(b)
    assert "taste_wall" not in _fired(b)
    assert b._session_error is True


def test_meeting_never_gets_the_free_voice_offer():
    # In Meeting the other party hears a voice speaking AS the user; the free
    # tier must not put a synthetic voice in a stranger's ear.
    b = _bridge({"cascade_ready": True}, mode="meeting")
    Bridge._on_quota_exceeded(b)
    assert "quota_wall" in _fired(b)
    assert "taste_wall" not in _fired(b)


def test_no_quota_snapshot_falls_back_to_the_paywall():
    b = _bridge(None)
    Bridge._on_quota_exceeded(b)
    assert "quota_wall" in _fired(b)


# ── the DAILY wall ─────────────────────────────────────────────────────────────
# A 402 arriving while the CASCADE is the live engine can only be the daily
# allowance: the server's cascade heartbeat path never compares against the
# license quota, it only books against the 10-min/day counter. Before this, such
# a 402 re-raised the TASTE wall — offering the free voice to someone who had
# been listening to it for ten minutes (owner report, 2026-07-13).

def test_cascade_402_raises_the_daily_wall_not_the_taste_wall():
    b = _bridge({"cascade_ready": True, "cascade_available": False, "tier": "free"},
                engine="cascade")
    Bridge._on_quota_exceeded(b)
    assert "daily_wall" in _fired(b)
    assert "taste_wall" not in _fired(b)     # you cannot hand them what they have
    assert "quota_wall" not in _fired(b)     # and it is not the paid paywall
    assert b.stopped and b.drained
    assert b._session_error is False         # the day ending is not a failure
    assert b._last_error_code is None


def test_the_daily_wall_does_not_depend_on_the_new_server_field():
    """The engine is the discriminator, not the quota flags — so the daily wall is
    correct even against a server that never sends cascade_available."""
    b = _bridge({"cascade_ready": True, "tier": "free"}, engine="cascade")
    Bridge._on_quota_exceeded(b)
    assert "daily_wall" in _fired(b)
    assert "taste_wall" not in _fired(b)


def test_taste_wall_still_fires_when_the_free_day_is_open():
    """The Pro engine ran out with free minutes left today: the handover offer is
    real, so make it."""
    b = _bridge({"cascade_ready": True, "cascade_available": True, "tier": "free"},
                engine="gemini")
    Bridge._on_quota_exceeded(b)
    assert ("taste_wall", {"mode": "video"}) in b.events
    assert "daily_wall" not in _fired(b)


def test_taste_and_daily_exhausted_together_is_not_an_offer_to_nowhere():
    """Taste spent AND today's free minutes already gone: continuing on the free
    voice would start a session the server refuses, so the wall must not offer it.
    cascade_available is False, so this falls through to the paywall."""
    b = _bridge({"cascade_ready": True, "cascade_available": False, "tier": "free"},
                engine="gemini")
    Bridge._on_quota_exceeded(b)
    assert "taste_wall" not in _fired(b)
    assert "quota_wall" in _fired(b)


def test_drain_lets_the_ring_empty_then_returns():
    """_drain_tts stops the translator (no NEW audio) and waits for the player's
    ring to finish the sentence it already holds."""
    translator = types.SimpleNamespace(stopped=[])
    translator.stop = lambda: translator.stopped.append(True)
    player = types.SimpleNamespace(tts_active=True)
    inc = types.SimpleNamespace(translator=translator, player=player)

    b = Bridge.__new__(Bridge)
    b.controller = types.SimpleNamespace(incoming=lambda: inc)

    def go_quiet():
        time.sleep(0.3)
        player.tts_active = False

    threading.Thread(target=go_quiet, daemon=True).start()
    t0 = time.time()
    Bridge._drain_tts(b, timeout=5.0)
    took = time.time() - t0
    assert translator.stopped            # no new audio while draining
    assert 0.2 <= took < 2.0             # waited for the ring, not the timeout
