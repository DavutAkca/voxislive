"""Closing the main window must also destroy the overlay window.

pywebview leaves its message loop only when the LAST window is destroyed. With
the overlay still up, X on the main window left webview.start() spinning: the
post-loop cleanup that stops the session never ran, so translation kept playing
into a now-headless overlay while the process held the single-instance mutex and
the app refused to reopen (field report, 2026-07-13).

These pin the closing edge: the overlay dies with the main window, the user's
overlay preference survives the shutdown, and nothing here can raise into
pywebview's FormClosing handler.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.webui import Bridge  # noqa: E402


def _bridge(overlay_win=None, overlay_enabled=True):
    """A Bridge carrying only what the close path touches."""
    b = Bridge.__new__(Bridge)
    b.cfg = {"overlay_enabled": overlay_enabled}
    b.saved = []
    b._save_cfg = lambda: b.saved.append(dict(b.cfg))
    b._win_geom = {"w": 1200, "h": 800}
    b._maximized = False
    b._overlay_win = overlay_win
    return b


def _closing(b):
    """Drive the closing edge, then disarm the hard-exit watchdog it arms — left
    running, its os._exit(0) would take the test process down with it."""
    b._on_win_closing()
    b._close_watchdog.cancel()
    return b._close_watchdog


class _FakeWindow:
    def __init__(self, boom=False):
        self.destroyed = False
        self._boom = boom

    def destroy(self):
        if self._boom:
            raise RuntimeError("window already gone")
        self.destroyed = True


def test_closing_main_window_destroys_overlay():
    win = _FakeWindow()
    b = _bridge(win)

    _closing(b)

    assert win.destroyed          # the loop can now reach instances == 0 and exit
    assert b._overlay_win is None


def test_closing_keeps_the_overlay_preference():
    """Tearing the overlay down at shutdown must not read as "user turned it
    off" — the next launch still opens with the overlay on."""
    b = _bridge(_FakeWindow(), overlay_enabled=True)

    _closing(b)

    assert b.cfg["overlay_enabled"] is True


def test_closing_persists_geometry():
    b = _bridge(_FakeWindow())
    b._win_geom = {"w": 1024, "h": 700, "x": 40, "y": 60}

    _closing(b)

    assert b.cfg["window"] == {"w": 1024, "h": 700, "x": 40, "y": 60, "max": False}
    assert b.saved


def test_closing_survives_a_dead_overlay_window():
    """A destroy() that throws must not escape into pywebview's FormClosing
    handler — a raising handler there can cancel the close and re-strand us."""
    b = _bridge(_FakeWindow(boom=True))

    _closing(b)  # must not raise

    assert b._overlay_win is None


def test_closing_without_an_overlay_is_a_no_op():
    b = _bridge(None)

    _closing(b)

    assert b._overlay_win is None


def test_closing_arms_the_hard_exit_watchdog():
    """Whatever else pins pywebview's message loop, the close must still end the
    process — otherwise the mutex-holding zombie comes back."""
    b = _bridge(_FakeWindow())

    watchdog = _closing(b)

    assert watchdog.daemon
    assert watchdog.function is not None


def test_toggle_overlay_off_still_persists_the_preference():
    """The manual toggle keeps its own semantics: destroy AND remember off."""
    win = _FakeWindow()
    b = _bridge(win, overlay_enabled=True)

    b.toggle_overlay(False)

    assert win.destroyed
    assert b._overlay_win is None
    assert b.cfg["overlay_enabled"] is False
