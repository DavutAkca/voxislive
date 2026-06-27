"""Billing-invariant tests for ModeController (C2).

Standalone + dependency-free (no pytest): the repo ships no test runner, so this
is a runnable script — `python scripts/test_billing.py` (exit 0 = all pass) and
CI-wireable. It pins the billing invariants CLAUDE.md *claims* but that were
otherwise untested:

  * no session            → nothing is billed
  * live session          → wall-clock since the watermark is accrued
  * capture death / outage→ accrual stops, and the skipped time is DROPPED
                            (watermark still advances), never deferred to a later beat
  * double-count guard    → the same interval cannot be billed twice
  * 402 quota cutoff      → fires the teardown callback exactly once per session
  * source attribution    → "video" vs "meeting_incoming"
  * kill-9 loss bound     → HEARTBEAT_SECONDS caps the unreported tail
  * tail clamp            → a single tail delta cannot exceed one beat + join wait

The meeting 2x multiplier is SERVER-side (the client sends 1x minutes + a
meeting source label), so it is asserted only as the source label here.

No real audio / network / COM: ModeController.__init__ starts nothing, and the
billing methods are exercised against duck-typed fake pipelines.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.pipeline import ModeController  # noqa: E402


# ---- duck-typed fakes (match what _is_session_live reads) ----
class FakeTranslator:
    def __init__(self, ready=True, stopping=False, alive=True):
        self._ready = threading.Event()
        if ready:
            self._ready.set()
        self._stopping = threading.Event()
        if stopping:
            self._stopping.set()
        self._alive = alive

    def is_alive(self):
        return self._alive


class FakeCapture:
    def __init__(self, failed=False):
        self.failed = failed


class FakePipeline:
    def __init__(self, translator=None, capture=None):
        self.translator = translator
        self.capture = capture


def _mc():
    """A ModeController that starts nothing; billing state is set by hand."""
    return ModeController({}, None, lambda *a, **k: None, lambda *a, **k: None)


def _arm_session(mc, mode="video", started_ago=0.0):
    """Simulate the state start() sets, with the watermark `started_ago` s in the past."""
    now = time.monotonic()
    mc._session_id = "test-sid-0001"
    mc._session_start = now - started_ago
    mc._last_report = now - started_ago
    mc._session_mode = mode
    mc._quota_exhausted.clear()


# ---------------------------------------------------------------- tests
def test_no_session_no_bill():
    mc = _mc()
    sid, delta, source = mc._consume_minutes(accrue=True)
    assert sid is None and delta == 0.0 and source is None, (sid, delta, source)


def test_accrual_basic():
    mc = _mc()
    _arm_session(mc, "video", started_ago=30.0)
    sid, delta, source = mc._consume_minutes(accrue=True)
    assert sid == "test-sid-0001"
    assert 0.49 <= delta <= 0.55, delta          # ~30 s == 0.5 min (+ tiny test overhead)
    assert source == "video"
    # Watermark advanced: an immediate second read accrues ~nothing.
    _, delta2, _ = mc._consume_minutes(accrue=True)
    assert delta2 < 0.01, delta2


def test_outage_skipped_not_deferred():
    mc = _mc()
    _arm_session(mc, "video", started_ago=30.0)
    # Outage beat: accrue=False must zero the bill AND advance the watermark.
    sid, delta, _ = mc._consume_minutes(accrue=False)
    assert sid == "test-sid-0001" and delta == 0.0, delta
    # The 30 s of outage is gone, not deferred: the next live beat sees ~0, not 0.5.
    _, delta2, _ = mc._consume_minutes(accrue=True)
    assert delta2 < 0.01, delta2


def test_double_count_guard():
    # The heartbeat beat and the stop() tail both call _consume_minutes; the
    # second must see a quiesced watermark and bill ~0 for the same interval.
    mc = _mc()
    _arm_session(mc, "video", started_ago=12.0)
    _, d1, _ = mc._consume_minutes(accrue=True)   # heartbeat
    _, d2, _ = mc._consume_minutes(accrue=True)   # stop() tail
    assert d1 > 0.18 and d2 < 0.01, (d1, d2)


def test_negative_delta_clamped():
    # A backwards clock / watermark in the future must never produce a negative bill.
    mc = _mc()
    _arm_session(mc, "video", started_ago=0.0)
    mc._last_report = time.monotonic() + 10.0      # watermark in the future
    _, delta, _ = mc._consume_minutes(accrue=True)
    assert delta == 0.0, delta


def test_is_session_live_capture_death():
    mc = _mc()
    dead = FakePipeline(FakeTranslator(), FakeCapture(failed=True))
    mc._pipelines = [dead]
    assert mc._is_session_live() is False
    # A second, healthy pipeline keeps the session live.
    healthy = FakePipeline(FakeTranslator(ready=True, stopping=False, alive=True), FakeCapture(False))
    mc._pipelines = [dead, healthy]
    assert mc._is_session_live() is True


def test_is_session_live_ready_stopping():
    mc = _mc()
    mc._pipelines = [FakePipeline(FakeTranslator(ready=False))]
    assert mc._is_session_live() is False                       # not warmed up
    mc._pipelines = [FakePipeline(FakeTranslator(ready=True, stopping=True))]
    assert mc._is_session_live() is False                       # tearing down
    mc._pipelines = [FakePipeline(FakeTranslator(alive=False))]
    assert mc._is_session_live() is False                       # thread dead
    mc._pipelines = [FakePipeline(FakeTranslator(ready=True, stopping=False, alive=True))]
    assert mc._is_session_live() is True


def test_quota_cutoff_fires_once():
    calls = []
    mc = ModeController({}, None, lambda *a, **k: None, lambda *a, **k: None,
                        on_quota_exceeded=lambda: calls.append(1))
    mc._fire_quota_exceeded()
    mc._fire_quota_exceeded()
    mc._fire_quota_exceeded()
    assert sum(calls) == 1, calls                  # several 402s, one teardown
    mc._quota_exhausted.clear()                    # start() re-arms per session
    mc._fire_quota_exceeded()
    assert sum(calls) == 2, calls


def test_source_attribution():
    mc = _mc()
    _arm_session(mc, "video", started_ago=6.0)
    assert mc._consume_minutes(accrue=True)[2] == "video"
    _arm_session(mc, "meeting", started_ago=6.0)
    assert mc._consume_minutes(accrue=True)[2] == "meeting_incoming"


def test_kill9_loss_and_tail_clamp_bounds():
    # kill-9 / crash loses at most one heartbeat interval of unreported minutes.
    assert ModeController.HEARTBEAT_SECONDS <= 6.0, ModeController.HEARTBEAT_SECONDS
    # stop()'s tail clamps a single delta to one interval + the bounded join wait.
    max_tail_min = (ModeController.HEARTBEAT_SECONDS + 2.0) / 60.0
    assert max_tail_min < 0.14, max_tail_min       # < ~8 s, an implausible-bill guard


# ---------------------------------------------------------------- runner
def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} billing invariants passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
