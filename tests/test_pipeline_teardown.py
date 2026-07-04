"""pipeline._stop_all must JOIN the translator thread so a late receiver
callback can't leak a ghost turn into the next session (audit P2-4/P2-7)."""
import threading
import time
import types

import app.pipeline as pipeline


class _FakeTranslator:
    """A real thread that exits once stop() is signalled — mimics a translator
    whose receiver could still fire on_text until the thread actually ends."""

    def __init__(self):
        self._stop = threading.Event()
        self.stopped = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            time.sleep(0.02)

    def stop(self):
        self.stopped = True
        self._stop.set()

    def is_alive(self):
        return self._thread.is_alive()

    def join(self, timeout=None):
        self._thread.join(timeout)


def test_stop_all_joins_translator_thread():
    tr = _FakeTranslator()
    pipe = types.SimpleNamespace(_source=None, capture=None, _stager=None,
                                 player=None, translator=tr)
    assert tr.is_alive()
    pipeline._stop_all(pipe)
    assert tr.stopped                     # stop() was called
    assert not tr.is_alive()              # and _stop_all waited for the thread


def test_stop_all_tolerates_non_thread_translator():
    # A translator without join/is_alive (or None components) must not crash.
    calls = []
    tr = types.SimpleNamespace(stop=lambda: calls.append("stop"))
    pipe = types.SimpleNamespace(_source=None, capture=None, _stager=None,
                                 player=None, translator=tr)
    pipeline._stop_all(pipe)              # must not raise
    assert calls == ["stop"]
