"""Filesystem path resolution that works both from source and from a frozen build.

Two distinct roots:

- **Bundled read-only assets** (`web/`, `models/`, `assets/`) ship inside the
  PyInstaller bundle. When frozen they live under `sys._MEIPASS`; from source
  they live in the repository tree.
- **User-writable data** (`config.json`, `profiles/`, `transcripts/`, `.env`).
  When frozen the install lands in `C:\\Program Files\\...`, which a standard
  user cannot write to, so this data goes to `%APPDATA%\\Voxis`. From source it
  stays in the repo root so the developer workflow is unchanged.
"""
import os
import sys
import tempfile
import threading

APP_NAME = "Voxis"
_INSTALL_SECRET_LOCK = threading.Lock()


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _repo_root() -> str:
    # app/paths.py -> app -> repo root (source layout only).
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bundle_root() -> str:
    # PyInstaller sets _MEIPASS to the bundle dir (onedir: the _internal folder).
    return getattr(sys, "_MEIPASS", None) or _repo_root()


def official_marker() -> str:
    """Path to the build-flavor marker. The official build ships this file inside
    the bundle; its presence in a frozen build selects the SaaS flavor. Absent in
    the open-source / GitHub build, which therefore stays BYOK.

    This is the single source of flavor truth for a signed artifact: it lives in
    the read-only bundle, so the launching environment cannot fabricate it (see
    app/config._resolve_official_release)."""
    return os.path.join(_bundle_root(), "OFFICIAL")


def store_marker() -> str:
    """Path to the Microsoft Store distribution marker. app/build_msix.py writes
    this into the MSIX layout (alongside OFFICIAL) so a running build can report
    which channel it shipped through. Absent in the Inno / sideload .exe."""
    return os.path.join(_bundle_root(), "STORE")


def is_store_build() -> bool:
    """True only for the MSIX (Microsoft Store) artifact: a frozen bundle that
    carries the STORE marker. The Inno official .exe is frozen+OFFICIAL but has no
    STORE marker, and a source run is not frozen at all."""
    return is_frozen() and os.path.exists(store_marker())


def client_channel() -> str:
    """The distribution channel this desktop build was delivered through, reported
    with each usage heartbeat so the backend can attribute minutes by source.
      * "store"   — Microsoft Store (MSIX).
      * "desktop" — Inno installer / sideload .exe (or a source run).
    The browser extension reports "extension" from its own client."""
    return "store" if is_store_build() else "desktop"


def user_data_dir() -> str:
    """User-writable root. Frozen: %APPDATA%\\Voxis (Windows) / $XDG_CONFIG_HOME
    (Linux/other); source: repo root (dev workflow unchanged on every platform)."""
    if is_frozen():
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
        else:
            # XDG Base Directory: config.json + per-install secrets live here.
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
                os.path.expanduser("~"), ".config")
        path = os.path.join(base, APP_NAME)
    else:
        path = _repo_root()
    os.makedirs(path, exist_ok=True)
    return path


def user_path(*parts: str) -> str:
    """A path under the user-writable data root (parents are created lazily)."""
    return os.path.join(user_data_dir(), *parts)


def _xdg_documents_dir() -> str:
    """The XDG Documents directory on Linux/other. Prefers `xdg-user-dir
    DOCUMENTS` (reads ~/.config/user-dirs.dirs, localized-safe), then the
    $XDG_DOCUMENTS_DIR env var, else ~/Documents.

    Note: when DOCUMENTS is not configured (common on a fresh headless install),
    `xdg-user-dir` returns $HOME itself — accepting that would dump transcripts in
    the home root, so a result equal to $HOME is rejected in favour of ~/Documents
    (verified on a stock Raspberry Pi OS install, 2026-07-18)."""
    home = os.path.expanduser("~")
    home_norm = os.path.normpath(home)

    def _usable(p: str) -> bool:
        return bool(p) and os.path.isdir(p) and os.path.normpath(p) != home_norm

    try:
        import subprocess
        out = subprocess.run(["xdg-user-dir", "DOCUMENTS"],
                             capture_output=True, text=True, timeout=5)
        p = (out.stdout or "").strip()
        if _usable(p):
            return p
    except Exception:
        pass
    env = (os.environ.get("XDG_DOCUMENTS_DIR") or "").strip()
    if _usable(env):
        return env
    return os.path.join(home, "Documents")


def documents_dir() -> str:
    """The user's real Documents folder via the Windows known-folder API.

    NOT %USERPROFILE%\\Documents: OneDrive Known-Folder-Move (and a localized
    Windows profile — e.g. Turkish 'Belgeler') relocate Documents, so the env-var
    guess points at a folder that does not exist. SHGetKnownFolderPath(FOLDERID_
    Documents) always resolves the true location. Falls back to ~/Documents only
    when the API call fails.

    On Linux/other, resolves the XDG Documents dir instead (xdg-user-dir /
    $XDG_DOCUMENTS_DIR, else ~/Documents)."""
    if sys.platform != "win32":
        return _xdg_documents_dir()
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
        folderid = _GUID(0xFDD39AD0, 0x238F, 0x46AF,
                         (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
        out = ctypes.c_wchar_p()
        # SHGetKnownFolderPath(rfid, dwFlags=0 (KF_FLAG_DEFAULT), hToken=None, ppszPath)
        rc = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folderid), 0, None, ctypes.byref(out))
        try:
            if rc == 0 and out.value:
                return out.value
        finally:
            if out.value:
                ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def legacy_transcripts_dir() -> str:
    """Pre-1.0.26 transcript location (%APPDATA%\\Voxis\\transcripts when frozen;
    repo transcripts/ from source). Retained so old sessions still list and can be
    migrated to the new user-facing default. On the Store MSIX this AppData path is
    virtualized into LocalCache\\Roaming — the reason the old default was buried."""
    return user_path("transcripts")


def default_transcripts_dir() -> str:
    """Built-in default when cfg['transcript_dir'] is unset. Frozen builds save to
    Documents\\Voxis\\Transcripts — a user-facing folder that, unlike %APPDATA%, is
    NOT virtualized by the MSIX container (full-trust packaged apps keep real
    Documents access). From source it stays in the repo transcripts/ dir so the
    developer workflow is unchanged."""
    if is_frozen():
        return os.path.join(documents_dir(), APP_NAME, "Transcripts")
    return os.path.join(_repo_root(), "transcripts")


def transcripts_dir(cfg: dict | None = None) -> str:
    """Active transcript save directory. A non-empty cfg['transcript_dir'] override
    wins (Settings-configurable); otherwise the built-in default."""
    custom = ((cfg or {}).get("transcript_dir") or "").strip()
    return custom or default_transcripts_dir()


def web_dir() -> str:
    """The single-file UI directory. Bundled at <bundle>/web; source at app/web."""
    if is_frozen():
        return os.path.join(_bundle_root(), "web")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def model_path(name: str) -> str:
    """A bundled model weight. Prefers the bundled copy; falls back to user dir
    (where vad.py may download it on first run when not bundled)."""
    if is_frozen():
        bundled = os.path.join(_bundle_root(), "models", name)
        if os.path.exists(bundled):
            return bundled
        return user_path("models", name)
    return os.path.join(_repo_root(), "models", name)


def icon_path() -> str:
    """The application icon. Bundled at <bundle>/assets; source at app/assets.

    Windows uses voxis.ico. On Linux the GTK/WebKit backend wants a PNG, so
    prefer voxis.png when present (added with the Linux packaging in Faz 7);
    falls back to the .ico so the call never returns a missing path."""
    base = _bundle_root() if is_frozen() else os.path.dirname(os.path.abspath(__file__))
    assets = os.path.join(base, "assets")
    if sys.platform != "win32":
        png = os.path.join(assets, "voxis.png")
        if os.path.exists(png):
            return png
    return os.path.join(assets, "voxis.ico")


def bundled_default_config() -> str:
    """The production config.json shipped inside the build, used to seed the
    user's config on first run. Empty/non-existent from source."""
    return os.path.join(_bundle_root(), "config.json")


def install_secret(name: str = "install.secret", nbytes: int = 32) -> bytes:
    """A stable, per-install random secret persisted under the user data root.

    Used as entropy for at-rest secret derivation (see app/byok_store) instead of
    a shared public constant, so a copied/leaked blob cannot be decrypted on
    another install. The file is created once with 0600-style perms; callers that
    need OS-level protection should still wrap their secrets with DPAPI."""
    path = user_path(name)
    with _INSTALL_SECRET_LOCK:
        try:
            with open(path, "rb") as f:
                data = f.read()
            if len(data) >= nbytes:
                return data[:nbytes]
        except OSError:
            pass

        data = os.urandom(nbytes)
        directory = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(
            dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
        try:
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            fd = -1
            os.replace(tmp, path)
            # Return what actually won on disk. Within this process the lock makes
            # it our value; re-reading also avoids returning an unpersisted secret
            # if an external process replaced it at the same boundary.
            with open(path, "rb") as f:
                persisted = f.read()
            if len(persisted) < nbytes:
                raise OSError(f"persisted install secret is shorter than {nbytes} bytes")
            return persisted[:nbytes]
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.remove(tmp)
            except OSError:
                pass
            # Never return a value that did not make it to disk: encrypting with
            # it would create a BYOK slot that becomes unreadable on restart.
            raise
