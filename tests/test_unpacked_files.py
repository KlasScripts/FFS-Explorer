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
