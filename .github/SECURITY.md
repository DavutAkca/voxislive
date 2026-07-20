# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub's
[**Report a vulnerability**](https://github.com/DavutAkca/voxislive/security/advisories/new)
form (the repository's **Security → Advisories** tab). We aim to acknowledge a
valid report within a few days and will coordinate a fix and disclosure with you.

When reporting, please include reproduction steps and the affected version.
**Never paste an API key, token, or any other secret** into a report.

## Design notes relevant to security

Voxis is an open-source **BYOK (bring-your-own-key)** desktop app:

- Your Google Gemini API key is stored **encrypted at rest with Windows DPAPI**
  (`CryptProtectData`, `CURRENT_USER` scope) plus a per-install entropy secret,
  under `profiles/byok/`. It is never written to a plaintext `.env` and never
  leaves your machine except to open the Gemini Live WebSocket your key authorizes.
- The open-source build never contacts Voxis services — no telemetry,
  authentication, or usage reporting. Translation uses the Gemini Live session
  opened by the user's key. Optional speaker labeling and local TTS may download
  hash-verified model assets from `k2-fsa/sherpa-onnx` GitHub releases on first
  use; those downloads contain no session audio or transcript data.
- The public repository is kept free of secrets and closed-core code by a
  release-hygiene gate (`scripts/check_release_hygiene.py`), enforced in CI and a
  local pre-push hook.

## Supported versions

The latest version on the `main` branch receives security fixes.
