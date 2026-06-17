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
            pass
        cfg["_pending_default_restore"] = None
        save_config(cfg)

    from app.webui import run
    run(cfg)


if __name__ == "__main__":
    main()
