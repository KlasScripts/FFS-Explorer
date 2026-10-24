"""config_seed.py — ship an examiner-editable config list with every
install. Qt-free.

PyInstaller 6 puts every datas= file under _internal/ (sys._MEIPASS), even
with dest='.' (tested 2026-10-24 with a throwaway onedir build), while an
editable list is read and saved next to the exe. So such a list ships as a
read-only SEED in _internal/config/, and merge_seed() copies its entries
into the editable file: a new install gets the whole list, an upgrade only
entries it hasn't been given before, and an entry the examiner removed
stays removed. The editable file records the shipped entries it has
already been given under SEEDED_KEY. Owner request 2026-10-24: "i want
this list to be with every install"."""

import json
import os
import sys

SEEDED_KEY = "seeded_from_install"


def bundled_seed_path(filename: str) -> str | None:
    """<_MEIPASS>/config/<filename> in a frozen build, else None."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    return os.path.join(base, "config", filename) if base else None


def read_doc(path: str) -> dict:
    """The JSON object in *path*, or {} if missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _entries(doc: dict) -> set:
    return {e.strip() for e in doc.get("entries", []) if isinstance(e, str) and e.strip()}


def merge_seed(seed_path: str | None, user_path: str, write) -> None:
    """Add the seed's entries not yet given to the user file, by calling
    *write(entries: set, seeded: set)*. A no-op when not frozen, when there
    is no seed, or when nothing is new. A write failure (e.g. a read-only
    install folder) leaves the list as it was."""
    if not seed_path or not os.path.exists(seed_path):
        return
    shipped = _entries(read_doc(seed_path))
    doc = read_doc(user_path)
    given = {e for e in doc.get(SEEDED_KEY, []) if isinstance(e, str)}
    new = shipped - given
    if not new:
        return
    try:
        write(_entries(doc) | new, given | shipped)
    except OSError:
        pass


def seeded_entries(user_path: str) -> set:
    """The shipped entries already given to the user file (kept on every
    rewrite of that file)."""
    return {e for e in read_doc(user_path).get(SEEDED_KEY, []) if isinstance(e, str)}
