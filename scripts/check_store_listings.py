"""Validate every store-listings/*.md against Partner Center's field limits.

The listings are written by several people/agents in parallel, so this is the
gate: a short description that overruns is silently clipped in the Store, and an
over-length search term is rejected at import. Run before building the CSV.
"""
import pathlib
import re
import sys

SL = pathlib.Path(__file__).resolve().parents[1] / "store-listings"

# The short-description field accepts 1000 chars but the Store only renders the
# opening stretch — past this the text is written and never read.
SHORT_DESC_VISIBLE = 270
DESC_MAX = 10000
FEATURES_MAX = 20
FEATURE_CHARS = 200
TERMS_MAX = 7
TERM_CHARS = 30
TERM_WORDS_TOTAL = 21

SKIP = {"README.md", "READY-TO-UPLOAD.md", "SCREENSHOTS-GUIDE.md",
        "LISTING-1039-UPLOAD.md"}
DEAD_MOAT = re.compile(r"browser (extension |add-?on )?(can'?t|cannot) reach|"
                       r"no browser can reach", re.I)


def sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip().lower()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def sec(s, prefix):
    for name, body in s.items():
        if name.startswith(prefix):
            return body
    return ""


def bullets(s, prefix):
    return [re.sub(r"^[-•]\s*", "", ln).strip()
            for ln in sec(s, prefix).splitlines()
            if ln.strip().startswith(("-", "•"))]


files = sorted(p for p in SL.glob("*.md") if p.name not in SKIP)
fails = 0
print(f"{'file':<16} {'short':>9}  {'desc':>6}  {'feat':>4}  {'terms':>5}  status")
print("-" * 72)

for p in files:
    s = sections(p.read_text(encoding="utf-8"))
    sd = sec(s, "short description")
    de = sec(s, "description")
    fe = bullets(s, "product features")
    st = bullets(s, "search terms")
    errs = []

    if not sd:
        errs.append("no short description")
    elif len(sd) > SHORT_DESC_VISIBLE:
        errs.append(f"short desc {len(sd)}>{SHORT_DESC_VISIBLE} (tail invisible)")

    if not de:
        errs.append("no description")
    elif len(de) > DESC_MAX:
        errs.append(f"desc {len(de)}>{DESC_MAX}")

    if len(fe) > FEATURES_MAX:
        errs.append(f"{len(fe)} features > {FEATURES_MAX}")
    for i, f in enumerate(fe, 1):
        if len(f) > FEATURE_CHARS:
            errs.append(f"feature{i} {len(f)}>{FEATURE_CHARS}")

    if len(st) != TERMS_MAX:
        errs.append(f"{len(st)} search terms, expected {TERMS_MAX}")
    for i, t in enumerate(st, 1):
        if len(t) > TERM_CHARS:
            errs.append(f"term{i} {len(t)}>{TERM_CHARS}: {t!r}")
    words = sum(len(t.split()) for t in st)
    if words > TERM_WORDS_TOTAL:
        errs.append(f"search terms {words} words > {TERM_WORDS_TOTAL}")

    # The listing this replaces led with a moat rivals now match; if it survived
    # a rewrite, the rewrite didn't happen.
    if DEAD_MOAT.search(sd + de):
        errs.append("still carries the retired browser-can't-reach moat")

    status = "ok" if not errs else "FAIL"
    if errs:
        fails += 1
    print(f"{p.name:<16} {len(sd):>4}/{SHORT_DESC_VISIBLE}  {len(de):>6}  "
          f"{len(fe):>4}  {len(st)}/{words:>2}w  {status}")
    for e in errs:
        print(f"{'':<16} !! {e}")

print("-" * 72)
print(f"{len(files)} listings, {fails} failing")
sys.exit(1 if fails else 0)
