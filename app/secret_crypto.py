"""At-rest secret wrapping for non-Windows platforms (Linux/macOS).

Windows wraps secrets with DPAPI (`CryptProtectData`), which binds decryption to
the OS user account — see `byok_store` / `voxis_client`, whose Windows paths are
untouched. Other platforms have no such account-bound API, so secrets are wrapped
with **Fernet** (AES-128-CBC + HMAC, from `cryptography` — already a dependency
via the legacy BYOK decoder) keyed by the caller's per-install entropy secret
(derived from `paths.install_secret`), and the caller keeps the file at mode
0600.

Security tradeoff (documented on purpose): protection rests on the install-secret
file's 0600 perms rather than OS-level account binding, so another *local* user
cannot read the ciphertext but root can — the same practical boundary DPAPI gives
against non-admin local users. This is the deliberate port compromise until a
desktop Secret-Service (keyring) integration lands.

Keyed by the raw 32-byte entropy digest the callers already compute; any other
length is hashed to 32 bytes so the Fernet key is always well-formed.
"""


def _key(entropy: bytes) -> bytes:
    import base64
    import hashlib
    digest = entropy if len(entropy) == 32 else hashlib.sha256(entropy).digest()
    return base64.urlsafe_b64encode(digest)


def fernet_encrypt(data: bytes, entropy: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(_key(entropy)).encrypt(data)


def fernet_decrypt(blob: bytes, entropy: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(_key(entropy)).decrypt(blob)
