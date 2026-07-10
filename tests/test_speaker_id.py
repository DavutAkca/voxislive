"""Speaker-change detection: fbank featurizer + online clustering (no model).

The CAM++ embedding model itself is exercised by the scripted simulation
(vault: speaker-labels feature note) against real multi-speaker WAVs; these
unit tests pin down the pure-numpy featurizer and the clustering/hysteresis
state machine with injected embeddings, so they run without the 27 MB model.
"""
import threading
import time

import numpy as np

from app.speaker_id import SAMPLE_RATE, SpeakerTracker, fbank


# ---- fbank ----

def test_fbank_shape_and_finiteness():
    wav = np.random.default_rng(0).standard_normal(SAMPLE_RATE).astype(np.float32) * 0.1
    f = fbank(wav)
    # snip_edges framing: 1 + (16000 - 400) // 160 = 98 frames, 80 mel bins.
    assert f.shape == (98, 80)
    assert np.isfinite(f).all()


def test_fbank_short_input_yields_empty():
    assert fbank(np.zeros(100, dtype=np.float32)).shape == (0, 80)


def test_fbank_deterministic():
    wav = np.sin(np.linspace(0, 440 * 2 * np.pi, SAMPLE_RATE)).astype(np.float32)
    np.testing.assert_array_equal(fbank(wav), fbank(wav))


# ---- clustering (_assign driven directly; no worker, no model) ----

def _bare_tracker():
    """A tracker with only the clustering state — the worker thread and model
    are irrelevant to _assign, so skip __init__ entirely."""
    t = object.__new__(SpeakerTracker)
    t._centroids = []
    t._current = None
    t._pending_new = []
    t._pending_switch = None
    return t


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_assign_first_embedding_is_speaker_one():
    t = _bare_tracker()
    assert t._assign(_unit([1, 0, 0])) == 1


def test_assign_same_voice_stays_silent():
    t = _bare_tracker()
    t._assign(_unit([1, 0, 0]))
    # Well above ATTACH_SIM vs the centroid: same speaker, no change event.
    assert t._assign(_unit([1, 0.1, 0])) is None
    assert t.n_speakers == 1


def test_assign_alien_voice_mints_after_two_windows():
    t = _bare_tracker()
    t._assign(_unit([1, 0, 0]))
    # Cosine 0 < NEW_NOW_SIM: clearly a new voice — but even that needs TWO
    # agreeing windows (a lone alien window is usually a music sting/noise).
    assert t._assign(_unit([0, 1, 0])) is None
    assert t._assign(_unit([0, 1, 0.05])) == 2
    assert t.n_speakers == 2


def test_assign_ambiguous_voice_needs_three_consistent_windows():
    t = _bare_tracker()
    t._assign(_unit([1, 0, 0]))
    # Cosine ~0.35: between NEW_NOW_SIM and ATTACH_SIM — ambiguous. Only
    # NEW_CONFIRM consecutive windows that agree with each other may mint.
    amb = _unit([0.35, float(np.sqrt(1 - 0.35 ** 2)), 0])
    assert t._assign(amb) is None
    assert t._assign(amb) is None
    assert t.n_speakers == 1
    assert t._assign(amb) == 2
    assert t.n_speakers == 2


def test_assign_scattered_windows_never_mint():
    """Sub-floor windows that DISAGREE with each other (music bleed, crosstalk)
    are noise, not a new voice — the phantom-speaker source in the field."""
    t = _bare_tracker()
    t._assign(_unit([1, 0, 0, 0]))
    assert t._assign(_unit([0, 1, 0, 0])) is None
    assert t._assign(_unit([0, 0, 1, 0])) is None   # disagrees with previous
    assert t._assign(_unit([0, 0, 0, 1])) is None   # still scattered
    assert t.n_speakers == 1                        # no phantom minted


def test_assign_returning_voice_reuses_label_after_confirm():
    t = _bare_tracker()
    t._assign(_unit([1, 0, 0]))                 # S1
    t._assign(_unit([0, 1, 0]))                 # alien window 1
    t._assign(_unit([0, 1, 0]))                 # -> mints S2
    back = _unit([1, 0.05, 0])
    assert t._assign(back) is None              # switch window 1: hold
    assert t._assign(back) == 1                 # confirmed -> back to S1
    assert t.n_speakers == 2


def test_assign_mixed_window_matching_both_stays_put():
    """A window that still matches the CURRENT speaker (≥ ATTACH_SIM) must
    never flip the label, even if another centroid scores higher — this was
    the S2↔S3 flicker on real content."""
    t = _bare_tracker()
    t._assign(_unit([1, 0, 0]))                 # S1
    t._assign(_unit([0, 1, 0]))
    t._assign(_unit([0, 1, 0]))                 # S2 (current)
    mixed = _unit([0.8, 0.7, 0])                # S1 wins, but S2 still matches
    assert t._assign(mixed) is None
    assert t._assign(mixed) is None             # stays S2 indefinitely
    assert t._current == 1


def test_assign_speaker_cap_stays_with_current():
    t = _bare_tracker()
    dims = np.eye(SpeakerTracker.MAX_SPEAKERS + 1, dtype=np.float32)
    t._assign(dims[0])
    for i in range(1, SpeakerTracker.MAX_SPEAKERS):
        t._assign(dims[i])
        t._assign(dims[i])                      # alien pair -> mint
    assert t.n_speakers == SpeakerTracker.MAX_SPEAKERS
    # One more orthogonal voice at capacity: no mint, no label jump — the
    # current label simply continues.
    cur = t._current
    assert t._assign(dims[SpeakerTracker.MAX_SPEAKERS]) is None
    assert t._assign(dims[SpeakerTracker.MAX_SPEAKERS]) is None
    assert t.n_speakers == SpeakerTracker.MAX_SPEAKERS
    assert t._current == cur


# ---- worker pipeline with an injected embedder (thread, no model) ----

class _FakeEmbedder:
    """Maps a constant-amplitude signal to a distinct unit embedding: the test
    feeds 'voices' as DC levels and the fake turns each level into its own
    orthogonal direction."""

    def embed(self, wav):
        level = int(round(float(np.mean(np.abs(wav))) * 10))
        e = np.zeros(8, dtype=np.float32)
        e[min(level, 7)] = 1.0
        return e


def test_tracker_worker_emits_changes_for_alternating_voices():
    events = []
    tr = SpeakerTracker(on_change=events.append, embedder_factory=_FakeEmbedder)
    frame = np.ones(512, dtype=np.float32)
    t0 = 100.0
    try:
        # 3 s of voice A (level 1), then 3 s of voice B (level 3), back-to-back.
        n_per_voice = int(3 * SAMPLE_RATE / 512)
        for i in range(n_per_voice):
            tr.feed([frame * 0.1], t=t0 + i * 512 / SAMPLE_RATE)
        for i in range(n_per_voice):
            tr.feed([frame * 0.3], t=t0 + (n_per_voice + i) * 512 / SAMPLE_RATE)
        deadline = time.monotonic() + 5.0
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        tr.stop()
    assert events[:2] == [1, 2]


def test_tracker_survives_broken_embedder_factory():
    """A missing/corrupt model must disable labels, never raise into the app."""
    def boom():
        raise RuntimeError("no model")
    tr = SpeakerTracker(on_change=lambda _: None, embedder_factory=boom)
    tr.feed([np.ones(512, dtype=np.float32)])
    tr.stop()  # worker already exited cleanly; stop() must not hang or raise


def test_tracker_stop_is_idempotent_and_bounded():
    tr = SpeakerTracker(on_change=lambda _: None, embedder_factory=_FakeEmbedder)
    t0 = time.monotonic()
    tr.stop()
    tr.stop()
    assert time.monotonic() - t0 < 5.0
    assert not tr._thread.is_alive()


def test_feed_after_stop_is_a_noop():
    tr = SpeakerTracker(on_change=lambda _: None, embedder_factory=_FakeEmbedder)
    tr.stop()
    tr.feed([np.ones(512, dtype=np.float32)])
    assert len(tr._queue) == 0
