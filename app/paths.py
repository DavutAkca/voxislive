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

APP_NAME = "Voxis"


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
    """User-writable root. Frozen: %APPDATA%\\Voxis; source: repo root."""
    if is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, APP_NAME)
    else:
        path = _repo_root()
    os.makedirs(path, exist_ok=True)
    return path


def user_path(*parts: str) -> str:
    """A path under the user-writable data root (parents are created lazily)."""
    return os.path.join(user_data_dir(), *parts)


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
    """The application icon. Bundled at <bundle>/assets; source at app/assets."""
    if is_frozen():
        return os.path.join(_bundle_root(), "assets", "voxis.ico")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "voxis.ico")


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
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= nbytes:
            return data[:nbytes]
    except OSError:
        pass
    data = os.urandom(nbytes)
    try:
        # 0o600 is honored on POSIX; on Windows the ACL is tightened by callers
        # that store sensitive material (e.g. app/byok_store).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError:
        pass
    return data
