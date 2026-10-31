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
always one platform anyway. Each entry matches three ways:
  - An EXACT match — this one specific file (or folder, along with
    everything inside it — see below) is excluded.
  - A FOLDER-PREFIX match — a real archive path that starts with
    "<entry>/" is excluded too, so adding a folder excludes its entire
    real subtree, not just files that happen to sit directly inside it.
  - A WILDCARD match (added 2026-10-10, direct request) — an entry
    containing '*' is a regex-style fragment match (each '*' matches
    any run of characters), same convention embedded_media_skip_list.py
    already uses, reused rather than reinvented — EXCEPT that a pattern
    with no TRAILING '*' is anchored to the real end of the path, not
    just "appears somewhere" (added 2026-10-10, direct correction: see
    _wildcard_matches' own docstring for the real bug this fixed).
    "*@2x.png*" / "*@3x.png*" (confirmed safe/generalizable across 3
    real iOS devices, 2026-10-10 — see KNOWN_PATTERN_LABELS below)
    match any PNG whose name contains that scale fragment. Deliberately
    PNG-only, not "any extension" — direct correction the same day:
    checked all 8 real "@2x"/"@3x" matches that were .jpg instead, and
    every one was a real Microsoft Edge/Google Chrome tab-switcher
    snapshot (an actual rendered screenshot of whatever page was open,
    "Library/Application Support/.../Snapshots/{id}/NNN@2x.jpg") — real
    per-session content, not bundled chrome, despite sharing the same
    Retina-scale naming convention; all 6,073 real .png matches stayed
    confirmed generic icons. Unlike
    the exact/folder-prefix modes, a wildcard entry is NOT itself a
    "this one location is noise" judgment about one folder — it's a
    structural, cross-app rule, which is exactly why it needs to be
    high-confidence (Apple's own Retina-asset-catalog naming
    convention, never used for real photos/videos) before being added
    at all; a duplicate-FILENAME rule without such a structural anchor
    was tried and rejected the same day — see CLAUDE.md's own "User
    Media" entry for why (it would have caught 2,491 real photo
    thumbnails, "5005.JPG", as false positives). Three more added the
    same day, same bar (a platform-reserved convention, never real
    user content, confirmed by checking EVERY matching file in a real
    archive, not a sample): "*__MACOSX/*._*" (the AppleDouble resource-
    fork sidecar every Mac-created zip leaves, preserving the real
    file's own subdirectory structure — "__MACOSX/images/._seq_0.png"
    for a real "images/seq_0.png" — verified by its own literal file
    signature, not just the name, across all 2,814 real matches in one
    archive, present on both iOS and Android);
    "*.9.png" (Android's reserved "nine-patch" stretchable-UI-chrome
    format — the encoding itself requires a border-pixel structure a
    real photo can't have); "*/drawable-*dpi*" and "*/mipmap-*dpi*"
    (Android's own reserved density-bucket resource folder naming).

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

import functools
import json
import os
import re
import sys

import config_seed

_cache = {"stat": None, "data": None}
_seed_merged = False

KNOWN_PATTERN_LABELS = {
    "*@2x.png*": "UI Graphics (@2x)",
    "*@3x.png*": "UI Graphics (@3x)",
    "*__MACOSX/*._*": "Zip Metadata (macOS)",
    "*.9.png": "UI Graphics (nine-patch)",
    "*/drawable-*dpi*": "UI Graphics (Android resource)",
    "*/mipmap-*dpi*": "UI Graphics (Android resource)",
}


def display_label(entry: str) -> str:
    """A short, human name for *entry* if it's a known pattern (shown in
    the "User Media Folders" dialog and the ignore-list review grid),
    else the raw entry string itself."""
    return KNOWN_PATTERN_LABELS.get(entry, entry)


def store_path() -> str:
    """Location of user_media_ignore_list.json (next to the exe when
    frozen, else config/ — same dev/frozen-path convention as
    research_store.py/embedded_media_skip_list.py)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "user_media_ignore_list.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "user_media_ignore_list.json")


def bundled_seed_path() -> str | None:
    """The copy shipped inside a frozen build (see config_seed.py)."""
    return config_seed.bundled_seed_path("user_media_ignore_list.json")


def _merge_bundled_seed() -> None:
    """Once per run: add shipped entries this install hasn't been given
    before (see config_seed.merge_seed). Until 2026-10-24 the list shipped
    with dest='.', which PyInstaller 6 puts under _internal/, so the
    frozen build never read it and started with an empty list."""
    global _seed_merged
    if _seed_merged:
        return
    _seed_merged = True
    config_seed.merge_seed(bundled_seed_path(), store_path(),
                           lambda entries, seeded: _write(entries, seeded))


def load() -> list:
    """Return the sorted list of ignored ui_paths, cached by file
    mtime+size — a missing file (never created, or deleted) is simply an
    empty list, not an error."""
    _merge_bundled_seed()
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


def _write(entries: set, seeded: set | None = None) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if seeded is None:      # keep the record of shipped entries already given
        seeded = config_seed.seeded_entries(path)
    doc = {
        "_note": ("Folders/files excluded from the Media Browser's own "
                 "\"User Media\" filter -- hand-maintained via that "
                 "button's right-click menu, or Preferences' own "
                 "\"Manage Ignore List...\" button. See "
                 "user_media_ignore_list.py."),
        "version": 1,
        "entries": sorted(entries),
    }
    if seeded:
        doc[config_seed.SEEDED_KEY] = sorted(seeded)
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


def _wildcard_matches(pattern: str, ui_path: str) -> bool:
    """True if *pattern* (containing '*') matches *ui_path* — each '*'
    matches any run of characters, same convention
    embedded_media_skip_list.py's own _entry_matches uses. UNLIKE that
    module, a pattern with NO trailing '*' is anchored to the END of
    ui_path, not just "appears somewhere" — added 2026-10-10, direct
    correction: "*.9.png" (no trailing '*') matched
    "version1.9.pngBACKUP.zip" before this fix, since the real file
    extension there is ".zip", not ".png" — the substring just happened
    to appear mid-path. A pattern that genuinely wants "appears
    anywhere, more can follow" (e.g. "*@2x.*" — more of the real
    extension always follows "@2x.") must say so explicitly with a
    trailing '*'."""
    return _wildcard_regex(pattern).search(ui_path) is not None


@functools.lru_cache(maxsize=1024)
def _wildcard_regex(pattern: str) -> "re.Pattern":
    """*pattern* compiled once: built per call, it cost 3.3 s of frozen
    window on IOS17 (163,000 checks after one tick of the root)."""
    regex = '.*'.join(re.escape(part) for part in pattern.split('*'))
    if not pattern.endswith('*'):
        regex += '$'
    return re.compile(regex)


def is_ignored(ui_path: str, entries: list | None = None) -> bool:
    """True if *ui_path* is excluded by an exact entry, sits inside a
    folder entry's own subtree, or matches a wildcard entry (one
    containing '*' — see this module's own docstring). Pass *entries*
    (an already-loaded list) when checking many paths in a row, to
    avoid re-reading the store for each one."""
    if entries is None:
        entries = load()
    for e in entries:
        if '*' in e:
            if _wildcard_matches(e, ui_path):
                return True
        elif ui_path == e or ui_path.startswith(e + '/'):
            return True
    return False
