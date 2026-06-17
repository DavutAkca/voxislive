"""pywebview bridge between the web UI and the Python audio engine.

JavaScript invokes Bridge methods through `window.pywebview.api`; the UI polls
`poll()` every 150 ms to drain status, translation and telemetry events.
"""
import logging
import os
import queue
import sys
import threading
import time

import webview

from . import APP_VERSION, i18n, updater
from .audio_io import detect_virtual_cable, find_device, list_device_names, resolve_name
from .config import (
    GEMINI_VOICES,
    IS_OFFICIAL_RELEASE,
    PROFILES,
    QUALITY_PRESETS,
    apply_profile,
    save_config,
)
from .i18n import t
from .pipeline import ModeController
from .paths import icon_path, user_path, web_dir
from .translator import get_usage

WEB_DIR = web_dir()
TRANSCRIPT_DIR = user_path("transcripts")
OBS_FILE = user_path("obs_subtitle.txt")

LANGS = ["tr", "en", "de", "fr", "es", "it", "pt", "ru", "ar", "ja", "ko", "zh-Hans"]
LINE_GAP = 2.5
FADE_MS = 6.0
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
    no longer resolves to a present device — a deliberate, valid choice is kept."""
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
    if play_ok and rec_ok:
        return
    found = detect_virtual_cable()
    if not found:
        return
    play, rec = found
    if not play_ok:
        devs["meeting_mic_playback"] = play
    if not rec_ok:
        devs["meeting_virtual_mic"] = rec
    save_config(cfg)


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        # Set on successful auth; used as the index into the BYOK store.
        self._user_id: str | None = None
        i18n.set_language(cfg.get("ui_language", "tr"))

        self._events: queue.Queue = queue.Queue()
        self.controller = ModeController(
            cfg, None, self._on_text, self._on_status,
            on_usage_reported=self._on_usage_reported,
        )

        self._lines, self._cur_line = [], ""
        self._last_t = 0.0
        # Source-transcription stream, paired to translation turns best-effort.
        self._cur_src, self._last_src = "", ""
        self._last_src_t = 0.0
        self._session_file = None
        self._overlay_win = None
        self._overlay_text = ""
        self._overlay_until = 0.0
        self._maximized = False
        self._badge = (t("badge_idle"), "#8593a6", "")
        self._update_manifest = None
        self._update_checked = False
        # Assigned in run() once the main window exists; referenced by
        # win_* controls and apply_update before then, so default to None.
        self._main_window = None
        # Serializes the session lifecycle: start/stop/_maybe_restart all run on
        # background threads, so without this a rapid start→stop or a flurry of
        # set_cfg restarts could spawn racing _start threads against one
        # controller. _lifecycle holds the lock for the duration of one
        # transition; _restart_token debounces set_cfg-driven restarts.
        self._lifecycle = threading.Lock()
        self._restart_token = 0
        self._last_obs_write = None
        self._hotkey_cancel = False

    # ---------- callbacks from audio threads ----------
    def _on_text(self, direction, text):
        now = time.time()
        if direction == "in":
            # Input transcription (what the speaker said). Accumulate per utterance
            # so a completed source can be paired with the translation turn it
            # produced. No UI event here — the source caption is attached when the
            # matching translation turn finalizes.
            if self._cur_src and (now - self._last_src_t) > LINE_GAP:
                self._last_src = self._cur_src.strip()
                self._cur_src = ""
            self._cur_src += text
            self._last_src_t = now
            return
        # direction == "out": the translated text stream.
        newline = bool(self._cur_line) and (now - self._last_t) > LINE_GAP
        if newline:
            self._lines.append(self._cur_line.strip())
            self._cur_line = ""
            # The turn that just ended pairs with the most recently completed
            # source utterance (correct by ordering despite the few-second lag).
            src = (self._last_src or self._cur_src).strip()
            if src:
                self._events.put(("src", src))
            self._last_src = ""
        self._cur_line += text
        self._last_t = now
        self._overlay_text = self._cur_line.strip()
        self._overlay_until = now + FADE_MS
        self._obs_write(self._cur_line.strip())
        self._events.put(("trans", text, newline))

    def _emit_status(self, msg, level="info"):
        """Push a status line to the UI.

        Carries an explicit level so the front end (and the error badge) never
        has to infer severity by sniffing a localized 'HATA:'/'ERROR' prefix.
        The legacy positional payload (the message string) is preserved so the
        existing JS poll handler keeps working; the structured fields ride
        alongside for callers that read them."""
        self._events.put(("status", msg, {"level": level, "msg": msg}))
        if level == "error":
            self._badge = (t("badge_error"), "#fb7185", "err")

    def _on_status(self, msg):
        # ModeController only forwards a localized string. Treat its events as
        # informational; error-badge state is set explicitly by the paths that
        # actually fail (e.g. _start), not by parsing a translated prefix.
        self._emit_status(msg, "info")

    def _on_usage_reported(self):
        self._events.put(("quota_refresh", None))

    def _obs_write(self, text):
        if not self.cfg.get("obs_subtitle_enabled"):
            return
        text = _cap_subtitle(text)
        # Only rewrite when the content actually changed — the translation stream
        # repaints the same line on every token, and an OBS text source re-reads
        # on file mtime, so skipping no-op writes avoids needless flicker/IO.
        if text == self._last_obs_write:
            return
        try:
            with open(OBS_FILE, "w", encoding="utf-8") as f:
                f.write(text)
            self._last_obs_write = text
        except OSError:
            pass

    # ---------- JS-facing API ----------
    def get_init(self):
        outs = list_device_names("output") or ["—"]
        mics = list_device_names("input")
        from . import byok_store
        byok_set = byok_store.has_byok(self._user_id) if self._user_id else False
        self._start_update_check()
        return {
            "version": APP_VERSION,
            "outputs": outs,
            "mics": [t("default_mic")] + mics,
            "langs": LANGS,
            "profiles": [[k, t(f"profile_{k}")] for k in ("custom", "meeting", "film", "conference")],
            "qualities": self._quality_options(),
            "gemini_voices": GEMINI_VOICES,
            "byok_set": byok_set,
            "official_release": IS_OFFICIAL_RELEASE,
            "onboarding_done": bool(self.cfg.get("onboarding_done", False)),
            "cfg": self._cfg_view(outs, mics),
        }

    def _quality_options(self):
        """End-user build sees two friendly choices (smooth vs savings); the
        developer build sees the full preset list for tuning."""
        if IS_OFFICIAL_RELEASE:
            return [["balanced", t("quality_smooth")],
                    ["turbo", t("quality_fast")],
                    ["max_savings", t("quality_saver")]]
        return [[k, t(f"quality_{k}")] for k in QUALITY_PRESETS]

    # ---------- auto-update ----------
    def _start_update_check(self):
        """Background check on first init; pushes an 'update' event if a newer
        version is available. Only active in frozen builds."""
        # Cost-isolation guarantee: an OSS build must not beacon to voxislive.com
        # on launch. Restrict the update check to the official SaaS build, or to
        # a developer who explicitly opted in via cfg['auto_update_enabled'].
        if not (IS_OFFICIAL_RELEASE or self.cfg.get("auto_update_enabled")):
            return
        if self._update_checked or not updater.available():
            return
        self._update_checked = True

        def work():
            try:
                manifest = updater.check(self.cfg.get("update_check_url", ""))
            except Exception:
                manifest = None
            if manifest:
                self._update_manifest = manifest
                ver = manifest.get("version", "")
                self._events.put(("update", {
                    "version": ver,
                    "notes": manifest.get("notes", ""),
                    "mandatory": bool(manifest.get("mandatory")),
                    "title": t("update_title", version=ver),
                    "btn": t("update_btn"),
                    "later": t("update_later"),
                }))

        threading.Thread(target=work, daemon=True).start()

    def apply_update(self):
        """Download (verifying checksum) and launch the silent installer, then
        close the app so the installer can replace files and relaunch it."""
        manifest = self._update_manifest
        if not manifest:
            return {"ok": False, "error": "no_update"}
        try:
            self._emit_status(t("update_downloading"))
            path = updater.download(manifest)
            updater.launch_installer(path)
        except Exception as e:
            _log.exception("update apply failed")
            self._emit_status(t("update_failed", err=str(e)), "error")
            return {"ok": False, "error": str(e)}
        # Hand off to the installer: close the window so locked files are
        # released. If destroy fails the process MUST still exit, otherwise the
        # installer cannot replace the locked binary — force it rather than
        # silently swallowing the failure and leaving a half-updated install.
        try:
            if self._main_window:
                self._main_window.destroy()
        except Exception:
            _log.exception("apply_update: window destroy failed; forcing exit")
            try:
                sys.exit(0)
            except SystemExit:
                os._exit(0)
        return {"ok": True}

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
        return c

    def get_cfg(self):
        return self._cfg_view()

    def _save_cfg(self) -> bool:
        """Persist config, surfacing (not swallowing) a write failure so the UI
        can warn instead of silently losing the setting."""
        try:
            save_config(self.cfg)
            return True
        except OSError:
            _log.exception("config save failed")
            self._emit_status(t("err_save_failed"), "error")
            return False

    def set_cfg(self, key, value):
        self.cfg[key] = value
        if key == "ui_language":
            i18n.set_language(value)
        if key == "duck_gain":
            self.controller.set_duck_gain(float(value))
            self._mark_custom()
        elif key == "tts_volume":
            self.controller.set_tts_volume(float(value))
        elif key in ("quality_preset", "target_language_incoming",
                     "target_language_outgoing", "gemini_voice"):
            if key == "quality_preset":
                self._mark_custom()
            self._maybe_restart()
        return self._save_cfg()

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

    def save_keys(self, gem):
        # Official-release builds never expose BYOK entry; refuse silently as
        # a defense-in-depth check.
        if IS_OFFICIAL_RELEASE:
            return False
        uid = self._ensure_user_id()
        if not uid:
            return False
        from . import byok_store
        current = byok_store.load_byok(uid)
        new_gem = gem.strip() if gem and gem.strip() else current.get("gemini", "")
        byok_store.save_byok(uid, new_gem)
        return True

    def clear_byok(self) -> bool:
        if IS_OFFICIAL_RELEASE:
            return False
        uid = self._ensure_user_id()
        if not uid:
            return False
        from . import byok_store
        byok_store.clear_byok(uid)
        return True

    def check_auth(self) -> dict:
        """Page-load auth check. Returns {authenticated, quota}. Non-blocking —
        uses the cached JWT for identity."""
        if not IS_OFFICIAL_RELEASE:
            return {"authenticated": True, "quota": None}
        from . import voxis_client
        jwt = voxis_client.get_jwt()
        if not jwt:
            return {"authenticated": False, "quota": None}
        self._user_id = voxis_client.user_id_from_jwt()
        info, _ = voxis_client.verify_session()
        if not info:
            return {"authenticated": False, "quota": None}
        return {"authenticated": True, "quota": info}

    def voxis_register(self, email: str, password: str) -> dict:
        if not IS_OFFICIAL_RELEASE:
            return {"ok": False, "quota": None, "error": "Registration is disabled in developer builds."}
        from . import voxis_client
        token, err = voxis_client.auth_register(email, password)
        if not token:
            return {"ok": False, "quota": None, "error": err or "Registration failed."}
        self._user_id = voxis_client.user_id_from_jwt()
        info, verr = voxis_client.verify_session()
        if not info:
            return {"ok": False, "quota": None, "error": verr or t("err_start_failed")}
        return {"ok": True, "quota": info, "error": None}

    def open_url(self, url: str) -> bool:
        # Allowlist http/https only so a crafted bridge call can never launch
        # file:, javascript: or other handler schemes via the default browser.
        import webbrowser
        from urllib.parse import urlparse
        try:
            parts = urlparse(url)
        except Exception:
            return False
        if parts.scheme not in ("http", "https"):
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
        return {"ok": True, "quota": info, "error": None}

    def voxis_quota(self) -> dict | None:
        if not IS_OFFICIAL_RELEASE:
            return None
        from . import voxis_client
        return voxis_client.get_quota()

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
        threading.Thread(target=self._start, args=(mode, bool(consented)), daemon=True).start()
        return True

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

    def _quota_ok(self) -> bool:
        """Cached-quota precheck so the local hotkey path cannot kick off a
        session the server would immediately reject. OSS builds have no quota.
        A None/unreachable quota is treated as allowed — the authoritative
        refusal still happens server-side in get_session_key()."""
        if not IS_OFFICIAL_RELEASE:
            return True
        from . import voxis_client
        quota = voxis_client.get_quota()
        if not quota or quota.get("unlimited"):
            return True
        remaining = quota.get("remaining")
        if remaining is None:
            remaining = (quota.get("allowed_minutes", 0.0)
                         - quota.get("used_minutes", 0.0))
        if remaining <= 0:
            self._emit_status(t("err_quota_exhausted"), "error")
            return False
        return True

    def _resolve_api_key(self) -> str:
        """Returns the Gemini API key.

        On official-release builds the SaaS path is forced — BYOK is never
        consulted regardless of any stored values. On open-source builds the
        BYOK store takes precedence so devs can use their own key.

        Raises RuntimeError with a user-actionable message when no key can be
        resolved, so callers never receive None silently.
        """
        if not IS_OFFICIAL_RELEASE:
            uid = self._ensure_user_id()
            if uid:
                from . import byok_store
                keys = byok_store.load_byok(uid)
                if keys.get("gemini"):
                    return keys["gemini"]
            raise RuntimeError(t("st_no_key_offline"))
        from . import voxis_client
        # Re-verify on every session start so the server-side cache entry is
        # fresh. check_auth() at page-load may have run minutes earlier.
        info, verr = voxis_client.verify_session()
        if not info:
            raise RuntimeError(verr or t("st_not_signed_in"))
        # Quota pre-check with the freshly verified snapshot.
        if not self._quota_ok():
            raise RuntimeError(t("err_quota_exhausted"))
        key, err = voxis_client.get_session_key()
        if not key:
            raise RuntimeError(err or t("st_no_key"))
        return key

    def _start(self, mode, consented=False):
        # Single-flight: serialize the whole transition so a rapid start→stop or
        # a burst of set_cfg restarts can never run two _start bodies against one
        # controller. start() is thereby idempotent for the active mode.
        with self._lifecycle:
            if not self._consent_ok(mode, consented):
                return
            if not self._cable_ok(mode):
                return
            self._badge = (t("badge_connecting"), "#fbbf24", "warn")
            try:
                gem_key = self._resolve_api_key()
                if not gem_key:
                    raise RuntimeError(t("st_no_key"))
                self.controller.api_key = gem_key
                self.controller.start(mode)
                self._badge = (t("badge_active", mode=self._mode_name(mode)), "#34d399", "on")
            except Exception as e:
                # Log the raw exception; surface a localized message to the UI
                # rather than forwarding str(e) (which may be an English/library
                # string) into the user-facing transcript.
                _log.exception("session start failed (mode=%s)", mode)
                self._emit_status(self._start_error_message(e), "error")

    def _start_error_message(self, exc) -> str:
        """Map a start failure to a localized, user-actionable message. A
        RuntimeError we raised already carries a localized string; anything else
        is an unexpected fault and gets a generic localized line."""
        if isinstance(exc, RuntimeError) and str(exc):
            return str(exc)
        return t("err_start_failed")

    def stop(self):
        threading.Thread(target=self._stop, daemon=True).start()
        return True

    def _stop(self):
        # Idempotent: serialized against _start so a stop racing a start cannot
        # tear down a half-built session, and a redundant stop is a no-op.
        with self._lifecycle:
            self.save_txt(silent=True)
            self.controller.stop()
            self._overlay_text = ""
            self._badge = (t("badge_idle"), "#8593a6", "")

    def save_txt(self, silent=False):
        lines = list(self._lines)
        if self._cur_line.strip():
            lines.append(self._cur_line.strip())
        if not lines:
            if not silent:
                self._emit_status(t("no_transcript"))
            return False
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        if not self._session_file:
            self._session_file = os.path.join(
                TRANSCRIPT_DIR, time.strftime("ceviri_%Y-%m-%d_%H-%M-%S.txt"))
        try:
            with open(self._session_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            _log.exception("transcript save failed")
            if not silent:
                self._emit_status(t("err_save_failed"), "error")
            return False
        if not silent:
            self._emit_status(t("saved_to", path=self._session_file))
        return True

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
        elif not on and self._overlay_win is not None:
            try:
                self._overlay_win.destroy()
            except Exception:
                pass
            self._overlay_win = None
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
        install it. Returns False if no system browser could be launched."""
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

    def overlay_fit(self, h):
        if self._overlay_win is None:
            return True
        try:
            h = max(64, min(260, int(h)))
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

    # ---------- poll (UI invokes every 150 ms) ----------
    def poll(self):
        evs = []
        try:
            while True:
                evs.append(self._events.get_nowait())
        except queue.Empty:
            pass
        in_sec, _o, usd = get_usage()
        speaking = any(getattr(getattr(p, "_source", None), "speech_active", False)
                       for p in self.controller._pipelines)
        mode = self.controller.mode
        session = (t("session_active", mode=self._mode_name(mode)) if mode
                   else t("session_idle"))
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
            "session": session,
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
                self._start(mode, consented=True)

        threading.Timer(0.4, run).start()

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
html,body{margin:0;height:100%;overflow:hidden;background:#0a0b10;
  font-family:'Inter','Segoe UI',sans-serif;-webkit-user-select:none;cursor:default}
#bar{display:flex;align-items:center;gap:16px;min-height:100%;box-sizing:border-box;
  padding:14px 22px;-webkit-app-region:drag;
  background:linear-gradient(180deg,#13151d,#0c0e14)}
#mark{width:34px;height:34px;flex:none;border-radius:9px;display:grid;place-items:center;
  background:linear-gradient(135deg,#7c8aff,#5b6cff);box-shadow:0 3px 14px rgba(91,108,255,.5)}
#divider{width:3px;align-self:stretch;flex:none;border-radius:3px;margin:2px 0;
  background:linear-gradient(180deg,#7c8aff,#5b6cff);box-shadow:0 0 10px rgba(124,138,255,.6)}
#txt{flex:1;color:#fff;font-size:25px;font-weight:600;line-height:1.34;
  text-shadow:0 1px 5px rgba(0,0,0,.55);max-height:101px;overflow:hidden}
</style></head><body>
<div id='bar'>
  <div id='mark'><svg width='19' height='19' viewBox='0 0 16 16'><path d='M2 5.5v5M5 3v10M8 6.5v3M11 3v10M14 5.5v5' stroke='#fff' stroke-width='1.9' stroke-linecap='round'/></svg></div>
  <div id='divider'></div>
  <div id='txt'></div>
</div>
<script>
const txt=document.getElementById('txt'); let vis=false, lastH=0;
function fit(){
  txt.scrollTop = txt.scrollHeight;
  const h=Math.ceil(document.getElementById('bar').scrollHeight);
  if(Math.abs(h-lastH)>3){ lastH=h; try{window.pywebview.api.overlay_fit(h);}catch(e){} }
}
async function p(){
  try{
    const x=await window.pywebview.api.overlay_text();
    if(x){
      if(txt.textContent!==x){ txt.textContent=x; requestAnimationFrame(fit); }
      if(!vis){ vis=true; window.pywebview.api.overlay_show(); }
    } else if(vis){ vis=false; window.pywebview.api.overlay_hide(); }
  }catch(e){}
  setTimeout(p,150);
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


def run(cfg):
    bridge = Bridge(cfg)
    # Auto-select the virtual cable in the background so device enumeration
    # doesn't block the window from appearing.
    threading.Thread(target=_autofill_meeting_devices, args=(cfg,),
                     daemon=True).start()
    icon = icon_path()
    if os.path.exists(icon):
        _set_taskbar_icon(icon, t("app_title"))
    window = webview.create_window(
        t("app_title"), os.path.join(WEB_DIR, "index.html"),
        js_api=bridge, width=1180, height=760, min_size=(940, 600),
        background_color="#0b0c10", frameless=True, easy_drag=False,
    )
    bridge._main_window = window
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
    # When the main window is destroyed (X or Alt+F4) ensure any active session
    # is stopped so the final usage report reaches the server.
    try:
        if bridge.controller.mode:
            bridge.controller.stop()
    except Exception:
        pass
