"""A live database hit read from the main file alone (its -wal could not be
applied) says so in its notes, not at the end of its location (2026-11-19;
from 2026-11-17 it was a bracketed location suffix). Old rows are moved in
place when the case is opened, so a rescan updates them instead of adding a
second row; a row whose plain form already exists is left alone."""
import os
import sqlite3

import db_utils
import embedded_media_scan as ems

PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x01' * 6000
SUFFIX = ' [main file without its WAL: the WAL could not be applied]'


def _row(location, sha='aa', notes=None):
    return {'source_ui_path': 'a.db', 'source_kind': 'sqlite', 'location': location,
            'display_name': 'a.db-t-c-1.png', 'media_kind': 'image', 'extracted_path': 'x',
            'sha256': sha, 'byte_length': 5, 'recovered': 0, 'recovery_source': None,
            'http_wrapped': 0, 'content_type_hint': None, 'scan_scope': 'everywhere',
            'notes': notes}


def test_old_suffix_rows_moved_to_notes(tmp_path):
    db = db_utils._open_results_db(str(tmp_path))
    db_utils.save_embedded_media_hits(db, [_row('t.c (rowid=1)' + SUFFIX, 'aa'),
                                           _row('t.c (rowid=2)' + SUFFIX, 'bb'),
                                           _row('t.c (rowid=2)', 'bb'),          # plain form exists
                                           _row('t.c (rowid=3)', 'cc')])
    db.close()
    db_utils._RESULTS_SCHEMA_READY.clear()        # a new session opens the case
    db = db_utils._open_results_db(str(tmp_path))
    got = {(h['location'], h['sha256']): h['notes'] for h in db_utils.load_embedded_media_hits(db)}
    assert got == {('t.c (rowid=1)', 'aa'): ems.WAL_NOT_APPLIED_NOTE,
                   ('t.c (rowid=2)' + SUFFIX, 'bb'): None,      # left as it was
                   ('t.c (rowid=2)', 'bb'): None,
                   ('t.c (rowid=3)', 'cc'): None}
    # a rescan finds the moved row: still one row, same note
    db_utils.save_embedded_media_hits(db, [_row('t.c (rowid=1)', 'aa', ems.WAL_NOT_APPLIED_NOTE)])
    assert sum(1 for h in db_utils.load_embedded_media_hits(db) if h['sha256'] == 'aa') == 1
    db.close()


def _db_with_wal(tmp_path):
    p = str(tmp_path / 'src.db')
    c = sqlite3.connect(p)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, img BLOB)')
    c.execute('INSERT INTO t (img) VALUES (?)', (PNG,))
    c.commit()
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    c.execute('INSERT INTO t (img) VALUES (?)', (b'x',))     # something in the -wal
    c.commit()
    raw, wal = open(p, 'rb').read(), open(p + '-wal', 'rb').read()
    c.close()
    assert wal
    return raw, wal


def test_live_hit_without_wal_noted(tmp_path, monkeypatch):
    raw, wal = _db_with_wal(tmp_path)
    real = ems.scan_sqlite_live
    calls = []

    def flaky(path, *a, **k):          # the committed view (with -wal) fails, as on a bad WAL
        calls.append(os.path.exists(path + '-wal'))
        if calls[-1]:
            raise sqlite3.DatabaseError('malformed')
        return real(path, *a, **k)
    monkeypatch.setattr(ems, 'scan_sqlite_live', flaky)
    rows = [r for r in ems.scan_container('sqlite', 'a.db', raw, str(tmp_path / 'out'), 'everywhere',
                                          wal_bytes=wal) if not r['recovered']]
    assert calls == [True, False]
    assert [(r['location'], r['notes']) for r in rows] == [('t.img (rowid=1)', ems.WAL_NOT_APPLIED_NOTE)]
    assert ems.live_sqlite_display_name('a.db', rows[0]['location'], '.png') == 'a.db-t-img-1.png'


def test_live_hit_normal_has_no_note(tmp_path):
    raw, wal = _db_with_wal(tmp_path)
    rows = [r for r in ems.scan_container('sqlite', 'a.db', raw, str(tmp_path / 'out'), 'everywhere',
                                          wal_bytes=wal) if not r['recovered']]
    assert [(r['location'], r['notes']) for r in rows] == [('t.img (rowid=1)', None)]


def test_partial_carve_location_and_note(tmp_path, monkeypatch):
    import sqlite_carve as sc
    rec = {'rowid': 5, 'page': 9, 'source': 'freed_page', 'status': 'page in use', 'payload': b''}
    monkeypatch.setattr(sc, 'carve_overflowing_records', lambda raw, wal, min_blob: [rec])
    monkeypatch.setattr(sc, 'blob_columns', lambda payload, min_size: [(1, PNG, 20000)])
    hits = list(ems.scan_sqlite_overflow(b'', None))
    assert len(hits) == 1 and hits[0]['partial']
    assert hits[0]['recovery_source'] == f'freed_page, overflow chain, PARTIAL {len(PNG):,} of 20,000 bytes: page in use'
    monkeypatch.setattr(ems, 'scan_sqlite_overflow', lambda raw, wal_bytes, known: iter(hits))
    monkeypatch.setattr(ems, 'scan_sqlite_deleted', lambda raw, wal_bytes: iter(()))
    rows = [r for r in ems.scan_container('sqlite', 'a.db', b'', str(tmp_path / 'o'), 'everywhere')
            if r['recovered']]
    assert [(r['location'], r['notes'], r['display_name']) for r in rows] == [
        ('rowid=5 (page 9, freed_page, overflow chain)',
         f'PARTIAL: {len(PNG):,} of 20,000 bytes recovered (page in use)', 'a.db-carved-partial.png')]


def test_old_partial_rows_moved_to_notes(tmp_path):
    old = 'rowid=5 (page 9, wal_frame, overflow chain, PARTIAL 7,000 of 20,000 bytes: bad pointer)'
    db = db_utils._open_results_db(str(tmp_path))
    db_utils.save_embedded_media_hits(db, [{**_row(old, 'pp'), 'recovered': 1,
                                            'recovery_source': old[old.index('wal_frame'):-1]},
                                           _row('rowid=6 (page 9, wal_frame, overflow chain)', 'qq')])
    db.close()
    db_utils._RESULTS_SCHEMA_READY.clear()
    db = db_utils._open_results_db(str(tmp_path))
    got = {h['sha256']: (h['location'], h['notes'], h['recovery_source'])
           for h in db_utils.load_embedded_media_hits(db)}
    assert got['pp'] == ('rowid=5 (page 9, wal_frame, overflow chain)',
                         'PARTIAL: 7,000 of 20,000 bytes recovered (bad pointer)',
                         'wal_frame, overflow chain, PARTIAL 7,000 of 20,000 bytes: bad pointer')
    assert got['qq'] == ('rowid=6 (page 9, wal_frame, overflow chain)', None, None)   # complete: untouched
    db.close()
