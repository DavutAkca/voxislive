# Release tooling

Maintainer scripts that protect the open-core boundary. Pure Python stdlib — no
install step.

## `check_release_hygiene.py` — the leak gate

Refuses to certify the tree for a public push if it finds:

- **Closed-core paths** tracked by git (`backend/`, `premium/`, `website/`,
  `voxis-dubber/`, `docs/server/`, `profiles/`, …) — catches a stray `git add -f`.
- **Live secrets** in tracked content (Google/Gemini keys, Stripe keys, PEM
  private keys, JWTs, Resend tokens) — documentation placeholders are ignored.
- **Open-core import leaks** — any `import premium` outside the single guarded
  site in `app/pipeline.py`, or a missing `_premium = None` fallback there.

```bash
python scripts/check_release_hygiene.py            # scan the tracked tree
python scripts/check_release_hygiene.py --history  # also scan full git history
python scripts/check_release_hygiene.py --staged   # scan only staged files
```

Exit code is `0` when clean, `1` on any violation.

### Private literals (never hard-coded here)

Some forbidden strings are themselves sensitive (the production host IP). They are
loaded at runtime so this public file never reveals them:

- env var `VOXIS_HYGIENE_EXTRA` — newline/comma separated literals (CI injects this
  from a repository secret), or
- file `.git/voxis-hygiene-private` — one literal per line (`#` comments allowed);
  lives inside `.git`, so it is never tracked.

## `install_hooks.py` — local pre-push gate

```bash
python scripts/install_hooks.py
```

Installs a `pre-push` hook that runs the leak gate before every push, so a leak is
caught on your machine first. CI (`.github/workflows/release-hygiene.yml`) runs the
same gate server-side as a backstop.
