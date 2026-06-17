"""Canlı Çeviri — giriş noktası (web tabanlı arayüz, pywebview).

Kullanım: .venv aktifken `python main.py` (veya basla.bat).
API anahtarları artık .env'de saklanmaz; kullanıcı başına BYOK (şifreli) veya
SaaS yolu (sunucudan oturum anahtarı) üzerinden çözümlenir.
"""
import logging
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from app import i18n
from app.config import load_config, save_config
from app.paths import user_path


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


def main():
    load_dotenv()
    _setup_logging()

    cfg = load_config()
    i18n.set_language(cfg.get("ui_language", "tr"))

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
