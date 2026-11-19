"""A table whose TEXT holds bytes that are not UTF-8 (WebKit IndexedDB
Records.key: binary keys) is carved: live rows are read as the carver
decodes text (U+FFFD for bad bytes), so a live row is never reported as
deleted and deleted rows are found (2026-11-19; before, one such value
made table_info give up on the whole table)."""
import sqlite3

import sqlite_reader as sr
import sqlite_recovery as rv


def test_table_with_binary_text_is_carved(tmp_path):
    p = str(tmp_path / 'idb.db')
    c = sqlite3.connect(p)
    c.execute('PRAGMA secure_delete=OFF')
    c.execute('CREATE TABLE Records (objectStoreID INTEGER, key TEXT, value BLOB, recordID INTEGER PRIMARY KEY)')
    for i in range(1, 21):
        c.execute("INSERT INTO Records VALUES (1, CAST(? AS TEXT), ?, ?)",
                  (b'\xff\xfe' + bytes([i]) + b'key', b'v' * 40, i))
    c.commit()
    c.execute('DELETE FROM Records WHERE recordID IN (5, 6, 7)')
    c.commit()
    c.close()
    conn, _ = sr.connect_committed(p)
    raw = open(p, 'rb').read()
    t = rv.table_info(conn, 'Records')
    assert t is not None and len(t.live) == 17
    assert '�' in t.live[1][1]
    recs, _ = rv.for_connection(raw, None, None, conn).records(t)
    conn.close()
    key = lambda i: (b'\xff\xfe' + bytes([i]) + b'key').decode('utf-8', 'replace')
    found = {r['values'][1] for r in recs if len(r['values']) > 1}
    # the deleted rows come back (a freed cell may lose its rowid to the
    # freeblock link, so matched by key), and no live row is listed
    assert {key(i) for i in (5, 6, 7)} <= found
    assert not found & {key(i) for i in t.live}
    assert not {r['rowid'] for r in recs} & set(t.live)


def test_ordinary_table_unchanged():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT)')
    conn.execute("INSERT INTO t VALUES (1, 'a')")
    assert rv._rows_by_rowid(conn, 't') == {1: (1, 'a')}
    assert conn.text_factory is str
