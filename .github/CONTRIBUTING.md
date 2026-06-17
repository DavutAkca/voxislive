# Contributing to Voxis

Thanks for your interest — contributions are welcome.

## License of contributions

By opening a pull request you agree your contribution is licensed under the
project's **PolyForm Noncommercial License 1.0.0** (see [LICENSE](../LICENSE))
and may be incorporated with attribution in the project history.

## Development setup (BYOK / developer build)

```powershell
git clone https://github.com/DavutAkca/voxislive.git
cd voxislive
python -m venv .venv            # Python 3.11-3.13, 64-bit (3.14 not supported yet)
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Provide your own Gemini key via **Settings → API key**; see
[docs/INSTALL_BYOK.md](../docs/INSTALL_BYOK.md). The developer build needs no
account and no server.

List your audio devices any time with `python -m app.audio_io`.

## Before you push

This is an **open-core** project: the public repo ships only the BYOK build, and
a release-hygiene gate keeps closed-core paths and secrets out of the tree.

- Install the local gate hook once: `python scripts/install_hooks.py`
- Run it any time: `python scripts/check_release_hygiene.py`
- **Never** add closed-core paths (`backend/`, `premium/`, `website/`, …), live
  secrets, API keys, tokens, or the production host to tracked files.

## Style

- Concise **English** comments that explain *why*, not *what*.
- Match the surrounding code's naming and idiom.
- Keep each pull request focused on one logical change.
