"""embedded_media_scan returns each (location, media) once — the row the
case's embedded_media_hits keeps — so the sweep's on-screen counts match
what is stored (IOS17: 989 shown against 280 stored before, 2026-11-01)."""
import embedded_media_scan as ems


def _row(loc, sha, source='a.db', recovered=1):
    return {'source_ui_path': source, 'location': loc, 'sha256': sha,
            'recovered': recovered, 'media_kind': 'image', 'n': (loc, sha, source)}


def test_repeat_finds_kept_once_first_wins():
    first = _row('rowid=7 (page 3, wal_frame)', 'aa')
    rows = [first, _row('rowid=7 (page 3, wal_frame)', 'aa'),      # same row, later WAL frame
            _row('rowid=7 (page 3, wal_frame)', 'bb'),             # other image, same place
            _row('rowid=8 (page 3, freed_page)', 'aa')]            # same image, other place
    out = ems._unique_rows(rows)
    assert len(out) == 3
    assert out[0] is first


def test_matches_the_tables_unique_key(tmp_path):
    """Stored rows are identical with or without the de-duplication."""
    import sqlite3
    rows = [_row('l1', 's1'), _row('l1', 's1'), _row('l2', 's1'), _row('l1', 's1', source='b.db')]
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE h (source_ui_path, location, sha256, '
               'UNIQUE(source_ui_path, location, sha256))')
    for r in rows:
        db.execute('INSERT OR IGNORE INTO h VALUES (?,?,?)', (r['source_ui_path'], r['location'], r['sha256']))
    stored = sorted(db.execute('SELECT * FROM h'))
    assert sorted((r['source_ui_path'], r['location'], r['sha256']) for r in ems._unique_rows(rows)) == stored
