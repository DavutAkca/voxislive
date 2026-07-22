"""Adaptive translated-speech playback pacing.

Translation providers can deliver a long spoken turn faster than realtime.  If
those samples are pushed straight into :class:`audio_io.Player`, the unplayed
tail grows while the source video continues, so spoken translation drifts
farther behind the already-visible captions.  This module bounds that playback
backlog and time-compresses speech with WSOLA, preserving pitch.

It deliberately sits *after* the provider: it cannot shorten the model's
first-audio delay, but it can drain audio that has already arrived without
changing provider prompts, endpointing, or generated text.
"""
from __future__ import annotations

import collections
import logging
import threading
import time

import numpy as np

_log = logging.getLogger("voxis")


_WIN_CACHE: dict[int, np.ndarray] = {}


def _get_hanning_window(frame: int) -> np.ndarray:
    w = _WIN_CACHE.get(frame)
    if w is None:
        w = np.hanning(frame).astype(np.float32)
        _WIN_CACHE[frame] = w
    return w


def time_compress_wsola(x: np.ndarray, speed: float, rate: int) -> np.ndarray:
    """Pitch-preserving WSOLA time compression for mono float32 speech.

    Thirty-millisecond frames, 50% output overlap, and an eight-millisecond
    similarity search keep the voice natural at the modest catch-up speeds used
    here (up to 1.25x).  This runs on the stager thread, never in PortAudio's
    realtime callback.
    """
    frame = int(rate * 0.030)
    hop_out = frame // 2
    hop_in = int(hop_out * speed)
    search = int(rate * 0.008)
    if speed <= 1.01 or len(x) < frame + hop_in + search + 1:
        return x
    win = _get_hanning_window(frame)
    n_frames = (len(x) - frame - search) // hop_in
    if n_frames < 2:
        return x
    # Frame 0 is already written below and the loop adds n_frames-1 more
    # frames. Allocating n_frames hops would leave one unwritten 15 ms zero tail
    # after every pacing block, producing a rhythmic gap in continuous speech.
    out = np.zeros((n_frames - 1) * hop_out + frame, dtype=np.float32)
    norm = np.zeros_like(out)
    selected = 0
    out[:frame] += x[:frame] * win
    norm[:frame] += win
    for k in range(1, n_frames):
        target = k * hop_in
        lo = max(0, target - search)
        hi = min(len(x) - frame, target + search)
        template = x[selected + hop_out:selected + hop_out + frame]
        if hi <= lo or len(template) < frame:
            selected = target
        else:
            corr = np.correlate(x[lo:hi + frame], template, mode="valid")
            selected = lo + int(np.argmax(corr))
        out_pos = k * hop_out
        out[out_pos:out_pos + frame] += x[selected:selected + frame] * win
        norm[out_pos:out_pos + frame] += win
    np.maximum(norm, 1e-6, out=norm)
    return out / norm


class AdaptivePlaybackStager:
    """Keep translated speech near the live captions without changing pitch.

    Audio is fed to the Player immediately, but only a short amount is staged in
    its ring.  Total pending + player backlog selects the catch-up rate:
    3 seconds -> 1.12x, 6 seconds -> 1.25x.  Once the backlog falls, playback
    automatically returns to 1x.  A backlog beyond 12 seconds is stale in a
    live conversation, so the oldest pending audio is trimmed as a last resort.
    """

    FEED_AHEAD_S = 3.0
    SPEED_STEPS = ((6.0, 1.25), (3.0, 1.12), (2.0, 1.06))
    PENDING_MAX_S = 12.0
    PENDING_KEEP_S = 4.0

    def __init__(self, player, on_status=None, input_rate: int = 24000):
        self._player = player
        self._on_status = on_status
        self.input_rate = int(input_rate)
        self._pending: collections.deque[bytes] = collections.deque()
        self._pending_bytes = 0
        self._lock = threading.Lock()
        self.speed = 1.0
        self.skipped_s = 0.0
        self.sped_s = 0.0
        self.feed_errors = 0
        self._feed_err_warned = False
        # Equal-power crossfade at block joins that follow a DISCONTINUITY — a
        # time-compressed block (WSOLA resets its overlap phase every block) or a
        # post-trim splice (stale audio dropped). Without it, sustained catch-up
        # compression clicks once per ~0.5 s output block and a trim clicks once,
        # which is the residual "pit pit" during backlog after the underrun fix.
        # Contiguous 1.0x joins are fed straight (no fade → no distortion). ~12 ms
        # is long enough to mask a phase step, short enough to be inaudible as a
        # blend. `_carry` holds the last _xfade samples of the previously emitted
        # audio so the join is a true overlap-add, not a retroactive edit.
        self._xfade = max(1, int(0.012 * self.input_rate))
        self._carry: np.ndarray | None = None      # float32 tail held back for the next join
        self._prev_compressed = False
        self._force_xfade = False                  # set by feed() when it trims stale audio
        self._run = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="translation-playback-stager")
        self._thread.start()

    @classmethod
    def speed_for_backlog(cls, backlog_s: float) -> float:
        for threshold, speed in cls.SPEED_STEPS:
            if backlog_s >= threshold:
                return speed
        return 1.0

    @property
    def backlog_s(self) -> float:
        player = self._player
        ring = player.tts.fill / player.rate if player is not None else 0.0
        with self._lock:
            pending = self._pending_bytes
        return pending / (self.input_rate * 2) + ring

    def feed(self, data: bytes) -> None:
        if not data:
            return
        trimmed = 0
        with self._lock:
            self._pending.append(data)
            self._pending_bytes += len(data)
            if self._pending_bytes > int(self.PENDING_MAX_S * self.input_rate * 2):
                keep = int(self.PENDING_KEEP_S * self.input_rate * 2)
                excess = self._pending_bytes - keep
                while self._pending and excess > 0:
                    chunk = self._pending.popleft()
                    if len(chunk) <= excess:
                        self._pending_bytes -= len(chunk)
                        trimmed += len(chunk)
                        excess -= len(chunk)
                    else:
                        # A provider may deliver many seconds in one callback.
                        # Keep the newest tail of that chunk instead of dropping
                        # the whole callback and producing avoidable silence.
                        self._pending.appendleft(chunk[excess:])
                        self._pending_bytes -= excess
                        trimmed += excess
                        excess = 0
        if trimmed:
            self.skipped_s += trimmed / (self.input_rate * 2)
            # The next emitted block is spliced onto a discontinuity — force a
            # crossfade even if it is played at 1.0x (contiguous fast path).
            self._force_xfade = True

    def clear(self) -> None:
        """Discard pending provider audio while keeping the worker reusable."""
        with self._lock:
            self._pending.clear()
            self._pending_bytes = 0
        self.speed = 1.0
        # A new stream starts after clear() (e.g. engine swap): drop the held
        # crossfade tail so the first block of the new stream is not blended
        # onto stale audio.
        self._carry = None
        self._prev_compressed = False
        self._force_xfade = False

    def _pop_block(self, block_bytes: int) -> bytes:
        buf = bytearray()
        with self._lock:
            while self._pending and len(buf) < block_bytes:
                need = block_bytes - len(buf)
                chunk = self._pending[0]
                if len(chunk) <= need:
                    self._pending.popleft()
                    self._pending_bytes -= len(chunk)
                    buf.extend(chunk)
                else:
                    # Provider callback sizes are not part of the contract.
                    # Gemini normally emits small deltas, but a reconnect or SDK
                    # update may hand us several seconds at once. Split that
                    # delta instead of defeating the 600 ms pacing block.
                    buf.extend(chunk[:need])
                    self._pending[0] = chunk[need:]
                    self._pending_bytes -= need
        return bytes(buf)

    def _emit(self, player, samples: np.ndarray, compressed: bool) -> None:
        """Push one block to the player, crossfading the join onto the previous
        block only when that join sits on a discontinuity.

        A discontinuity exists when this block or the previous one was
        time-compressed (WSOLA restarts its overlap phase per block) or when a
        stale-audio trim just spliced the stream (`_force_xfade`). In those
        cases the last `_xfade` samples of the previous block were held back in
        `_carry`; here they are equal-power-blended with this block's head, so
        the phase step is smeared over ~12 ms instead of clicking. A purely
        contiguous 1.0x run feeds the held tail straight through — no blend, so
        no comb/echo artifact on audio that never had a discontinuity."""
        xf = self._xfade
        need_xf = compressed or self._prev_compressed or self._force_xfade
        self._force_xfade = False
        self._prev_compressed = compressed
        # Too short to hold a tail: flush any carry, feed straight, reset.
        if samples.shape[0] <= 2 * xf:
            if self._carry is not None:
                samples = np.concatenate([self._carry, samples])
            self._carry = None
            self._push(player, samples)
            return
        head, body, tail = samples[:xf], samples[xf:-xf], samples[-xf:]
        if self._carry is None:
            out = np.concatenate([head, body])            # first block of a stream
        elif need_xf:
            n = min(xf, self._carry.shape[0])
            fade_in = np.linspace(0.0, 1.0, n, dtype=np.float32)
            joined = head[:n] * fade_in + self._carry[-n:] * (1.0 - fade_in)
            out = np.concatenate([joined, head[n:], body])
        else:
            out = np.concatenate([self._carry, head, body])  # contiguous, no blend
        self._carry = tail
        self._push(player, out)

    def _push(self, player, samples: np.ndarray) -> None:
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        player.feed_tts_pcm16(pcm)

    def _loop(self) -> None:
        block_bytes = int(0.6 * self.input_rate) * 2
        while self._run:
            player = self._player
            if player is not None:
                try:
                    while (self._run and self.backlog_s > 0
                           and player.tts.fill / player.rate < self.FEED_AHEAD_S):
                        # Measure before removing the next block: that block is
                        # still unplayed backlog and must count at the threshold.
                        backlog = self.backlog_s
                        data = self._pop_block(block_bytes)
                        if not data:
                            break
                        speed = self.speed_for_backlog(backlog)
                        self.speed = speed
                        samples = (np.frombuffer(data, dtype=np.int16)
                                   .astype(np.float32) / 32768.0)
                        compressed = speed > 1.0
                        if compressed:
                            paced = time_compress_wsola(
                                samples, speed, self.input_rate)
                            self.sped_s += max(
                                0.0, (len(samples) - len(paced)) / self.input_rate)
                            samples = paced
                        self._emit(player, samples, compressed)
                except Exception:
                    self.feed_errors += 1
                    if not self._feed_err_warned:
                        self._feed_err_warned = True
                        _log.exception(
                            "translation playback stager feed failed (#%d)",
                            self.feed_errors)
                        if self._on_status is not None:
                            try:
                                self._on_status(
                                    "translator: audio playback fault — translated "
                                    "voice may be silent")
                            except Exception:
                                pass
            time.sleep(0.012)

    def stop(self) -> None:
        self._run = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self.clear()
        self._player = None
