#!/usr/bin/env python3
"""Install the local git pre-push hook that runs the release hygiene gate.

Run once after cloning:  python scripts/install_hooks.py

The hook blocks any push whose tree carries a closed-core path or a live secret,
so a leak is caught on the maintainer's machine before it ever reaches GitHub
(CI enforces the same gate server-side as a backstop).
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

HOOK = """#!/bin/sh
# Voxis pre-push leak gate — auto-installed by scripts/install_hooks.py
exec python scripts/check_release_hygiene.py
"""


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    hooks_dir = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=root, capture_output=True, text=True,
    ).stdout.strip()
    hooks_path = (root / hooks_dir).resolve()
    hooks_path.mkdir(parents=True, exist_ok=True)
    target = hooks_path / "pre-push"
    target.write_text(HOOK, encoding="utf-8", newline="\n")
    target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"Installed pre-push hook -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
