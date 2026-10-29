"""epoch_store.py — which database columns hold an epoch time, and in which
format, for the File Preview Database tab. Qt-free.

Three layers, highest first:
  1. The examiner's own choices from the Epoch Formats dialog, saved next
     to the exe (config/ when run from source) in epoch_formats_user.json.
     An empty format means "not a time": it hides a built-in setting or a
     guess for that column.
  2. Built-in settings for well-known databases, config/epoch_formats.json
     (shipped inside the build under _internal/config).
  3. epoch_guess.guess() on the column's own values.

An entry is a database path, a table, a column and a format code from
epoch_guess.FORMATS. The path is the canonical app path: an iOS container
GUID is replaced by the app's bundle id (FfsAdapter.canonical_app_path),
so one entry applies to every phone and every install of the app. A
leading "Dump/" or "private/var/" is dropped, and an Android user number
(data/user/0, system_ce/0) is written as data/data and system_ce/*.

A built-in entry may give the path from the app id on, e.g.
"com.whatsapp/databases/msgstore.db": it matches any path ending with it.
An examiner entry is saved with the full canonical path.
"""

from __future__ import annotations

import json
import os
import re
import sys

import config_seed

_FILENAME = "epoch_formats.json"
_USER_FILENAME = "epoch_formats_user.json"
_cache: dict = {}


def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def builtin_path() -> str:
    return config_seed.bundled_seed_path(_FILENAME) or os.path.join(_root(), "config", _FILENAME)


def user_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), _USER_FILENAME)
    return os.path.join(_root(), "config", _USER_FILENAME)


def normalize_path(path: str) -> str:
    text = (path or "").replace("\\", "/").lstrip("/")
    for prefix in ("Dump/", "private/var/"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = re.sub(r"^data/user/\d+/", "data/data/", text)
    text = re.sub(r"^data/(system_ce|system_de|user_de|misc_ce|misc_de)/\d+/", r"data/\1/*/", text)
    return text


def _load(path: str) -> list[dict]:
    try:
        st = os.stat(path)
    except OSError:
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    rows = []
    for e in config_seed.read_doc(path).get("formats", []):
        if not isinstance(e, dict):
            continue
        p, t, c = e.get("path"), e.get("table"), e.get("column")
        if isinstance(p, str) and isinstance(t, str) and isinstance(c, str):
            rows.append({"path": normalize_path(p), "table": t, "column": c,
                         "format": e.get("format") or ""})
    _cache[path] = (stamp, rows)
    return rows


def _matches(entry_path: str, path: str) -> bool:
    return path == entry_path or path.endswith("/" + entry_path)


def lookup(path: str, table: str, column: str) -> tuple[str | None, str | None]:
    """(format code, source) for one column, or (None, None) when no
    entry covers it. source is "yours" or "built-in". A code of "" means
    the examiner marked the column as not a time."""
    path = normalize_path(path)
    for source, rows in (("yours", _load(user_path())), ("built-in", _load(builtin_path()))):
        for e in rows:
            if e["table"] == table and e["column"] == column and _matches(e["path"], path):
                return e["format"], source
    return None, None


def builtin(path: str, table: str, column: str) -> str | None:
    """The built-in code for one column, ignoring the examiner's own file."""
    path = normalize_path(path)
    for e in _load(builtin_path()):
        if e["table"] == table and e["column"] == column and _matches(e["path"], path):
            return e["format"]
    return None


def save_user(path: str, table: str, choices: dict) -> None:
    """Record the examiner's choice for each column in *choices*
    ({column: code, "" for not a time, None to drop the saved choice})."""
    path = normalize_path(path)
    file = user_path()
    rows = [e for e in _load(file)
            if not (e["path"] == path and e["table"] == table and e["column"] in choices)]
    for column, code in choices.items():
        if code is not None:
            rows.append({"path": path, "table": table, "column": column, "format": code})
    doc = {
        "_note": ("Epoch formats chosen in the Database tab's Epoch Formats dialog. "
                  "Overrides config/epoch_formats.json and the guess. An empty "
                  "format means the column is not a time. See epoch_store.py."),
        "version": 1,
        "formats": sorted(rows, key=lambda e: (e["path"], e["table"], e["column"])),
    }
    os.makedirs(os.path.dirname(file), exist_ok=True)
    tmp = file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, file)
    _cache.pop(file, None)
