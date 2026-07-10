"""Canlı Çeviri — giriş noktası (web tabanlı arayüz, pywebview).

Kullanım: .venv aktifken `python main.py` (veya basla.bat).
API anahtarları artık .env'de saklanmaz; kullanıcı başına BYOK (şifreli) veya
SaaS yolu (sunucudan oturum anahtarı) üzerinden çözümlenir.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from app import i18n
from app.config import load_config, save_config
from app.paths import user_path

# WebView2 Evergreen Runtime client id (Microsoft-documented). Its presence with
# a real `pv` version under EdgeUpdate\\Clients means the runtime is installed.
_WEBVIEW2_CLIENT = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# Named-mutex handle kept alive for the whole process so the single-instance
# guard holds until exit (releasing it would let a second copy start).
_INSTANCE_MUTEX = None


def _acquire_single_instance() -> bool:
    """Session-local single-instance guard. Two concurrent Voxis processes mean
    two loopback captures, dueling default-endpoint switches, config.json races
    and doubled usage heartbeats — so the second launch focuses the existing
    window and exits instead. Fail-open: any unexpected error allows startup
    (a working install must never be blocked by the guard itself)."""
    global _INSTANCE_MUTEX
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.CreateMutexW(None, False, "Voxis.SingleInstance")
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        if not handle:
            return True
        if not already:
            _INSTANCE_MUTEX = handle  # hold for process lifetime
            return True
        k32.CloseHandle(handle)
        # Bring the running instance to the front (best-effort). If the mutex is
        # held but NO window exists, the "instance" is a headless zombie from a
        # wedged shutdown (see webui.run's hard-exit note) — blocking startup on
        # it would make the app unopenable until the user finds Task Manager, so
        # fail open and start anyway (the guard's own contract: a working
        # install must never be blocked by the guard itself).
        try:
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, i18n.t("app_title"))
            if hwnd:
                u.ShowWindow(hwnd, 9)  # SW_RESTORE
                u.SetForegroundWindow(hwnd)
                logging.getLogger("voxis").info(
                    "second instance blocked; focused existing window")
                return False
        except Exception:
            return False  # couldn't probe: assume a real instance, stay safe
        logging.getLogger("voxis").warning(
            "single-instance mutex held but no window found — stale zombie "
            "process; starting anyway")
        return True
    except Exception:
        return True


def _setup_logging():
    """Route logs to %APPDATA%\\Voxis\\voxis.log (the same file voxis_client
    appends network detail to). Without this the root logger has no handler, so a
    session-start traceback (_log.exception in app/webui.py) is lost in a windowed
    .exe — the exact failure we cannot otherwise diagnose on a user's machine.

    Idempotent: re-invocation never stacks duplicate handlers."""
    root = logging.getLogger()
    if any(getattr(h, "_voxis_file", False) for h in root.handlers):
        return
    try:
        handler = RotatingFileHandler(
            user_path("voxis.log"), maxBytes=512 * 1024, backupCount=2,
            encoding="utf-8", delay=True,
        )
    except OSError:
        return  # never let logging setup itself block startup
    handler._voxis_file = True  # marker for the idempotency guard above
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    # Root stays at WARNING so noisy libraries are quiet; our own namespace logs
    # at INFO so connection/lifecycle breadcrumbs accompany the eventual traceback.
    root.setLevel(logging.WARNING)
    logging.getLogger("voxis").setLevel(logging.INFO)


def _webview2_present() -> bool:
    """True if the Edge WebView2 Evergreen Runtime is installed.

    Reads the Microsoft-documented EdgeUpdate client key in all three locations
    (per-machine 64-bit WOW6432Node view, per-machine native view, per-user) and
    requires a real `pv` version — a stale key left behind by an uninstall holds
    "0.0.0.0" and must not count as installed (MS distribution docs).

    Fail-open: any unexpected error returns True so a genuinely working install
    is never blocked by a false negative."""
    try:
        import winreg
    except ImportError:
        return True  # non-Windows: pywebview uses a different backend
    subkey = "SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\" + _WEBVIEW2_CLIENT
    subkey_wow = "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\" + _WEBVIEW2_CLIENT
    probes = [
        (winreg.HKEY_LOCAL_MACHINE, subkey_wow, 0),
        (winreg.HKEY_LOCAL_MACHINE, subkey, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_CURRENT_USER, subkey, 0),
    ]
    for hive, path, flag in probes:
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | flag) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and str(pv).strip() not in ("", "0.0.0.0"):
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return False


def _preflight_webview2() -> bool:
    """Block start with a friendly native dialog when the WebView2 runtime is
    missing, instead of letting pywebview show a blank window. Returns True when
    startup may proceed, False when the user should be sent to the download page.

    Windows-only and fail-open: on any other OS, or if detection itself fails,
    this returns True so a working install never sees a dialog."""
    if sys.platform != "win32":
        return True
    if _webview2_present():
        return True
    logging.getLogger("voxis").warning("WebView2 runtime not detected at startup")
    import ctypes

    MB_YESNO = 0x4
    MB_ICONWARNING = 0x30
    MB_SETFOREGROUND = 0x10000
    IDYES = 6
    title = i18n.t("webview2_missing_title")
    body = i18n.t("webview2_missing_body")
    try:
        choice = ctypes.windll.user32.MessageBoxW(
            None, body, title, MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND)
    except Exception:
        return True  # never let the dialog itself block a usable install
    if choice == IDYES:
        try:
            import webbrowser
            webbrowser.open(_WEBVIEW2_DOWNLOAD)
        except Exception:
            pass
    return False


def main():
    load_dotenv()
    _setup_logging()

    cfg = load_config()
    i18n.set_language(cfg.get("ui_language", "tr"))

    # Single-instance: a second copy would double captures/heartbeats and fight
    # over default endpoints — focus the running window and exit instead.
    if not _acquire_single_instance():
        return

    # Pre-flight: pywebview needs the Edge WebView2 runtime; without it the window
    # renders blank. Show a friendly native prompt + download link and exit early
    # rather than leaving the user staring at an empty frame. Fail-open by design.
    if not _preflight_webview2():
        return

    # Önceki oturum çöktüyse sistem ses cihazları sanal kabloda kalmış olabilir
    pending = cfg.get("_pending_default_restore")
    if pending:
        try:
            from app import win_audio
            win_audio.restore(pending)
        except Exception:
            # Restore failed (COM hiccup, device briefly absent): KEEP the
            # snapshot so the next launch retries, instead of stranding the
            # user's default endpoints on the virtual cable forever.
            pass
        else:
            cfg["_pending_default_restore"] = None
            save_config(cfg)

    # A crash mid-duck leaves OTHER apps' session volumes lowered — and Windows
    # persists per-app levels, so without this they'd stay quiet forever. The
    # snapshot restore runs on its own thread + COM apartment (the main thread's
    # apartment belongs to pywebview); the file is deleted only after success,
    # so an early exit just retries next launch.
    import threading
    from app import session_duck
    threading.Thread(target=session_duck.restore_pending, daemon=True,
                     name="duck-restore").start()

    from app.webui import run
    run(cfg)


if __name__ == "__main__":
    main()
