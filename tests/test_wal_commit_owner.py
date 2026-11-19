"""Records in WAL frames of a page that is free now are given to the table
whose leaf the page was when the frame's transaction committed (the
database as of that commit: main file + WAL frames up to it), table
certain, older versions of live rows included (2026-11-19; before, they
were dropped: IOS17 AddressBookImages, three earlier contact photos of
live rows). Ground truth: real SQLite in WAL mode, every version it wrote
logged; two tables with the same column count, so a record given to the
wrong table shows."""
import os
import shutil
import sqlite3

import sqlite_reader as sr
import sqlite_recovery as rv


def _blob(tag, n):
    return (b'\x89PNG\r\n\x1a\n' + tag.encode() * n)[: 8 + len(tag) * n]


def _build(tmp_path):
    p = str(tmp_path / 'live.db')
    c = sqlite3.connect(p, isolation_level=None)
    c.execute('PRAGMA page_size=1024')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA wal_autocheckpoint=0')
    truth = {'a': set(), 'b': set()}

    def put(t, rid, tag, n):
        v = (rid, tag, _blob(tag, n))
        c.execute(f'INSERT OR REPLACE INTO {t} VALUES (?,?,?)', v)
        truth[t].add(v)
    c.execute('CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT, img BLOB)')
    c.execute('CREATE TABLE b (id INTEGER PRIMARY KEY, name TEXT, img BLOB)')
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')        # the schema in the main file
    for t in ('a', 'b'):
        c.execute('BEGIN')
        for rid in range(1, 31):
            put(t, rid, f'{t}{rid}x', 60)          # ~240 bytes: several rows per leaf
        c.execute('COMMIT')
    for t in ('a', 'b'):                           # older versions of rows 3 and 4, with overflow
        put(t, 3, f'{t}3y', 900)
        put(t, 4, f'{t}4y', 900)
    for t in ('a', 'b'):                           # empty the first leaves...
        c.execute(f'DELETE FROM {t} WHERE id <= 12')
    for t in ('a', 'b'):                           # ...and bring rows 3 and 4 back elsewhere
        put(t, 3, f'{t}3z', 40)
        put(t, 4, f'{t}4z', 40)
    raw = open(p, 'rb').read()
    wal = open(p + '-wal', 'rb').read()
    live = {t: set(c.execute(f'SELECT id, name, img FROM {t}')) for t in ('a', 'b')}
    c.close()
    d = tmp_path / 'copy'
    d.mkdir()
    open(d / 'x.db', 'wb').write(raw)
    open(d / 'x.db-wal', 'wb').write(wal)
    return str(d / 'x.db'), raw, wal, truth, live


def _carve(db, raw, wal, with_fr=False):
    conn, _ = sr.connect_committed(db)
    ck = sr.connect_checkpoint(db)
    fr = rv.for_connection(raw, wal, None, conn)
    out = {}
    for t in ('a', 'b'):
        ti = rv.table_info(conn, t, ck)
        out[t] = fr.records(ti)[0]
    conn.close()
    ck and ck.close()
    return (out, fr) if with_fr else out


def test_commit_time_owner_gives_right_table_and_true_versions(tmp_path):
    db, raw, wal, truth, live = _build(tmp_path)
    got, fr = _carve(db, raw, wal, with_fr=True)
    added = {t: [r for r in got[t] if r.get('leaf_at_commit') is not None] for t in got}
    assert added['a'] and added['b']
    for t, recs in added.items():
        other = 'b' if t == 'a' else 'a'
        for r in recs:
            v = tuple(r['values'])
            if r['truncated']:
                assert any(g[:len(v)] == v for g in truth[t])
            else:
                assert v in truth[t], (t, r['method'], r['rowid'])     # a real version of THIS table
            assert v not in truth[other]
            assert r['table_certain'] and r['file'] == 'wal'
            assert 'this table\'s when WAL frame' in rv.describe(r)
    # earlier versions of live rows 3 and 4 are found, except where every copy
    # of them sits on a page in use now (the rule that such a page is never
    # read this way is kept): their bytes may be reused, so not claimed
    in_use = fr._pages_in_use()
    n_found = 0
    for t in ('a', 'b'):
        found = {tuple(r['values']) for r in got[t]}
        for rid in (3, 4):
            for g in {g for g in truth[t] if g[0] == rid} - live[t]:
                if g in found:
                    n_found += 1
                    continue
                pages = {o['page'] for o in fr._overflow() if o.get('rowid') == rid
                         and any(isinstance(v, bytes) and v == g[2] for v in o['values'])}
                assert pages and pages <= in_use, (t, rid, g[1], pages)
        assert all(r['older_version'] for r in got[t] if r['rowid'] in (3, 4) and r.get('leaf_at_commit') is not None)
    assert n_found >= 5          # this build: 7 of the 8 earlier versions


def test_no_wrong_table_anywhere(tmp_path):
    db, raw, wal, truth, live = _build(tmp_path)
    for t, recs in _carve(db, raw, wal).items():
        other = 'b' if t == 'a' else 'a'
        for r in recs:
            assert tuple(r['values']) not in truth[other], (t, r['method'])
