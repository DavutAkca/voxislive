"""The volume keys must reach what the user hears.

In vbcable mode Windows' default endpoint is the cable, so the keys stop
controlling the headphones Voxis plays to. The mirror copies that endpoint's
level onto our output. Two things must hold: the mapping matches what Windows
actually applies (dB, not slider position), and a failure can never leave the
session silent.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import endpoint_volume as ev  # noqa: E402


class _Vol:
    def __init__(self, db=0.0, mute=False):
        self._db, self._mute = db, mute

    def GetMute(self):          # noqa: N802 - COM interface name
        return self._mute

    def GetMasterVolumeLevel(self):  # noqa: N802
        return self._db


def test_full_scale_is_unity():
    assert ev._gain_from(_Vol(db=0.0)) == pytest.approx(1.0)


def test_mute_means_silence():
    assert ev._gain_from(_Vol(db=-6.0, mute=True)) == 0.0


def test_level_follows_decibels_not_the_slider():
    # Windows' slider at ~34% sits near -16 dB → ~0.16 amplitude. Mirroring the
    # slider position (0.34) would be more than twice as loud as Windows itself.
    assert ev._gain_from(_Vol(db=-16.0)) == pytest.approx(0.158, abs=0.005)
    assert ev._gain_from(_Vol(db=-6.02)) == pytest.approx(0.5, abs=0.01)


def test_gain_is_clamped():
    assert ev._gain_from(_Vol(db=+12.0)) == 1.0     # never amplify past unity
    assert ev._gain_from(_Vol(db=-96.0)) >= 0.0


def test_stop_hands_the_gain_back():
    # A mirror that stopped (or crashed) must not leave the next session — or the
    # free-voice preview's own Player — stuck at a stale, quiet gain.
    player = types.SimpleNamespace(master_gain=0.1)
    m = ev.EndpointVolumeMirror(player)
    m.stop()
    assert player.master_gain == 1.0
