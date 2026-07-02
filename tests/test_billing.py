"""ModeController._consume_minutes — the atomic billing watermark. Money math:
the same interval must never be consumable twice, and outage time must be
skipped (not deferred)."""
import time

from app.pipeline import ModeController


def _mc():
    cfg = {"capture_backend": "driverless", "devices": {}}
    return ModeController(cfg, None, lambda *a: None, lambda *a: None)


def test_no_session_yields_nothing():
    mc = _mc()
    assert mc._consume_minutes(accrue=True) == (None, 0.0, None)


def test_accrues_elapsed_and_advances_watermark():
    mc = _mc()
    mc._session_id = "s1"
    mc._session_mode = "video"
    mc._last_report = time.monotonic() - 12.0  # 12 s ago
    sid, delta, source = mc._consume_minutes(accrue=True)
    assert sid == "s1" and source == "video"
    assert 0.19 <= delta <= 0.25  # ~0.2 min
    # Watermark advanced: an immediate second consume sees ~nothing.
    _, delta2, _ = mc._consume_minutes(accrue=True)
    assert delta2 < 0.01


def test_outage_time_is_dropped_not_deferred():
    mc = _mc()
    mc._session_id = "s1"
    mc._session_mode = "video"
    mc._last_report = time.monotonic() - 30.0
    sid, delta, _ = mc._consume_minutes(accrue=False)
    assert sid == "s1" and delta == 0.0
    # The skipped interval must NOT come back on the next accruing consume.
    _, delta2, _ = mc._consume_minutes(accrue=True)
    assert delta2 < 0.01


def test_meeting_mode_reports_incoming_source():
    mc = _mc()
    mc._session_id = "s1"
    mc._session_mode = "meeting"
    mc._last_report = time.monotonic()
    _, _, source = mc._consume_minutes(accrue=True)
    assert source == "meeting_incoming"


def test_negative_clock_glitch_clamped_to_zero():
    mc = _mc()
    mc._session_id = "s1"
    mc._session_mode = "video"
    mc._last_report = time.monotonic() + 60.0  # future watermark (clock glitch)
    _, delta, _ = mc._consume_minutes(accrue=True)
    assert delta == 0.0
