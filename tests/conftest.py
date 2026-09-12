"""Shared pytest fixtures for ios-ffs-browser's test suite.

Every fixture here builds a genuinely real SQLite file — via ordinary
sqlite3 module calls (CREATE TABLE/INSERT/DELETE), never hand-encoded
bytes — so the on-disk format is 100% authentic, indistinguishable in
structure from any real evidence file. Only the *content* is synthetic;
the byte-level layout is written by the real SQLite engine, the same
methodology used throughout this project's own manual verification this
session (e.g. the WITHOUT ROWID discovery, the overflow-page fix).
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))


@pytest.fixture
def basic_db_raw(tmp_path) -> bytes:
    """A small, real SQLite file covering the specific real edge cases
    found and fixed in sqlite_carve.py this session:
      - messages: an ordinary rowid table, plus a real index on it
        (urls_url_index-equivalent — a real index page to resolve).
      - kv_store: a genuine `WITHOUT ROWID` table (the real Chromium
        `clusters_and_visits`/`cluster_visit_duplicates` bug this session
        found and fixed — physically index-shaped despite
        sqlite_master.type saying 'table').
      - a message long enough to force a real overflow page (the real
        sms_messages.py bug this session's own locate_live_row fix was
        built for).
    """
    db_path = str(tmp_path / "basic.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("CREATE INDEX idx_messages_body ON messages(body)")
    conn.execute("CREATE TABLE kv_store(k TEXT, v TEXT, PRIMARY KEY(k)) WITHOUT ROWID")
    conn.executemany("INSERT INTO messages(body) VALUES (?)",
                     [(f"hello world {i}",) for i in range(20)])
    # A payload long enough to spill onto an overflow page at this page
    # size (usable_size - 35 is the real inline threshold — see
    # sqlite_carve._cell_local_payload_size's own docstring).
    conn.execute("INSERT INTO messages(body) VALUES (?)", ("x" * 6000,))
    conn.executemany("INSERT INTO kv_store(k, v) VALUES (?, ?)",
                     [(f"key{i}", f"value{i}") for i in range(10)])
    conn.commit()
    conn.close()

    with open(db_path, "rb") as f:
        return f.read()


@pytest.fixture
def wal_db(tmp_path):
    """A real (base_raw, wal_raw) pair where the WAL genuinely holds a
    SUPERSEDED value a live query no longer sees — mirroring the real
    LINE fts_message_content docid=27 case this session found (a value
    present in the WAL, absent from the live table). Uses the same
    "blocking reader" trick verified earlier this session to prevent
    SQLite's own checkpoint-on-close from destroying the WAL before it
    can be read: a second connection holds a read transaction open while
    the write happens, so the writer's own close() can't checkpoint.

    Returns (base_raw, wal_raw, deleted_rowid, deleted_text) — the rowid
    and text of the row that's live in the WAL but genuinely gone from
    the live table, for tests to assert against.
    """
    db_path = str(tmp_path / "waltest.db")
    for ext in ("", "-wal", "-shm"):
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)

    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA page_size=4096")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, text TEXT)")
    writer.execute("INSERT INTO notes(text) VALUES ('kept row')")
    writer.commit()
    # Force a checkpoint now, before the blocker ever attaches, so the
    # schema + kept row land in the base file. Without this the writer
    # connection never closes before the blocker starts holding its read
    # lock, so no checkpoint would happen at all and base_raw would come
    # back with no trace of the notes table (verified empirically).
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    blocker = sqlite3.connect(db_path)
    blocker.execute("BEGIN")
    blocker.execute("SELECT * FROM notes").fetchall()

    writer.execute("INSERT INTO notes(text) VALUES ('this note gets deleted')")
    writer.commit()
    deleted_rowid = writer.execute(
        "SELECT id FROM notes WHERE text = 'this note gets deleted'").fetchone()[0]
    writer.execute("DELETE FROM notes WHERE id = ?", (deleted_rowid,))
    writer.commit()
    writer.close()   # blocker still open -- can't checkpoint

    with open(db_path, "rb") as f:
        base_raw = f.read()
    with open(db_path + "-wal", "rb") as f:
        wal_raw = f.read()

    blocker.close()
    return base_raw, wal_raw, deleted_rowid, "this note gets deleted"
