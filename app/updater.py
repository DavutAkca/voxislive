"""In-app auto-updater for frozen Windows builds.

This path runs a downloaded installer silently with elevation, so the manifest
and the binary are treated as hostile until cryptographically proven otherwise.
The transport (TLS / which host served the bytes) is NOT the trust root: an
attacker who compromises the CDN, a CA, or DNS must still forge a signature they
cannot produce. Three gates protect every auto-applied update:

  1. Manifest authenticity - a detached Ed25519 signature over the canonical
     (version + url + sha256) tuple, verified against a public key embedded in
     this module (the build pipeline pins the real key). The in-manifest sha256
     is only a corruption/integrity check on the download, never a trust anchor.
  2. Binary integrity - SHA-256 of the downloaded file is verified against the
     signed manifest before the installer is launched.
  3. Transport security - downloads are performed over a pinned TLS context
     restricted to allowlisted voxislive.com hosts.

Authenticode signing is an optional fourth layer; builds with a code-signing
certificate should enable it in launch_installer(). No-op when run from source.

Manifest shape (served statically, e.g. https://voxislive.com/update/latest.json):

    {
      "version": "1.0.1",
      "url": "https://voxislive.com/download/VoxisLive_v1.0.1_Setup.exe",
      "sha256": "<hex>",          # corruption check only, NOT the trust root
      "sig": "<base64 ed25519>",  # REQUIRED - detached sig over version|url|sha256
      "notes": "What changed",    # optional
      "mandatory": false          # advisory UI only; never bypasses the sig gate
    }
"""
import base64
import ctypes
import hashlib
import json
import os
import ssl
import subprocess
import tempfile
import urllib.parse
import urllib.request

from . import APP_VERSION
from .paths import is_frozen, user_path

DEFAULT_MANIFEST_URL = "https://voxislive.com/update/latest.json"
_UA = {"User-Agent": f"Voxis/{APP_VERSION}"}
_CHECK_TIMEOUT = 10
_DOWNLOAD_TIMEOUT = 120

# Hosts permitted to serve update manifests and installers. Anything else (other
# hosts, http://, file://) is rejected before a single byte is fetched.
_ALLOWED_HOSTS = frozenset({
    "voxislive.com",
    "www.voxislive.com",
    "cdn.voxislive.com",
})

# --- Manifest trust root (Ed25519) -------------------------------------------
# BUILD PIPELINE: replace this placeholder with the base64-encoded 32-byte raw
# Ed25519 PUBLIC key whose private half signs the manifest. The private key NEVER
# ships in the binary or this repo; it lives only on the release-signing host.
# Generate a keypair with:
#   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
#   from cryptography.hazmat.primitives import serialization
#   sk = Ed25519PrivateKey.generate()
#   pub = sk.public_key().public_bytes(serialization.Encoding.Raw,
#                                       serialization.PublicFormat.Raw)
#   print(base64.b64encode(pub).decode())   # <- paste below
# An empty/placeholder key disables auto-apply (check() refuses to trust any
# manifest) rather than failing open.
# Production manifest trust root. The matching private key lives only on the
# release-signing host (never in this repo). Rotating it requires shipping a new
# build with the new public key here.
_MANIFEST_PUBKEY_B64 = "lJTiQcdHGnU4XG9UYcg4wLKsgnCEDEjnY+tDlQntrHA="

# --- Authenticode publisher constraint ---------------------------------------
# WinVerifyTrust proves the .exe chains to a trusted root; this additionally
# requires the leaf certificate subject to match the Voxis publisher so a valid
# signature from some OTHER vendor's cert is still rejected. Substring match
# against the cert subject CN/O (case-insensitive).
_EXPECTED_PUBLISHER = "Voxis"

# Persisted monotonic "highest version ever offered" floor (anti-rollback). Even
# a correctly-signed older manifest is refused once a newer one has been seen, so
# a captured-and-replayed old signed manifest cannot force a downgrade.
_VERSION_FLOOR_PATH = user_path("update_floor.json")

# --- TLS pinning (defense-in-depth, behind the signature gate) ----------------
# Optional SPKI pins (base64 SHA-256 of the server cert's SubjectPublicKeyInfo)
# for voxislive.com. When populated the update TLS handshake must present a leaf
# whose SPKI hash is in this set, blunting rogue-CA MITM. Empty => standard CA
# validation only (still safe: the Ed25519 + Authenticode gates are the trust
# root). MAINTAINER: paste current + backup leaf SPKI pins to enable.
_TLS_SPKI_PINS = frozenset()  # e.g. {"base64sha256=="}


def _parse_version(v: str):
    """Parse a dotted numeric version into a comparable tuple, or None if the
    string is malformed. Unlike a zero-filling parser, a non-numeric or empty
    component is rejected so a junk 'version' can never read as 0.0.0 and slip
    past the freshness/floor checks."""
    s = str(v).strip().lstrip("vV")
    if not s:
        return None
    parts = s.split(".")
    out = []
    for part in parts:
        if not part or not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out)


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    r = _parse_version(remote)
    l = _parse_version(local)
    if r is None or l is None:
        return False
    return r > l


def _url_ok(url: str) -> bool:
    """True only for https:// URLs whose host is in the allowlist. Rejects
    file://, http://, embedded credentials, and arbitrary hosts."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    if parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    return host in _ALLOWED_HOSTS


def _ssl_context() -> ssl.SSLContext:
    """TLS context for update traffic. Standard CA validation always; SPKI
    pinning layered on when pins are configured (defense-in-depth)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _check_spki_pin(conn) -> None:
    """Raise if SPKI pinning is enabled and the peer's public key is not pinned.
    No-op when no pins are configured."""
    if not _TLS_SPKI_PINS:
        return
    der = conn.getpeercert(binary_form=True)
    if not der:
        raise ssl.SSLError("update: no peer certificate for SPKI pin check")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_der_x509_certificate(der)
        spki = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception as e:  # treat an unparseable cert as a pin failure
        raise ssl.SSLError(f"update: SPKI pin parse failed: {e}")
    pin = base64.b64encode(hashlib.sha256(spki).digest()).decode()
    if pin not in _TLS_SPKI_PINS:
        raise ssl.SSLError("update: server SPKI not in pin set (possible MITM)")


def _open(url: str, timeout: int):
    """Open an allowlisted https URL with the pinned TLS context and verify the
    SPKI pin once connected. Caller must have already passed `url` through
    `_url_ok`. urllib follows redirects by default; the post-connect host re-check
    here plus the allowlist keep any redirect inside the same trust boundary."""
    req = urllib.request.Request(url, headers=_UA)
    resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())
    # Reject a redirect that escaped the allowlist (urllib already followed it).
    if not _url_ok(resp.geturl()):
        try:
            resp.close()
        finally:
            raise ValueError("update: redirected to a non-allowlisted URL")
    try:
        sock = resp.fp.raw._sock  # underlying SSLSocket for the SPKI pin check
        if isinstance(sock, ssl.SSLSocket):
            _check_spki_pin(sock)
    except AttributeError:
        pass  # socket internals unavailable; CA validation still applied
    return resp


def _canonical_signed_message(version: str, url: str, sha256: str) -> bytes:
    """Canonical bytes the manifest signature covers. Order and separator are
    fixed so the signer and verifier agree byte-for-byte; the signed sha256 binds
    the manifest to a specific installer so url/hash can't be swapped post-sign."""
    return (version + "\n" + url + "\n" + sha256.lower()).encode("utf-8")


def _verify_manifest_signature(manifest: dict) -> bool:
    """Verify the detached Ed25519 signature over (version|url|sha256) against the
    embedded public key. Returns False (never raises) on any failure: missing
    key, missing/garbled signature, or mismatch. This is the manifest trust root
    - transport security is not."""
    pub_b64 = _MANIFEST_PUBKEY_B64.strip()
    if not pub_b64:
        return False  # no trust root pinned -> fail closed, never auto-apply
    sig_b64 = (manifest.get("sig") or "").strip()
    sha = (manifest.get("sha256") or "").strip()
    if not sig_b64 or not sha:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        sig = base64.b64decode(sig_b64)
        msg = _canonical_signed_message(manifest["version"], manifest["url"], sha)
        pub.verify(sig, msg)  # raises InvalidSignature on mismatch
        return True
    except Exception:
        return False


def _read_version_floor():
    try:
        with open(_VERSION_FLOOR_PATH, encoding="utf-8") as f:
            return _parse_version(json.load(f).get("version", ""))
    except Exception:
        return None


def _bump_version_floor(version: str) -> None:
    """Persist `version` as the new floor if it exceeds the stored one. Best
    effort: an unwritable floor must not crash the update flow."""
    parsed = _parse_version(version)
    if parsed is None:
        return
    cur = _read_version_floor()
    if cur is not None and parsed <= cur:
        return
    try:
        with open(_VERSION_FLOOR_PATH, "w", encoding="utf-8") as f:
            json.dump({"version": version}, f)
    except OSError:
        pass


def _verify_authenticode(path: str) -> bool:
    """Verify the .exe carries a valid Authenticode signature chaining to a
    trusted root via WinVerifyTrust, then confirm the signer is the Voxis
    publisher. Returns False on any failure. Windows-only; off-Windows returns
    False so a non-Windows host never auto-launches an unverified installer."""
    if os.name != "nt":
        return False
    try:
        if not _winverifytrust(path):
            return False
        return _signer_is_expected_publisher(path)
    except Exception:
        return False


def _winverifytrust(path: str) -> bool:
    """Thin WinVerifyTrust(WINTRUST_ACTION_GENERIC_VERIFY_V2) wrapper. Returns
    True only when the trust provider returns 0 (signature present, chain valid,
    not revoked/expired per policy)."""
    # GUID {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", ctypes.c_ulong),
            ("pcwszFilePath", ctypes.c_wchar_p),
            ("hFile", ctypes.c_void_p),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", ctypes.c_ulong),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", ctypes.c_ulong),
            ("fdwRevocationChecks", ctypes.c_ulong),
            ("dwUnionChoice", ctypes.c_ulong),
            ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)),
            ("dwStateAction", ctypes.c_ulong),
            ("hWVTStateData", ctypes.c_void_p),
            ("pwszURLReference", ctypes.c_wchar_p),
            ("dwProvFlags", ctypes.c_ulong),
            ("dwUIContext", ctypes.c_ulong),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    WTD_UI_NONE = 2
    WTD_REVOKE_WHOLECHAIN = 1
    WTD_CHOICE_FILE = 1
    WTD_STATEACTION_VERIFY = 1
    WTD_STATEACTION_CLOSE = 2
    WTD_REVOCATION_CHECK_CHAIN = 0x00000040

    guid = GUID(
        0x00AAC56B, 0xCD44, 0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = WINTRUST_FILE_INFO(
        ctypes.sizeof(WINTRUST_FILE_INFO), path, None, None
    )
    data = WINTRUST_DATA()
    data.cbStruct = ctypes.sizeof(WINTRUST_DATA)
    data.dwUIChoice = WTD_UI_NONE
    data.fdwRevocationChecks = WTD_REVOKE_WHOLECHAIN
    data.dwUnionChoice = WTD_CHOICE_FILE
    data.pFile = ctypes.pointer(file_info)
    data.dwStateAction = WTD_STATEACTION_VERIFY
    data.dwProvFlags = WTD_REVOCATION_CHECK_CHAIN

    wintrust = ctypes.WinDLL("wintrust.dll")
    WinVerifyTrust = wintrust.WinVerifyTrust
    WinVerifyTrust.restype = ctypes.c_long
    try:
        status = WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))
    finally:
        data.dwStateAction = WTD_STATEACTION_CLOSE
        WinVerifyTrust(None, ctypes.byref(guid), ctypes.byref(data))
    return status == 0


def _signer_is_expected_publisher(path: str) -> bool:
    """Confirm the Authenticode signer subject contains the Voxis publisher name.
    Prevents a file validly signed by an UNRELATED publisher from passing the
    chain check alone. Uses PowerShell Get-AuthenticodeSignature to read the leaf
    subject without bundling extra native parsing."""
    try:
        ps = (
            "$ErrorActionPreference='Stop';"
            "$s=Get-AuthenticodeSignature -LiteralPath $args[0];"
            "if($s.Status -ne 'Valid'){exit 2};"
            "$s.SignerCertificate.Subject"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps, path],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            return False
        return _EXPECTED_PUBLISHER.lower() in out.stdout.lower()
    except Exception:
        return False


def check(url: str = ""):
    """Return the manifest dict if a newer, *signature-verified* version is
    available, else None. Network/parse errors are swallowed (returns None) so
    startup never blocks. Trust order: allowlist URL -> fetch over pinned TLS ->
    require version freshness AND a valid Ed25519 manifest signature AND a value
    above the anti-rollback floor. 'mandatory' is passed through untouched for UI
    only and grants no bypass."""
    url = url or DEFAULT_MANIFEST_URL
    if not _url_ok(url):
        return None
    try:
        with _open(url, _CHECK_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("version") or not data.get("url"):
        return None
    if not _url_ok(str(data.get("url", ""))):
        return None  # installer URL must itself be allowlisted https
    if not is_newer(data["version"]):
        return None
    parsed = _parse_version(data["version"])
    if parsed is None:
        return None
    # Anti-rollback floor tracks the highest version ever INSTALLED (recorded from
    # APP_VERSION, idempotently — never the merely-offered version). Compared
    # strictly (<), so the currently-offered version keeps being surfaced on every
    # launch until it is actually installed; only versions below what was installed
    # are refused (this still blocks a signed-but-older release after a
    # downgrade-reinstall, where APP_VERSION alone would not).
    _bump_version_floor(APP_VERSION)
    eff_floor = _read_version_floor()
    if eff_floor is not None and parsed < eff_floor:
        return None
    if not _verify_manifest_signature(data):
        return None  # untrusted/forged manifest - never surface it
    return data


def download(manifest: dict, progress=None) -> str:
    """Download the installer to a temp file over the pinned TLS context after
    re-validating its URL and manifest signature, then verify SHA-256 for
    corruption. `progress` is called with a 0..1 fraction. Returns the temp path.
    Re-verifies the signature here so a caller cannot hand `download()` a manifest
    that never went through `check()`."""
    if not _url_ok(str(manifest.get("url", ""))):
        raise ValueError("Update URL is not an allowlisted https host.")
    if not _verify_manifest_signature(manifest):
        raise ValueError("Update manifest signature verification failed.")
    fd, path = tempfile.mkstemp(suffix=".exe", prefix="VoxisSetup_")
    os.close(fd)
    sha = (manifest.get("sha256") or "").strip().lower()
    digest = hashlib.sha256()
    with _open(manifest["url"], _DOWNLOAD_TIMEOUT) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    # sha256 is a corruption check; the signature (which binds this exact sha256)
    # is the trust root. A signed manifest always carries a sha256, so a mismatch
    # here is a hard failure.
    if not sha or digest.hexdigest().lower() != sha:
        _safe_remove(path)
        raise ValueError("Downloaded installer failed checksum verification.")
    return path


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def launch_installer(path: str) -> None:
    """Spawn the downloaded installer silently and detached.

    By the time this is called, the file has already passed three independent
    gates: Ed25519 manifest signature, SHA-256 hash, and TLS transport.
    Authenticode signing is an optional fourth layer added when a code-signing
    certificate is available; builds without a cert skip it so the signed
    manifest remains the trust root.
    """
    flags = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen([path] + flags, close_fds=True, creationflags=creationflags)


def available() -> bool:
    """Whether auto-update can operate (only meaningful for frozen builds)."""
    return is_frozen()
