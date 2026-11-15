"""Nested archive entry times (nested_archive.entry_mdate / parse_mdate):
UT and gzip times are UTC; a zip's DOS time is local, zone unknown, and is
never turned into a UTC time (A-10, Q5)."""
import struct
import zipfile

import nested_archive as na


def _info(dt, ut=None):
    i = zipfile.ZipInfo('x/a.jpg', date_time=dt)
    i.extra = struct.pack('<HHBI', 0x5455, 5, 1, ut) if ut else b''
    return i


def test_ut_is_utc():
    m = na.entry_mdate(_info((2024, 7, 1, 8, 0, 0), ut=1719835200))   # 12:00 UTC
    assert m == '2024-07-01 12:00:00 UTC'
    assert na.parse_mdate(m, legacy_is_utc=False) == (1719835200.0, None)


def test_dos_only_is_local_and_never_utc():
    m = na.entry_mdate(_info((2024, 7, 1, 8, 0, 0)))
    assert m == '2024-07-01 08:00:00 (local, zone unknown)'
    assert na.parse_mdate(m, legacy_is_utc=True) == (None, m)
    assert na.entry_mdate(_info((1980, 1, 1, 0, 0, 0))) is None


def test_values_stored_before_the_labels():
    # zip: was DOS, local
    assert na.parse_mdate('2024-07-01 08:00:00', legacy_is_utc=False) == \
        (None, '2024-07-01 08:00:00 (local, zone unknown)')
    # gzip: was its MTIME, UTC
    assert na.parse_mdate('2024-07-01 12:00:00', legacy_is_utc=True) == (1719835200.0, None)
    assert na.parse_mdate(None, True) == (None, None)
    assert na.parse_mdate('garbage UTC', True) == (None, None)


def test_lost_records_are_rebuilt_from_the_stored_files(tmp_path):
    import os
    from contextlib import closing
    from db_utils import _open_cache_db, load_nested_archives, load_nested_archive_entries
    case = str(tmp_path)
    out = os.path.join(case, 'nested_archives')
    os.makedirs(out)
    ui = 'data/media/0/Download/holiday.zip'
    with zipfile.ZipFile(os.path.join(out, na.stored_name_for(ui)), 'w') as z:
        z.writestr(zipfile.ZipInfo('a.jpg', date_time=(2024, 7, 1, 8, 0, 0)), b'\xff\xd8\xff' + b'0' * 20)
    with open(os.path.join(out, 'deadbeef0000_other.zip'), 'wb') as f:
        f.write(b'PK')
    meta = {ui: {'size': 123}, 'data/x/other.zip': {'size': 1}}
    res = na.recover_missing_records(case, meta, lambda name: 'Picture' if name.endswith('.jpg') else 'Other')
    assert res['recovered'] == 1 and res['unmatched'] == ['deadbeef0000_other.zip']
    with closing(_open_cache_db(case)) as db:
        recs = load_nested_archives(db)
        ents = load_nested_archive_entries(db, ui)
    assert [r['ui_path'] for r in recs] == [ui] and recs[0]['entry_count'] == 1
    assert ents[0]['entry_path'] == 'a.jpg' and ents[0]['file_type'] == 'Picture'
    assert ents[0]['mdate'] == '2024-07-01 08:00:00 (local, zone unknown)'
    # nothing left to do the second time
    assert na.recover_missing_records(case, meta, lambda n: 'Other')['recovered'] == 0
