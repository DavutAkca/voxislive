"""Session transcript persistence + caption export.

Each translation session is saved as one JSON file under the user's
`transcripts/` directory. The JSON is the canonical record (timestamped,
bilingual where a source transcription was captured); TXT / SRT / VTT are
rendered on demand from it.

Wire format (schema v1):

    {
      "version": 1,
      "started": 1718700000.0,          # epoch seconds (session start)
      "started_iso": "2026-06-18T12:00:00",
      "app_version": "x.y.z",
      "mode": "video" | "meeting" | "",
      "ui_language": "tr",
      "target_in": "tr",                # incoming target language code
      "target_out": "en",               # outgoing target language code
      "turns": [
        {"t": 0.0, "dir": "out", "src": "original ...", "text": "translated ..."},
        ...
      ]
    }

`t` is the turn's offset in seconds from session start. The translate model is
natively simultaneous and stays a few seconds behind the speaker, so `t` is an
approximate caption sync, not a frame-accurate cue — adequate for SRT/VTT.
"""
import json
import os
import time

SCHEMA_VERSION = 1
# Minimum on-screen duration for a caption cue (seconds) when we cannot derive a
# longer span from the next turn's start — keeps the last cue readable.
MIN_CUE_S = 1.6
# Maximum cue duration so a long gap before the next turn doesn't leave a caption
# frozen on screen for the whole pause.
MAX_CUE_S = 7.0


def session_dir_name(started: float) -> str:
    """Canonical per-session FOLDER name keyed on the session start time.

    Each session is self-contained in its own directory (transcript JSON, caption
    exports, and the optional dual-track WAVs all share this folder + stamp), so a
    whole session can be archived or copied as one unit and file names never
    collide across sessions. Ivo's request, 1.0.28."""
    return time.strftime("voxis_%Y-%m-%d_%H-%M-%S", time.localtime(started))


def session_filename(started: float) -> str:
    """Canonical per-session JSON filename keyed on the session start time."""
    return session_dir_name(started) + ".json"


def build_record(started, turns, *, app_version="", mode="",
                 ui_language="", target_in="", target_out="") -> dict:
    """Assemble a schema-v1 record from the in-memory turn list. `turns` is a
    list of {"t", "dir", "src", "text"} dicts (src may be empty)."""
    return {
        "version": SCHEMA_VERSION,
        "started": float(started),
        "started_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "app_version": app_version,
        "mode": mode or "",
        "ui_language": ui_language or "",
        "target_in": target_in or "",
        "target_out": target_out or "",
        "turns": [
            {
                "t": float(turn.get("t", 0.0)),
                "dir": turn.get("dir", "out"),
                "src": (turn.get("src") or "").strip(),
                "text": (turn.get("text") or "").strip(),
            }
            for turn in turns
            if (turn.get("text") or "").strip()
        ],
    }


def save_record(directory: str, record: dict, *, subdir: str | None = None) -> str:
    """Persist a record JSON inside its own per-session folder under `directory`
    (the transcripts root), returning the written path.

    Layout: `<directory>/voxis_<stamp>/voxis_<stamp>.json`. The folder + file share
    one stamp so the JSON, its caption exports, and the optional WAVs form a
    self-contained, copy-as-one-unit set.

    `subdir` lets the caller pin the folder name (e.g. the live session already
    created it at start, so the recorder's WAVs and this JSON land together);
    otherwise it is derived from the record's start time. The JSON filename always
    matches the folder stamp so all of a session's files share it."""
    started = record.get("started", time.time())
    name = subdir or session_dir_name(started)
    session_dir = os.path.join(directory, name)
    os.makedirs(session_dir, exist_ok=True)
    path = os.path.join(session_dir, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def load_record(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_record_paths(directory: str):
    """Yield full paths of every session JSON under `directory`, covering both the
    per-session-folder layout (`voxis_<stamp>/voxis_<stamp>.json`, current) and the
    legacy flat layout (`voxis_<stamp>.json` directly in the root, pre-1.0.28)."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith("voxis_"):
            continue
        full = os.path.join(directory, name)
        if name.endswith(".json") and os.path.isfile(full):
            yield full  # legacy flat record
        elif os.path.isdir(full):
            try:
                inner = os.listdir(full)
            except OSError:
                continue
            for m in inner:
                if m.startswith("voxis_") and m.endswith(".json"):
                    fp = os.path.join(full, m)
                    if os.path.isfile(fp):
                        yield fp


def list_records(directory: str) -> list[dict]:
    """Return a newest-first summary list of saved sessions. Each entry carries
    enough metadata for the history list without loading every turn body."""
    out = []
    for path in _iter_record_paths(directory):
        name = os.path.basename(path)
        try:
            rec = load_record(path)
        except (OSError, ValueError):
            continue
        # Tolerate a corrupted/hand-edited record: a non-list `turns` or a
        # null/non-numeric `started` must skip-or-coerce this one record, not
        # abort the whole History listing with a TypeError.
        turns = rec.get("turns", [])
        if not isinstance(turns, list):
            turns = []
        try:
            started = float(rec.get("started") or 0.0)
        except (TypeError, ValueError):
            started = 0.0
        first = turns[0] if turns and isinstance(turns[0], dict) else {}
        out.append({
            "file": name,
            "started": started,
            "started_iso": rec.get("started_iso", ""),
            "mode": rec.get("mode", ""),
            "target_in": rec.get("target_in", ""),
            "target_out": rec.get("target_out", ""),
            "turns": len(turns),
            # Short preview from the first translated line.
            "preview": (first.get("text", "") or "")[:80],
        })
    out.sort(key=lambda r: r.get("started", 0.0), reverse=True)
    return out


def _cue_bounds(turns, idx):
    """Derive (start, end) seconds for cue `idx` from turn offsets."""
    start = float(turns[idx].get("t", 0.0))
    if idx + 1 < len(turns):
        nxt = float(turns[idx + 1].get("t", start + MIN_CUE_S))
        end = max(start + MIN_CUE_S, min(nxt, start + MAX_CUE_S))
        # Never overlap the following cue: when two turns start closer than
        # MIN_CUE_S, the floor above would push end past nxt. Clamp so cues stay
        # non-overlapping/monotonic (a short cue is better than a stacked one).
        if nxt > start:
            end = min(end, nxt)
    else:
        end = start + MIN_CUE_S
    return start, end


def _fmt_ts(seconds: float, *, vtt: bool) -> str:
    """Format a timestamp as SRT (HH:MM:SS,mmm) or VTT (HH:MM:SS.mmm)."""
    seconds = max(0.0, seconds)
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    sep = "." if vtt else ","
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _cue_text(turn, *, bilingual: bool) -> str:
    """Caption body: translation, optionally with the source line above it."""
    text = turn.get("text", "").strip()
    src = turn.get("src", "").strip()
    if bilingual and src:
        return f"{src}\n{text}"
    return text


def render_txt(record: dict, *, bilingual: bool = False) -> str:
    """Plain-text dump. Mono (default): one translation line per turn (parity with
    the legacy .txt export). Bilingual: each turn as its source line above the
    translation, turns separated by a blank line — for localization/dubbing work
    where both languages side by side beats a translated-only export."""
    turns = record.get("turns", [])
    if not bilingual:
        lines = [t.get("text", "").strip()
                 for t in turns if t.get("text", "").strip()]
        return "\n".join(lines) + ("\n" if lines else "")
    blocks = []
    for t in turns:
        text = t.get("text", "").strip()
        if not text:
            continue
        src = t.get("src", "").strip()
        blocks.append(f"{src}\n{text}" if src else text)
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_srt(record: dict, *, bilingual: bool = True) -> str:
    turns = record.get("turns", [])
    blocks = []
    for i, turn in enumerate(turns):
        body = _cue_text(turn, bilingual=bilingual)
        if not body:
            continue
        start, end = _cue_bounds(turns, i)
        blocks.append(
            f"{len(blocks) + 1}\n"
            f"{_fmt_ts(start, vtt=False)} --> {_fmt_ts(end, vtt=False)}\n"
            f"{body}\n"
        )
    return "\n".join(blocks)


def render_vtt(record: dict, *, bilingual: bool = True) -> str:
    turns = record.get("turns", [])
    blocks = ["WEBVTT\n"]
    for i, turn in enumerate(turns):
        body = _cue_text(turn, bilingual=bilingual)
        if not body:
            continue
        start, end = _cue_bounds(turns, i)
        blocks.append(
            f"{_fmt_ts(start, vtt=True)} --> {_fmt_ts(end, vtt=True)}\n"
            f"{body}\n"
        )
    return "\n".join(blocks)


_RENDERERS = {"txt": render_txt, "srt": render_srt, "vtt": render_vtt}


def export(record: dict, fmt: str, *, bilingual: bool = True) -> tuple[str, str]:
    """Render `record` to `fmt` ('txt'|'srt'|'vtt').

    `bilingual` keeps the source line alongside the translation (default) or, when
    False, emits a translated-only export. Returns (content, extension). Raises
    ValueError on an unknown format.
    """
    fmt = (fmt or "").lower()
    if fmt not in _RENDERERS:
        raise ValueError(f"unknown export format: {fmt!r}")
    return _RENDERERS[fmt](record, bilingual=bilingual), fmt
