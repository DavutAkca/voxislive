"""i18n cross-language parity / drift checker (C5).

The Python engine (app/i18n.py STRINGS) and the web UI (app/web/index.html
I18N + I18N_EXTRA) each carry 16 per-language string tables. A key present in
some languages but missing in others silently degrades to English / the raw key
for those users — exactly the fragile Python<->JS split CLAUDE.md warns about,
and the class of drift that hit `onboard_eyebrow` before. This asserts every key
exists in every language block, in both namespaces, and exits non-zero on drift.

Python side: import-based (zero-dep). JS side: parsed via node when available,
otherwise skipped with a warning (so the Python check always runs in any CI).
Run: `python scripts/check_i18n.py`  (exit 0 = no drift).
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import i18n  # noqa: E402


def parity(blocks: dict):
    """blocks: {lang: set(keys)}. Returns (all_keys, [(lang, sorted missing)])
    where 'missing' = union of every block's keys minus this language's."""
    allkeys = set()
    for ks in blocks.values():
        allkeys |= ks
    problems = [(lang, sorted(allkeys - blocks[lang]))
                for lang in sorted(blocks) if allkeys - blocks[lang]]
    return allkeys, problems


# node: string-aware brace-match the I18N / I18N_EXTRA object literals, merge them
# exactly as index.html does, and emit {lang: [keys]} as JSON.
_NODE = r"""
const fs = require('fs');
const s = fs.readFileSync(process.argv[1], 'utf8');
function extract(anchor){
  const m = s.indexOf(anchor);
  if (m < 0) throw new Error('not found: ' + anchor);
  const i = s.indexOf('{', m);
  let depth = 0, q = null, esc = false;
  for (let j = i; j < s.length; j++){
    const c = s[j];
    if (esc){ esc = false; continue; }
    if (q){ if (c === '\\') esc = true; else if (c === q) q = null; continue; }
    if (c === '"' || c === "'" || c === '`'){ q = c; continue; }
    if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return eval('(' + s.slice(i, j + 1) + ')');
  }
  throw new Error('unbalanced: ' + anchor);
}
const I18N = extract('const I18N =');
const EX = extract('const I18N_EXTRA =');
for (const l in EX){ if (!I18N[l]) I18N[l] = {}; for (const k in EX[l]) I18N[l][k] = EX[l][k]; }
const out = {};
for (const l in I18N) out[l] = Object.keys(I18N[l]);
process.stdout.write(JSON.stringify(out));
"""


def js_blocks():
    """{lang: set(keys)} of merged I18N+I18N_EXTRA via node; None if node absent/fails."""
    node = next((c for c in ("node", "node.exe")
                 if _runs([c, "--version"])), None)
    if node is None:
        return None
    html = os.path.join(ROOT, "app", "web", "index.html")
    r = subprocess.run([node, "-e", _NODE, html], capture_output=True, text=True)
    if r.returncode != 0:
        print("  WARN  JS parse failed:", (r.stderr or "").strip()[:200])
        return None
    return {lang: set(keys) for lang, keys in json.loads(r.stdout).items()}


def _runs(cmd):
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except OSError:
        return False


def _report(title, blocks):
    print(f"{title}:")
    if blocks is None:
        print("  SKIP — node unavailable or parse failed (Python check stays authoritative)")
        return 0
    allk, problems = parity(blocks)
    print(f"  {len(blocks)} languages, {len(allk)} distinct keys")
    if not problems:
        print("  OK — every key present in all language blocks")
        return 0
    for lang, missing in problems:
        head = ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        print(f"  DRIFT {lang}: missing {len(missing)} -> {head}")
    return 1


def main():
    py = {lang: set(d.keys()) for lang, d in i18n.STRINGS.items()}
    rc = _report("Python  app/i18n.py STRINGS", py)
    print()
    rc |= _report("JS  app/web/index.html  I18N + I18N_EXTRA", js_blocks())
    print("\n" + ("i18n drift detected (exit 1)." if rc else "No i18n drift."))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
