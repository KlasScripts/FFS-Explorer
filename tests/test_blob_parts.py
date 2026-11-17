"""db_utils.save_blob / load_blob / open_blob: a blob larger than one part is
stored in parts and read back whole, both ways; a smaller one replaces the
parts of an earlier larger one; a missing part or another version is never
read as half a blob. SQLite's own value limit, scaled down, shows the
failure the parts avoid ("string or blob too big", 2026-11-17)."""
import os
import sqlite3

import pytest

import db_utils


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db_utils, 'BLOB_PART', 1000)
    c = sqlite3.connect(str(tmp_path / 'c.db'))
    c.execute('CREATE TABLE blobs (key TEXT PRIMARY KEY, version TEXT, data BLOB)')
    c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1500)      # one value may not pass 1,500 bytes
    yield c
    c.close()


def test_large_blob_in_parts(conn):
    data = os.urandom(4321)
    with pytest.raises(sqlite3.DataError):             # what a single value did
        conn.execute("INSERT INTO blobs VALUES ('x', '1', ?)", (data,))
    db_utils.save_blob(conn, 'load_snapshot', '5', data)
    assert conn.execute('SELECT count(*) FROM blobs').fetchone()[0] == 5
    assert db_utils.load_blob(conn, 'load_snapshot', '5') == data
    f = db_utils.open_blob(conn, 'load_snapshot', '5')
    got = b''
    while True:
        chunk = f.read(777)
        if not chunk:
            break
        got += chunk
    f.close()
    assert got == data
    assert db_utils.load_blob(conn, 'load_snapshot', '4') is None


def test_smaller_blob_replaces_parts_and_missing_part_is_refused(conn):
    db_utils.save_blob(conn, 'load_snapshot', '5', b'a' * 3500)
    db_utils.save_blob(conn, 'load_snapshot', '5', b'b' * 10)
    assert conn.execute('SELECT count(*) FROM blobs').fetchone()[0] == 1
    assert db_utils.load_blob(conn, 'load_snapshot', '5') == b'b' * 10
    db_utils.save_blob(conn, 'load_snapshot', '5', b'c' * 2500)
    conn.execute("UPDATE blobs SET version='4' WHERE key=?", ('load_snapshot\x1fpart2',))
    assert db_utils.load_blob(conn, 'load_snapshot', '5') is None
    assert db_utils.open_blob(conn, 'load_snapshot', '5') is None
