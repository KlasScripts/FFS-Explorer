"""Files unpacked from nested archives: typed like the header scan types the
main archive, and indexed so Process Case stages after unpacking can read them
(they don't exist in the main zip)."""
import os
import sqlite3
import zipfile

import nested_archive


def _ext_type(name):
    return {'.jpg': 'Picture', '.plist': 'Property List'}.get(os.path.splitext(name)[1].lower(), 'Other')


def test_extension_wins_then_bytes_then_text():
    t = nested_archive.classify_unpacked
    assert t('a.jpg', b'bplist00......', _ext_type) == 'Picture'          # extension first
    assert t('Settings', b'bplist00' + b'\0' * 30, _ext_type) == 'Property List'
    assert t('state', b'SQLite format 3\0' + b'\0' * 30, _ext_type) == 'Database'
    assert t('notes', b'plain readable text\n' * 3, _ext_type) == 'Text'  # the step unpacking used to skip
    assert t('blob', bytes(range(256)) * 4, _ext_type) == 'Other'
    assert t('empty', b'', _ext_type) == 'Other'


def test_index_maps_virtual_paths_to_their_archive(tmp_path):
    case = tmp_path / 'case'
    (case / 'nested_archives').mkdir(parents=True)
    stored = case / 'nested_archives' / 'abc_bundle.zip'
    with zipfile.ZipFile(stored, 'w') as z:
        z.writestr('Settings', b'bplist00' + b'\0' * 30)
        z.writestr('dir/', b'')
    from db_utils import _open_cache_db, save_nested_archive, save_nested_archive_entries
    db = _open_cache_db(str(case))
    save_nested_archive(db, 'private/var/x/bundle.zip', 'abc_bundle.zip', 100, 1)
    save_nested_archive_entries(db, 'private/var/x/bundle.zip',
                                [('Settings', None, 38, 'Property List'), ('dir/', None, 0, 'Other')])
    db.close()
    idx = nested_archive.unpacked_file_index(str(case))
    assert idx == {'private/var/x/bundle.zip/Settings': (str(stored), 'Settings', 'Property List')}
    from zip_reader import read_nested_entry
    assert read_nested_entry(*idx['private/var/x/bundle.zip/Settings'][:2]).startswith(b'bplist00')


def _case_with_unpacked(tmp_path, zip_members: dict, blob: bytes | None = None):
    case = tmp_path / 'case'
    (case / 'nested_archives').mkdir(parents=True)
    from db_utils import _open_cache_db, save_nested_archive, save_nested_archive_entries
    db = _open_cache_db(str(case))
    with zipfile.ZipFile(case / 'nested_archives' / 'a_pkg.zip', 'w') as z:
        for name, data in zip_members.items():
            z.writestr(name, data)
    save_nested_archive(db, 'var/pkg.zip', 'a_pkg.zip', 1, len(zip_members))
    save_nested_archive_entries(db, 'var/pkg.zip', [(n, None, len(d), 'Other') for n, d in zip_members.items()])
    if blob is not None:
        (case / 'nested_archives' / 'b_log').write_bytes(blob)
        save_nested_archive(db, 'var/log.gz', 'b_log', 1, 1)
        save_nested_archive_entries(db, 'var/log.gz', [('log', None, len(blob), 'Text')])
    db.close()
    return str(case)


def test_unpacked_files_group_with_main_archive_files(tmp_path, monkeypatch):
    import duplicate_scan
    case = _case_with_unpacked(tmp_path, {'photo.jpg': b'SAME-PHOTO-BYTES', 'other.bin': b'unique'},
                               blob=b'SAME-PHOTO-BYTES')
    unp = nested_archive.unpacked_duplicate_candidates(case)
    assert {c[0] for c in unp} == {'var/pkg.zip/photo.jpg', 'var/pkg.zip/other.bin', 'var/log.gz/log'}

    class MainEntry:            # a main-archive file with the same content
        def __init__(self, *a):
            pass

        def read(self):
            return b'SAME-PHOTO-BYTES'
    monkeypatch.setattr(duplicate_scan, 'ZipEntry', MainEntry)
    import zlib
    main_info = type('I', (), {'file_size': 16, 'CRC': zlib.crc32(b'SAME-PHOTO-BYTES')})()
    main = [('DCIM/IMG_1.jpg', 'phys', main_info, 0, 0, 'f' * 64)]   # has a GrayKey hash; unpacked don't
    res = duplicate_scan.find_duplicate_groups('unused.zip', main + unp)
    assert set(res) == {'DCIM/IMG_1.jpg', 'var/pkg.zip/photo.jpg', 'var/log.gz/log'}
    assert len({v['md5'] for v in res.values()}) == 1 and len(next(iter(res.values()))['md5']) == 32


def test_unreadable_unpacked_file_is_never_reported(tmp_path):
    import duplicate_scan
    case = _case_with_unpacked(tmp_path, {'a': b'dup', 'b': b'dup'})
    unp = nested_archive.unpacked_duplicate_candidates(case)
    gone = [c[:6] + (lambda: None,) if c[0].endswith('/a') else c for c in unp]
    assert duplicate_scan.find_duplicate_groups('unused.zip', gone) == {}
