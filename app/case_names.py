"""case_names.py — bring a case made by an older version up to the current
file-name rule (ASSUMPTIONS.md A-80).

Older versions read zip names with Python's zipfile: cp437 when a name had
no UTF-8 flag (GrayKey writes UTF-8 without it, so those names were
garbled), and on Windows every backslash became "/" (so iOS Mail's
"[Gmail]/\\Sent.imapmbox" became "[Gmail]//Sent.imapmbox"). The case data
kept those names. zip_index records, for every entry the rule changes, the
name an older version gave it (ZipIndex.renames()); this module rewrites
those paths wherever the case stores one, once per case:

  * caseresults.db and casecache.db: every path column (bookmarks, search
    hits and scope, embedded-media hits, processing results, thumbnails,
    ...). A stored path is changed only where it is the renamed file, or a
    path inside a renamed folder, and its leading part matches that entry's
    own folder in the zip.
  * casecache.db: the saved folder tree and the search file list are
    deleted, so the next open rebuilds them from the index.
  * One run_log row (run_type 'zip_name_rule') lists every name pair and
    how many stored paths each table had changed.

Report tables are not changed: a report keeps the paths it was run with
until the parser is run again.

When a changed path collides with one already stored under the new name
(the same file, recorded again after the change), the old-named row is
removed and counted in the run log. Qt-free.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3

NAME_RULE = "A-80"
SETTING = "zip_name_rule"

# (database, table, column) for every column that holds an archive path.
_PATH_COLUMNS = (
    ("caseresults.db", "bookmark_entries", "ui_path"),
    ("caseresults.db", "search_results", "filename"),
    ("caseresults.db", "search_scope_files", "ui_path"),
    ("caseresults.db", "embedded_media_hits", "source_ui_path"),
    ("caseresults.db", "cache_record_sources", "source_ui_path"),
    ("caseresults.db", "media_seen", "ui_path"),
    ("caseresults.db", "artifact_attachment_links", "ui_path"),
    ("casecache.db", "thumbnails", "ui_path"),
    ("casecache.db", "header_types", "ui_path"),
    ("casecache.db", "media_metadata", "ui_path"),
    ("casecache.db", "duplicate_files", "ui_path"),
    ("casecache.db", "nested_archives", "ui_path"),
    ("casecache.db", "nested_archive_entries", "archive_ui_path"),
    ("casecache.db", "evidence_page_map", "ui_path"),
    ("casecache.db", "leveldb_search_index", "folder_ui_path"),
    ("casecache.db", "processing_registry", "ui_path"),
)

# Saved views built from the old names; each is rebuilt when missing.
_REBUILT_BLOBS = ("load_snapshot", "folder_data", "search_entries")
# Of those, the ones only an earlier open can have saved: their presence
# means an older version used the case. (search_entries is not one: on a
# case's first open, the search's own file list is being built from the
# new names at the same time.)
_EARLIER_OPEN_BLOBS = ("load_snapshot", "folder_data")

_OR_TERMS = 200          # instr() terms per query


class _Renamer:
    """Rewrites stored paths for a list of (old zip name, new zip name).

    A stored path is the zip name with a leading part removed (the format's
    prefix, e.g. "filesystem1/" or "/private/var/"), so each pair is split
    where old and new first differ, at the start of that level: the tail
    from there must end the stored path's matching part, and the part before
    it must end the zip name's own unchanged folders."""

    def __init__(self, pairs):
        self._by_tail: dict[str, list[tuple[str, str]]] = {}
        for old, new in pairs:
            k = 0
            while k < min(len(old), len(new)) and old[k] == new[k]:
                k += 1
            start = old.rfind("/", 0, k) + 1
            self._by_tail.setdefault(old[start:], []).append((old[:start], new[start:]))
        self.tails = sorted(self._by_tail, key=len, reverse=True)
        alt = "|".join(re.escape(t) for t in self.tails)
        self._re = re.compile(rf"(?:(?<=/)|^)({alt})(?=/|$)") if alt else None

    def convert(self, path: str) -> str:
        """*path* with the first renamed level replaced, or *path* unchanged
        when nothing matches unambiguously."""
        if self._re is None:
            return path
        pos = 0
        while True:
            m = self._re.search(path, pos)
            if m is None:
                return path
            head = path[:m.start(1)]
            news = {new for zip_head, new in self._by_tail[m.group(1)] if zip_head.endswith(head)}
            if len(news) == 1:
                return head + news.pop() + path[m.end(1):]
            pos = m.start(1) + 1


def _columns(con, table: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _rewrite(con, renamer: _Renamer, table: str, column: str) -> tuple[int, int]:
    """(rows changed, old-named rows removed as duplicates) for one column."""
    if column not in _columns(con, table):
        return 0, 0
    changed = removed = 0
    seen = set()
    for k in range(0, len(renamer.tails), _OR_TERMS):
        terms = renamer.tails[k:k + _OR_TERMS]
        where = " OR ".join(f"instr({column}, ?) > 0" for _ in terms)
        rows = con.execute(f"SELECT rowid, {column} FROM {table} WHERE {where}", terms).fetchall()
        for rowid, value in rows:
            if rowid in seen or not isinstance(value, str):
                continue
            seen.add(rowid)
            new = renamer.convert(value)
            if new == value:
                continue
            try:
                con.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (new, rowid))
                changed += 1
            except sqlite3.IntegrityError:
                con.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                removed += 1
    return changed, removed


def migrate(zip_path: str, case_dir: str, index_opener) -> dict | None:
    """Update the case once. *index_opener()* returns the archive's
    ZipIndex (or None when it cannot be built yet). Returns a summary when
    anything was renamed, None when the case was already current or had
    nothing to change (the setting is still recorded then)."""
    # Opened (and created on a new case) the app's own way: on a case's
    # first open caseresults.db does not exist yet, and skipping it left a
    # new case unmarked, so its next open took it for an older version's.
    from db_utils import _open_results_db
    res = _open_results_db(case_dir)
    try:
        row = res.execute("SELECT value FROM case_settings WHERE key = ?", (SETTING,)).fetchone()
        if row and row[0] == NAME_RULE:
            return None
        index = index_opener()
        if index is None:
            return None                      # try again on the next open
        try:
            pairs = index.renames()
        finally:
            index.close()
        summary = None
        if pairs:
            renamer = _Renamer(pairs)
            counts: dict[str, list[int]] = {}
            views_dropped = 0
            cache_path = os.path.join(case_dir, "casecache.db")
            if os.path.exists(cache_path):
                cache = sqlite3.connect(cache_path, timeout=30)
                try:
                    with cache:
                        for db, table, column in _PATH_COLUMNS:
                            if db == "casecache.db":
                                counts[f"{table}.{column}"] = list(_rewrite(cache, renamer, table, column))
                        views_dropped = cache.execute(
                            f"SELECT COUNT(*) FROM blobs WHERE key IN ({','.join('?' * len(_EARLIER_OPEN_BLOBS))})",
                            _EARLIER_OPEN_BLOBS).fetchone()[0]
                        cache.execute(
                            f"DELETE FROM blobs WHERE key IN ({','.join('?' * len(_REBUILT_BLOBS))})",
                            _REBUILT_BLOBS)
                finally:
                    cache.close()
            with res:
                for db, table, column in _PATH_COLUMNS:
                    if db == "caseresults.db":
                        counts[f"{table}.{column}"] = list(_rewrite(res, renamer, table, column))
            counts = {k: v for k, v in counts.items() if v != [0, 0]}
        if pairs and (counts or views_dropped):    # a case an older version had used
            summary = {"rule": NAME_RULE, "names": [list(p) for p in pairs],
                       "paths_changed": {k: v[0] for k, v in counts.items()},
                       "duplicates_removed": {k: v[1] for k, v in counts.items() if v[1]},
                       "saved_views_rebuilt": views_dropped}
            with res:
                res.execute(
                    "INSERT INTO run_log (run_type, total, processed, output_rows, complete, notes, completed_at)"
                    " VALUES ('zip_name_rule', ?, ?, ?, 1, ?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
                    (len(pairs), sum(v[0] for v in counts.values()), sum(v[1] for v in counts.values()),
                     json.dumps(summary, ensure_ascii=False)))
        with res:
            res.execute("INSERT OR REPLACE INTO case_settings (key, value) VALUES (?, ?)", (SETTING, NAME_RULE))
        return summary
    finally:
        res.close()
