"""pywebview bridge between the web UI and the Python audio engine.

JavaScript invokes Bridge methods through `window.pywebview.api`; the UI polls
`poll()` to drain status, translation and telemetry events — every 70 ms while
a session is live (the caption is the fastest user-visible signal, so the poll
cadence is part of the latency budget), relaxing to 250 ms when idle.
"""
import logging
import os
import queue
import threading
import time

import webview

from . import APP_VERSION, i18n
from .audio_io import detect_virtual_cable, find_device, list_device_names
from .config import (
    ENGINE_CASCADE,
    GEMINI_VOICES,
    IS_OFFICIAL_RELEASE,
    QUALITY_PRESETS,
    apply_profile,
    save_config,
)
from .i18n import t
from .pipeline import ModeController
from .paths import icon_path, legacy_transcripts_dir, transcripts_dir, user_path, web_dir
from . import store_review, transcript_store

WEB_DIR = web_dir()
OBS_FILE = user_path("obs_subtitle.txt")
# Allowed transcript file extensions for the open/reveal bridge (path-traversal +
# type guard). JSON is the canonical record; the rest are on-demand exports.
_TRANSCRIPT_EXTS = (".json", ".txt", ".srt", ".vtt")

# Target languages offered in the picker — the full documented set for the
# gemini-3.5-live-translate-preview model (ai.google.dev live-translate table),
# passed verbatim as translation_config.target_language_code. Popular first, then
# alphabetical by English name; endonym labels live in LANG_NAMES (web/index.html).
LANGS = [
    "tr", "en", "es", "fr", "de", "it", "pt", "pt-BR", "pt-PT", "ru", "ar",
    "zh-Hans", "ja", "ko", "hi", "id", "vi", "th", "pl", "uk", "af", "ak",
    "sq", "am", "hy", "az", "eu", "be", "bn", "bg", "my", "ca", "zh-Hant",
    "hr", "cs", "da", "nl", "et", "fil", "fi", "gl", "ka", "el", "gu",
    "ha", "he", "hu", "is", "jv", "kn", "kk", "km", "rw", "lo", "lv",
    "lt", "mk", "ms", "ml", "mr", "mn", "ne", "nb", "fa", "pa", "ro",
    "sr", "sd", "si", "sk", "sl", "su", "sw", "sv", "ta", "te", "ur",
    "uz", "zu",
]
LINE_GAP = 2.5
# When a speaker change has been detected, the translated stream is split at
# the next micro-pause this long — far below LINE_GAP, so back-to-back
# speakers still land in separate, separately-labeled turns.
SPK_GAP = 0.7
FADE_MS = 6.0
# Prefetched session-key freshness window (seconds). Short on purpose so a
# stale entry falls back to the normal synchronous fetch. Only RAW keys are
# ever cached — a single-use ephemeral token (2-min new-session window,
# shorter than this TTL) would fail the first connect terminally, so
# _prefetch_session_key skips caching those.
KEY_PREFETCH_TTL = 240.0
# Overlay/OBS subtitle width cap on a word boundary so a runaway turn never
# produces an unbounded single line in the overlay window or the OBS file.
SUBTITLE_MAX = 260
# Watchdog: abandon a hotkey capture that receives no keypress within this many
# seconds so the blocking read can never hang the bridge thread.
HOTKEY_CAPTURE_TIMEOUT = 8.0

_log = logging.getLogger("voxis.webui")

# Locale-independent default-device sentinel. set_device treats an empty string
# as "system default"; the UI renders t('default_mic') but always round-trips
# this sentinel so device matching never depends on the active UI language.
DEFAULT_DEVICE = ""


def _cap_subtitle(text: str, limit: int = SUBTITLE_MAX) -> str:
    """Trim a caption to the most recent `limit` chars on a word boundary so the
    leading partial word is dropped rather than shown mid-token."""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    sp = cut.find(" ")
    return cut[sp + 1:] if 0 <= sp < 40 else cut


def _autofill_meeting_devices(cfg):
    """Auto-select an installed virtual cable for the two-way meeting path so the
    user never has to hand-edit config.json. Only fills a field that is unset or
    no longer resolves to a present device — a deliberate, valid choice is kept.

    `system_capture` (the vbcable-backend incoming capture device, pipeline.py
    `_acquire_capture`) is the same physical endpoint as `meeting_virtual_mic`
    (both are the cable's recording/output side) and must be kept in sync with
    it — a user whose only virtual cable is VoiceMeeter (not VB-CABLE) would
    otherwise have `meeting_virtual_mic` autofilled correctly while
    `system_capture` stays on the hardcoded VB-CABLE-only default, crashing
    both video mode (feedback guard) and meeting mode (device not found)."""
    devs = cfg.setdefault("devices", {})

    def resolves(name, kind):
        if not name:
            return False
        try:
            find_device(name, kind)
            return True
        except Exception:
            return False

    play_ok = resolves(devs.get("meeting_mic_playback", ""), "output")
    rec_ok = resolves(devs.get("meeting_virtual_mic", ""), "input")
    capture_ok = resolves(devs.get("system_capture", ""), "input")
    if play_ok and rec_ok and capture_ok:
        return
    found = detect_virtual_cable()
    if not found:
        return
    play, rec = found
    if not play_ok:
        devs["meeting_mic_playback"] = play
    if not rec_ok:
        devs["meeting_virtual_mic"] = rec
    if not capture_ok:
        devs["system_capture"] = rec
    save_config(cfg)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        # Set on successful auth; used as the index into the BYOK store.
        self._user_id: str | None = None
        # Last license/quota snapshot from verify; the paid-tier badge gate reads it.
        self._last_quota: dict | None = None
        # One-shot guard for the app_launched funnel milestone (check_auth may run
        # several times per process; the event should fire once).
        self._launch_reported: bool = False
        # One-shot guard for the anonymous app_opened milestone. Distinct from
        # app_launched: this fires on the FIRST check_auth regardless of login,
        # so the funnel can see opens that never reach authentication.
        self._opened_reported: bool = False
        # The free-voice preview loads a voice (and may download one) off the UI
        # thread; one at a time, so a double click can't race two downloads.
        self._preview_lock = threading.Lock()
        self._preview_busy: bool = False
        # The last line Voxis SPOKE, kept apart from self._lines because stop()
        # clears those once the transcript is saved — and the A/B card is offered
        # precisely AFTER stop, when the user is finally looking at the window.
        # Without this the demo has nothing to replay at the one moment it runs.
        self._last_line: str = ""
        # Resolve "" (= never explicitly chosen) to the Windows display
        # language, and write the resolution back so get_init/JS and the
        # Settings dropdown all see one concrete locale. Not persisted here:
        # until the user picks a language, each launch follows the OS.
        cfg["ui_language"] = i18n.resolve_language(cfg.get("ui_language", ""))
        i18n.set_language(cfg["ui_language"])

        # Bounded like every other queue in the engine: if the webview stops
        # polling (hung/backgrounded window) events must not accumulate without
        # limit. _put_event drops the OLDEST on overflow — the UI shows a live
        # stream, so the freshest events always win.
        self._events: queue.Queue = queue.Queue(maxsize=400)
        # Serialize config writes: pywebview runs each JS api call on its own
        # thread, so a slider drag can overlap several _save_cfg calls. Without
        # this they raced on the on-disk file (see save_config) and one would
        # spuriously fail with a "cannot write to disk" error.
        self._save_lock = threading.Lock()
        self.controller = ModeController(
            cfg, None, self._on_text, self._on_status,
            on_usage_reported=self._on_usage_reported,
            on_quota_exceeded=self._on_quota_exceeded,
            on_session_failed=self._on_session_failed,
            on_speaker=self._on_speaker,
        )

        self._lines, self._cur_line = [], ""
        self._last_t = 0.0
        # Source-transcription stream, paired to translation turns best-effort.
        # Source (input-transcription) pairing state. _src_buf accumulates the
        # in-flight source utterance's deltas; a completed utterance (segmented by
        # a >LINE_GAP pause in the source stream, as Gemini produces) is queued in
        # _src_done until the translation turn it produced finalizes and pops it.
        # A FIFO — not a single slot — so two source utterances completing before
        # one translation turn finalizes cannot overwrite each other (the old
        # two-slot _last_src/_cur_src scheme dropped the first, and its per-turn
        # _cur_src clear wiped source belonging to a later turn, blanking the JSON
        # `src` field localization/dubbing relies on — Ivo, 1.0.27). Qwen streams
        # source continuously with no pause, so nothing ever queues and each turn
        # simply consumes _src_buf — no repeated leading segment.
        self._src_buf = ""
        self._src_done: list[tuple[int | None, str]] = []  # (speaker, text)
        self._last_src_t = 0.0
        # Speaker labeling (local tracker, incoming direction). _cur_spk is the
        # label the tracker believes is talking NOW; _src_spk is the label the
        # in-flight source buffer STARTED under (the buffer finalizes on the
        # next arrival, by which time _cur_spk may already be the next voice).
        # Labels are anonymous session-scoped ints rendered as "S1"/"S2" —
        # deliberately language-neutral, like professional subtitle tags, so
        # exports read the same regardless of UI language.
        self._cur_spk: int | None = None
        self._src_spk: int | None = None
        self._spk_seen: set[int] = set()
        self._pending_spk_break = False
        self._session_file = None
        # Path of the transcript auto-saved by the most recent stop(). Unlike
        # _session_file it SURVIVES the stop-time buffer clear, so a user who
        # presses Stop and then clicks "Save transcript" gets the already-saved
        # file back instead of a confusing "nothing to save".
        self._last_saved_file = None
        # Structured, timestamped turn log for JSON persistence + caption export.
        # Each entry: {"t": offset_s, "dir": "out", "src": str, "text": str}.
        # Parallel to self._lines (kept for the plain-text path); reset per session.
        self._turns = []
        self._session_start = 0.0
        self._turn_start = 0.0
        # Per-session output folder, decided at start so the transcript JSON, its
        # caption exports, and the optional dual-track WAVs all land together in
        # one self-contained directory (Ivo, 1.0.28). _session_dirname is the bare
        # folder name (voxis_<stamp>); _session_dir is its full path under the
        # active transcripts dir. Both cleared on stop.
        self._session_dir = None
        self._session_dirname = None
        self._overlay_win = None
        self._overlay_text = ""
        self._overlay_until = 0.0
        self._maximized = False
        self._minimized = False
        # Latest non-maximized window geometry, persisted to cfg["window"] on close
        # and restored at next launch.
        self._win_geom = {}
        self._badge = (t("badge_idle"), "#8593a6", "")
        # Assigned in run() once the main window exists; referenced by
        # win_* controls before then, so default to None.
        self._main_window = None
        # Serializes the session lifecycle: start/stop/_maybe_restart all run on
        # background threads, so without this a rapid start→stop or a flurry of
        # set_cfg restarts could spawn racing _start threads against one
        # controller. _lifecycle holds the lock for the duration of one
        # transition; _restart_token debounces set_cfg-driven restarts.
        self._lifecycle = threading.Lock()
        # Serializes the shared transcript/overlay/OBS buffers that the audio
        # receiver thread(s) mutate via _on_text. Meeting mode runs two such
        # threads, so the read-modify-write must not interleave. RLock because
        # _on_text re-enters through _obs_write on the same thread.
        self._text_lock = threading.RLock()
        self._restart_token = 0
        self._last_obs_write = None
        self._hotkey_cancel = False
        # Rolling tail of recent status lines (raw, incl. the "capture: backend=..."
        # diagnostic) + the last coarse error code — assembled into a problem
        # report's diagnostics. Bounded so it can never grow unbounded.
        self._status_log: list[str] = []
        self._last_error_code = ""
        # Per-session failure flag. _last_error_code is sticky across sessions (it
        # rides into problem reports); the rating prompt needs to know whether
        # THIS session failed, so it gets its own flag, reset on every start.
        self._session_error = False
        # Prefetched /auth/session-key result per incoming target (official
        # build): warmed in the background at login / target change / session
        # stop so pressing Start skips the issuance round-trip (~200-400 ms).
        # Single-use + short TTL — on any miss the start does its normal
        # synchronous fetch, so failures cost nothing.
        self._key_cache: dict[str, tuple] = {}
        self._key_cache_lock = threading.Lock()
        # Bumped whenever a cached grant becomes invalid (the quota ran out). A
        # prefetch that was ALREADY IN FLIGHT when that happened carries a grant
        # issued under the old quota — clearing the dict cannot stop it, because it
        # lands afterwards and writes itself back in. It then hands a spent free
        # account a VOICED (paid) engine on the next Start, which we pay for and
        # the user gets billed past 100%. The epoch is the fix: a prefetch may only
        # publish if the world has not moved under it.
        self._key_epoch = 0
        # Idle-only sound-check probe ("do I hear this device?"): a loopback
        # capture whose only output is a peak level for the UI meter. Never runs
        # alongside a session (see soundcheck_start / _start).
        self._sc = None
        self._sc_level = 0.0
        self._sc_timer = None
        # One-time move of transcripts from the old (virtualized) AppData location
        # to the user-facing default. Best-effort; never blocks startup.
        self._migrate_transcripts()

    # ---------- callbacks from audio threads ----------
    def _put_event(self, ev):
        """Enqueue a UI event, dropping the oldest on overflow (never blocks —
        callers are audio/heartbeat threads that must not stall)."""
        try:
            self._events.put_nowait(ev)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(ev)
            except queue.Full:
                pass

    def _on_text(self, direction, text):
        # Meeting mode runs two translator receiver threads into this one method;
        # serialize so their read-modify-write on the shared transcript / overlay /
        # OBS state cannot interleave (which previously mispaired source vs
        # translation in exports and let both threads truncate the OBS file).
        with self._text_lock:
            self._on_text_locked(direction, text)

    def _on_speaker(self, label: int):
        """Speaker-change event from the local tracker (its worker thread).

        Splits the in-flight source utterance at the change so its words stay
        with the voice that (mostly — detection lags the true boundary by a
        couple of seconds) said them, tags subsequent source with the new
        label, and arms a soft break on the translated stream so back-to-back
        speakers stop merging into one caption line (see _on_text_locked)."""
        with self._text_lock:
            if label == self._cur_spk:
                return
            prev = self._cur_spk
            self._cur_spk = label
            self._spk_seen.add(label)
            if prev is None:
                return  # first assignment — nothing to split yet
            buf = self._src_buf.strip()
            if buf:
                self._src_done.append((self._src_spk, buf))
                self._src_buf = ""
            self._pending_spk_break = True

    def _peek_spk(self) -> int | None:
        """Best-effort label for the translation line streaming NOW: the oldest
        unpaired source utterance's speaker (FIFO pairing), else the in-flight
        buffer's. Only meaningful once ≥2 speakers were seen. Caller holds
        _text_lock."""
        if len(self._spk_seen) < 2:
            return None
        if self._src_done:
            return self._src_done[0][0]
        return self._src_spk if self._src_buf else self._cur_spk

    def _on_text_locked(self, direction, text):
        now = time.time()
        if direction == "in":
            # Input transcription (what the speaker said). Accumulate per utterance
            # so a completed source can be paired with the translation turn it
            # produced. No UI event here — the source caption is attached when the
            # matching translation turn finalizes.
            if self._src_buf and (now - self._last_src_t) > LINE_GAP:
                # A speech pause completed this source utterance — queue it for the
                # translation turn it produced. Source leads the translation by the
                # model's ear-voice lag, so it is queued before that turn finalizes.
                # Tagged with the label the buffer STARTED under: this finalize
                # runs on the NEXT utterance's first token, by which time
                # _cur_spk may already be the next voice.
                self._src_done.append((self._src_spk, self._src_buf.strip()))
                self._src_buf = ""
            if not self._src_buf:
                self._src_spk = self._cur_spk
            self._src_buf += text
            self._last_src_t = now
            # Live "heard now" feed: the accumulating source utterance streams to
            # the UI's ghost line as it is spoken (the definitive, paired source
            # still lands with the 'src' event when its translation finalizes —
            # source LEADS translation by the ear-voice lag, so the live text
            # must not be attached to the currently rendering turn).
            self._put_event(("hear_live", self._src_buf.strip()))
            return
        # direction == "out": the translated text stream.
        if not self._session_start:
            # First translated token of the session anchors the timeline; turn
            # offsets are measured from here (approximate caption sync).
            self._session_start = now
        gap = now - self._last_t
        # An armed speaker break fires at the next micro-pause: the model gives
        # no word timestamps, so the change cannot split the stream exactly —
        # the short output pause between the two voices' translations is the
        # best available seam.
        newline = bool(self._cur_line) and (
            gap > LINE_GAP or (self._pending_spk_break and gap > SPK_GAP))
        if newline:
            finished = self._cur_line.strip()
            self._cur_line = ""
            # The turn that just ended pairs with — and consumes — its source
            # (correct by ordering despite the few-second lag).
            spk, src = self._pop_source()
            if src:
                # The label rides along only in a genuinely multi-speaker
                # session (same ≥2 gate as _peek_spk), so a lone speaker is
                # never tagged "S1" on screen. The JSON turn keeps the raw
                # label either way — export renderers apply the same gate.
                self._put_event(("src", src,
                                 spk if len(self._spk_seen) >= 2 else None))
            # Engine re-speak guard: after an internal reconnect Gemini can
            # re-emit the tail utterance, producing two identical consecutive
            # turns (field transcript 2026-07-10, t=39s). A long turn that
            # exactly repeats the previous one is that artifact, not real
            # speech — keep the first, drop the echo. Short exact repeats
            # ("Evet." twice) are plausible dialogue and stay.
            dup = (len(finished) >= 20 and self._turns
                   and self._turns[-1].get("text") == finished)
            # Record the finalized turn with its start offset and paired source.
            if finished and not dup:
                self._lines.append(finished)
                self._last_line = finished   # survives stop(); see Bridge.__init__
                turn = {
                    "t": max(0.0, self._turn_start - self._session_start),
                    "dir": "out",
                    "src": src,
                    "text": finished,
                }
                if spk is not None:
                    turn["spk"] = spk
                self._turns.append(turn)
        if not self._cur_line:
            # Mark when this (new) turn began so its cue start is the speech
            # onset, not the moment it finalized one LINE_GAP later. A fresh
            # turn boundary also satisfies any armed speaker break.
            self._turn_start = now
            self._pending_spk_break = False
        self._cur_line += text
        self._last_t = now
        line = self._cur_line.strip()
        hint = self._peek_spk()
        if hint is not None:
            line = f"S{hint}: {line}"
        self._overlay_text = line
        self._overlay_until = now + FADE_MS
        self._obs_write(line)
        self._put_event(("trans", text, newline, hint))

    def _pop_source(self) -> tuple[int | None, str]:
        """(speaker, text) for the translation turn that just finalized: the
        oldest completed source utterance if one is queued (Gemini's paused
        stream), else whatever is still accumulating (Qwen's gapless stream) —
        consumed so the next turn cannot re-emit it. Caller holds _text_lock."""
        while self._src_done:
            spk, src = self._src_done.pop(0)
            src = src.strip()
            if src:
                return spk, src
        src = self._src_buf.strip()
        self._src_buf = ""
        return (self._src_spk if src else self._cur_spk), src

    def _pending_source(self) -> str:
        """All source not yet paired to a turn (queue + in-flight buffer), for the
        stop-time flush of a source-only session. Caller holds _text_lock."""
        parts = [s for _, s in self._src_done if s.strip()]
        if self._src_buf.strip():
            parts.append(self._src_buf.strip())
        return " ".join(parts).strip()

    def _emit_status(self, msg, level="info"):
        """Push a status line to the UI.

        Carries an explicit level so the front end (and the error badge) never
        has to infer severity by sniffing a localized 'HATA:'/'ERROR' prefix.
        The legacy positional payload (the message string) is preserved so the
        existing JS poll handler keeps working; the structured fields ride
        alongside for callers that read them."""
        # Raw engineering diagnostics (the "capture: backend=..." line, the
        # translator stall/clone notices) go to voxis.log + the problem-report
        # tail below — never to the user-facing transcript.
        diagnostic = isinstance(msg, str) and msg.startswith(("capture: ", "translator: "))
        if diagnostic:
            logging.getLogger("voxis").info(msg)
        else:
            self._put_event(("status", msg, {"level": level, "msg": msg}))
        # Keep a bounded tail for the problem-report diagnostics (raw text; it is
        # scrubbed again at report-assembly time).
        if isinstance(msg, str) and msg:
            self._status_log.append(msg)
            if len(self._status_log) > 40:
                del self._status_log[:-40]
        if level == "error":
            self._badge = (t("badge_error"), "#fb7185", "err")

    def _on_status(self, msg):
        # ModeController only forwards a localized string. Treat its events as
        # informational; error-badge state is set explicitly by the paths that
        # actually fail (e.g. _start), not by parsing a translated prefix.
        self._emit_status(msg, "info")

    def _on_usage_reported(self):
        self._put_event(("quota_refresh", None))

    def _on_quota_exceeded(self):
        """Server reported the license is exhausted (402 on /usage/report). The
        server isn't in the audio path, so the cutoff is enforced here.

        For a free tier whose taste just ran out the cutoff is a HANDOVER, not a
        wall — but an explicit one. The first design swapped engines under the
        live session, and in the field the owner heard the chip say "Pro voice"
        while Piper was speaking: with two engines inside one session, the UI and
        the audio can disagree. This design makes that state impossible: one
        session, one engine. The Pro voice finishes its sentence, the session
        stops, and a card asks whether to continue on the free voice — with the
        same last sentence replayable in BOTH voices, so the user hears exactly
        what they are choosing between (owner's design, 2026-07-13). "Continue"
        starts a NEW session, which the server routes to the cascade from the
        first frame — a path that runs in production already.

        The hard stop remains for everyone else: paid accounts out of minutes,
        Meeting mode, a disabled cascade, a spent daily allowance.

        Three walls, not two. A 402 arriving while the CASCADE is the live engine
        can only be the daily allowance: the server's cascade heartbeat path never
        compares against the license quota at all (handlers/usage.go), it only
        books against the 10-min/day counter. Reading the engine — rather than the
        quota flags — is what tells the two apart, so someone already on the free
        voice is told "today's free minutes are up, they come back tomorrow"
        instead of being offered the free voice they are currently listening to
        (owner report, 2026-07-13).

        Runs on a usage-report worker thread; self.stop() dispatches teardown to
        its own thread under the session lock, so calling it here is safe."""
        # A key prefetched before the quota ran out must not let the next Start
        # sail past the paywall — drop it so the start re-asks the server. Bumping
        # the epoch under the same lock also disowns any prefetch still IN FLIGHT:
        # clearing alone left a race where that fetch (carrying a voiced grant from
        # when the user still had minutes) landed afterwards and put the PAID engine
        # back in the cache, which the next Start then spent. That is how a free
        # account kept running the paid engine past its taste — billed to the user
        # (>100% quota) and paid for by us (field data, 2026-07-13).
        with self._key_cache_lock:
            self._key_cache.clear()
            self._key_epoch += 1
        self._put_event(("quota_refresh", None))

        q = self._last_quota
        try:
            on_cascade = self.controller.current_engine() == "cascade"
        except Exception:  # noqa: BLE001
            # Never let an engine read cost us the quota snapshot: without it the
            # free tier would fall through to the PAID paywall.
            on_cascade = False

        if on_cascade:
            # The daily wall. Not an error, and not the taste wall: the free voice
            # is not taken away, it just comes back tomorrow.
            self._last_error_code = None
            self._emit_status(t("st_daily_free_done"), "warn")
            self._drain_tts()
            self.stop()
            self._put_event(("daily_wall", None))
            return

        try:
            free_open = q.get("cascade_available") if isinstance(q, dict) else None
            if free_open is None and isinstance(q, dict):
                # Server predates cascade_available (deploy order: server first,
                # but never assume it). Fall back to the old flag.
                free_open = q.get("cascade_ready") is True
            wall_free = (free_open is True and self.controller.mode != "meeting")
        except Exception:  # noqa: BLE001
            wall_free = False
        if wall_free:
            mode = self.controller.mode
            # Not an error: the taste simply ended. Leaving _session_error unset
            # also keeps the rating prompt honest about clean sessions.
            self._last_error_code = None
            self._emit_status(t("st_taste_wall"), "warn")
            self._drain_tts()          # let the Pro voice finish its sentence
            self.stop()
            self._put_event(("taste_wall", {"mode": mode}))
            return

        self._last_error_code = "st_quota_exceeded"
        self._session_error = True
        self._emit_status(t("st_quota_exceeded"), "warn")
        # Raise the in-app paywall card at the mid-session cutoff (the highest-
        # intent moment) instead of a silent stop. JS reads the live QUOTA global
        # for the number; this only fires once per session (guarded upstream).
        self._put_event(("quota_wall", None))
        self.stop()

    def _drain_tts(self, timeout: float = 8.0):
        """Let the paid voice finish what it has already produced before the
        session closes. Stopping the translator first means no NEW audio arrives;
        the player's ring then plays out its last sentence and goes quiet. Cutting
        instead of draining would clip the goodbye mid-word — and that clipped
        word is the last impression of the paid tier the user gets."""
        inc = self.controller.incoming()
        if inc is None:
            return
        try:
            inc.translator.stop()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and inc.player.tts_active:
                time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass

    def _on_session_failed(self):
        """A translator thread died mid-session (terminal error / retries
        exhausted). Billing already stopped via _is_session_live; tear the session
        down so capture, ducking and the endpoint redirection are released and the
        badge isn't a false green. Mirrors _on_quota_exceeded; stop() is
        self-dispatching so calling it from the heartbeat thread is safe.

        Uses its own st_session_failed string (NOT st_capture_lost): a dead
        translator is a connection failure, and mislabeling it as a capture
        fault poisons field diagnosis (error_reason rides into problem reports)."""
        self._last_error_code = "st_session_failed"
        self._session_error = True
        self._emit_status(t("st_session_failed"), "error")
        self.stop()

    def _obs_write(self, text):
        if not self.cfg.get("obs_subtitle_enabled"):
            return
        # Cap the CAPTION first so SUBTITLE_MAX still bounds the spoken line; the
        # badge is appended afterward as its own row and is exempt from the cap.
        text = _cap_subtitle(text)
        out = text
        if self._show_badge():
            out = f"{text}\n{t('powered_by')}"
        # Only rewrite when the content actually changed — the translation stream
        # repaints the same line on every token, and an OBS text source re-reads
        # on file mtime, so skipping no-op writes avoids needless flicker/IO. The
        # dedupe key is the full payload (caption + badge) so toggling the badge
        # or switching UI language forces one repaint.
        if out == self._last_obs_write:
            return
        try:
            with open(OBS_FILE, "w", encoding="utf-8") as f:
                f.write(out)
            self._last_obs_write = out
        except OSError:
            pass

    # ---------- JS-facing API ----------
    def get_init(self):
        outs = list_device_names("output") or ["—"]
        mics = list_device_names("input")
        from . import byok_store
        from .config import resolve_engine
        uid = self._ensure_user_id()
        engines = self._engine_options()  # gemini-only on OSS; both on official
        byok_status = {e: (byok_store.has_byok(uid, e) if uid else False) for e in engines}
        byok_set = byok_status.get("gemini", False)  # back-compat: single bool
        from .paths import client_channel
        return {
            "version": APP_VERSION,
            "channel": client_channel(),
            "outputs": outs,
            "mics": [t("default_mic")] + mics,
            "langs": LANGS,
            "profiles": [[k, t(f"profile_{k}")] for k in ("custom", "meeting", "film", "conference")],
            "qualities": self._quality_options(),
            "gemini_voices": GEMINI_VOICES,
            "byok_set": byok_set,
            "byok_status": byok_status,
            "engines": engines,
            "engine": resolve_engine(self.cfg),
            "official_release": IS_OFFICIAL_RELEASE,
            # Dev-only free-tier preview state (checkbox in the BYOK section;
            # the official build hides that whole section, and the setter below
            # refuses anyway, so this never leaks into production behavior).
            "cascade_preview": (not IS_OFFICIAL_RELEASE
                                and bool(self.cfg.get("cascade_preview", False))),
            "badge_removable": self._badge_removable(),
            "onboarding_done": bool(self.cfg.get("onboarding_done", False)),
            "cfg": self._cfg_view(outs, mics),
        }

    def _beta_allowed(self) -> bool:
        """Beta (Qwen) eligibility. Dev builds: always. Official: the server's
        per-account flag, refreshed by check_auth/verify. The Beta TAB was
        removed in 1.0.33 (Qwen graduated to the standard server-routed primary
        engine); the cfg["beta"] opt-in remains config-file-driven — the dev
        A/B path and older field builds keep working, and the server re-checks
        eligibility on session-key anyway."""
        if not IS_OFFICIAL_RELEASE:
            return True
        return bool(getattr(self, "_beta_flag", False))

    def _quality_options(self):
        """End-user build sees two friendly choices (smooth vs savings); the
        developer build sees the full preset list for tuning."""
        if IS_OFFICIAL_RELEASE:
            return [["balanced", t("quality_smooth")],
                    ["turbo", t("quality_fast")],
                    ["callout", t("quality_callout")],
                    ["max_savings", t("quality_saver")]]
        return [[k, t(f"quality_{k}")] for k in QUALITY_PRESETS]

    def _engine_options(self):
        """Engine choices for the selector. Keys only — the per-locale benefit
        labels resolve from the JS I18N dict (app/i18n.py is TR/EN-only).
        OpenAI is an official-build feature; the OSS/BYOK build is Gemini-only.
        Qwen is EXCLUDED here on purpose: it is served by the SERVER's
        per-target routing (primary engine for its voiced targets), never a
        user-facing selector choice."""
        from .config import VALID_ENGINES, ENGINE_GEMINI, ENGINE_QWEN
        if IS_OFFICIAL_RELEASE:
            return [e for e in VALID_ENGINES if e != ENGINE_QWEN]
        return [ENGINE_GEMINI]

    # ---------- store ----------
    def open_store_page(self):
        """Open the Voxis Microsoft Store listing in the Store app. Updates are
        delivered by the Store itself; this is just a shortcut to the listing.
        No-op-safe: failures are swallowed so a missing Store app never raises."""
        url = "ms-windows-store://pdp/?productid=9P5Z0KVS58RS"
        try:
            os.startfile(url)  # Windows shell handles the ms-windows-store: scheme
            return {"ok": True}
        except Exception as e:
            _log.exception("open_store_page failed")
            return {"ok": False, "error": str(e)}

    def set_cascade_preview(self, enabled):
        """DEV-ONLY: force the free-tier cascade engine for the next session
        (Settings checkbox inside the BYOK section). Double-gated like every
        dev affordance: the official build hides the UI and refuses here."""
        if IS_OFFICIAL_RELEASE:
            return False
        self.cfg["cascade_preview"] = bool(enabled)
        save_config(self.cfg)
        return True

    def _cfg_view(self, outs=None, mics=None):
        outs = outs or list_device_names("output")
        mics = mics or list_device_names("input")
        c = dict(self.cfg)
        cur_out = self.cfg["devices"].get("headphones_output", "")
        cur_mic = self.cfg["devices"].get("microphone", "")
        c["devices"] = dict(self.cfg["devices"])
        c["devices"]["headphones_output_label"] = next(
            (n for n in outs if cur_out and cur_out.lower() in n.lower()), outs[0] if outs else "")
        c["devices"]["microphone_label"] = next(
            (n for n in mics if cur_mic and cur_mic.lower() in n.lower()), t("default_mic"))
        # Resolved transcript folder for the Settings readout (the raw
        # cfg["transcript_dir"] may be "" = default; show where files actually go).
        c["transcript_dir_display"] = self._transcript_dir()
        return c

    def get_cfg(self):
        return self._cfg_view()

    def _save_cfg(self) -> bool:
        """Persist config, surfacing (not swallowing) a write failure so the UI
        can warn instead of silently losing the setting. Held under a lock so
        concurrent bridge threads (e.g. a slider drag) can't race on the file."""
        try:
            with self._save_lock:
                save_config(self.cfg)
            return True
        except OSError:
            _log.exception("config save failed")
            self._emit_status(t("err_save_failed"), "error")
            return False

    def set_cfg(self, key, value):
        # The attribution badge can only be turned off by a paid subscriber;
        # silently ignore a disable attempt from a free/OSS user (defense-in-depth
        # behind the already-disabled UI toggle).
        if key == "branding_badge_enabled" and not value and not self._badge_removable():
            value = True
        self.cfg[key] = value
        if key == "ui_language":
            i18n.set_language(value)
        if key == "duck_gain":
            self.controller.set_duck_gain(float(value))
            self._mark_custom()
        elif key == "tts_volume":
            self.controller.set_tts_volume(float(value))
        elif key in ("quality_preset", "target_language_incoming",
                     "target_language_outgoing", "gemini_voice", "engine"):
            if key == "quality_preset":
                self._mark_custom()
            if key == "target_language_incoming":
                # New target = new per-target engine routing: warm its key so
                # the next Start (or the restart below) skips the issuance RTT.
                self._prefetch_session_key()
            self._maybe_restart()
        return self._save_cfg()

    # ---------- attribution badge gating ----------
    def _is_paid(self) -> bool:
        """True only for an official build with an active PAID license. Free
        tiers, unknown/unreachable quota, and the OSS build all return False, so
        the 'Powered by Voxis' badge stays on (removing it is a paid perk)."""
        if not IS_OFFICIAL_RELEASE:
            return False
        q = self._last_quota
        if not isinstance(q, dict):
            return False
        if q.get("unlimited"):
            return True
        tier = str(q.get("tier") or q.get("plan") or "").strip().lower()
        return tier in ("creator", "pro", "enterprise", "premium", "paid")

    def _badge_removable(self) -> bool:
        """Whether the user may turn the attribution badge off (paid only)."""
        return self._is_paid()

    def _show_badge(self) -> bool:
        """Effective overlay/OBS attribution visibility: paid users honor their
        Settings toggle; everyone else always shows it."""
        if not self._badge_removable():
            return True
        return bool(self.cfg.get("branding_badge_enabled", True))

    def set_profile(self, name):
        apply_profile(self.cfg, name)
        ok = self._save_cfg()
        self._maybe_restart()
        return ok

    def _is_default_device(self, name) -> bool:
        """True when the UI returned the 'default mic' entry. Matches the empty
        sentinel and the localized label rather than the Turkish literal so a
        non-TR UI maps back to the system default correctly."""
        return not name or name == t("default_mic")

    def set_device(self, kind, name):
        if kind == "output":
            self.cfg["devices"]["headphones_output"] = name
        else:
            self.cfg["devices"]["microphone"] = (
                DEFAULT_DEVICE if self._is_default_device(name) else name)
        ok = self._save_cfg()
        self._maybe_restart()
        return ok

    def _ensure_user_id(self) -> str | None:
        """Resolves the BYOK store identifier from the JWT payload locally.

        Independent of license/quota state so an unlicensed user can still
        persist their own API key.
        """
        if not IS_OFFICIAL_RELEASE:
            return "developer"
        if self._user_id:
            return self._user_id
        from . import voxis_client
        self._user_id = voxis_client.user_id_from_jwt()
        return self._user_id

    def save_keys(self, gem, oai=""):
        # Official-release builds never expose BYOK entry; refuse silently as
        # a defense-in-depth check.
        if IS_OFFICIAL_RELEASE:
            return False
        uid = self._ensure_user_id()
        if not uid:
            return False
        from . import byok_store
        current = byok_store.load_byok(uid)
        # OSS/BYOK is Gemini-only (OpenAI is an official-build feature); the `oai`
        # arg is kept for signature back-compat but ignored — only the Gemini key
        # is written, preserving any previously stored value when blank.
        new_gem = gem.strip() if gem and gem.strip() else current.get("gemini", "")
        byok_store.save_byok(uid, new_gem, current.get("openai", ""))
        return True

    def clear_byok(self, engine=None) -> bool:
        if IS_OFFICIAL_RELEASE:
            return False
        uid = self._ensure_user_id()
        if not uid:
            return False
        from . import byok_store
        byok_store.clear_byok(uid, engine)
        return True

    # ---------- problem reporting ----------
    # User-initiated only: nothing here transmits without an explicit Send click
    # (the offline queue flushes a payload the user already consented to). The
    # whole feature is official-build-only — the OSS/BYOK build hard-gates the
    # network call in voxis_client.send_report, mirroring report_usage.
    def _build_diagnostics(self) -> dict:
        """Fixed allowlist of non-identifying technical context. Never dumps
        config.json or env — only these keys, scrubbed again before send."""
        import platform
        from .paths import client_channel
        cfg = self.cfg
        # Engine + model of the LIVE session (not the config selector) — the field
        # that actually answers "was this a Gemini or a Qwen-beta session?". Falls
        # back to the routed engine when idle.
        engine = ""
        try:
            engine = self.controller.current_engine() or ""
        except Exception:
            engine = ""
        try:
            from .config import resolve_model, route_engine
            if not engine:
                engine = route_engine(cfg, cfg.get("target_language_incoming", ""))
            model = resolve_model(cfg, engine or None)
        except Exception:
            model = cfg.get("model", "")
        beta = cfg.get("beta") or {}
        beta_enabled = bool(beta.get("enabled")) and self._beta_allowed()
        backend = "vbcable" if cfg.get("capture_backend", "driverless") == "vbcable" else "driverless"
        return {
            "app_version": APP_VERSION,
            "channel": client_channel(),
            "official": IS_OFFICIAL_RELEASE,
            "os": "%s %s" % (platform.system(), platform.release()),
            "os_build": platform.version(),
            "arch": platform.machine(),
            "mode": getattr(self.controller, "mode", None) or "idle",
            "quality": cfg.get("quality_preset", ""),
            "engine": engine,
            "beta_enabled": beta_enabled,
            # Qwen-beta voice-clone mode: distinguishes "all speakers one voice by
            # design" (once) from "per-response clone failing" (always) when a
            # report complains voices collapsed — invisible without it.
            "beta_clone": (beta.get("clone", "off") if beta_enabled else ""),
            "beta_source": (beta.get("source_lang", "auto") if beta_enabled else ""),
            "model": model,
            "capture_backend": backend,
            "lang_target_incoming": cfg.get("target_language_incoming", ""),
            "lang_target_outgoing": cfg.get("target_language_outgoing", ""),
            "error_reason": self._last_error_code or "",
            "recent_status": list(self._status_log[-15:]),
        }

    def _collect_transcript(self) -> str:
        """Render the current session's paired turns as plain text. Only ever
        called when the user ticks the opt-in checkbox."""
        with self._text_lock:
            turns = list(self._turns)
        lines = []
        for tn in turns:
            src = (tn.get("src") or "").strip()
            txt = (tn.get("text") or "").strip()
            if src and txt:
                lines.append(src + "  ->  " + txt)
            elif txt:
                lines.append(txt)
        return ("\n".join(lines))[:200000]

    def _collect_log_tail(self, max_bytes: int = 32768) -> str:
        """Tail of the app's own log (voxis.log — network/config errors). Scrubbed
        before it can leave the device. Auto-included so even a one-line or empty
        report still carries the diagnostic the engine recorded."""
        from . import report_scrub
        path = user_path("voxis.log")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError:
            return ""
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return report_scrub.scrub_text(data)

    def _build_report_payload(self, form: dict) -> dict:
        """Assemble + scrub (scrub-v1) the report payload from the modal form.
        Secrets/PII are redacted here so they never leave the device."""
        from . import report_scrub
        from .paths import client_channel
        import uuid
        form = form or {}
        include_tx = bool(form.get("include_transcript"))
        message = (form.get("message") or "").strip()[:4000]
        repro = (form.get("repro") or "").strip()[:2000]
        # The reply email is purpose-bound (user typed it for a reply) — kept
        # as-is, not scrubbed; everything else is redacted.
        email = (form.get("email") or "").strip()[:200]
        payload = {
            "category": form.get("category") or "other",
            "severity": form.get("severity") or "normal",
            "message": report_scrub.scrub_text(message),
            "repro": report_scrub.scrub_text(repro),
            "email": email,
            "transcript_included": include_tx,
            "transcript": report_scrub.scrub_text(self._collect_transcript()) if include_tx else "",
            "diagnostics": report_scrub.scrub_value(self._build_diagnostics()),
            "log": self._collect_log_tail(),
            "correlation": uuid.uuid4().hex,
            "channel": client_channel(),
            "scrub_schema": report_scrub.SCRUB_SCHEMA,
        }
        return payload

    def preview_report(self, form: dict) -> dict:
        """Return the exact scrubbed payload that Send would transmit, for the
        modal's 'preview data to be sent' expander (transparency affordance)."""
        if not IS_OFFICIAL_RELEASE:
            return {}
        try:
            return self._build_report_payload(form)
        except Exception:
            self._log_report_error("preview")
            return {}

    def send_report(self, form: dict) -> dict:
        """JS -> Python: submit a problem report. Official-build only.

        Returns {ok, ticket?, deduped?} on success, {ok:False, queued:True} when
        the network is down (saved for explicit flush), or {ok:False, error}."""
        if not IS_OFFICIAL_RELEASE:
            return {"ok": False, "error": "disabled"}
        try:
            # Message is optional: the scrubbed app log + diagnostics are attached
            # automatically, so a one-click report still carries what we need.
            form = form or {}
            payload = self._build_report_payload(form)
        except Exception:
            self._log_report_error("build")
            return {"ok": False, "error": "internal"}
        from . import voxis_client
        res = voxis_client.send_report(payload)
        if res.get("ok"):
            return {"ok": True, "ticket": res.get("ticket", ""), "deduped": bool(res.get("deduped"))}
        if res.get("retryable"):
            self._queue_report(payload)
            return {"ok": False, "queued": True}
        return {"ok": False, "error": res.get("error", "failed")}

    def _report_queue_path(self) -> str:
        return user_path("reports_pending.json")

    def _queue_report(self, payload: dict) -> None:
        """Persist a report that couldn't be sent (network/5xx) for an explicit
        flush on next app start / next modal open. Capped + deduped by
        correlation so a retry can never double-file. Never transmits."""
        import json
        path = self._report_queue_path()
        try:
            queued = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    queued = json.load(f) or []
            corr = payload.get("correlation")
            queued = [q for q in queued if q.get("correlation") != corr]
            queued.append(payload)
            queued = queued[-10:]  # cap
            with open(path, "w", encoding="utf-8") as f:
                json.dump(queued, f)
        except Exception:
            self._log_report_error("queue")

    def flush_reports(self) -> int:
        """Send any queued reports. Called on startup and when the report modal
        opens — both are explicit user contexts (app launch / opening the form),
        never a silent background flush. Returns the count successfully sent."""
        if not IS_OFFICIAL_RELEASE:
            return 0
        import json
        path = self._report_queue_path()
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                queued = json.load(f) or []
        except Exception:
            return 0
        if not queued:
            return 0
        from . import voxis_client
        remaining, sent = [], 0
        for payload in queued:
            res = voxis_client.send_report(payload)
            if res.get("ok"):
                sent += 1
            elif res.get("retryable"):
                remaining.append(payload)  # keep transient failures for next time
            # non-retryable (400/disabled): drop — re-queueing would never succeed.
        try:
            if remaining:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(remaining, f)
            else:
                os.remove(path)
        except Exception:
            pass
        return sent

    def _log_report_error(self, where: str) -> None:
        try:
            from . import voxis_client
            voxis_client._log_detail("report:" + where, RuntimeError("report assembly/dispatch failed"))
        except Exception:
            pass

    def check_auth(self) -> dict:
        """Page-load auth check. Returns {authenticated, quota}. Non-blocking —
        uses the cached JWT for identity."""
        if not IS_OFFICIAL_RELEASE:
            return {"authenticated": True, "quota": None}
        from . import voxis_client
        # Top-of-funnel: the app is open. Fire once per process BEFORE the JWT
        # gate below, so users who launch but never sign in are still counted
        # (app_launched only fires post-auth). Anonymous, device-hash attributed.
        if not self._opened_reported:
            self._opened_reported = True
            voxis_client.report_app_opened_async()
        jwt = voxis_client.get_jwt()
        if not jwt:
            return {"authenticated": False, "quota": None}
        self._user_id = voxis_client.user_id_from_jwt()
        info, err = voxis_client.verify_session()
        if not info:
            # Distinguish a transient transport failure (server unreachable) from
            # a real auth rejection: verify_session returns the localized
            # "server unreachable" message ONLY on a transport error (a 401
            # clears the JWT). This keeps a still-authenticated user with a brief
            # network drop from being shown a logged-out login form.
            offline = bool(err) and err == t("st_server_unreachable")
            return {"authenticated": False, "offline": offline, "quota": None}
        self._last_quota = info
        # Per-account beta (Qwen) eligibility rides on the verify snapshot.
        self._beta_flag = bool(info.get("beta"))
        # Activation funnel: the app is up AND authenticated. Fire once per process.
        if not self._launch_reported:
            self._launch_reported = True
            voxis_client.report_event_async("app_launched")
        # Warm the session key in the background so the first Start after
        # opening the app skips the issuance round-trip.
        self._prefetch_session_key()
        return {"authenticated": True, "quota": info}

    def win_resize(self, width, height, anchor="br") -> bool:
        """Resize the frameless main window from the custom JS edge/corner grips
        (pywebview 6.2.1 has no native frameless resize). `anchor` names the edge
        or corner being dragged; the opposite corner is held fixed via FixPoint so
        left/top drags move the window correctly. Clamped to the min (940x600).
        No-op while maximized so an edge drag can't produce a half-maximized window."""
        if self._maximized:
            return False
        try:
            w = max(int(width), 940)
            h = max(int(height), 600)
            if self._main_window is None:
                return False
            fp = self._fixpoint(anchor)
            if fp is not None:
                self._main_window.resize(w, h, fp)
            else:
                self._main_window.resize(w, h)
            return True
        except Exception:
            pass
        return False

    @staticmethod
    def _fixpoint(anchor):
        try:
            from webview.window import FixPoint as F
        except Exception:
            return None
        N, S, E, W = F.NORTH, F.SOUTH, F.EAST, F.WEST
        return {
            "r": N | W, "b": N | W, "br": N | W,
            "l": N | E, "bl": N | E,
            "t": S | W, "tr": S | W,
            "tl": S | E,
        }.get(anchor, N | W)

    # ── Window geometry persistence (size/position/maximized) ────────────────
    @staticmethod
    def _work_area_size():
        """Primary-monitor work-area size (px), best-effort. Used to reject an
        OS-driven maximize (Win+Up / title-bar double-click) that fires `resized`
        before `maximized` — without ordering guarantees the full work-area size
        would otherwise be stored as the RESTORE geometry, sticking the window at
        screen size after un-maximize. Returns None on any failure."""
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
        try:
            import ctypes
            # SM_CXMAXIMIZED = 61, SM_CYMAXIMIZED = 62
            return (ctypes.windll.user32.GetSystemMetrics(61),
                    ctypes.windll.user32.GetSystemMetrics(62))
        except Exception:
            return None

    def _on_win_resized(self, *a):
        if len(a) >= 2 and not self._maximized and not self._minimized:
            w, h = int(a[0]), int(a[1])
            # Skip a resize matching the work area: an OS maximize whose `resized`
            # arrives before `maximized` must not pollute the restore geometry.
            wa = self._work_area_size()
            if wa is not None and abs(w - wa[0]) <= 4 and abs(h - wa[1]) <= 4:
                return
            self._win_geom["w"], self._win_geom["h"] = w, h

    def _on_win_moved(self, *a):
        if len(a) >= 2 and not self._maximized and not self._minimized:
            self._win_geom["x"], self._win_geom["y"] = int(a[0]), int(a[1])

    def _on_win_maximized(self, *a):
        self._maximized = True

    def _on_win_minimized(self, *a):
        # Windows reports a minimized window's position/size via a sentinel
        # (e.g. -32000,-32000) — without this guard that lands in _win_geom and
        # gets persisted on close as if it were real restore geometry.
        self._minimized = True

    def _on_win_restored(self, *a):
        self._maximized = False
        self._minimized = False

    def _on_win_closing(self, *a):
        try:
            g = dict(self._win_geom)
            g["max"] = bool(self._maximized)
            self.cfg["window"] = g
            self._save_cfg()
        except Exception:
            pass
        # pywebview only leaves its message loop once EVERY window is gone
        # (winforms on_close: Application.Exit() fires at instances == 0), so an
        # open overlay outlives the main window: webview.start() never returns,
        # the post-loop cleanup that stops the session never runs, and the app
        # keeps translating (and billing) into a headless overlay while holding
        # the single-instance mutex — "closed it, it won't reopen" (field report,
        # 2026-07-13). Tear the overlay down here, on the closing edge, so the
        # loop can actually end. Not via toggle_overlay(False): that persists
        # overlay_enabled=False and would silently lose the user's preference.
        self._destroy_overlay()
        # Belt and braces: the user asked to close, so the process MUST die. If
        # anything still pins the message loop (a window we failed to destroy, a
        # wedged WebView2 teardown), webview.start() never returns and _shutdown
        # is never reached — the exact zombie this bug produced. Nothing may
        # cancel the close past this point, so an unconditional bounded exit is
        # safe; the normal path beats the timer and this never fires.
        self._close_watchdog = threading.Timer(20.0, _shutdown, args=(self,))
        self._close_watchdog.daemon = True
        self._close_watchdog.name = "voxis-close-watchdog"
        self._close_watchdog.start()

    def _destroy_overlay(self):
        win, self._overlay_win = self._overlay_win, None
        if win is None:
            return
        try:
            win.destroy()
        except Exception:
            _log.debug("overlay destroy failed", exc_info=True)

    def open_url(self, url: str) -> bool:
        # Allowlist http/https/mailto only so a crafted bridge call can never
        # launch file:, javascript: or other handler schemes via the default
        # browser. mailto is safe (opens the mail client, executes nothing) and
        # carries the Beta-application prefilled email.
        import webbrowser
        from urllib.parse import urlparse
        try:
            parts = urlparse(url)
        except Exception:
            return False
        if parts.scheme not in ("http", "https", "mailto"):
            return False
        try:
            webbrowser.open(url)
        except Exception:
            pass
        return True

    def voxis_login(self, email: str, password: str) -> dict:
        if not IS_OFFICIAL_RELEASE:
            return {"ok": False, "quota": None, "error": "Login is disabled in developer builds."}
        from . import voxis_client
        token, err = voxis_client.pb_login(email, password)
        if not token:
            return {"ok": False, "quota": None, "error": err or "Login failed."}
        self._user_id = voxis_client.user_id_from_jwt()
        info, verr = voxis_client.verify_session()
        if not info:
            # Credentials are valid but the server rejected session verification
            # (no active license, quota exceeded, etc.). Clear the JWT so the
            # user is not left in a half-authenticated state, and surface the
            # actual server reason instead of a generic "login failed" string.
            voxis_client.clear_jwt()
            return {"ok": False, "quota": None, "error": verr or t("err_start_failed")}
        self._last_quota = info
        self._prefetch_session_key()
        return {"ok": True, "quota": info, "error": None}

    def google_login(self) -> dict:
        """Browser-relay Google/email sign-in (D1). Google blocks OAuth inside
        embedded webviews, and passwordless Google users have no password for
        /auth/login — so the sign-in runs in the SYSTEM browser on
        voxislive.com/app-login (PocketBase mints the token natively), and that
        page relays the PB token back to a single-use 127.0.0.1 listener guarded
        by a random nonce. No new Google Cloud config; reuses the live web flow."""
        if not IS_OFFICIAL_RELEASE:
            return {"ok": False, "quota": None, "error": "Login is disabled in developer builds."}
        import http.server
        import secrets
        import webbrowser
        import json as _json
        from urllib.parse import quote

        nonce = secrets.token_urlsafe(24)
        captured: dict = {}
        done = threading.Event()

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _cors(self):
                origin = self.headers.get("Origin", "") or "*"
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                # Chrome/Edge Private Network Access: a public HTTPS page fetching
                # a loopback address gets a preflight that must be answered with
                # this header or the POST is blocked.
                self.send_header("Access-Control-Allow-Private-Network", "true")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                # Redirect fallback: if the page's fetch(POST) is blocked (CORS /
                # Private-Network-Access), app-login navigates here with the token
                # in the query instead. Same nonce gate.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                token = (q.get("token") or [""])[0]
                ok = bool(token) and (q.get("nonce") or [""])[0] == nonce
                if ok and "token" not in captured:
                    captured["token"] = token
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<!doctype html><meta charset=utf-8><title>Voxis</title>"
                    b"<body style=\"font-family:Segoe UI,system-ui,sans-serif;background:#050507;"
                    b"color:#fafafa;display:flex;min-height:100vh;align-items:center;justify-content:center\">"
                    b"<div style=\"text-align:center\"><h2>Signed in</h2>"
                    b"<p style=\"color:#a1a1aa\">You can close this tab and return to Voxis.</p></div>")
                if ok:
                    done.set()

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    body = self.rfile.read(length) if length else b""
                    data = _json.loads(body.decode("utf-8") or "{}")
                except Exception:
                    data = {}
                token = data.get("token") if isinstance(data, dict) else None
                ok = bool(token) and data.get("nonce") == nonce
                if ok and "token" not in captured:
                    captured["token"] = token
                self.send_response(200 if ok else 400)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}' if ok else b'{"ok":false}')
                if ok:
                    done.set()

            def log_message(self, *a):  # silence default stderr access log
                pass

        from . import voxis_client
        try:
            httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        except OSError as exc:
            voxis_client._log_detail("google_login bind", exc)
            return {"ok": False, "quota": None, "error": t("err_start_failed")}
        port = httpd.server_address[1]
        srv = threading.Thread(target=httpd.serve_forever, daemon=True)
        srv.start()

        url = f"https://voxislive.com/app-login?port={port}&nonce={quote(nonce)}"
        try:
            webbrowser.open(url)
        except Exception:
            pass

        done.wait(timeout=300)
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

        token = captured.get("token")
        if not token:
            return {"ok": False, "quota": None, "error": t("auth_browser_timeout")}

        voxis_client.set_jwt(token)
        self._user_id = voxis_client.user_id_from_jwt()
        info, verr = voxis_client.verify_session()
        if not info:
            voxis_client.clear_jwt()
            return {"ok": False, "quota": None, "error": verr or t("err_start_failed")}
        self._last_quota = info
        self._beta_flag = bool(info.get("beta"))
        self._prefetch_session_key()
        return {"ok": True, "quota": info, "error": None}

    def voxis_quota(self) -> dict | None:
        if not IS_OFFICIAL_RELEASE:
            return None
        from . import voxis_client
        q = voxis_client.get_quota()
        if q:
            self._last_quota = q
        return q

    def voxis_logout(self) -> bool:
        from . import voxis_client
        voxis_client.clear_jwt()
        self._user_id = None
        return True

    def capture_hotkey(self, action):
        """Block on the next key combo, then bind it to `action`.

        Bounded by a watchdog and an explicit cancel_hotkey() so a recording
        box that never receives a keypress cannot hang the bridge thread. The
        captured combo is validated (non-empty, not already bound to another
        action) before it is persisted."""
        try:
            import keyboard
        except Exception:
            return None

        result: dict = {}
        done = threading.Event()
        self._hotkey_cancel = False

        def worker():
            try:
                result["combo"] = keyboard.read_hotkey(suppress=False)
            except Exception:
                result["combo"] = None
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        if not done.wait(HOTKEY_CAPTURE_TIMEOUT):
            # read_hotkey is blocking; nudge it with a synthetic keypress so the
            # worker returns instead of leaking a stuck thread on timeout.
            self._hotkey_cancel = True
            try:
                keyboard.press_and_release("esc")
            except Exception:
                pass
            done.wait(1.0)

        combo = result.get("combo")
        if self._hotkey_cancel or not combo:
            return None
        hk = self.cfg.setdefault("hotkeys", {})
        # Reject a combo already bound to a different action — duplicate bindings
        # would make _register_hotkeys' last writer silently win.
        if any(combo == c for a, c in hk.items() if a != action):
            self._emit_status(t("err_hotkey_duplicate"), "error")
            return None
        hk[action] = combo
        self._save_cfg()
        self._register_hotkeys()
        return combo

    def cancel_hotkey(self) -> bool:
        """Abort an in-flight capture_hotkey() (UI closed the recording box)."""
        self._hotkey_cancel = True
        try:
            import keyboard
            keyboard.press_and_release("esc")
        except Exception:
            pass
        return True

    def start(self, mode, consented=False):
        # consented=True means the UI consent modal was just accepted for THIS
        # start (the user may decline "don't show again", so it is not persisted).
        threading.Thread(target=self._start_thread, args=(mode, bool(consented)),
                         daemon=True).start()
        return True

    def _start_thread(self, mode, consented):
        # Endpoint switching calls win_audio._ensure_com, which CoInitializes this
        # short-lived session thread. Pair it with shutdown_com on the same thread
        # so the COM apartment is released and the thread id never lingers in
        # win_audio's per-thread bookkeeping (a later reused tid would otherwise
        # skip init and fault on CO_E_NOTINITIALIZED). No-op if we never owned it.
        try:
            self._start(mode, consented)
        finally:
            from . import win_audio
            win_audio.shutdown_com()

    def _consent_ok(self, mode, consented=False) -> bool:
        """Defense-in-depth consent gate. The primary consent modal lives in the
        UI; this backstop guarantees a path that never renders the modal (e.g. a
        hotkey) cannot launch meeting mode — which streams the other party's
        audio to a third party — before consent is given. Passes when consent was
        just acknowledged for this start OR was persisted via 'don't show again'."""
        if mode == "meeting" and not consented and not self.cfg.get("meeting_consent_ack"):
            self._emit_status(t("err_consent_required"), "error")
            return False
        return True

    def _cable_ok(self, mode) -> bool:
        """Defense-in-depth virtual-cable gate. Meeting mode streams the user's
        translated voice into a virtual microphone (VB-CABLE); with no cable
        installed there is nowhere to route it. The UI checks this before start,
        but the hotkey path never renders that prompt, so backstop it here.
        Only a clean None result (no cable found) blocks — a detection fault is
        allowed through so a transient COM error never hard-blocks a user who
        actually has a cable."""
        if mode != "meeting":
            return True
        try:
            available = detect_virtual_cable() is not None
        except Exception:
            return True
        if not available:
            self._emit_status(t("err_cable_required"), "error")
            return False
        return True

    def _prefetch_session_key(self):
        """Warm the session-key cache for the current incoming target (official
        build only). Fire-and-forget on a daemon thread: any error just leaves
        the cache cold and the next session start does its normal synchronous
        fetch. The beta (Qwen) resolver never reads this cache."""
        if not IS_OFFICIAL_RELEASE:
            return
        target = self.cfg.get("target_language_incoming", "")
        if not target:
            return

        with self._key_cache_lock:
            epoch = self._key_epoch

        def work():
            try:
                from . import voxis_client  # noqa: PLC0415
                key, engine, model, quality, quota, workspace, key_type, _err = voxis_client.get_session_key(
                    target=target, caps=voxis_client.SESSION_KEY_CAPS)
                # Never cache an ephemeral token: its new-session window (2 min)
                # is shorter than KEY_PREFETCH_TTL, and a dead single-use token
                # at session start fails the first connect TERMINALLY instead of
                # retrying. The prefetch still warms TLS + the server-side token
                # cache, so the synchronous start fetch stays cheap.
                if key and key_type != "ephemeral":
                    with self._key_cache_lock:
                        # Only publish if the quota has not been exhausted while we
                        # were in flight. Otherwise this grant — issued when the
                        # user still had Pro minutes — would resurrect the paid
                        # engine for a spent free account.
                        if self._key_epoch == epoch:
                            self._key_cache[target] = (time.time(), engine, key,
                                                       model, quality, workspace)
                if isinstance(quota, dict):
                    self._last_quota = quota
            except Exception:
                pass  # cold cache == old behavior

        threading.Thread(target=work, daemon=True,
                         name="voxis-key-prefetch").start()

    def _pop_prefetched_key(self, target):
        """Single-use cache take — ephemeral tokens are not reused across
        sessions. Returns (engine, key, model, quality, workspace) or None
        (stale/miss).

        A grant is only as good as the quota it was issued under. The epoch guard
        in the prefetch stops the common race, but a grant can also simply go stale
        in the cache (TTL is 4 min) while the last minutes burn away. So the free/
        paid boundary is re-checked HERE, at the moment of use: a spent taste never
        starts a voiced session, whatever the cache holds. Belt to the epoch's
        braces — this is the invariant the server cannot enforce for us, because it
        never sees a start that reuses a grant it issued minutes ago."""
        with self._key_cache_lock:
            hit = self._key_cache.pop(target, None)
        if not hit or time.time() - hit[0] >= KEY_PREFETCH_TTL:
            return None
        engine = hit[1]
        if engine != ENGINE_CASCADE and self._taste_spent():
            # Out of Pro minutes, holding a Pro grant: drop it and let the start do
            # its synchronous fetch, which the server answers with the cascade.
            return None
        return hit[1:]

    def _taste_spent(self) -> bool:
        """True when the license has no billable minutes left. Fails OPEN (False)
        on an unknown quota: refusing a paid grant we are unsure about would break
        a paying customer, while wrongly allowing one costs at most one session
        that the server's own 402 stops within a heartbeat."""
        q = self._last_quota
        if not isinstance(q, dict) or q.get("unlimited"):
            return False
        rem = q.get("remaining")
        if rem is None:
            allowed, used = q.get("allowed_minutes"), q.get("used_minutes")
            if allowed is None or used is None:
                return False
            rem = allowed - used
        try:
            return float(rem) <= 0
        except (TypeError, ValueError):
            return False

    def _apply_qwen_workspace(self, engine, workspace):
        """Adopt the server-issued DashScope workspace id (workspace-scoped
        sk-ws-… keys need it for the MAAS WS host — see make_translator).
        Server-controlled so a workspace change never needs a client release."""
        if engine == "qwen" and workspace:
            self.cfg["qwen_workspace"] = str(workspace)

    def _build_engine_resolver(self):
        """Returns resolve(target) -> (engine, key, model), called once per pipeline.

        SaaS asks the server, which routes by TARGET language and can fail over to
        Gemini (the engine selector is server-controlled). Dev/BYOK routes locally
        over the stored keys. Raises a localized error if no key is available.
        """
        from .config import ENGINE_GEMINI, resolve_model, qwen_can_voice
        # Beta engine opt-in (Qwen): only honored when the account is
        # beta-eligible (server flag; dev builds are always eligible) AND the
        # user switched it on. Never touches the normal Gemini/OpenAI routing.
        beta_qwen = (self._beta_allowed()
                     and bool((self.cfg.get("beta") or {}).get("enabled")))
        if not IS_OFFICIAL_RELEASE:
            # OSS/BYOK is Gemini-only; OpenAI is an official-build feature.
            from . import byok_store
            from .config import ENGINE_GEMINI, ENGINE_QWEN
            uid = self._ensure_user_id()
            keys = byok_store.load_byok(uid) if uid else {}
            # Dev free-tier preview WINS over every other dev route (a leftover
            # beta/qwen config must not shadow it — that is exactly what shipped
            # Qwen instead of cascade on the first field try). force_gemini
            # (mid-session failover) still yields the real engine.
            if self.cfg.get("cascade_preview") and keys.get("gemini"):
                from .config import ENGINE_CASCADE  # noqa: PLC0415

                def resolve(target, force_gemini=False):
                    if force_gemini or not self.cfg.get("cascade_preview"):
                        return (ENGINE_GEMINI, keys.get("gemini"),
                                resolve_model(self.cfg, ENGINE_GEMINI))
                    return (ENGINE_CASCADE, keys.get("gemini"),
                            resolve_model(self.cfg, ENGINE_GEMINI))
                return resolve
            # Dev beta path: a DashScope key in config.json ("qwen_key") selects
            # the Qwen engine locally — sandbox-style, no server round-trip.
            if beta_qwen and self.cfg.get("qwen_key"):
                gem = keys.get("gemini")
                def resolve(target, force_gemini=False):
                    # Qwen has no VOICE for some targets (text-only tier) — those
                    # would give subtitles with no audio, so prefer Gemini when a
                    # key is available. force_gemini is the mid-session failover
                    # (see IncomingPipeline._failover_to_gemini).
                    if gem and (force_gemini or not qwen_can_voice(self.cfg, target)):
                        if not force_gemini:
                            _log.info("Qwen beta has no voice for target %r; using Gemini", target)
                        return ENGINE_GEMINI, gem, resolve_model(self.cfg, ENGINE_GEMINI)
                    if force_gemini:
                        raise RuntimeError(t("st_no_key_offline"))
                    return (ENGINE_QWEN, self.cfg.get("qwen_key"),
                            resolve_model(self.cfg, ENGINE_QWEN))
                return resolve
            if not keys.get("gemini"):
                raise RuntimeError(t("st_no_key_offline"))

            def resolve(target, force_gemini=False):
                return ENGINE_GEMINI, keys.get("gemini"), resolve_model(self.cfg, ENGINE_GEMINI)
            return resolve

        from . import voxis_client

        # Mid-session failover (IncomingPipeline._failover_to_gemini): a routed
        # engine that gave up — a spent DashScope balance reports as a terminal
        # 'arrearage' — needs a Gemini key NOW. Calling /auth/session-key with no
        # ?caps is the server's backward-compat path and always answers with the
        # plain Gemini key (session_key.go defaults engine to "gemini" when the
        # client is not routing-aware), so no server change is needed.
        def gemini_key():
            key, _engine, model, quality, quota, _ws, _kt, err = voxis_client.get_session_key()
            if not key:
                raise RuntimeError(err or t("st_no_key"))
            if isinstance(quota, dict):
                self._last_quota = quota
            if quality:
                self.cfg["quality_preset"] = quality
            return ENGINE_GEMINI, key, (model or resolve_model(self.cfg, ENGINE_GEMINI))

        # Gemini key fountain for LiveTranslator: called on the translator's
        # thread before every reconnect once its single-use ephemeral token has
        # been spent (a raw-key session never calls it). No target → the server
        # always answers Gemini (an empty target routes to the catch-all), and
        # because ephemeral tokens are uses:1 this re-runs the quota + device
        # gates on every 13-min rotation — the point of Tier A5. Raising here
        # just fails that reconnect attempt; the translator retries with backoff.
        def gemini_key_provider():
            key, _engine, _model, _quality, quota, _ws, _kt, err = \
                voxis_client.get_session_key(caps=voxis_client.SESSION_KEY_CAPS)
            if not key:
                raise RuntimeError(err or t("st_no_key"))
            if isinstance(quota, dict):
                self._last_quota = quota
            return key

        if beta_qwen:
            # SaaS beta: ask the server for the Qwen session key explicitly. The
            # server re-checks the account's beta flag (client is not trusted)
            # and refuses otherwise — then we fall through to normal routing.
            def resolve(target, force_gemini=False):
                err = None
                if force_gemini:
                    return gemini_key()
                # Skip Qwen entirely for a target it can't voice (text-only tier):
                # the standard engine gives translated speech, not just subtitles.
                if qwen_can_voice(self.cfg, target):
                    key, engine, model, quality, quota, workspace, _kt, err = voxis_client.get_session_key(
                        target=target, caps=voxis_client.SESSION_KEY_CAPS, engine="qwen")
                    if key and engine == "qwen":
                        if isinstance(quota, dict):
                            self._last_quota = quota
                        self._apply_qwen_workspace(engine, workspace)
                        return engine, key, (model or resolve_model(self.cfg, engine))
                else:
                    _log.info("Qwen beta has no voice for target %r; using standard routing", target)
                key, engine, model, quality, quota, workspace, _kt, err2 = voxis_client.get_session_key(
                    target=target, caps=voxis_client.SESSION_KEY_CAPS,
                    mode=getattr(self, "_starting_mode", None))
                if key:
                    if isinstance(quota, dict):
                        self._last_quota = quota
                    if quality:
                        self.cfg["quality_preset"] = quality
                    self._apply_qwen_workspace(engine, workspace)
                    return engine, key, (model or resolve_model(self.cfg, engine))
                raise RuntimeError(err or err2 or t("st_no_key"))
            resolve.gemini_key_provider = gemini_key_provider
            return resolve

        # Single-round-trip start: /auth/session-key now verifies the token
        # inline on a cold server cache and returns the quota snapshot alongside
        # the key, so the old verify → quota → session-key sequence (3 RTTs on a
        # slow link) collapses into this one call. Auth/quota/license failures
        # surface as localized errors from get_session_key itself.
        # Zero-round-trip start: a fresh prefetched key (warmed at login /
        # target change / previous stop) skips even that one call.
        def resolve(target, force_gemini=False):
            if force_gemini:
                return gemini_key()
            pre = self._pop_prefetched_key(target)
            if pre:
                engine, key, model, quality, workspace = pre
                if quality:
                    self.cfg["quality_preset"] = quality
                self._apply_qwen_workspace(engine, workspace)
                return engine, key, (model or resolve_model(self.cfg, engine))
            key, engine, model, quality, quota, workspace, _kt, err = voxis_client.get_session_key(
                target=target, caps=voxis_client.SESSION_KEY_CAPS,
                mode=getattr(self, "_starting_mode", None))
            if key:
                if isinstance(quota, dict):
                    self._last_quota = quota  # keeps the paid-badge gate fresh
                if quality:
                    self.cfg["quality_preset"] = quality  # server-controlled default
                self._apply_qwen_workspace(engine, workspace)
                return engine, key, (model or resolve_model(self.cfg, engine))
            # Routed engine unavailable (503) → fall back to Gemini via the legacy path.
            key, engine, model, quality, quota, workspace, _kt, err2 = voxis_client.get_session_key()
            if key:
                if quality:
                    self.cfg["quality_preset"] = quality
                return "gemini", key, (model or resolve_model(self.cfg, "gemini"))
            raise RuntimeError(err or err2 or t("st_no_key"))
        resolve.gemini_key_provider = gemini_key_provider
        return resolve

    def _start(self, mode, consented=False):
        # Single-flight: serialize the whole transition so a rapid start→stop or
        # a burst of set_cfg restarts can never run two _start bodies against one
        # controller. start() is thereby idempotent for the active mode.
        with self._lifecycle:
            if not self._consent_ok(mode, consented):
                return
            if not self._cable_ok(mode):
                return
            # The key resolvers run per pipeline and only know the TARGET; the
            # server needs the mode too, because it refuses to cascade a meeting
            # (the other party would hear a synthetic voice speaking as the user).
            self._starting_mode = mode
            # A running sound-check probe must not coexist with the session's
            # own capture — release it before the pipeline opens its stream.
            self.soundcheck_stop()
            self._badge = (t("badge_connecting"), "#fbbf24", "warn")
            try:
                # Per-target engine+key+model resolver (SaaS=server-routed,
                # dev=local). Built once; each pipeline calls it for its target.
                self.controller.resolve = self._build_engine_resolver()
                # Fresh session: drop the previous stop's auto-saved file so a
                # post-stop Save on the NEW session can't re-surface a stale one.
                self._last_saved_file = None
                self._session_error = False
                # Decide this session's self-contained output folder up front (from
                # the wall-clock start) so the recorder's WAVs and the transcript
                # JSON saved on stop share one folder + stamp. The folder itself is
                # created lazily on first write (recorder / save_txt), so a blocked
                # Documents dir can't fail the start here.
                self._session_dirname = transcript_store.session_dir_name(time.time())
                self._session_dir = os.path.join(self._transcript_dir(),
                                                 self._session_dirname)
                self.controller.start(mode, session_dir=self._session_dir)
                self._badge = (t("badge_active", mode=self._mode_name(mode)), "#34d399", "on")
            except Exception as e:
                # Log the raw exception; surface a localized message to the UI
                # rather than forwarding str(e) (which may be an English/library
                # string) into the user-facing transcript.
                _log.exception("session start failed (mode=%s)", mode)
                self._emit_status(self._start_error_message(e), "error")

    def _start_error_message(self, exc) -> str:
        """Map a start failure to a localized, user-actionable message. A
        RuntimeError we raised already carries a localized string. A ValueError
        comes from device resolution (audio_io.find_device / the CABLE feedback
        guard in pipeline.py) — actionable in English but not localized, so map
        it to a generic "check your audio device setup" line instead of losing
        the signal in the fully generic fallback. Anything else is an
        unexpected fault and gets that generic localized line."""
        if isinstance(exc, RuntimeError) and str(exc):
            return str(exc)
        if isinstance(exc, ValueError) and str(exc):
            return t("err_device_config")
        return t("err_start_failed")

    # A session earns the rating ask only if Voxis actually did the job: it
    # produced translation, ran long enough to be more than a poke, and nothing
    # failed. Asking right after a crash is how an app collects one-star ratings.
    REVIEW_MIN_SECONDS = 120.0
    REVIEW_AFTER_SESSIONS = 3

    def _note_good_session(self):
        """Count a clean session and, on the third, raise the rating prompt once.

        Called from _stop while the session's own state is still intact. Never
        raises — a bookkeeping failure must not break the teardown path."""
        try:
            if self._session_error or not self._session_start:
                return
            if time.time() - self._session_start < self.REVIEW_MIN_SECONDS:
                return
            if self.cfg.get("review_prompted") or not store_review.available():
                return
            n = int(self.cfg.get("good_sessions", 0) or 0) + 1
            self.cfg["good_sessions"] = n
            if n >= self.REVIEW_AFTER_SESSIONS:
                # Marked before the prompt is shown, not after it is answered: a
                # card dismissed by closing the window must not come back.
                self.cfg["review_prompted"] = True
                self._put_event(("review", None))
            save_config(self.cfg)
        except Exception:  # noqa: BLE001 - never let this break stop()
            logging.getLogger("voxis").debug("review prompt bookkeeping failed",
                                             exc_info=True)

    def rate_voxis(self):
        """Open the Store's own rating sheet. Nothing is offered in return — see
        store_review for why that matters."""
        return store_review.open_review_page()

    # ---------- the inverse demo (free-voice preview) ----------
    def free_voice_preview(self):
        """Speak the line the user just heard in the FREE tier's voice, then hand
        the paid voice straight back. See free_preview for why the comparison has
        to happen HERE — mid-taste, reversible — and not at the wall.

        Returns immediately: the first call may download a ~60 MB voice, which
        must not block the UI thread. Progress arrives as ('preview', {...})
        events; JS localizes the `code`, so no string crosses this boundary."""
        with self._preview_lock:
            if self._preview_busy:
                return {"ok": False, "code": "busy"}
            self._preview_busy = True
        threading.Thread(target=self._preview_thread, daemon=True).start()
        return {"ok": True}

    def _preview_thread(self):
        try:
            from . import free_preview  # noqa: PLC0415 - lazy: pulls sherpa
            with self._text_lock:
                line = self._last_line
            if not line.strip():
                logging.getLogger("voxis").info("free-voice preview: no line to replay")
                self._preview_event("error", "no_line")
                return
            lang = self.cfg.get("target_language_incoming") or "en"
            if not free_preview.voice_available(lang):
                # Not a failure — the honest shape of the free tier in this
                # language. Saying so is worth more than hiding the button.
                self._preview_event("error", "no_voice")
                return
            self._preview_event("loading", None)
            pcm = free_preview.synth_pcm16(lang, line)
            self._play_clip(pcm, "playing")
            self._preview_event("done", None)
        except Exception as exc:  # noqa: BLE001 - a favour asked of the user must never crash it
            logging.getLogger("voxis").info("free-voice preview failed: %s", exc)
            self._preview_event("error", "failed")
        finally:
            with self._preview_lock:
                self._preview_busy = False

    def pro_voice_replay(self):
        """Replay the paid voice, so the two can be heard back to back. Offered
        after the free clip, when the contrast is freshest — the highest-intent
        moment of the whole taste."""
        with self._preview_lock:
            if self._preview_busy:
                return {"ok": False, "code": "busy"}
            self._preview_busy = True
        threading.Thread(target=self._pro_replay_thread, daemon=True).start()
        return {"ok": True}

    def _pro_replay_thread(self):
        try:
            from . import free_preview  # noqa: PLC0415
            pcm = self.controller.recent_pro_pcm()
            if not pcm:
                logging.getLogger("voxis").info("pro-voice replay: nothing buffered")
                self._preview_event("error", "no_pro")
                return
            self._play_clip(pcm, "playing_pro")
            self._preview_event("done", None)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("voxis").info("pro-voice replay failed: %s", exc)
            self._preview_event("error", "failed")
        finally:
            with self._preview_lock:
                self._preview_busy = False

    def _play_clip(self, pcm: bytes, state: str):
        """Play a demo clip wherever the user happens to be. During a session it
        borrows the live Player (and the paid voice stands down for the clip's
        length); afterwards it opens its own — the A/B card lives after the
        session, because that is when the user is actually looking at Voxis."""
        from . import free_preview  # noqa: PLC0415

        secs = free_preview.duration_seconds(pcm)
        self._preview_event(state, None, seconds=round(secs, 1))
        pipe = self.controller.incoming()
        if pipe is not None:
            pipe.play_free_preview(pcm, secs)
            time.sleep(secs + 0.6)
        else:
            free_preview.play_standalone(self.cfg, pcm)

    def _preview_event(self, state, code, **extra):
        self._put_event(("preview", {"state": state, "code": code, **extra}))

    def mark_seen(self, key):
        """Persist a one-time UI beat (the ladder explainer, the contrast card) so
        it never asks twice. Whitelisted: JS must not be able to write arbitrary
        config keys through this door."""
        if key not in ("ladder_seen", "contrast_shown", "latency_note_seen"):
            return False
        self.cfg[key] = True
        return self._save_cfg()

    def stop(self):
        threading.Thread(target=self._stop_thread, daemon=True).start()
        return True

    def _stop_thread(self):
        # Endpoint restore runs on this stop thread and CoInitializes it via
        # win_audio._ensure_com; balance it with shutdown_com on the same thread.
        try:
            self._stop()
        finally:
            from . import win_audio
            win_audio.shutdown_com()

    def _stop(self):
        # Idempotent: serialized against _start so a stop racing a start cannot
        # tear down a half-built session, and a redundant stop is a no-op.
        # Invalidate any pending _maybe_restart debounce timer FIRST: its run()
        # reads controller.mode outside the lock, and mid-teardown that still
        # says the old mode — without this bump the timer would resurrect the
        # session (capture + billing) the user just stopped.
        self._restart_token += 1
        with self._lifecycle:
            # Auto-save the session on stop so the transcript is never lost.
            # Saved silently here (avoids a status race with the teardown below);
            # the path + open/reveal actions are surfaced once, after teardown.
            saved = self.save_txt(silent=True)
            # Read the session's own state before the teardown below clears it.
            self._note_good_session()
            self.controller.stop()
            self._overlay_text = ""
            self._badge = (t("badge_idle"), "#8593a6", "")
            # New session starts fresh: clear the per-session timeline + buffers
            # so the next run does not append onto the stopped session's turns.
            # Guarded by _text_lock against any still-draining _on_text call.
            with self._text_lock:
                self._turns = []
                self._session_start = 0.0
                self._turn_start = 0.0
                self._session_dir = None
                self._session_dirname = None
                self._session_file = None
                self._lines, self._cur_line = [], ""
                self._src_buf, self._src_done = "", []
                self._cur_spk, self._src_spk = None, None
                self._spk_seen = set()
                self._pending_spk_break = False
        # Tell the user where the auto-saved transcript went and offer open/reveal
        # actions, so pressing Stop confirms the save instead of leaving them to
        # click "Save transcript" and hit "nothing to save". Remember the path so
        # a post-stop Save button click can re-surface it (see save_txt).
        if isinstance(saved, dict) and saved.get("ok"):
            self._last_saved_file = saved["path"]
            self._emit_status(t("saved_to", path=saved["path"]))
            self._put_event(("saved", saved["file"]))
        # Re-warm the session key for the (likely same-target) next start — the
        # previous one was consumed by this session's resolver.
        self._prefetch_session_key()

    def _flush_turns(self):
        """Fold the in-progress turn into the structured log so a session that is
        stopped mid-utterance still records its last line. Idempotent.

        Runs at stop() before the translators are joined, so it can race a live
        _on_text — take the same lock to keep the shared buffers consistent."""
        with self._text_lock:
            tail = self._cur_line.strip()
            if not tail:
                # No pending translation. If the whole session produced NO
                # translation at all (Qwen can drop its text stream mid-session
                # while source ASR keeps arriving), fold the captured source alone
                # so the session is still saved — a bilingual QA user relies on the
                # source side to inspect segmentation even when the translation is
                # lossy — instead of being reported as "nothing to save" and lost.
                # A normal session already has turns/lines, so this never adds a
                # spurious trailing source-only turn to it.
                pend_src = self._pending_source()
                if pend_src and not self._turns and not self._lines:
                    if not self._session_start:
                        self._session_start = time.time()
                    self._turns.append({
                        "t": 0.0, "dir": "out", "src": pend_src, "text": "",
                    })
                return
            if self._turns and self._turns[-1].get("text") == tail:
                return
            if not self._session_start:
                self._session_start = time.time()
            start = self._turn_start or self._session_start
            spk, src = self._pop_source()
            turn = {
                "t": max(0.0, start - self._session_start),
                "dir": "out",
                "src": src,
                "text": tail,
            }
            if spk is not None:
                turn["spk"] = spk
            self._turns.append(turn)

    def _build_record(self):
        return transcript_store.build_record(
            self._session_start or time.time(),
            self._turns,
            app_version=APP_VERSION,
            mode=self.controller.mode or "",
            ui_language=self.cfg.get("ui_language", ""),
            target_in=self.cfg.get("target_language_incoming", ""),
            target_out=self.cfg.get("target_language_outgoing", ""),
        )

    def save_txt(self, silent=False):
        """Persist the session as a JSON record (the canonical, timestamped
        store). Backs the 'Save transcript' button; also called on stop.
        Returns {ok, path, file} on success (the JS renders open/reveal actions
        from it) or False on nothing-to-save / write failure."""
        self._flush_turns()
        if not self._turns:
            # Nothing new in the buffer. But if this session was already
            # auto-saved on stop, re-surface that file (path + open/reveal) so a
            # post-stop "Save transcript" click confirms the save instead of
            # claiming there is nothing to save.
            if self._last_saved_file and os.path.exists(self._last_saved_file):
                if not silent:
                    self._emit_status(t("saved_to", path=self._last_saved_file))
                return {"ok": True, "path": self._last_saved_file,
                        "file": os.path.basename(self._last_saved_file)}
            if not silent:
                self._emit_status(t("no_transcript"))
            return False
        record = self._build_record()
        primary = self._transcript_dir()
        # Save into this session's own folder (same one the recorder wrote its WAVs
        # into), so the whole session stays self-contained. subdir may be None for a
        # save with no active session — save_record then derives it from the record.
        subdir = self._session_dirname
        path = None
        try:
            path = transcript_store.save_record(primary, record, subdir=subdir)
        except OSError:
            # Documents can be blocked (Controlled Folder Access) or unwritable —
            # never lose a transcript: retry into the legacy AppData dir and report
            # THAT path so the user can still find it.
            _log.exception("transcript save to %s failed; retrying legacy dir", primary)
            legacy = legacy_transcripts_dir()
            try:
                path = transcript_store.save_record(legacy, record, subdir=subdir)
            except OSError:
                _log.exception("transcript save failed")
                if not silent:
                    self._emit_status(t("err_save_failed"), "error")
                return False
        self._session_file = path
        if not silent:
            self._emit_status(t("saved_to", path=path))
        return {"ok": True, "path": path, "file": os.path.basename(path)}

    # ---------- transcript directory + reveal ----------
    def _transcript_dir(self) -> str:
        """Active save directory (Documents\\Voxis\\Transcripts by default, or the
        user's configured folder)."""
        return transcripts_dir(self.cfg)

    def _transcript_dirs(self) -> list:
        """Directories to scan for saved sessions: the active dir first, then the
        legacy AppData dir (so pre-move sessions still appear even if migration
        was skipped/partial). Deduped, order-preserving."""
        dirs, seen = [], set()
        for d in (self._transcript_dir(), legacy_transcripts_dir()):
            key = os.path.normcase(os.path.abspath(d))
            if key not in seen:
                seen.add(key)
                dirs.append(d)
        return dirs

    def _safe_transcript_name(self, file: str) -> bool:
        """Reject path traversal + non-transcript files (the bare filename must
        equal its own basename and carry a known extension)."""
        return bool(file) and os.path.basename(file) == file \
            and file.lower().endswith(_TRANSCRIPT_EXTS)

    def _find_transcript(self, file: str) -> str | None:
        """Full path of a saved file, searched across the active + legacy dirs.
        Returns None if the name is unsafe or the file does not exist.

        Handles both the per-session-folder layout (`voxis_<stamp>/<file>`, current)
        and the legacy flat layout (`<file>` directly in the dir). `file` stays a
        bare basename (traversal-guarded); the session subfolder is resolved here,
        never trusted from the caller."""
        if not self._safe_transcript_name(file):
            return None
        for d in self._transcript_dirs():
            path = os.path.join(d, file)
            if os.path.isfile(path):
                return path  # legacy flat
            # Nested: scan this dir's per-session folders for the file.
            try:
                subs = os.listdir(d)
            except OSError:
                continue
            for sub in subs:
                if not sub.startswith("voxis_"):
                    continue
                cand = os.path.join(d, sub, file)
                if os.path.isfile(cand):
                    return cand
        return None

    def _migrate_transcripts(self):
        """One-time move of pre-1.0.26 transcripts from the legacy AppData dir into
        the active (user-facing) dir. On the Store MSIX the legacy read resolves
        through the container's LocalCache view while the write lands in real
        Documents — this also rescues files Windows would delete on uninstall.
        Best-effort: any per-file failure is skipped, never fatal."""
        try:
            import shutil
            src = legacy_transcripts_dir()
            dst = self._transcript_dir()
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
                return
            try:
                names = [n for n in os.listdir(src)
                         if n.startswith("voxis_") and n.lower().endswith(_TRANSCRIPT_EXTS)]
            except OSError:
                return
            if not names:
                return
            os.makedirs(dst, exist_ok=True)
            for n in names:
                target = os.path.join(dst, n)
                if os.path.exists(target):
                    continue  # never clobber a file already in the new location
                try:
                    shutil.move(os.path.join(src, n), target)
                except OSError:
                    pass  # locked / permission — leave it; it still lists via legacy
        except Exception:
            _log.exception("transcript migration skipped")

    def open_transcript(self, file: str) -> dict:
        """Open a saved transcript file in its default app (JSON in the editor,
        etc.). Traversal-guarded; searches the active + legacy dirs."""
        path = self._find_transcript(file)
        if not path:
            return {"ok": False, "error": "not_found"}
        try:
            os.startfile(path)  # noqa: S606 — Windows shell open, path validated
            return {"ok": True}
        except OSError as e:
            _log.exception("open_transcript failed")
            return {"ok": False, "error": str(e)}

    def reveal_transcript(self, file: str) -> dict:
        """Open Explorer with the transcript file selected ('Open containing
        folder'). Also proves to the user where the file actually lives."""
        path = self._find_transcript(file)
        if not path:
            return {"ok": False, "error": "not_found"}
        try:
            import subprocess
            # explorer /select, highlights the file in its folder. Not shell=True;
            # path is validated + normalized so no argument injection is possible.
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return {"ok": True}
        except OSError as e:
            _log.exception("reveal_transcript failed")
            return {"ok": False, "error": str(e)}

    def open_transcript_folder(self) -> dict:
        """Open the active transcript folder in Explorer (creating it if needed)."""
        d = self._transcript_dir()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
            return {"ok": True, "path": d}
        except OSError as e:
            _log.exception("open_transcript_folder failed")
            return {"ok": False, "error": str(e)}

    def choose_transcript_dir(self) -> dict:
        """Folder-picker for a custom transcript directory. Validates writability
        before persisting so a bad choice can't silently break saving."""
        win = self._main_window
        if win is None:
            return {"ok": False, "error": "no_window"}
        try:
            sel = win.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            _log.exception("choose_transcript_dir dialog failed")
            return {"ok": False, "error": "dialog_failed"}
        if not sel:
            return {"ok": False, "cancelled": True}
        folder = sel[0] if isinstance(sel, (list, tuple)) else sel
        # Writability probe: create + remove a temp file so we never persist a
        # directory the app cannot actually write transcripts into.
        probe = os.path.join(folder, ".voxis_write_test")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("")
            os.remove(probe)
        except OSError:
            return {"ok": False, "error": "unwritable"}
        self.cfg["transcript_dir"] = folder
        self._save_cfg()
        return {"ok": True, "path": folder}

    def reset_transcript_dir(self) -> dict:
        """Clear the custom folder override; revert to the built-in default."""
        self.cfg["transcript_dir"] = ""
        self._save_cfg()
        return {"ok": True, "path": self._transcript_dir()}

    # ---------- transcript history + export ----------
    def list_sessions(self) -> list:
        """Newest-first summaries of saved sessions for the history panel, merged
        across the active + legacy dirs (deduped by filename, active wins)."""
        merged, seen = [], set()
        for d in self._transcript_dirs():
            for rec in transcript_store.list_records(d):
                name = rec.get("file")
                if name in seen:
                    continue
                seen.add(name)
                merged.append(rec)
        merged.sort(key=lambda r: r.get("started", 0.0), reverse=True)
        return merged

    def load_session(self, file: str) -> dict | None:
        """Load one saved session's full record. `file` is the bare filename
        returned by list_sessions; path traversal is rejected."""
        if not file or os.path.basename(file) != file or not file.endswith(".json"):
            return None
        path = self._find_transcript(file)
        if not path:
            return None
        try:
            return transcript_store.load_record(path)
        except (OSError, ValueError):
            return None

    def delete_session(self, file: str) -> bool:
        if not file or os.path.basename(file) != file or not file.endswith(".json"):
            return False
        path = self._find_transcript(file)
        if not path:
            return False
        parent = os.path.dirname(path)
        # A self-contained session folder (voxis_<stamp>/ directly under a
        # transcripts dir) is removed whole — JSON + WAVs + caption exports —
        # so deleting a session leaves nothing orphaned. Legacy flat records
        # remove just the single JSON. The grandparent==root check keeps rmtree
        # from ever escaping a known transcripts directory.
        try:
            roots = {os.path.normcase(os.path.abspath(d))
                     for d in self._transcript_dirs()}
            grandparent = os.path.normcase(os.path.abspath(os.path.dirname(parent)))
            if os.path.basename(parent).startswith("voxis_") and grandparent in roots:
                import shutil
                shutil.rmtree(parent)
            else:
                os.remove(path)
            return True
        except OSError:
            return False

    def export_session(self, file: str, fmt: str, bilingual: bool = True) -> dict:
        """Render a saved session to TXT/SRT/VTT next to its JSON. `bilingual`
        keeps the source line alongside the translation (default) or emits a
        translated-only file. Returns {ok, path?, file?, error?}. No tier gating —
        available on every build."""
        if not file or os.path.basename(file) != file or not file.endswith(".json"):
            return {"ok": False, "error": "not_found"}
        src_path = self._find_transcript(file)
        record = self.load_session(file)
        if record is None or src_path is None:
            return {"ok": False, "error": "not_found"}
        bilingual = bool(bilingual)
        try:
            content, ext = transcript_store.export(record, fmt, bilingual=bilingual)
        except ValueError:
            return {"ok": False, "error": "bad_format"}
        # Write the export beside its source JSON (wherever that turned out to be).
        # Bilingual and translated-only variants get distinct names so exporting
        # both formats of the same session never overwrites the other.
        suffix = "_bilingual" if bilingual else ""
        out_path = os.path.join(os.path.dirname(src_path),
                                file[:-len(".json")] + suffix + "." + ext)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            _log.exception("transcript export failed")
            self._emit_status(t("err_save_failed"), "error")
            return {"ok": False, "error": "write_failed"}
        self._emit_status(t("saved_to", path=out_path))
        return {"ok": True, "path": out_path, "file": os.path.basename(out_path)}

    def toggle_overlay(self, on):
        self.cfg["overlay_enabled"] = bool(on)
        self._save_cfg()
        if on and self._overlay_win is None:
            try:
                w, sw, sh = 780, 1920, 1080
                try:
                    import ctypes
                    sw = ctypes.windll.user32.GetSystemMetrics(0)
                    sh = ctypes.windll.user32.GetSystemMetrics(1)
                except Exception:
                    pass
                self._ov_w = w
                self._ov_x = (sw - w) // 2
                self._ov_bottom = int(sh * 0.86)
                self._overlay_win = webview.create_window(
                    "VoxisOverlay", html=_OVERLAY_HTML, frameless=True, on_top=True,
                    width=w, height=84, x=self._ov_x, y=self._ov_bottom - 84,
                    background_color="#0a0b10", js_api=self, hidden=True,
                )
            except Exception:
                self._overlay_win = None
        elif not on:
            self._destroy_overlay()
        return True

    # ---------- virtual cable (meeting mode) ----------
    def meeting_cable_available(self) -> bool:
        """True when a virtual audio cable is installed, so the UI can block
        meeting mode (which routes the translated voice into a virtual mic)
        before start instead of failing mid-launch."""
        try:
            return detect_virtual_cable() is not None
        except Exception:
            return False

    def open_cable_download(self) -> bool:
        """Open the VB-CABLE download page so a user missing the virtual mic can
        install it. Disabled on the official build: Store policy 10.1.5 excludes
        an app from facilitating acquisition of a non-Microsoft driver, so the
        SaaS flavor only informs and the user installs VB-CABLE themselves.
        Returns False if disabled or no system browser could be launched."""
        if IS_OFFICIAL_RELEASE:
            return False
        try:
            import webbrowser
            return webbrowser.open("https://vb-audio.com/Cable/")
        except Exception:
            return False

    # ---------- onboarding tour (modal/JS lives in the web UI) ----------
    def mark_onboarding_done(self) -> bool:
        self.cfg["onboarding_done"] = True
        self._save_cfg()
        return True

    def reset_onboarding(self) -> bool:
        # Backs the "show tour again" link.
        self.cfg["onboarding_done"] = False
        self._save_cfg()
        return True

    # ---------- main-window controls (custom title bar) ----------
    def win_minimize(self):
        try:
            self._main_window.minimize()
        except Exception:
            pass
        return True

    def win_toggle_max(self):
        try:
            if self._maximized:
                self._main_window.restore()
            else:
                self._main_window.maximize()
            self._maximized = not self._maximized
        except Exception:
            pass
        return True

    def win_close(self):
        try:
            self._main_window.destroy()
        except Exception:
            pass
        return True

    def overlay_text(self):
        if time.time() > self._overlay_until:
            return ""
        # Local was named `t`, shadowing the module-level i18n t(); renamed so
        # this method can localize if ever needed.
        return _cap_subtitle(self._overlay_text)

    def overlay_badge(self):
        """Localized attribution text for the overlay footer, or "" when the badge
        is disabled. The overlay window is a separate pywebview document with its
        own JS scope (no access to the main window's I18N dict), so it pulls the
        localized string from here via window.pywebview.api.overlay_badge()."""
        if not self._show_badge():
            return ""
        return t("powered_by")

    def overlay_poll(self):
        """Single combined poll for the overlay window: caption + badge in one
        bridge round-trip (the overlay previously made two separate api calls
        every 150 ms tick — half the crossings for the same data)."""
        return {"text": self.overlay_text(), "badge": self.overlay_badge()}

    def overlay_fit(self, h):
        if self._overlay_win is None:
            return True
        try:
            # Upper clamp allows for the optional attribution footer row.
            h = max(64, min(300, int(h)))
            w = self._ov_w
            self._overlay_win.resize(w, h)
            self._overlay_win.move(self._ov_x, self._ov_bottom - h)
            self._round_overlay()
        except Exception:
            pass
        return True

    def _round_overlay(self):
        """Clips the overlay to a rounded rectangle region (no transparency)."""
        import ctypes
        from ctypes import wintypes
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = u.FindWindowW(None, "VoxisOverlay")
        if not hwnd:
            return
        rect = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        pw, ph = rect.right - rect.left, rect.bottom - rect.top
        if pw <= 0 or ph <= 0:
            return
        radius = min(pw, ph, max(22, ph // 2))
        rgn = g.CreateRoundRectRgn(0, 0, pw + 1, ph + 1, radius, radius)
        u.SetWindowRgn(hwnd, rgn, True)

    def overlay_show(self):
        if self._overlay_win is not None:
            try:
                self._overlay_win.show()
                self._round_overlay()
            except Exception:
                pass
        return True

    def overlay_hide(self):
        if self._overlay_win is not None:
            try:
                self._overlay_win.hide()
            except Exception:
                pass
        return True

    # ---------- sound check (idle-only loopback level probe) ----------
    def soundcheck_start(self):
        """Start the 'do I hear this device?' probe: the same process-exclude
        loopback the session uses, but its only output is a peak level for the
        modal's meter (poll's 'sc' field). Refused while a session runs; auto
        stops after 60 s so an abandoned modal can't hold the capture forever."""
        if self.controller.mode or self._sc is not None:
            return {"ok": self._sc is not None}
        try:
            from .process_loopback import ProcessExcludeLoopback  # noqa: PLC0415

            def on_chunk(pcm):
                try:
                    samples = memoryview(pcm).cast("h")
                    peak = max(abs(s) for s in samples) / 32768.0 if len(samples) else 0.0
                except Exception:
                    return
                # Fast attack, slow decay so short transients stay visible a beat.
                self._sc_level = max(peak, self._sc_level * 0.85)

            self._sc = ProcessExcludeLoopback(on_chunk)
            self._sc.start()
            self._sc_timer = threading.Timer(60.0, self.soundcheck_stop)
            self._sc_timer.daemon = True
            self._sc_timer.start()
            return {"ok": True}
        except Exception:
            _log.exception("soundcheck: could not start loopback probe")
            self._sc = None
            return {"ok": False}

    def soundcheck_stop(self):
        sc, self._sc = self._sc, None
        timer, self._sc_timer = self._sc_timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if sc is not None:
            try:
                sc.stop()
            except Exception:
                pass
        self._sc_level = 0.0
        return True

    # ---------- poll (UI invokes every 150 ms) ----------
    def poll(self):
        evs = []
        try:
            while True:
                evs.append(self._events.get_nowait())
        except queue.Empty:
            pass
        # Lazy: translator pulls google.genai; a module-top import would put the
        # heavy runtime back on the cold start this codebase deliberately avoids.
        from .translator import get_usage  # noqa: PLC0415
        in_sec, _o, usd = get_usage()
        speaking = any(getattr(getattr(p, "_source", None), "speech_active", False)
                       for p in self.controller._pipelines)
        mode = self.controller.mode
        session = (t("session_active", mode=self._mode_name(mode)) if mode
                   else t("session_idle"))
        from .config import resolve_model, route_engine
        eng = (self.controller.current_engine()
               or route_engine(self.cfg, self.cfg.get("target_language_incoming", "")))
        return {
            "events": evs,
            "usage": t("usage_fmt", min=in_sec / 60, usd=usd),
            "badge": {"text": self._badge[0].lstrip("● ").strip(), "color": self._badge[1]},
            "dotcls": self._badge[2],
            "vad": speaking,
            "level": self.controller.current_level(),
            "latency": self.controller.current_latency(),
            "playing": self.controller.is_playing(),
            "mode": mode,
            "engine": eng,
            "model": resolve_model(self.cfg, eng),
            "session": session,
            "maximized": bool(self._maximized),
            # Sound-check meter level (0..1); only meaningful while the probe runs.
            "sc": round(self._sc_level, 3) if self._sc is not None else None,
        }

    # ---------- helpers ----------
    def _mode_name(self, mode):
        return t(f"mode_{mode}").split("  ")[-1] if mode else ""

    def _mark_custom(self):
        if self.cfg.get("active_profile") != "custom":
            self.cfg["active_profile"] = "custom"

    def _maybe_restart(self):
        """Restart the active session to pick up a config change. Debounced: a
        burst of set_cfg calls (e.g. dragging a slider, rapid dropdown changes)
        collapses into a single restart so we don't spawn racing _start threads
        for every intermediate value."""
        if not self.controller.mode:
            return
        self._restart_token += 1
        token = self._restart_token

        def run():
            # Only the most recent restart request survives the debounce window.
            if token != self._restart_token:
                return
            mode = self.controller.mode
            if mode:
                # A restart of an already-running session: consent was necessarily
                # granted to reach this state, so a settings change must not be
                # blocked by the meeting-consent backstop (it would otherwise tear
                # the session down on any config edit when the user declined
                # "don't show again").
                #
                # Go through _start_thread (not _start directly): endpoint switching
                # CoInitializes this throwaway Timer thread, and _start_thread's
                # finally: shutdown_com() releases the apartment + clears the tid on
                # the same thread. Calling _start directly leaked the apartment and
                # left the tid in win_audio's bookkeeping, faulting a later reused
                # tid with CO_E_NOTINITIALIZED.
                self._start_thread(mode, True)

        # daemon: threading.Timer threads are non-daemon by default, and this
        # one's callback runs a FULL session start (network + PortAudio + COM).
        # A user who changed a setting and closed the window inside the debounce
        # left that thread alive after webview.start() returned — a headless
        # zombie process holding the Voxis.SingleInstance mutex, which made the
        # next launch silently refuse to open until the zombie was killed in
        # Task Manager (field report, 2026-07-10).
        t = threading.Timer(0.4, run)
        t.daemon = True
        t.start()

    def _register_hotkeys(self):
        try:
            import keyboard
            keyboard.remove_all_hotkeys()
        except Exception:
            return
        hk = self.cfg.get("hotkeys", {})
        try:
            for mode in ("video", "meeting"):
                if hk.get(mode):
                    keyboard.add_hotkey(hk[mode], lambda m=mode: self._hotkey(m))
            if hk.get("stop"):
                keyboard.add_hotkey(hk["stop"], lambda: self._hotkey("stop"))
            if hk.get("overlay"):
                keyboard.add_hotkey(hk["overlay"], lambda: self._hotkey("overlay"))
        except Exception:
            pass

    def _hotkey(self, action):
        if action == "stop":
            if self.controller.mode:
                self.stop()
        elif action == "overlay":
            self.toggle_overlay(self._overlay_win is None)
        elif not self.controller.mode:
            self.start(action)


_OVERLAY_HTML = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<style>
/* Graphite Console language (see index.html): flat graphite, hairline border,
   amber = live/on-air signal. System font on purpose — the overlay is a
   transient always-on-top window and must not stall on a webfont fetch. */
html,body{margin:0;height:100%;overflow:hidden;background:#131518;
  font-family:'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif;-webkit-user-select:none;cursor:default}
#bar{display:flex;align-items:center;gap:16px;min-height:100%;box-sizing:border-box;
  padding:14px 22px;-webkit-app-region:drag;
  background:#16181c;border:1px solid rgba(235,240,245,.12)}
#mark{width:34px;height:34px;flex:none;border-radius:8px;display:grid;place-items:center;
  background:#e9eef3}
#divider{width:3px;align-self:stretch;flex:none;border-radius:3px;margin:2px 0;
  background:#ffb224;box-shadow:0 0 9px rgba(255,178,36,.55)}
#txt{flex:1;color:#f2f5f8;font-size:25px;font-weight:600;line-height:1.34;
  text-shadow:0 1px 5px rgba(0,0,0,.55);max-height:101px;overflow:hidden}
#col{flex:1;display:flex;flex-direction:column;gap:3px;min-width:0}
#brand{font-size:10.5px;font-weight:600;letter-spacing:.12em;color:rgba(233,238,243,.44);
  text-shadow:0 1px 3px rgba(0,0,0,.5);display:none}
</style></head><body>
<div id='bar'>
  <div id='mark'><svg width='19' height='19' viewBox='0 0 1075.8 1075.8'><path fill='#131518' d='M89.65 332.95 L278.17 301.19 L367.78 737.34 L268.17 888.72 Z'/><path fill='#FFB224' d='M219.39 684.87 C278.89 597.47 341.64 519.58 421.23 455.83 C556.55 347.46 794.02 246.48 986.15 187.08 C878.25 251.89 758.69 321.26 640.05 417.53 C543.00 492.74 431.90 634.48 344.32 785.53 L320.60 575.30 Z'/></svg></div>
  <div id='divider'></div>
  <div id='col'>
    <div id='txt'></div>
    <div id='brand'></div>
  </div>
</div>
<script>
const txt=document.getElementById('txt'); const brand=document.getElementById('brand');
let vis=false, lastH=0, lastBrand=null, fast=false;
function fit(){
  txt.scrollTop = txt.scrollHeight;
  const h=Math.ceil(document.getElementById('bar').scrollHeight);
  if(Math.abs(h-lastH)>3){ lastH=h; try{window.pywebview.api.overlay_fit(h);}catch(e){} }
}
async function p(){
  try{
    // One combined bridge call per tick: caption + attribution badge together.
    const r=await window.pywebview.api.overlay_poll();
    const b=(r&&r.badge)||'';
    if(b!==lastBrand){ lastBrand=b; brand.textContent=b||''; brand.style.display=b?'block':'none'; requestAnimationFrame(fit); }
    const x=(r&&r.text)||'';
    fast = !!x;
    if(x){
      if(txt.textContent!==x){ txt.textContent=x; requestAnimationFrame(fit); }
      if(!vis){ vis=true; window.pywebview.api.overlay_show(); }
    } else if(vis){ vis=false; window.pywebview.api.overlay_hide(); }
  }catch(e){}
  // Adaptive: 70 ms while a caption is on screen (subtitle sync is part of the
  // latency budget), 200 ms while blank.
  setTimeout(p, fast?70:200);
}
window.addEventListener('pywebviewready',p);setTimeout(p,400);
</script></body></html>"""


def _set_taskbar_icon(icon_path: str, title: str):
    """Sets an explicit AppUserModelID and updates the window icon so the
    process is grouped under Voxis (not python.exe) in the taskbar."""
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Voxis.App.1")
    except Exception:
        pass

    def apply():
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x80, 0, 1
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
        u = ctypes.windll.user32
        for _ in range(40):
            hwnd = u.FindWindowW(None, title)
            if hwnd:
                for size, which in ((32, ICON_BIG), (16, ICON_SMALL)):
                    hicon = u.LoadImageW(0, icon_path, IMAGE_ICON, size, size,
                                         LR_LOADFROMFILE)
                    if hicon:
                        u.SendMessageW(hwnd, WM_SETICON, which, hicon)
                return
            time.sleep(0.25)

    threading.Thread(target=apply, daemon=True).start()


_shutting_down = threading.Lock()


def _shutdown(bridge, grace: float = 8.0):
    """Stop any live session, then GUARANTEE process death.

    Called from the normal exit (webview.start() returned) and from the closing
    watchdog. The stop path crosses COM (endpoint restore), PortAudio teardown
    and the network; any of those wedging after the window is gone would leave a
    headless zombie still holding the Voxis.SingleInstance mutex, so the app
    "won't reopen until killed in Task Manager" (field report, 2026-07-10). Run
    the stop on a daemon thread with a bounded grace, then hard-exit: normal
    interpreter shutdown would itself wait on any straggler non-daemon thread.
    Whoever gets here first owns the teardown; the loser just returns.
    """
    if not _shutting_down.acquire(blocking=False):
        return

    def _stop_session():
        try:
            if bridge.controller.mode:
                bridge.controller.stop()
        except Exception:
            _log.debug("final session stop failed", exc_info=True)

    t = threading.Thread(target=_stop_session, daemon=True,
                         name="voxis-final-cleanup")
    t.start()
    t.join(grace)  # final heartbeat + endpoint restore normally take <2 s
    try:
        logging.shutdown()  # flush voxis.log before the hard exit
    except Exception:
        pass
    os._exit(0)


def run(cfg):
    bridge = Bridge(cfg)
    # Auto-select the virtual cable in the background so device enumeration
    # doesn't block the window from appearing.
    threading.Thread(target=_autofill_meeting_devices, args=(cfg,),
                     daemon=True).start()
    # Flush any problem reports queued from a previous offline send. App start is
    # an explicit user context (not a silent background flush) and the payloads
    # already carry the user's original consent. Best-effort, off the UI thread.
    threading.Thread(target=bridge.flush_reports, daemon=True).start()
    icon = icon_path()
    if os.path.exists(icon):
        _set_taskbar_icon(icon, t("app_title"))
    # Restore saved window geometry (size/position), clamped to the minimum.
    geo = cfg.get("window") if isinstance(cfg.get("window"), dict) else {}

    def _geo_num(v, default):
        # A hand-edited / corrupt config with a non-numeric w/h must not crash the
        # launch — fall back to the default like the x/y restore below already does.
        try:
            return int(v)
        except (TypeError, ValueError):
            return default
    win_w = max(_geo_num(geo.get("w"), 1180), 940)
    win_h = max(_geo_num(geo.get("h"), 760), 600)
    geo_kwargs = {}
    if isinstance(geo.get("x"), int) and isinstance(geo.get("y"), int):
        # Only restore the saved position if it still lands on a connected
        # display; otherwise (unplugged monitor / dock change) let pywebview
        # center the window so it can't open off-screen and invisible.
        gx, gy = geo["x"], geo["y"]
        try:
            import ctypes
            u = ctypes.windll.user32
            SM_XV, SM_YV, SM_CXV, SM_CYV = 76, 77, 78, 79  # virtual-screen metrics
            vx, vy = u.GetSystemMetrics(SM_XV), u.GetSystemMetrics(SM_YV)
            vw, vh = u.GetSystemMetrics(SM_CXV), u.GetSystemMetrics(SM_CYV)
            on_screen = (vx - 8 <= gx <= vx + vw - 100) and (vy - 8 <= gy <= vy + vh - 80)
        except Exception:
            on_screen = True  # fail-open: trust the saved coords if probing fails
        if on_screen:
            geo_kwargs["x"], geo_kwargs["y"] = gx, gy
    window = webview.create_window(
        t("app_title"), os.path.join(WEB_DIR, "index.html"),
        js_api=bridge, width=win_w, height=win_h, min_size=(940, 600),
        background_color="#0b0c10", frameless=True, easy_drag=False,
        resizable=True, **geo_kwargs,
    )
    bridge._main_window = window
    bridge._win_geom = {"w": win_w, "h": win_h, **{k: geo[k] for k in ("x", "y") if k in geo_kwargs}}
    # Persist size/position/maximized across launches.
    try:
        window.events.resized += bridge._on_win_resized
        window.events.moved += bridge._on_win_moved
        window.events.maximized += bridge._on_win_maximized
        window.events.minimized += bridge._on_win_minimized
        window.events.restored += bridge._on_win_restored
        window.events.closing += bridge._on_win_closing
    except Exception:
        pass
    if geo.get("max"):
        def _restore_max():
            try:
                window.maximize()
                bridge._maximized = True
            except Exception:
                pass
        try:
            window.events.shown += _restore_max
        except Exception:
            pass
    bridge._register_hotkeys()
    if cfg.get("overlay_enabled"):
        bridge.toggle_overlay(True)
    kwargs = {}
    if os.path.exists(icon):
        kwargs["icon"] = icon
    try:
        webview.start(**kwargs)
    except TypeError:
        # Older pywebview without the icon parameter.
        webview.start()
    _shutdown(bridge)
