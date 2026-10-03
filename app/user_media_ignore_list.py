"""user_media_ignore_list.py — a global, hand-maintained list of folders
and files to exclude from the Media Browser's "User Media" filter (added
2026-09-27, direct request: "i would like to make a filter for media
files that shows all media files in location that are user created...
i also wa[n]t to be able to select a folder and file [and] be able to
remove via [a] right click").

"User Media" itself shows real media files (pictures/videos) found under
a small, fixed set of default locations known to hold USER-created
content rather than app/OS chrome — see FastZipBrowser.
_user_media_include_prefixes() for the exact iOS/Android path list. This
module is the separate, examiner-editable EXCLUSION layer on top of
that: a folder or file added here is hidden from that filter specifically
(never from the ordinary File Browser/Media Browser — only from this one
filtered view), for a location that turns out to be noise despite being
under an otherwise-user-content area (e.g. a specific app's own internal
image cache living inside its App-Group container).

A single, FLAT, GLOBAL (cross-case) list — deliberately not split by
platform the way embedded_media_skip_list.py is: entries here are real
UI PATHS (not bare filenames), so an iOS path and an Android path can
never collide the way a bare database filename could, and a case is
always one platform anyway. Each entry matches two ways:
  - An EXACT match — this one specific file (or folder, along with
    everything inside it — see below) is excluded.
  - A FOLDER-PREFIX match — a real archive path that starts with
    "<entry>/" is excluded too, so adding a folder excludes its entire
    real subtree, not just files that happen to sit directly inside it.

Deliberately checked into the project (not gitignored, same as
research_status.json/parser_versions.json/embedded_media_skip_list_*.json
— see .gitignore, which only excludes the genuinely per-installation
files like ffs_archives.json) — direct request: "i want these selected
to be shared to github," so an examiner's own additions travel with the
repo for whoever else uses it next, the same way this project's other
hand-curated noise lists already do.

**Adding a new entry is gated by a separate, OFF-by-default preference**
(`user_media_ignore_editable`, Preferences ▸ Media Browser) — direct
request: "the option to add new files and folder to ignore... is off by
default[,] and when switch[ed] on tell the user the risk [of] hid[ing] a
folder... you should only add if you know what you're doing." Excluding
something here means it silently disappears from the "User Media"
filter specifically — real, if narrow, risk of an examiner (or whoever
inherits this list from the repo) missing genuine evidence, which is why
adding is opt-in and warned about while REMOVING an entry (undoing an
exclusion) is always allowed regardless of the preference."""

import json
import os
import sys

_cache = {"stat": None, "data": None}


def store_path() -> str:
    """Location of user_media_ignore_list.json (next to the exe when
    frozen, else config/ — same dev/frozen-path convention as
    research_store.py/embedded_media_skip_list.py)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "user_media_ignore_list.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "user_media_ignore_list.json")


def load() -> list:
    """Return the sorted list of ignored ui_paths, cached by file
    mtime+size — a missing file (never created, or deleted) is simply an
    empty list, not an error."""
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache["stat"], _cache["data"] = None, []
        return []
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        entries = raw.get("entries", [])
        data = sorted({e.strip() for e in entries
                      if isinstance(e, str) and e.strip()})
    except Exception:
        data = []
    _cache["stat"], _cache["data"] = stat, data
    return data


def _write(entries: set) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "_note": ("Folders/files excluded from the Media Browser's own "
                 "\"User Media\" filter -- hand-maintained via that "
                 "button's right-click menu, or Preferences' own "
                 "\"Manage Ignore List...\" button. See "
                 "user_media_ignore_list.py."),
        "version": 1,
        "entries": sorted(entries),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)
    _cache["stat"] = None   # force a re-read on the next load()


def add_entries(paths) -> None:
    """Add one or more ui_paths (folders or files) to the list."""
    entries = set(load())
    for p in paths:
        p = (p or "").strip()
        if p:
            entries.add(p)
    _write(entries)


def remove_entry(path: str) -> None:
    entries = set(load())
    entries.discard(path)
    _write(entries)


def is_ignored(ui_path: str, entries: list | None = None) -> bool:
    """True if *ui_path* is excluded by an exact entry, or sits inside a
    folder entry's own subtree. Pass *entries* (an already-loaded list)
    when checking many paths in a row, to avoid re-reading the store for
    each one."""
    if entries is None:
        entries = load()
    for e in entries:
        if ui_path == e or ui_path.startswith(e + '/'):
            return True
    return False
