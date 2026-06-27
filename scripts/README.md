# Release tooling

Maintainer scripts that protect the open-core boundary and pin invariants. Pure
Python stdlib — no install step (the i18n checker also uses `node` if present).

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

## `test_billing.py` — billing-invariant tests

```bash
python scripts/test_billing.py
```

Pins the `ModeController` billing invariants that were otherwise untested: no
session → no bill, capture-death / outage → accrual stops (skipped, not deferred),
no double-counting, the 402 quota cutoff fires once per session, source attribution
(`video` vs `meeting_incoming`), and the kill-9 / tail-clamp loss bounds. Uses
duck-typed fakes — no real audio, network or COM. Exit `0` = all invariants hold.

## `check_i18n.py` — i18n drift gate

```bash
python scripts/check_i18n.py
```

Asserts cross-language parity in both string tables — the Python engine
(`app/i18n.py` `STRINGS`, import-based) and the web UI (`app/web/index.html`
`I18N` + `I18N_EXTRA`, parsed via `node` when available). A key present in some
languages but missing in others silently degrades to English / the raw key for
those users; this catches that drift. Exit `0` = no drift.
