"""EXIF/video 'taken' time (media_metadata_scan): DateTimeOriginal from the
Exif sub-IFD with its offset, never IFD0's DateTime; a time without an
offset is local and is never treated as UTC."""
import io
import sqlite3

from PIL import Image

import media_metadata_scan as m
from db_utils import save_media_metadata, load_media_metadata, _open_cache_db


def _jpeg(datetime=None, original=None, offset=None):
    ex = Image.Exif()
    if datetime:
        ex[0x0132] = datetime
    sub = ex.get_ifd(0x8769)
    if original:
        sub[0x9003] = original
    if offset:
        sub[0x9011] = offset
    b = io.BytesIO()
    Image.new('RGB', (8, 8)).save(b, 'JPEG', exif=ex)
    b.seek(0)
    return b


def test_taken_is_date_time_original_not_date_time():
    # The two differ on a real Android 15 photo; the old code reported
    # DateTime (last changed) as Taken.
    r = m.extract_image_exif(_jpeg('2020:01:01 00:00:00', '2019:06:06 12:00:00', '+02:00'))
    assert r['taken'] == '2019-06-06 12:00:00'
    assert r['taken_offset'] == '+02:00'


def test_date_time_alone_is_not_a_taken_time():
    r = m.extract_image_exif(_jpeg(datetime='2020:01:01 00:00:00'))
    assert 'taken' not in r and 'taken_offset' not in r
    assert r['width'] == 8                       # the rest is still read


def test_taken_without_offset_is_local():
    r = m.extract_image_exif(_jpeg(original='2019:06:06 12:00:00'))
    assert r['taken'] == '2019-06-06 12:00:00' and 'taken_offset' not in r
    assert m.taken_display(r) == '2019-06-06 12:00:00 (local, offset unknown)'
    assert m.taken_utc_seconds(r) is None        # never placed as UTC


def test_offset_forms():
    assert m._normalize_offset('+02:00') == '+02:00'
    assert m._normalize_offset('-0530') == '-05:30'
    for bad in ('', '   :  ', '+25:00', 'Z', None, b'+02:00'):
        assert m._normalize_offset(bad) is None, bad
    r = m.extract_image_exif(_jpeg(original='2019:06:06 12:00:00', offset='   :  '))
    assert 'taken_offset' not in r               # blank EXIF offset = none


def test_utc_seconds_and_display():
    f = {'taken': '2019-06-06 12:00:00', 'taken_offset': '+02:00'}
    assert m.taken_utc_seconds(f) == 1559815200.0        # 10:00:00 UTC
    assert m.taken_display(f) == '2019-06-06 12:00:00 +02:00'
    neg = {'taken': '2019-06-06 12:00:00', 'taken_offset': '-05:30'}
    assert m.taken_utc_seconds(neg) == 1559842200.0      # 17:30:00 UTC
    assert m.taken_display({}) == ''


def test_offset_column_saved_and_added_to_old_cache(tmp_path):
    # An existing case's cache has media_metadata without taken_offset.
    db = tmp_path / 'casecache.db'
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE media_metadata (ui_path TEXT NOT NULL PRIMARY KEY, taken TEXT, '
                'camera TEXT, gps TEXT, duration_s REAL, width INTEGER, height INTEGER, '
                'software TEXT, orientation INTEGER)')
    con.execute("INSERT INTO media_metadata (ui_path, taken) VALUES ('old.jpg', '2019-01-01 00:00:00')")
    con.commit()
    con.close()
    with _open_cache_db(str(tmp_path)) as conn:
        save_media_metadata(conn, {'new.jpg': {'taken': '2019-06-06 12:00:00',
                                               'taken_offset': '+02:00'}})
        got = load_media_metadata(conn)
    assert got['new.jpg']['taken_offset'] == '+02:00'
    assert got['old.jpg'] == {'taken': '2019-01-01 00:00:00'}   # kept, no offset


def test_timeline_file_times_leave_out_accessed():
    """Accessed is never offered on the Timeline (owner decision,
    2026-11-15: not trustworthy on phones)."""
    from timeline_viewer import TimelineViewerMixin
    o = TimelineViewerMixin.__new__(TimelineViewerMixin)
    o._time_cols = [('Changed', 'ctime'), ('Modified', 'mtime'), ('Accessed', 'atime')]
    o.full_metadata = {'a/b.jpg': {'ctime': 1700000000, 'mtime': 1700000100, 'atime': 1700000200}}
    labels = [lbl for lbl, _ms, _w in o._timeline_file_times('a/b.jpg')]
    assert labels == ['Changed', 'Modified']
