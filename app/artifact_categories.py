"""artifact_categories.py — the category, and app, each artifact report is
listed under in Run Artifact Parsers and the Artifact Viewer
(Category → App → Report). Qt-free.

The mapping is config/artifact_categories.json, keyed by script name per
platform, not a declaration in each parser: a parser's version is a hash
of its file (parser_versions.py), so writing a category into every parser
would mark every existing report as "parser changed". A parser's own
module-level `category` / `app_group_label` wins over the file.

A category not in the file's list, or a parser the file doesn't name and
that declares none, is listed under OTHER: shown, never hidden.

An app node is only made for an app with more than one report; a single
report sits directly under its category.
"""
import json
import os
import sys

OTHER = "Other (category not set)"
_cache = {"stat": None, "data": None}


def store_path() -> str:
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "config", "artifact_categories.json")


def _load() -> dict:
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _cache["stat"], _cache["data"] = stat, data
    return data


def categories() -> list:
    """The categories in display order, then OTHER."""
    return list(_load().get("categories") or []) + [OTHER]


def classify(platform: str, script_name: str, mod=None) -> tuple:
    """(category, app or None) for one report."""
    entry = (_load().get(platform) or {}).get(script_name) or {}
    cat = getattr(mod, 'category', None) or entry.get("category")
    app = getattr(mod, 'app_group_label', None) or entry.get("app")
    if cat not in (_load().get("categories") or []):
        cat = OTHER
    return cat, app


def arrange(platform: str, items) -> list:
    """[(category, [(app or None, [(script_name, mod), ...]), ...]), ...]
    for *items* [(script_name, mod)], categories in display order (empty
    ones left out), and within one: apps with several reports first by
    name, then single reports by their name. An app with one report is
    given as (None, [that report])."""
    by_cat: dict = {}
    for sn, mod in items:
        cat, app = classify(platform, sn, mod)
        by_cat.setdefault(cat, {}).setdefault(app, []).append((sn, mod))
    name = lambda sm: (getattr(sm[1], 'name', None) or sm[0]).lower()
    out = []
    for cat in categories():
        apps = by_cat.get(cat)
        if not apps:
            continue
        groups, singles = [], []
        for app, members in apps.items():
            if app and len(members) > 1:
                groups.append((app, sorted(members, key=lambda sm: (
                    0 if getattr(sm[1], 'is_group_overview', False) else 1,
                    getattr(sm[1], 'group_sort_key', 100), name(sm)))))
            else:
                singles.extend(members)
        groups.sort(key=lambda g: g[0].lower())
        out.append((cat, groups + [(None, [m]) for m in sorted(singles, key=name)]))
    return out
