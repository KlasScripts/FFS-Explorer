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


class _Adapter:
    """A plain-prefix adapter: ui_path == zip name."""
    def resolve(self, ui_path):
        return ui_path

    def ui_path_from_physical(self, physical):
        return physical.rstrip('/')


def test_gzip_time_is_read_from_the_original_archive(tmp_path):
    import gzip as _gzip
    import io
    import os
    from contextlib import closing
    import zip_cd_cache
    from db_utils import _open_cache_db, load_nested_archive_entries
    case = str(tmp_path / 'case')
    os.makedirs(os.path.join(case, 'nested_archives'))
    # an FFS-like archive (stored, never compressed) holding a .gz whose
    # header says 2024-07-01 12:00:00 UTC
    buf = io.BytesIO()
    with _gzip.GzipFile(fileobj=buf, mode='wb', mtime=1719835200) as g:
        g.write(b'hello log line\n')
    ffs = str(tmp_path / 'EXTRACTION_FFS.zip')
    with zipfile.ZipFile(ffs, 'w', compression=zipfile.ZIP_STORED) as z:
        z.writestr('data/log/app.log.gz', buf.getvalue())
    zip_cd_cache.save(ffs, case)
    ui = 'data/log/app.log.gz'
    with open(os.path.join(case, 'nested_archives', na.stored_name_for(ui)), 'wb') as f:
        f.write(b'hello log line\n')                       # the decompressed copy
    meta = {ui: {'size': len(buf.getvalue())}}
    res = na.recover_missing_records(case, meta, lambda n: 'Other', zip_path=ffs, adapter=_Adapter())
    assert res['recovered'] == 1
    with closing(_open_cache_db(case)) as db:
        ents = load_nested_archive_entries(db, ui)
    assert ents[0]['entry_path'] == 'app.log' and ents[0]['mdate'] == '2024-07-01 12:00:00 UTC'
    # without the archive it is still recovered, with no time
    assert na._gzip_mdate_from_archive(ffs, case, 'data/log/missing.gz') is None


def test_a_file_unpacked_under_an_older_path_is_matched(tmp_path, monkeypatch):
    import os
    from contextlib import closing
    import zip_cd_cache
    from db_utils import _open_cache_db, load_nested_archives
    case = str(tmp_path)
    out = os.path.join(case, 'nested_archives')
    os.makedirs(out)
    old_ui, new_ui = 'data/media/0/Download/cafÃ©.zip', 'data/media/0/Download/café.zip'
    with zipfile.ZipFile(os.path.join(out, na.stored_name_for(old_ui)), 'w') as z:
        z.writestr('a.txt', b'x')

    class _Index:
        def renames(self):
            return [(old_ui, new_ui)]

        def close(self):
            pass
    monkeypatch.setattr(zip_cd_cache, 'open_index', lambda zp, cd: _Index())
    meta = {new_ui: {'size': 10}}
    assert na.recover_missing_records(case, meta, lambda n: 'Other')['recovered'] == 0   # no record of the old name
    res = na.recover_missing_records(case, meta, lambda n: 'Other', zip_path='x.zip', adapter=_Adapter())
    assert res['recovered'] == 1 and res['unmatched'] == []
    with closing(_open_cache_db(case)) as db:
        rec = load_nested_archives(db)[0]
    assert rec['ui_path'] == new_ui and rec['stored_filename'] == na.stored_name_for(old_ui)
