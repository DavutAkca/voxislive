#!/usr/bin/env python3
"""Release hygiene gate for the public (open-source) Voxis repository.

Purpose
-------
The public GitHub repo must NEVER carry the closed-core layer (backend, premium
DSP, marketing site, dubbing product, server-side anti-abuse specs, SaaS install
guide) or any live secret (API keys, Stripe keys, private keys, the production
host IP, the device pepper, JWTs). Those live only on the maintainer's machine
and the server; `.gitignore` keeps them out of the tree, but a stray `git add`
or a hard-coded constant can still leak them. This script is the enforcing gate.

What it checks
--------------
1. Forbidden tracked paths   — no closed-core file is tracked by git.
2. Secret content scan       — tracked text files carry no live credentials.
3. (optional) history scan   — the same secret patterns never appear in git
   history, AND no closed-core path ever existed in history (a file committed
   once and later removed stays recoverable from the log). Consciously-accepted
   prior leaks are grandfathered via HISTORY_PATH_ALLOWLIST.

It is dependency-free (stdlib only) so it runs identically on the maintainer's
Windows box (pre-push hook) and in GitHub Actions (Ubuntu). Exit code is 0 when
clean, 1 on any violation — wire it into CI and a pre-push hook.

Private literals
----------------
Some forbidden strings are themselves sensitive (the production host IP, an
internal hostname) and must NOT be hard-coded into this public file. They are
loaded at runtime from sources that never enter the tree:
  * env var VOXIS_HYGIENE_EXTRA  — newline/comma separated literals
  * file  .git/voxis-hygiene-private — one literal per line ('#' comments OK)
CI can inject VOXIS_HYGIENE_EXTRA from a repository secret.

Usage
-----
    python scripts/check_release_hygiene.py            # scan the tracked tree
    python scripts/check_release_hygiene.py --history  # also scan git history
    python scripts/check_release_hygiene.py --staged   # scan only staged files
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# --- Closed-core surface: must never be tracked in the public repo ----------
# Directory prefixes (any tracked path under these is a violation).
FORBIDDEN_PREFIXES = (
    "backend/",          # Go auth-core + PocketBase — private fork
    "premium/",          # open-core premium DSP — the moat
    "website/",          # marketing site — deployed separately
    "voxis-dubber/",     # separate commercial product
    "extension/",        # Chromium/Edge MV3 extension — closed-core SaaS product
    "docs/server/",      # server-side anti-abuse strategy
    "profiles/",         # per-user DPAPI-wrapped BYOK slots
    "production_release/",  # packaged build output
    "transcripts/",      # user session output
)
# Exact paths that are forbidden even though a sibling with the same stem is OK
# (e.g. `.env` is forbidden but `.env.example` is the published template).
FORBIDDEN_EXACT = (
    ".env",
    ".env.local",
    "config.json",
    "config.json._pending_default_restore",
    "docs/INSTALL_SAAS.md",  # end-user SaaS guide — published on voxislive.com only
    "obs_subtitle.txt",
)
# Regex for the `.env.<anything>.local` family.
FORBIDDEN_EXACT_RE = re.compile(r"^\.env\.[^/]+\.local$")

# Paths that DID exist in public history once and are consciously accepted as
# not worth a history rewrite (a force-push would break every clone and fork).
# Every entry must be a harmless POLICY leak — no live secret ever lived here.
# The tree gate still rejects these for the working tree; only the history-path
# scan grandfathers them.
#   docs/INSTALL_SAAS.md — Turkish end-user install guide, published on
#   voxislive.com. Committed in PR #4 (c06257c), removed + gitignored in PR #5
#   (4df6899). Zero secrets / IPs / credentials; left in history by decision.
HISTORY_PATH_ALLOWLIST = frozenset({
    "docs/INSTALL_SAAS.md",
})

# --- Secret content signatures ----------------------------------------------
# Each entry: (human label, compiled pattern). Patterns are deliberately tight
# so documentation placeholders (e.g. "AIza...", "sk_live_...") do NOT match.
SECRET_PATTERNS = (
    # Google / Gemini API key: "AIza" + exactly 35 chars from the key alphabet.
    # The placeholder "AIza..." / "AIza…" cannot match (dots/ellipsis are not in
    # the alphabet, and they are far shorter than 35 chars).
    ("Google/Gemini API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    # Stripe live/test secret + restricted + webhook signing keys.
    ("Stripe secret key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}")),
    ("Stripe webhook secret", re.compile(r"\bwhsec_[0-9A-Za-z]{16,}")),
    # PEM private key blocks of any flavour.
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    # Device pepper assigned a real (non-empty, non-placeholder) value.
    ("Device pepper value", re.compile(r"(?i)\bDEVICE_PEPPER\b\s*[:=]\s*['\"][^'\"\s]{6,}['\"]")),
    # A bare JWT (three base64url segments) hard-coded in source.
    ("Hard-coded JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # Resend / SMTP API token.
    ("Resend API key", re.compile(r"\bre_[0-9A-Za-z]{16,}")),
)

# Substrings that, if present on a matched line, mark it as a safe placeholder.
PLACEHOLDER_HINTS = ("...", "…", "EXAMPLE", "example", "your-", "<", "xxxx", "XXXX")

# Extensions we never scan for secret text (binary assets / models).
BINARY_EXT = {".ico", ".png", ".jpg", ".jpeg", ".gif", ".onnx", ".pyc", ".so",
              ".dll", ".exe", ".zip", ".gz", ".woff", ".woff2", ".ttf", ".otf"}


def _load_private_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    """Forbidden literals supplied out-of-tree (env var + .git private file).

    Each literal is matched verbatim (regex-escaped). Keeping them here, rather
    than hard-coded above, means this public file never reveals the secrets it
    is guarding (e.g. the production host IP)."""
    literals: list[str] = []
    env = os.environ.get("VOXIS_HYGIENE_EXTRA", "")
    for chunk in env.replace(",", "\n").splitlines():
        s = chunk.strip()
        if s:
            literals.append(s)
    root = Path(__file__).resolve().parent.parent
    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=root,
            capture_output=True, text=True,
        ).stdout.strip()
        priv = (root / git_dir / "voxis-hygiene-private").resolve()
        if priv.exists():
            for line in priv.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    literals.append(s)
    except OSError:
        pass
    return [("Private forbidden literal", re.compile(re.escape(s))) for s in literals]


def _git(*args: str) -> str:
    """Run a git command from the repo root and return stdout (text)."""
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout


def tracked_files(staged_only: bool) -> list[str]:
    if staged_only:
        raw = _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    else:
        raw = _git("ls-files")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _forbidden_path_label(norm: str) -> str | None:
    """Return a human label if `norm` is on the closed-core surface, else None."""
    if any(norm.startswith(p) for p in FORBIDDEN_PREFIXES):
        return "closed-core path"
    if norm in FORBIDDEN_EXACT or FORBIDDEN_EXACT_RE.match(norm):
        return "private file"
    return None


def check_paths(files: list[str]) -> list[str]:
    violations = []
    for f in files:
        norm = f.replace("\\", "/")
        label = _forbidden_path_label(norm)
        if label:
            violations.append(f"tracked {label}: {norm}")
    return violations


def check_open_core(files: list[str]) -> list[str]:
    """Open-core boundary guard.

    The premium package is gitignored and never shipped. The ONLY place allowed
    to import it is app/pipeline.py, and there it must be wrapped so an OSS clone
    (no premium/) keeps working. A stray `import premium` anywhere else would both
    crash OSS clones and advertise the closed package API."""
    root = Path(__file__).resolve().parent.parent
    import_re = re.compile(r"^\s*(?:import\s+premium\b|from\s+premium\b)")
    violations = []
    pipeline_seen = False
    for f in files:
        norm = f.replace("\\", "/")
        if not norm.endswith(".py"):
            continue
        text = _read_text(root / f)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if import_re.match(line):
                if norm == "app/pipeline.py":
                    pipeline_seen = True
                else:
                    violations.append(f"{f}:{lineno}: unguarded premium import outside app/pipeline.py")
        # The single allowed import must have a None fallback proving the guard.
        if norm == "app/pipeline.py" and pipeline_seen and "_premium = None" not in text:
            violations.append("app/pipeline.py: premium import is not guarded by a `_premium = None` fallback")
    return violations


def _read_text(path: Path) -> str | None:
    if path.suffix.lower() in BINARY_EXT:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:  # crude binary sniff
        return None
    return data.decode("utf-8", errors="replace")


def scan_content(files: list[str], patterns) -> list[str]:
    root = Path(__file__).resolve().parent.parent
    violations = []
    for f in files:
        text = _read_text(root / f)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pat in patterns:
                m = pat.search(line)
                if not m:
                    continue
                if any(h in line for h in PLACEHOLDER_HINTS):
                    continue  # documentation placeholder, not a live secret
                snippet = m.group(0)
                redacted = snippet[:6] + "…" if len(snippet) > 6 else snippet
                violations.append(f"{f}:{lineno}: {label} -> {redacted}")
    return violations


def scan_history(patterns) -> list[str]:
    """Scan the full diff history of public paths for the same signatures."""
    public_globs = ["app/", "client/", "docs/", "installer/", "scripts/",
                    ".github/", "main.py", "start.bat", "requirements.txt",
                    "README.md", "README.tr.md", "README.de.md",
                    ".env.example", "config.example.json"]
    diff = _git("log", "--all", "-p", "--", *public_globs)
    violations = []
    for lineno, line in enumerate(diff.splitlines(), 1):
        if not line.startswith("+"):
            continue
        for label, pat in patterns:
            m = pat.search(line)
            if m and not any(h in line for h in PLACEHOLDER_HINTS):
                violations.append(f"history: {label} -> {m.group(0)[:6]}…")
    return violations


def scan_history_paths() -> list[str]:
    """Forbidden-path history guard.

    A closed-core file committed once and later removed/gitignored is gone from
    the working tree but still recoverable from the log. The content history
    scan only catches live secrets, not policy leaks like a server-only doc.
    This lists every path that ever appeared in any commit and flags those on
    the closed-core surface — except HISTORY_PATH_ALLOWLIST entries, which are
    consciously-accepted prior leaks not worth a history rewrite."""
    raw = _git("log", "--all", "--pretty=format:", "--name-only")
    seen: set[str] = set()
    violations = []
    for line in raw.splitlines():
        norm = line.strip().replace("\\", "/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if norm in HISTORY_PATH_ALLOWLIST:
            continue
        label = _forbidden_path_label(norm)
        if label:
            violations.append(f"{label} in history: {norm}")
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description="Public-repo release hygiene gate.")
    ap.add_argument("--history", action="store_true", help="also scan git history")
    ap.add_argument("--staged", action="store_true", help="scan only staged files")
    args = ap.parse_args()

    patterns = SECRET_PATTERNS + tuple(_load_private_patterns())
    files = tracked_files(args.staged)
    path_violations = check_paths(files)
    content_violations = scan_content(files, patterns)
    opencore_violations = check_open_core(files)
    history_violations = scan_history(patterns) if args.history else []
    history_path_violations = scan_history_paths() if args.history else []

    all_violations = (
        [("FORBIDDEN PATH", v) for v in path_violations]
        + [("LIVE SECRET", v) for v in content_violations]
        + [("OPEN-CORE", v) for v in opencore_violations]
        + [("HISTORY SECRET", v) for v in history_violations]
        + [("HISTORY PATH", v) for v in history_path_violations]
    )

    scope = "staged files" if args.staged else f"{len(files)} tracked files"
    print(f"Voxis release hygiene gate — scanned {scope}"
          + (" + git history" if args.history else ""))

    if not all_violations:
        print("OK: public surface is clean. No closed-core paths, no live secrets.")
        return 0

    print(f"\nBLOCKED: {len(all_violations)} violation(s) — these must not reach GitHub:\n")
    for kind, detail in all_violations:
        print(f"  [{kind}] {detail}")
    print("\nRefusing to certify this tree for public push.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
