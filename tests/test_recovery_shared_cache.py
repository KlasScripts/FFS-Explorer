"""The embedded-media sweep keeps every table's carved records in
casecache.db for the Database tab, so both must carve from the same inputs
and keep them under the same key: sqlite_recovery.recovery_files lays the
files out as sqlite_viewer.prepare_database does, and db_cache_key is the
tab's key. Checked on a plain, a WAL and a hot-journal database."""
import hashlib
import os
import shutil

import pytest

import sqlite_recovery as rv
import sqlite_fixtures as fx


def _read(p):
    return open(p, 'rb').read() if os.path.exists(p) else None


@pytest.mark.parametrize('make', ['plain', 'wal', 'journal'])
def test_sweep_carves_what_the_tab_carves(tmp_path, make):
    from sqlite_viewer import prepare_database, _prepared_key, _SQLITE_MAGIC
    if make == 'plain':
        db, _ = fx.deleted_rows_db(str(tmp_path))
    elif make == 'wal':
        db, _ = fx.wal_db(str(tmp_path))
    else:
        db, _ = fx.hot_journal_db(str(tmp_path))
    raw, wal, journal = _read(db), _read(db + '-wal'), _read(db + '-journal')
    name = os.path.basename(db)
    tab = prepare_database(raw, name, wal, journal)
    mine = str(tmp_path / 'mine')
    os.makedirs(mine)
    files, tables = rv.recovery_files(raw, wal, journal, mine, name)
    assert sorted(tables) == sorted(tab['tables'])
    for t in tables:
        a = rv.records_for_files(tab['recovery_files'], t)
        b = rv.records_for_files(files, t)
        assert rv.encode_records(a[0]) == rv.encode_records(b[0]) and a[1:] == b[1:], t
    # the same key as the tab's
    key = _prepared_key('a/' + name, raw, wal if wal and wal[:16] != _SQLITE_MAGIC else None,
                        journal if journal and journal[:16] != _SQLITE_MAGIC else None)
    assert rv.db_cache_key('a/' + name, raw, wal, journal) == hashlib.sha1(repr(key).encode()).hexdigest()
    shutil.rmtree(tab['tmpdir'], ignore_errors=True)


def test_kept_records_read_back(tmp_path):
    db, _ = fx.deleted_rows_db(str(tmp_path / 'f'))
    raw = _read(db)
    os.makedirs(tmp_path / 'x')
    files, tables = rv.recovery_files(raw, None, None, str(tmp_path / 'x'))
    items = [('k', t, *rv.records_for_files(files, t)) for t in tables]
    case = str(tmp_path / 'case')
    os.makedirs(case)
    assert rv.cache_put_many(case, items) == len(items)
    for _k, t, recs, notes, ncols in items:
        got = rv.cache_get(case, 'k', t)
        assert got is not None and rv.encode_records(got[0]) == rv.encode_records(recs) and got[2] == ncols


def test_encoded_in_worker_read_back(tmp_path):
    """The sweep encodes in its worker process (encode_many) and the
    window's process only writes the blobs (cache_put_encoded): what reads
    back is the same as cache_put_many's, and a too-large table is left out
    before anything is sent back."""
    db, _ = fx.deleted_rows_db(str(tmp_path / 'f'))
    raw = _read(db)
    os.makedirs(tmp_path / 'x')
    files, tables = rv.recovery_files(raw, None, None, str(tmp_path / 'x'))
    items = [('k', t, *rv.records_for_files(files, t)) for t in tables]
    enc = rv.encode_many(items)
    assert all(isinstance(blob, bytes) for _k, _t, blob, _n, _c in enc)
    case = str(tmp_path / 'case')
    os.makedirs(case)
    assert rv.cache_put_encoded(case, enc) == len(items)
    for _k, t, recs, notes, ncols in items:
        got = rv.cache_get(case, 'k', t)
        assert got is not None and rv.encode_records(got[0]) == rv.encode_records(recs)
        assert got[1] == notes and got[2] == ncols


def test_encode_many_leaves_out_too_large(monkeypatch):
    monkeypatch.setattr(rv, 'CACHE_MAX_BYTES', 10)
    assert rv.encode_many([('k', 't', [[b'x' * 1000, 'y' * 1000]], [], 2)]) == []
