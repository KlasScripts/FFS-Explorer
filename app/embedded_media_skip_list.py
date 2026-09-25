"""embedded_media_skip_list.py — a global, hand-maintained list of
SQLite databases (and plists) an examiner has confirmed are never worth
running the embedded-media sweep against: either the file never holds
any embedded image/video at all, or whatever it does hold is always
app/UI chrome (icons, launcher art) with no evidentiary value. Direct
request, 2026-09-24: "we are reviewing some big db and it can take a
while to check them but there are some db that either have no media
file and are unlikely to ever have any and there will be some that do
have images/video but they are always going to be icon or some such
thing that will never be of use... i would like a [list] of sqlite db
that are not worth looking at."

Same architecture as research_store.py/photo_flags.json/parser_versions.json
— a hand-editable JSON file, GLOBAL (cross-case, since "this database is
always noise" is a property of the app/database itself, not one specific
case — the same map_cache.db is exactly as uninteresting on every other
Android case with Google Maps installed), cached by mtime+size so a
re-read only happens when the file actually changes. Deliberately NOT
auto-populated or guessed at from content — an entry only ever exists
because an examiner looked at a specific real database and decided it's
not worth scanning again, the same "escalate, don't silently decide for
the examiner" standing rule every other noise-filtering feature in this
project already follows (see app_intelligence.py's own removed
`known_real_store`/kept `_DB_NOISE_*` lists for the precedent this design
follows most closely).

**Split into two separate files, one per platform, 2026-09-24** — direct
follow-up: "i want to split for the user as when they view the skip list
it will confuse them if there [are] ios [entries] when looking at
android." Every public function below takes a `platform` argument
('android' or 'ios') and reads/writes `embedded_media_skip_list_android.json`
/ `embedded_media_skip_list_ios.json` accordingly — an Android case's own
View/Edit List dialog only ever shows (or lets you add to) the Android
file, never iOS entries and vice versa. This was already functionally
unnecessary for MATCHING correctness (an Android package name and an iOS
bundle id can never collide even sharing one file — a case is also always
one platform, so a cross-platform entry could never accidentally fire
either way), but the user-facing REVIEW experience is the real reason for
the split, not a correctness fix.

Each entry is a plain string, matched three ways:
  - No '/' in it: an exact, case-insensitive BASENAME match (e.g.
    "map_cache.db") — matches that filename wherever it appears in the
    archive, the common case for an app whose own cache file is always
    the same name across every install/case.
  - Contains '/' but no '*': a case-insensitive SUBSTRING match against
    the full ui_path — for disambiguating a generic filename (e.g.
    "cache.db") that's only confirmed noise under one specific app's own
    path.
  - Contains '*': a wildcard SUBSTRING match (each '*' matches any run of
    characters, case-insensitive) — for a path that's stable EXCEPT for
    one segment known to vary per case (an iOS container GUID, an
    Android per-user-profile numeric id). Added 2026-09-24, direct
    follow-up: FastZipBrowser._app_scoped_skip_entry generates these
    automatically for a known-variable segment it can't otherwise
    resolve to a stable identity (e.g. an iOS Bundle/Application
    container's own GUID, distinct from — and not covered by — the Data
    container GUID that's already resolved to a bundle id); an examiner
    can also type one by hand via the "Add…" dialog.
"""

import json
import os
import re
import sys

_PLATFORMS = ('android', 'ios')
_cache = {p: {"stat": None, "data": None} for p in _PLATFORMS}


def _check_platform(platform: str) -> str:
    if platform not in _PLATFORMS:
        raise ValueError(f"platform must be 'android' or 'ios', got {platform!r}")
    return platform


def store_path(platform: str) -> str:
    """Location of embedded_media_skip_list_<platform>.json (next to the
    exe when frozen, else config/ — same dev/frozen-path convention as
    research_store.py/photo_flags.json). One file per platform — see the
    module's own docstring for why."""
    _check_platform(platform)
    name = f"embedded_media_skip_list_{platform}.json"
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), name)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", name)


def load(platform: str) -> list:
    """Return the sorted list of skip-list entries for *platform*
    ('android' or 'ios'), cached by file mtime+size — a missing file
    (never created, or deleted) is simply an empty list, not an error."""
    _check_platform(platform)
    path = store_path(platform)
    cache = _cache[platform]
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache["stat"], cache["data"] = None, []
        return []
    if cache["stat"] == stat and cache["data"] is not None:
        return cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        entries = raw.get("entries", [])
        data = sorted({e.strip() for e in entries
                      if isinstance(e, str) and e.strip()})
    except Exception:
        data = []
    cache["stat"], cache["data"] = stat, data
    return data


def _write(platform: str, entries: set) -> None:
    path = store_path(platform)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "_note": ("Databases/plists confirmed not worth scanning for "
                 "embedded media, on this ONE platform -- hand-maintained "
                 "via the Process Case dialog's own 'View/Edit List' "
                 "button, or a file's right-click menu. See "
                 "embedded_media_skip_list.py."),
        "version": 1,
        "platform": platform,
        "entries": sorted(entries),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)
    _cache[platform]["stat"] = None   # force a re-read on the next load()


def add_entry(entry: str, platform: str) -> None:
    _check_platform(platform)
    entry = (entry or "").strip()
    if not entry:
        return
    entries = set(load(platform))
    entries.add(entry)
    _write(platform, entries)


def remove_entry(entry: str, platform: str) -> None:
    _check_platform(platform)
    entries = set(load(platform))
    entries.discard(entry)
    _write(platform, entries)


def _entry_matches(e_lower: str, name_lower: str, path_lower: str) -> bool:
    """The one place the three matching rules (bare basename / substring
    fragment / wildcard fragment) live — every public function below
    calls this rather than re-deriving the rule itself, so they can never
    silently drift apart. *e_lower*/*name_lower*/*path_lower* are all
    already lower-cased by the caller."""
    if '*' in e_lower:
        pattern = '.*'.join(re.escape(part) for part in e_lower.split('*'))
        return re.search(pattern, path_lower) is not None
    if '/' in e_lower:
        return e_lower in path_lower
    return name_lower == e_lower


def entry_matches_any_path(entry: str, ui_paths) -> bool:
    """True if a CANDIDATE entry (not yet saved) matches at least one
    real path in *ui_paths* — the reverse of matches()/
    find_matching_entries() (one entry against MANY paths, rather than
    one path against many entries), added 2026-09-24 for the "Add…"
    dialog's own validation: direct request, "when adding a new path can
    you validate it against the current ffs and warn the user if it is
    not there" — a typo, or an entry meant for a different case's own
    layout, would otherwise silently create a skip-list entry that never
    matches anything, giving false confidence a database is being
    excluded when it never actually is."""
    entry = (entry or "").strip().lower()
    if not entry:
        return False
    return any(_entry_matches(entry, p.rsplit('/', 1)[-1].lower(), p.lower())
              for p in ui_paths)


def matches(ui_path: str, entries: list | None = None, platform: str | None = None) -> bool:
    """True if *ui_path* should be skipped. *entries* lets a caller pass
    an already-loaded list (e.g. once per scan run) instead of re-reading
    the store for every candidate file; if omitted, *platform* ('android'
    or 'ios') says which platform's own store to load instead."""
    return bool(find_matching_entries(ui_path, entries, platform))


def find_matching_entries(ui_path: str, entries: list | None = None,
                          platform: str | None = None) -> list:
    """Every stored entry that matches *ui_path* — used by the File
    Browser's right-click "Remove from Embedded-Media Skip List" action
    to remove exactly the entries that actually apply to this file,
    rather than guessing at a single one (a basename entry added via
    this dialog and a longer disambiguating fragment added separately
    could both match the same real file). Pass *entries* directly (e.g.
    once per scan run) or *platform* to load that platform's own store
    fresh — exactly one of the two should be given."""
    if entries is None:
        entries = load(platform) if platform else []
    if not entries:
        return []
    name_lower = ui_path.rsplit('/', 1)[-1].lower()
    path_lower = ui_path.lower()
    return [e for e in entries if _entry_matches(e.lower(), name_lower, path_lower)]
