"""A prefetched session key is a GRANT, and a grant is only as good as the quota
it was issued under.

The field data (2026-07-13) showed a free QA account running the PAID engine well
past its 15-minute taste — billable minutes reaching ~30/15, with `qwen` sessions
appearing AFTER the cap. The mechanism: the key prefetch runs on a background
thread with a 4-minute TTL. When the quota ran out mid-session, _on_quota_exceeded
cleared the cache — but a prefetch already IN FLIGHT (carrying a voiced grant
issued while the user still had minutes) landed afterwards and wrote itself back
in. The next Start spent that grant: a spent free account got the paid engine,
which we pay for and the user is billed for.

Two guards, pinned here: an epoch that stops the in-flight write, and a use-time
check that refuses a voiced grant whenever the taste is spent.
"""
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ENGINE_CASCADE  # noqa: E402
from app.webui import Bridge  # noqa: E402


def _bridge(quota=None):
    b = Bridge.__new__(Bridge)
    b._key_cache = {}
    b._key_cache_lock = threading.Lock()
    b._key_epoch = 0
    b._last_quota = quota
    return b


def _grant(engine, age=0.0):
    """(stamped_at, engine, key, model, quality, workspace)"""
    return (time.time() - age, engine, "KEY", "model", "balanced", None)


IN_QUOTA = {"allowed_minutes": 15, "used_minutes": 3, "remaining": 12}
SPENT = {"allowed_minutes": 15, "used_minutes": 15, "remaining": 0}
PAID = {"unlimited": True}


def test_a_voiced_grant_is_refused_once_the_taste_is_spent():
    b = _bridge(SPENT)
    b._key_cache["en"] = _grant("qwen")
    assert Bridge._pop_prefetched_key(b, "en") is None   # never the paid engine


def test_a_cascade_grant_still_serves_a_spent_account():
    """The free tier must stay reachable — refusing everything would grey out the
    whole free journey, which is the failure this guard must not reintroduce."""
    b = _bridge(SPENT)
    b._key_cache["en"] = _grant(ENGINE_CASCADE)
    got = Bridge._pop_prefetched_key(b, "en")
    assert got is not None and got[0] == ENGINE_CASCADE


def test_a_voiced_grant_serves_a_user_who_still_has_minutes():
    b = _bridge(IN_QUOTA)
    b._key_cache["en"] = _grant("qwen")
    got = Bridge._pop_prefetched_key(b, "en")
    assert got is not None and got[0] == "qwen"


def test_a_paying_customer_is_never_refused():
    """Fails OPEN on an unlimited/unknown quota: breaking a paying customer is far
    worse than one session the server's own 402 would stop anyway."""
    for quota in (PAID, None, {}):
        b = _bridge(quota)
        b._key_cache["en"] = _grant("gemini")
        assert Bridge._pop_prefetched_key(b, "en") is not None


def test_quota_exceeded_bumps_the_epoch_so_an_in_flight_prefetch_cannot_republish():
    """THE regression. Clearing the dict cannot stop a fetch that is already in
    the air; only the epoch can."""
    b = _bridge(IN_QUOTA)
    b.controller = types.SimpleNamespace(mode="video", incoming=lambda: None,
                                         current_engine=lambda: "qwen")
    b.events = []
    b._put_event = b.events.append
    b._emit_status = lambda *a, **k: None
    b._drain_tts = lambda timeout=8.0: None
    b.stop = lambda: None
    b._session_error = False
    b._last_error_code = None

    # A prefetch took the epoch and is now "in flight" with a VOICED grant.
    with b._key_cache_lock:
        epoch = b._key_epoch

    Bridge._on_quota_exceeded(b)          # the taste runs out mid-session

    # …and only now does the in-flight fetch come back and try to publish.
    with b._key_cache_lock:
        may_publish = (b._key_epoch == epoch)
        if may_publish:
            b._key_cache["en"] = _grant("qwen")

    assert not may_publish                 # the world moved; the grant is disowned
    assert b._key_cache == {}              # so no paid engine is left lying around


def test_a_prefetch_that_lands_with_no_quota_change_still_publishes():
    """The guard must not break the happy path it was bolted onto — the prefetch
    exists to make Start feel instant."""
    b = _bridge(IN_QUOTA)
    with b._key_cache_lock:
        epoch = b._key_epoch
        may_publish = (b._key_epoch == epoch)
        if may_publish:
            b._key_cache["en"] = _grant("qwen")
    assert may_publish
    assert Bridge._pop_prefetched_key(b, "en") is not None


def test_a_stale_grant_is_dropped_by_ttl():
    b = _bridge(IN_QUOTA)
    b._key_cache["en"] = _grant("qwen", age=10_000)
    assert Bridge._pop_prefetched_key(b, "en") is None
