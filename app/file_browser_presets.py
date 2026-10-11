"""file_browser_presets.py — examiner-editable, saved filter+sort presets
for the File Browser's preset dropdown (added 2026-10-11, direct request:
"i want a dropdown in the filebrowser view that let the user uses, edit
and creat the filter,sorts... for the edit and new i want to be able to
filter by multiple column... maybe group into the 3 column types t and
d, filetype and other and for each you can have multiple. then there
need to be a sort section... [that] is the same as you can sort by one
column after another").

This is the single shared store for BOTH that new File Browser dropdown
and the Media Browser's three Picture/Video/Graphics quick-filter buttons
(media_viewer.py's MediaViewerMixin) — a direct choice when scoping this
("this is were the video,graphics,picture preset are and they can be
altered" → asked to confirm whether editing one here should also change
the Media Browser button; answered "Fully unify both"). Editing/deleting
"Pictures" here changes both; a brand-new preset only ever shows up in
the File Browser dropdown (the Media Browser's own row stays a fixed
3-button toggle, mapped to whichever presets are literally named
"Pictures"/"Videos"/"Graphics" — see media_viewer.py's own
_SORT_PRESET_NAME_FOR_KIND).

A preset is:
    {"name": str,
     "date_filters": [{"column": header, "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}, ...],
     "type_filter": {"types": [label, ...]} | null,
     "text_filters": [{"columns": [header, ...], "mode": "contains"|"ends_with"|"equals",
                        "values": [str, ...]}, ...],
     "sort": [{"column": header, "descending": bool}, ...]}

date_filters/text_filters/sort are lists so "for each you can have
multiple" (direct request) holds for Time&Date (several ranges, e.g.
Modified AND Changed) and Other columns (several column+value criteria)
— every entry in a list is ANDed with the others. Within one text_filter
entry, "values" lets ONE criterion itself mean "any of these" (OR).
type_filter stays a single optional object, not a list — there's only
ever one real 'Type' column, so "multiple" there is just the existing
GUI's own multi-select checklist (any number of type labels checked),
already a single set, not independent entries.

Pictures/Videos/Graphics are shipped as TYPE filters (the File Type
checklist, NOT a text_filters/ends_with extension list) — revised
2026-10-11, direct correction after an earlier version used extension
matching: "i do not want you to use ext for the filter i want you to
seperate out png and gif into graphic type both via the ext and the
header scan[,] so that the filter only need to use the type without
filltering further and missing some images." The real fix was moving
the split UPSTREAM of this module entirely, into the Type system
itself: ffs-explorer.py's _EXT_TO_TYPE and header_scan.py's
classify_magic() BOTH now give PNG/GIF their own 'Graphic' Type,
separate from 'Picture' (see header_scan.py's GRAPHIC_EXTENSIONS/
_SIGNATURES) — so an extensionless PNG/GIF recovered only by magic
bytes gets 'Graphic' too, and a simple `type_filter: {"types":
["Graphic"]}` can never miss one the way an extension list could (the
"missing some images" risk the direct request named: TIFF/RAW already
showed as Type="Picture" via _EXT_TO_TYPE but were invisible to the old
extension-list approach, which only checked a handful of literal
extensions — a type_filter needs no such enumeration, and additionally
sidesteps the exact end-anchoring wildcard bug class this project
already hit once in user_media_ignore_list.py, since it never does
string matching on a filename at all).
"""

import json
import os
import sys

_cache = {"stat": None, "data": None}

BUILTIN_PRESET_NAMES = ('Pictures', 'Videos', 'Graphics')


def _builtin_media_preset(name: str, type_label: str, sort_column: str) -> dict:
    return {
        "name": name,
        "date_filters": [],
        "type_filter": {"types": [type_label]},
        "text_filters": [],
        "sort": [{"column": sort_column, "descending": True}],
    }


def default_presets() -> list:
    return [
        _builtin_media_preset("Pictures", "Picture", "Modified"),
        _builtin_media_preset("Videos", "Video", "Modified"),
        _builtin_media_preset("Graphics", "Graphic", "Size (Bytes)"),
    ]


def store_path() -> str:
    """Location of file_browser_presets.json (next to the exe when
    frozen, else config/ — same dev/frozen-path convention as
    user_media_ignore_list.py/scan_rules.py)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "file_browser_presets.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "file_browser_presets.json")


def load() -> list:
    """Return the saved list of presets (insertion order preserved),
    cached by file mtime+size. Seeds the three builtins on first-ever
    read if the file doesn't exist yet — a missing file is "never saved
    anything," not an error, same convention as every other hand-
    maintained list in this project."""
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        presets = default_presets()
        save(presets)
        return presets
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = raw.get("presets", [])
    except Exception:
        data = []
    _cache["stat"], _cache["data"] = stat, data
    return data


def find(name: str) -> dict | None:
    for p in load():
        if p.get("name") == name:
            return p
    return None


def save(presets: list) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "_note": ("Filter+sort presets for the File Browser's preset "
                 "dropdown (and the Media Browser's Picture/Video/"
                 "Graphics buttons, which read the three built-in "
                 "entries by name) -- hand-maintained via that "
                 "dropdown's Edit/New buttons. See "
                 "app/file_browser_presets.py."),
        "version": 1,
        "presets": presets,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)
    _cache["stat"] = None   # force a re-read on the next load()


def upsert(preset: dict) -> None:
    """Add *preset*, or replace the existing one with the same name."""
    presets = [p for p in load() if p.get("name") != preset["name"]]
    presets.append(preset)
    save(presets)


def delete(name: str) -> None:
    presets = [p for p in load() if p.get("name") != name]
    save(presets)


def match_text(cell: str, mode: str, values: list) -> bool:
    """True if *cell* (a real column value, never None/empty when this
    is called) matches *mode* against ANY of *values* — the OR-within-
    one-criterion semantics described in this module's own docstring."""
    cf = cell.casefold()
    for v in values:
        vf = v.casefold()
        if mode == "ends_with":
            if cf.endswith(vf):
                return True
        elif mode == "equals":
            if cf == vf:
                return True
        else:  # "contains" (the default)
            if vf in cf:
                return True
    return False
