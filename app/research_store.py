"""research_store.py — global artifact research knowledge base.

Examiners research artifacts (especially SEGB / Biome streams) and record the
outcome here so the next case benefits: *of value*, *no value*, or *unknown*,
with a required free-text reason and an automatic assessed date.

Marks are keyed by artifact identity, not by path:
  - Biome stream files/folders key on the stream name ("stream:<name>"), since
    the numeric segb filename differs in every extraction.
  - Everything else keys on the filename ("file:<basename>").

The store is a single hand-editable JSON file shared across all cases.  In a
frozen build it sits next to the .exe (like photo_flags.json); in dev it lives
in config/ beside the source.  Findings age — artifact behaviour changes
between iOS versions — so assessed dates are always surfaced with the mark.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

OUTCOMES = ("of_value", "no_value", "unknown")

OUTCOME_LABELS = {
    "of_value": "Of value",
    "no_value": "No value",
    "unknown": "Unknown",
}

# Blue = worth looking at, red = researched dead end (matches the app's
# existing #c62828 warning red), orange = review needed (outcome unknown, or a
# firm mark that has gone stale: old research and/or untested iOS version).
OUTCOME_COLORS = {
    "of_value": "#1565c0",
    "no_value": "#c62828",
    "unknown": "#b8860b",
}
REVIEW_COLOR = "#b8860b"   # amber — deliberately far from the no-value red

# A firm mark degrades to "review needed" once the research is older than this
# or the device's iOS version wasn't covered by the research.
STALE_DAYS = 183  # ~6 months

AGEING_NOTE = ("Research findings age — artifact behaviour changes between "
               "iOS versions. Re-verify old assessments.")

GOSPEL_NOTE = ("Not gospel: this is a pointer to past research, not a "
               "conclusion about THIS case. Re-verify relevance before "
               "relying on it.")

# Matches the stream dir itself, its local/ child, and files inside it.
# (segb_schemas.stream_key_for_path requires a trailing /local so it misses
# the bare stream folder node shown in the tree.)
_STREAM_RE = re.compile(r"streams/(?:public|restricted)/([^/]+)(?:/|$)", re.I)
_STREAM_DIR_RE = re.compile(r"streams/(?:public|restricted)/[^/]+/?$", re.I)

_cache = {"stat": None, "data": None}


def store_path() -> str:
    """Location of research_status.json (next to exe when frozen, else config/)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "research_status.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "research_status.json")


def stream_key(ui_path: str):
    """Biome stream name for any path touching a stream, else None."""
    m = _STREAM_RE.search(ui_path or "")
    return m.group(1) if m else None


def is_stream_dir(ui_path: str) -> bool:
    """True only for the stream folder node itself (the one that gets colored)."""
    return bool(_STREAM_DIR_RE.search(ui_path or ""))


def key_for_path(ui_path: str, is_folder: bool):
    """Research key for a tree/table entry, or None if not markable.

    Stream dir, its local/ child and the segb files inside all resolve to the
    same "stream:<name>" key.  Plain files key on their basename; folders
    outside a stream aren't markable.
    """
    sk = stream_key(ui_path)
    if sk:
        return "stream:" + sk
    if is_folder:
        return None
    name = (ui_path or "").rstrip("/").rsplit("/", 1)[-1]
    return ("file:" + name) if name else None


def key_display(key: str) -> str:
    """Human wording for what a key matches, e.g. 'Biome stream: X'."""
    if key.startswith("stream:"):
        return "Biome stream: " + key[7:]
    return "Filename: " + key[5:]


def load() -> dict:
    """Return {key: {outcome, reason, assessed}}; cached by file mtime+size."""
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache["stat"], _cache["data"] = None, {}
        return {}
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        marks = raw.get("marks", {})
        data = {k: v for k, v in marks.items() if isinstance(v, dict)}
    except Exception:
        data = {}
    _cache["stat"], _cache["data"] = stat, data
    return data


def get(key: str):
    return load().get(key)


def _write(marks: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {"_note": AGEING_NOTE, "version": 1, "marks": marks}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _cache["stat"] = None  # force re-read next load()


def upsert(key: str, outcome: str, reason: str,
           ios_versions: str = "", published: str = "") -> dict:
    """Save/update a mark; stamps the assessed date (UTC). Returns the record.

    *ios_versions*: which iOS versions the research covered (free text,
    e.g. "15, 16–17.2").  *published*: when the source research was published
    (YYYY, YYYY-MM or YYYY-MM-DD) — used as the age basis when given, since
    year-old research entered today is still year-old knowledge.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"bad outcome: {outcome!r}")
    marks = dict(load())
    rec = {
        "outcome": outcome,
        "reason": reason.strip(),
        "ios_versions": (ios_versions or "").strip(),
        "published": (published or "").strip(),
        "assessed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
    }
    marks[key] = rec
    _write(marks)
    return rec


def remove(key: str) -> None:
    marks = dict(load())
    if key in marks:
        del marks[key]
        _write(marks)


def color_for(outcome: str):
    return OUTCOME_COLORS.get(outcome)


def age_text(assessed: str) -> str:
    """'2026-07-18 (3 months ago)' from a stored ISO timestamp."""
    date_part = (assessed or "").split("T")[0]
    try:
        then = datetime.strptime(date_part, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return assessed or "unknown date"
    days = max(0, (datetime.now(timezone.utc) - then).days)
    if days < 1:
        ago = "today"
    elif days < 31:
        ago = f"{days} day{'s' if days != 1 else ''} ago"
    elif days < 365:
        months = days // 30
        ago = f"{months} month{'s' if months != 1 else ''} ago"
    else:
        years = days // 365
        ago = f"{years} year{'s' if years != 1 else ''} ago"
    return f"{date_part} ({ago})"


def _parse_date(text: str):
    """Lenient UTC date parse: ISO timestamp, YYYY-MM-DD, YYYY-MM or YYYY."""
    t = (text or "").strip().split("T")[0]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def is_stale(assessed: str, days: int = STALE_DAYS) -> bool:
    then = _parse_date(assessed)
    if then is None:
        return False
    return (datetime.now(timezone.utc) - then).days > days


def _majors(text: str) -> set:
    """Major iOS versions mentioned in free text, expanding simple ranges.

    "15, 16.2" -> {15, 16};  "15–17" / "15-17" -> {15, 16, 17}.
    """
    out = set()
    text = text or ""
    for a, b in re.findall(r"(?<![\d.])(\d{1,2})(?:\.\d+)?\s*[-–—]\s*(\d{1,2})(?:\.\d+)?", text):
        lo, hi = int(a), int(b)
        if lo <= hi <= lo + 30:
            out.update(range(lo, hi + 1))
    for m in re.findall(r"(?<![\d.])(\d{1,2})(?:\.\d+)*", text):
        out.add(int(m))
    return out


def device_major(device_ios: str):
    m = re.match(r"\s*(\d+)", device_ios or "")
    return int(m.group(1)) if m else None


def staleness(rec: dict, device_ios: str = None, days: int = STALE_DAYS) -> list:
    """Reasons this mark needs re-review for the current case ([] if none).

    Age basis is the research's published date when given, else the assessed
    date.  A version mismatch fires when the device's major iOS version is
    known and not among the versions the research covered.
    """
    reasons = []
    published = _parse_date(rec.get("published", ""))
    basis_label = "published" if published else "assessed"
    basis_raw = rec.get("published") if published else rec.get("assessed", "")
    basis = published or _parse_date(basis_raw)
    if basis and (datetime.now(timezone.utc) - basis).days > days:
        reasons.append(f"research {basis_label} {age_text(basis_raw)} — "
                       f"older than {days // 30} months")
    tested = (rec.get("ios_versions") or "").strip()
    dm = device_major(device_ios)
    if tested and dm is not None:
        majors = _majors(tested)
        if majors and dm not in majors:
            reasons.append(f"this device runs iOS {device_ios.strip()}, "
                           f"not covered by the tested versions ({tested})")
    return reasons


def effective_color(rec: dict, device_ios: str = None):
    """Display color: outcome color, degraded to orange when review is needed."""
    if rec.get("outcome") == "unknown" or staleness(rec, device_ios):
        return REVIEW_COLOR
    return OUTCOME_COLORS.get(rec.get("outcome"))


def tooltip_for(key: str, rec: dict, device_ios: str = None) -> str:
    label = OUTCOME_LABELS.get(rec.get("outcome"), rec.get("outcome", "?"))
    stale = staleness(rec, device_ios)
    head = f"Research: {label}"
    if stale:
        head += " — REVIEW NEEDED"
    lines = [f"{head} — {key_display(key)}"]
    facts = [f"Assessed {age_text(rec.get('assessed', ''))}"]
    if rec.get("published"):
        facts.append(f"published {rec['published']}")
    if rec.get("ios_versions"):
        facts.append(f"tested iOS {rec['ios_versions']}")
    lines.append(" · ".join(facts))
    reason = (rec.get("reason") or "").strip()
    if reason:
        lines.append(reason)
    for r in stale:
        lines.append("⚠ " + r)
    lines.append("⚠ " + GOSPEL_NOTE)
    return "\n".join(lines)
