"""Give the volume keys back to the user in vbcable mode.

The bug this fixes: in vbcable mode Voxis makes CABLE Input the Windows default
render endpoint (pipeline._switch_defaults) so other apps play into the cable.
From that moment the volume keys, the OSD slider and the mute key all act on the
CABLE — not on the headphones. Meanwhile Voxis plays the mix (translation +
ambient) straight to the headphones through its own stream, and WASAPI loopback
captures the cable's mix BEFORE the endpoint volume is applied. Net effect: the
user turns the volume down and *nothing changes* — they have lost control of
their own audio. (Driverless mode is unaffected: the default endpoint IS the
device we play to, so Windows already attenuates us.)

The fix is to mirror rather than fight: read the default endpoint's master level
(the very thing the user is moving) and apply it to our output gain. The keys
then "just work", and mute means mute.

Level is read in dB, not as the 0..1 slider position, because that is what
Windows actually applies — half-way on the slider is roughly -16 dB, not -6 —
so mirroring the scalar would feel wrong at every position but the ends.
"""
from __future__ import annotations

import logging
import math
import threading

log = logging.getLogger("voxis")

POLL_SECONDS = 0.15   # ~7 Hz: instant to a human, invisible to the CPU
# The endpoint the keys control is whichever is DEFAULT *right now*, and that
# changes under us: the session flips the default to the cable, and the user can
# switch device mid-session. Caching the endpoint once was the original bug —
# the mirror latched onto the headphones and then watched the wrong device
# forever. Re-resolving is a COM enumeration, so do it on a slow beat, not every
# poll.
RERESOLVE_SECONDS = 1.0


def _default_endpoint():
    """(name, IAudioEndpointVolume) for the CURRENT default render endpoint — the
    one the user's volume keys are moving, which in vbcable mode is the cable.

    pycaw's AudioDevice already hands back an activated IAudioEndpointVolume; the
    hand-rolled Activate() call the internet is full of only works on older pycaw
    (GetSpeakers used to return a raw IMMDevice)."""
    from pycaw.pycaw import AudioUtilities  # noqa: PLC0415

    dev = AudioUtilities.GetSpeakers()
    return (getattr(dev, "FriendlyName", "?") or "?"), dev.EndpointVolume


def _endpoint_volume():
    return _default_endpoint()[1]


def _gain_from(vol) -> float:
    if vol.GetMute():
        return 0.0
    db = float(vol.GetMasterVolumeLevel())      # dB, e.g. -16.0
    return max(0.0, min(1.0, math.pow(10.0, db / 20.0)))


class EndpointVolumeMirror:
    """Follows the default endpoint's level onto `player.master_gain` until
    stopped. Best-effort by contract: if COM or pycaw fails we leave the gain at
    1.0 — losing the mirror is a nuisance, muting the session would be a fault."""

    def __init__(self, player):
        self._player = player
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="endpoint-volume")

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        # The gain lives on the player; hand it back at full scale so a later
        # session (or the free-voice preview's own Player) never starts muted.
        try:
            self._player.master_gain = 1.0
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        import time  # noqa: PLC0415

        import comtypes  # noqa: PLC0415

        try:
            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        vol, resolved_at, watching = None, 0.0, None
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if vol is None or (now - resolved_at) >= RERESOLVE_SECONDS:
                    try:
                        name, vol = _default_endpoint()
                        resolved_at = now
                        if name != watching:
                            # Names the device the keys actually control. If this
                            # ever says the headphones during a vbcable session,
                            # the mirror started before the default was flipped.
                            log.info("volume mirror: following '%s'", name)
                            watching = name
                    except Exception as exc:  # noqa: BLE001
                        if vol is None:
                            log.info("endpoint volume mirror unavailable (%s) — "
                                     "volume keys will not reach the translation", exc)
                            return
                try:
                    self._player.master_gain = _gain_from(vol)
                except Exception:  # noqa: BLE001 - a transient COM fault must not
                    vol = None     # kill the mirror; re-resolve and carry on
                self._stop.wait(POLL_SECONDS)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
