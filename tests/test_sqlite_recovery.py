"""sqlite_recovery: deleted and older records, checked against what the real
SQLite engine wrote (tests/sqlite_fixtures.py), never against the tool's own
output. Recall: every deleted or replaced version that survives is found.
Precision: no live row is ever reported, and a record never comes back
with values that were not written."""
import os
import shutil
import sqlite3

import pytest

import sqlite_carve as sc
import sqlite_reader as sr
import sqlite_recovery as rv
from sqlite_fixtures import deleted_rows_db, hot_journal_db, wal_db


def _recover(path, table):
    conn, _ = sr.connect_committed(path)
    ck = sr.connect_checkpoint(path)

    def rd(p):
        return open(p, 'rb').read() if os.path.exists(p) else None
    fr = rv.for_connection(rd(path), rd(path + '-wal'), rd(path + '-journal'), conn)
    t = rv.table_info(conn, table, ck)
    recs, notes = fr.records(t)
    conn.close()
    if ck:
        ck.close()
    return t, recs, notes


def _only_delete_in_wal(folder):
    """WAL holding only a delete of rows 7 and 8 (no earlier frame of the
    page): those rows survive only as live cells in the main file."""
    src = os.path.join(folder, 'src.db')
    c = sqlite3.connect(src, isolation_level=None)
    c.execute('pragma journal_mode=wal')
    c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table m(id integer primary key, body text)')
    for i in range(1, 21):
        c.execute('insert into m values(?,?)', (i, f'row {i} ' + 'z' * 30))
    c.execute('pragma wal_checkpoint(truncate)')
    c.execute('delete from m where id in (7, 8)')
    os.makedirs(os.path.join(folder, 'phone'))
    out = os.path.join(folder, 'phone', 'm.db')
    for s in ('', '-wal'):
        shutil.copyfile(src + s, out + s)
    c.close()
    return out


def test_rows_deleted_after_the_checkpoint_are_whole_rows(tmp_path):
    path = _only_delete_in_wal(str(tmp_path))
    t, recs, _ = _recover(path, 'm')
    got = {r['rowid']: r for r in recs if r['method'] == 'checkpoint_deleted'}
    assert set(got) == {7, 8}
    assert got[7]['values'] == [7, 'row 7 ' + 'z' * 30]
    assert got[7]['table_certain'] and got[7]['file'] == 'main'


def test_wal_changes_and_older_versions(tmp_path):
    path, truth = wal_db(str(tmp_path))
    t, recs, _ = _recover(path, 'messages')
    body = t.columns.index('body')
    deleted = {r['rowid']: r['values'][body] for r in recs
               if r['method'] == 'checkpoint_deleted'}
    assert deleted == truth['deleted']
    older = {r['rowid']: r['values'][body] for r in recs if r['older_version']}
    assert older == truth['old_versions']
    for r in recs:                                       # never a live version
        assert t.live.get(r['rowid']) != tuple(r['values'])


@pytest.mark.parametrize('n,delete', [
    (300, range(40, 60)),                  # neighbours: freeblocks merge
    (300, [3, 50, 51, 52, 200]),           # scattered and low rowids
    (3000, range(1000, 1300, 7)),          # two-byte rowids
])
def test_plain_deletes_are_recovered_exactly(tmp_path, n, delete):
    path, truth = deleted_rows_db(str(tmp_path), n=n, delete=delete)
    t, recs, _ = _recover(path, 'chat')
    written = {(f'sender{i % 7}', b, 1700000000 + i) for i, b in truth['deleted'].items()}
    found = {tuple(r['values'][1:]) for r in recs}
    assert found == written                              # all of them, nothing else
    live = {tuple(v[1:]) for v in t.live.values()}
    assert not found & live


def test_secure_delete_leaves_nothing_to_find(tmp_path):
    path, _ = deleted_rows_db(str(tmp_path), secure_delete=True)
    _t, recs, _ = _recover(path, 'chat')
    assert recs == []


def test_hot_journal_unfinished_write_is_labelled(tmp_path):
    path, n = hot_journal_db(str(tmp_path))
    t, recs, notes = _recover(path, 'favicons')
    assert len(t.live) == n
    assert all(v[1] == 'x' * 200 for v in t.live.values())      # committed = rolled back
    unfinished = [r for r in recs if r['method'] == 'unfinished_write']
    assert unfinished and all(r['values'][1] == 'changed' + 'x' * 200 for r in unfinished)
    for r in recs:                                               # every 'changed' value is labelled
        if any(isinstance(v, str) and v.startswith('changed') for v in r['values']):
            assert r['in_unfinished_write']
    assert any('unfinished write' in rv.describe(r) for r in recs)


def test_each_page_is_decoded_once_across_tables(tmp_path):
    path, _ = deleted_rows_db(str(tmp_path))
    conn, _ = sr.connect_committed(path)
    fr = rv.for_connection(open(path, 'rb').read(), None, None, conn)
    t = rv.table_info(conn, 'chat')
    first, _ = fr.records(t)
    memo = len(fr._memo)
    again, _ = fr.records(t)
    assert again == first and len(fr._memo) == memo


def test_report_recovery_ignores_reserved_paths_entries(tmp_path):
    """Regression: a function among the parser's paths (reserved '_' keys)
    made the table search raise, and every bare-name recoverable_tables
    entry recovered nothing (2026-10-22 to 2026-11-16)."""
    path, truth = deleted_rows_db(str(tmp_path))
    paths = {'_app_base_ui_path': 'data/data/x', '_ui_path_for': lambda p: p,
             '_read_zip_bytes': lambda n: None, 'db': path}
    rows = sc.recover_deleted_rows(paths, 'chat')
    assert {r['body'] for r in rows} == set(truth['deleted'].values())
    assert all(r['recovery_version'] == rv.RECOVERY_VERSION for r in rows)
    assert all(r['raw_file'] == 'main' and r['raw_offset'] for r in rows)


def test_hot_journal_unfinished_delete_is_labelled(tmp_path):
    """The app was deleting rows 1000-2999 when the phone was acquired: the
    committed view still has them (the delete never committed). Rows the
    in-flight file had already removed are labelled; no other row is."""
    path, n = hot_journal_db(str(tmp_path),
                             change='delete from favicons where id between 1000 and 2999')
    t, recs, _ = _recover(path, 'favicons')
    assert len(t.live) == n                                # nothing deleted in committed
    flagged = {r['rowid'] for r in recs if r['method'] == 'unfinished_delete'}
    assert flagged, 'expected some rows the in-flight file had removed'
    assert flagged <= set(range(1000, 3000))              # never a row it was not deleting
    for r in recs:
        if r['method'] == 'unfinished_delete':
            assert r['values'] == list(t.live[r['rowid']]) and r['in_unfinished_write']
            assert 'unfinished write had removed' in rv.describe(r)


def test_kept_results_round_trip_exactly(tmp_path):
    """casecache.db keeps a table's records (JSON, bytes as base64, never
    pickle) and gives back exactly what was carved; another recovery
    version never sees them."""
    path, _truth = deleted_rows_db(str(tmp_path))
    t, recs, notes = _recover(path, 'chat')
    recs = recs + [dict(recs[0], values=[1, b'\x00\xffbinary', 2.5, None])]
    case = tmp_path / 'case'
    case.mkdir()
    assert rv.cache_put(str(case), 'k1', 'chat', recs, notes, t.ncols)
    got = rv.cache_get(str(case), 'k1', 'chat')
    assert got == (recs, notes, t.ncols)
    assert rv.cache_get(str(case), 'k1', 'other') is None
    real = rv.RECOVERY_VERSION
    try:
        rv.RECOVERY_VERSION = real + 1
        assert rv.cache_get(str(case), 'k1', 'chat') is None
    finally:
        rv.RECOVERY_VERSION = real


def test_live_row_is_located_where_the_committed_view_has_it(tmp_path):
    """Record mode: a row only in the WAL is found there, and a row the WAL
    changed is never cited at its stale bytes in the main file."""
    path, truth = wal_db(str(tmp_path))
    main, wal = open(path, 'rb').read(), open(path + '-wal', 'rb').read()
    for rid, text in truth['committed'].items():
        loc = rv.locate_committed_row(main, wal, 'messages', rid)
        data = wal if loc['file'] == 'wal' else main
        cell = data[loc['abs_offset']:loc['abs_offset'] + loc['length']]
        plen, c1 = sc.read_varint(cell, 0)
        got, c2 = sc.read_varint(cell, c1)
        assert got == rid and sc.decode_record(cell[c1 + c2:c1 + c2 + plen])[1] == text
    assert rv.locate_committed_row(main, wal, 'messages', 5) is None       # deleted in the WAL
    assert rv.locate_committed_row(main, None, 'messages', 5)['file'] == 'main'


def _two_shapes_freed(path, twin: bool):
    """Table a's rows on pages that are then freed (all of them deleted,
    secure_delete off). With *twin*, table b has the same shape (two
    columns: text, integer) and live rows of the same pattern; without it,
    b is a different shape."""
    c = sqlite3.connect(path, isolation_level=None)
    c.execute('pragma secure_delete=off')
    c.execute('pragma journal_mode=delete')
    c.execute('create table a(id integer primary key, body text, n integer)')
    if twin:
        c.execute('create table b(id integer primary key, body text, n integer)')
    else:
        c.execute('create table b(id integer primary key, x real, y blob, z text)')
    c.execute('begin')
    for i in range(1, 400):
        c.execute('insert into a(body, n) values (?, ?)', (f'message number {i:04d}', 1000 + i))
    for i in range(1, 400):
        if twin:
            c.execute('insert into b(body, n) values (?, ?)', (f'other number - {i:04d}', 1000 + i))
        else:
            c.execute('insert into b(x, y, z) values (?, ?, ?)', (i / 3, b'\x00\x01', 'z'))
    c.execute('commit')
    c.execute('delete from a where id > 6')         # its later pages are freed
    c.close()


@pytest.mark.parametrize('twin', [False, True])
def test_a_freed_page_record_must_fit_no_other_table(tmp_path, twin):
    """Owner decision 2026-11-27 (A-95): a record whose table is not certain
    (here a freed page) is kept only when it fits this table's own pattern
    and no other table's. With a twin table of the same shape, neither table
    is given the freed records, and both say why; without one, they are
    table a's, labelled table not certain."""
    db = str(tmp_path / 'x.db')
    _two_shapes_freed(db, twin)
    deleted = {i: (i, f'message number {i:04d}', 1000 + i) for i in range(7, 400)}
    _t, recs, notes = _recover(db, 'a')
    freed = [r for r in recs if r['method'] == 'freed_page']
    if twin:
        assert freed == []
        assert any('also fit another table (b)' in n for n in notes)
        _t, recs_b, notes_b = _recover(db, 'b')
        assert [r for r in recs_b if r['method'] == 'freed_page'] == []
    else:
        assert freed and all(not r['table_certain'] for r in freed)
        for r in freed:
            assert deleted[r['rowid']] == tuple(r['values'])


def test_free_space_scan_with_a_column_count_finds_the_same_records(tmp_path):
    """_brute_force_records(ncols=N) returns exactly the N-column records
    the unrestricted scan finds (the carver keeps only those)."""
    db, _truth = deleted_rows_db(str(tmp_path), n=60, delete=[3, 9, 10, 11, 30, 41])
    raw = open(db, 'rb').read()
    hdr = sc.parse_db_header(raw)
    ps = hdr['page_size']
    for pno in range(1, len(raw) // ps + 1):
        page = sc._page_bytes(raw, ps, pno)
        ho = 100 if pno == 1 else 0
        if page[ho] != sc._LEAF_TABLE_PAGE:
            continue
        for start, end, kind in sc._free_byte_ranges(page, ho):
            every = sc._brute_force_records(page[start:end], start, pno, kind)
            for n in {len(r['values']) for r in every} | {1, 2, 3}:
                assert (sc._brute_force_records(page[start:end], start, pno, kind, ncols=n)
                        == [r for r in every if len(r['values']) == n])


def test_damage_says_why_a_record_is_not_whole():
    base = {'method': 'freeblock', 'truncated': False, 'partly_overwritten': False}
    assert rv.damage(base) == ''
    assert 'cut off' in rv.damage(dict(base, truncated=True))
    assert 'partly overwritten' in rv.damage(dict(base, partly_overwritten=True))
    assert 'header rebuilt' in rv.damage(dict(base, method='header_rebuilt'))
    assert 'all zero' in rv.damage(dict(base, values=[1, bytes(570)]))
    assert rv.damage(dict(base, values=[1, b'\x00' * 4])) == ''
    d = rv.damage(dict(base, method='overflow', truncated=True, overflow_status='chain broken'))
    assert d == 'overflow chain incomplete (chain broken)'


def test_report_carver_keeps_results_and_reuses_them(tmp_path):
    """The deleted reports' carver: carved once in the worker process, kept
    in casecache.db, and a second carver of the same bytes reads them back
    unchanged without carving."""
    db, _truth = deleted_rows_db(str(tmp_path), n=40, delete=[5, 6, 20])
    case = tmp_path / 'case'
    case.mkdir()
    c1 = rv.ReportCarver(db, 'ui/d.db', str(case))
    try:
        first = c1.records('chat')
    finally:
        c1.close()
    assert c1.carved == 1 and first[0]
    c2 = rv.ReportCarver(db, 'ui/d.db', str(case))
    try:
        again = c2.records('chat')
    finally:
        c2.close()
    assert c2.carved == 0
    assert again == first
