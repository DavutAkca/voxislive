"""Session-key prefetch cache semantics (the ~200-400 ms start-feel win).

Network is never touched here: only the cache take logic — freshness TTL,
single-use consumption, and per-target isolation — which is what guarantees
that any miss falls back to the unchanged synchronous fetch path.
"""
import threading
import time

from app.webui import KEY_PREFETCH_TTL, Bridge


def _bare_bridge():
    b = object.__new__(Bridge)
    b._key_cache = {}
    b._key_cache_lock = threading.Lock()
    return b


def _put(b, target, ts, engine="gemini", key="k", model="m", quality="balanced",
         workspace=None):
    b._key_cache[target] = (ts, engine, key, model, quality, workspace)


def test_fresh_hit_is_returned_and_consumed():
    b = _bare_bridge()
    _put(b, "tr", time.time())
    assert b._pop_prefetched_key("tr") == ("gemini", "k", "m", "balanced", None)
    # Single-use: ephemeral tokens are never reused across sessions.
    assert b._pop_prefetched_key("tr") is None


def test_stale_entry_misses():
    b = _bare_bridge()
    _put(b, "tr", time.time() - KEY_PREFETCH_TTL - 1)
    assert b._pop_prefetched_key("tr") is None
    assert "tr" not in b._key_cache  # stale entries are dropped, not kept


def test_targets_are_isolated():
    b = _bare_bridge()
    _put(b, "tr", time.time())
    assert b._pop_prefetched_key("en") is None   # other target: miss
    assert b._pop_prefetched_key("tr") is not None
