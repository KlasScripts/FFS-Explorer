"""case_names: a case made by an older version gets the A-80 names.

The case databases are built here with the old names in them, so which
stored paths must change (and which must not) is known before the code
runs."""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import case_names as cn  # noqa: E402

MAIL = "mobile/Library/Mail/F86/[Gmail]"
PAIRS = [
    # Cellebrite-style zip names (prefix "filesystem1/private/var/"), read on Windows.
    (f"filesystem1/private/var/{MAIL}//Sent.imapmbox", f"filesystem1/private/var/{MAIL}/\\Sent.imapmbox"),
    (f"filesystem1/private/var/{MAIL}//Sent.imapmbox/Info.plist",
     f"filesystem1/private/var/{MAIL}/\\Sent.imapmbox/Info.plist"),
    # GrayKey-style name (prefix "/"), garbled by cp437.
    ("/private/var/db/chronod/1170-├ù-2436.png", "/private/var/db/chronod/1170-×-2436.png"),
]


class _Index:
    def __init__(self, pairs):
        self.pairs, self.closed = pairs, False

    def renames(self):
        return self.pairs

    def close(self):
        self.closed = True


def _case(tmp_path):
    res = sqlite3.connect(tmp_path / "caseresults.db")
    res.executescript("""
        CREATE TABLE case_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE run_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_type TEXT NOT NULL,
            run_at TEXT, completed_at TEXT, total INTEGER, processed INTEGER,
            output_rows INTEGER, complete INTEGER NOT NULL DEFAULT 0, notes TEXT);
        CREATE TABLE bookmark_entries (id INTEGER PRIMARY KEY, group_id INTEGER, ui_path TEXT NOT NULL,
            UNIQUE(group_id, ui_path));
        CREATE TABLE search_results (term_id INTEGER, filename TEXT, offset INTEGER, context TEXT);
    """)
    res.executemany("INSERT INTO bookmark_entries (group_id, ui_path) VALUES (?, ?)", [
        (1, f"{MAIL}//Sent.imapmbox/Info.plist"),          # the file, old name
        (1, f"{MAIL}//Sent.imapmbox"),                     # its folder, old name
        (1, f"{MAIL}/Sent.imapmbox/Info.plist"),           # a different, real path: unchanged
        (1, "private/var/db/chronod/1170-├ù-2436.png"),
        (1, "other/db/chronod/1170-├ù-2436.png"),  # same file name, other folder: unchanged
        (2, f"{MAIL}//Sent.imapmbox"),                     # old name of a row group 2 already has new
        (2, f"{MAIL}/\\Sent.imapmbox"),
    ])
    res.execute("INSERT INTO search_results VALUES (1, ?, 10, 'x')",
                (f"{MAIL}//Sent.imapmbox/Info.plist",))
    res.commit()
    res.close()
    cache = sqlite3.connect(tmp_path / "casecache.db")
    cache.executescript("""
        CREATE TABLE blobs (key TEXT PRIMARY KEY, version TEXT, data BLOB);
        CREATE TABLE header_types (ui_path TEXT PRIMARY KEY, detected_type TEXT);
    """)
    cache.executemany("INSERT INTO blobs VALUES (?, '1', x'00')",
                      [("load_snapshot",), ("folder_data",), ("search_entries",), ("keep_me",)])
    cache.execute("INSERT INTO header_types VALUES (?, 'png')",
                  ("private/var/db/chronod/1170-├ù-2436.png",))
    cache.commit()
    cache.close()


def _rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_old_names_are_updated_once_and_logged(tmp_path):
    _case(tmp_path)
    index = _Index(PAIRS)
    summary = cn.migrate("x.zip", str(tmp_path), lambda: index)
    assert index.closed
    res = tmp_path / "caseresults.db"
    got = _rows(res, "SELECT group_id, ui_path FROM bookmark_entries ORDER BY id")
    assert got == [
        (1, f"{MAIL}/\\Sent.imapmbox/Info.plist"),
        (1, f"{MAIL}/\\Sent.imapmbox"),
        (1, f"{MAIL}/Sent.imapmbox/Info.plist"),
        (1, "private/var/db/chronod/1170-×-2436.png"),
        (1, "other/db/chronod/1170-├ù-2436.png"),
        (2, f"{MAIL}/\\Sent.imapmbox"),
    ]
    assert _rows(res, "SELECT filename FROM search_results") == [(f"{MAIL}/\\Sent.imapmbox/Info.plist",)]
    cache = tmp_path / "casecache.db"
    assert _rows(cache, "SELECT ui_path FROM header_types") == [("private/var/db/chronod/1170-×-2436.png",)]
    assert _rows(cache, "SELECT key FROM blobs") == [("keep_me",)]
    assert summary["paths_changed"] == {"bookmark_entries.ui_path": 3, "search_results.filename": 1,
                                        "header_types.ui_path": 1}
    assert summary["duplicates_removed"] == {"bookmark_entries.ui_path": 1}
    (run_type, notes), = _rows(res, "SELECT run_type, notes FROM run_log")
    assert run_type == "zip_name_rule" and json.loads(notes)["names"] == [list(p) for p in PAIRS]
    # Recorded: the next open does nothing, and does not even open the index.
    assert cn.migrate("x.zip", str(tmp_path), lambda: 1 / 0) is None


def test_a_case_with_nothing_renamed_is_marked_current(tmp_path):
    _case(tmp_path)
    assert cn.migrate("x.zip", str(tmp_path), lambda: _Index([])) is None
    assert _rows(tmp_path / "caseresults.db", "SELECT value FROM case_settings") == [(cn.NAME_RULE,)]
    assert len(_rows(tmp_path / "casecache.db", "SELECT key FROM blobs")) == 4


def test_no_index_yet_means_try_again_next_open(tmp_path):
    _case(tmp_path)
    assert cn.migrate("x.zip", str(tmp_path), lambda: None) is None
    assert _rows(tmp_path / "caseresults.db", "SELECT value FROM case_settings") == []


def test_a_new_case_gets_no_log_entry(tmp_path):
    """Renames exist (the archive has such names), but nothing was stored
    by an older version: nothing to report."""
    _case(tmp_path)
    for db, sql in (("caseresults.db", "DELETE FROM bookmark_entries; DELETE FROM search_results"),
                    ("casecache.db", "DELETE FROM blobs WHERE key != 'search_entries'; DELETE FROM header_types")):
        con = sqlite3.connect(tmp_path / db)
        con.executescript(sql)
        con.close()
    assert cn.migrate("x.zip", str(tmp_path), lambda: _Index(PAIRS)) is None
    assert _rows(tmp_path / "caseresults.db", "SELECT * FROM run_log") == []
    assert _rows(tmp_path / "caseresults.db", "SELECT value FROM case_settings") == [(cn.NAME_RULE,)]
    # The search's file list, being built at the same time on a first open,
    # is not a sign of an older version (it is still rebuilt).
    assert _rows(tmp_path / "casecache.db", "SELECT key FROM blobs") == []


def test_first_open_creates_the_results_db_and_marks_the_case(tmp_path):
    """On a case's first open there is no caseresults.db yet; the case is
    still marked current, so the next open does nothing."""
    assert cn.migrate("x.zip", str(tmp_path), lambda: _Index(PAIRS)) is None
    assert _rows(tmp_path / "caseresults.db",
                 "SELECT value FROM case_settings WHERE key = 'zip_name_rule'") == [(cn.NAME_RULE,)]
    assert cn.migrate("x.zip", str(tmp_path), lambda: 1 / 0) is None
