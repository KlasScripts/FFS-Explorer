"""LevelDB search indexing reports a folder it could not read in full,
instead of indexing it as empty: _parallel_extract returns the files it
could not extract, writes each one whole or not at all, and
run_leveldb_search_index raises LevelDbIndexError rather than returning no
rows (until 2026-11-05 each of these was saved as "indexed, no records")."""
import os
import zipfile

import pytest

from leveldb_viewer import (LevelDbIndexError, _already_extracted,
                            _parallel_extract, run_leveldb_search_index)
from zip_entry import ZipEntry


def _zip(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    with zipfile.ZipFile(path) as z:
        return {i.filename: ZipEntry(str(path), i.filename, i) for i in z.infolist()}


def test_extracts_whole_files_and_reports_the_rest(tmp_path):
    entries = _zip(tmp_path / 'a.zip', {'db/000003.log': b'x' * 5000, 'db/CURRENT': b'MANIFEST-1\n'})
    out = tmp_path / 'out'
    out.mkdir()
    errors = _parallel_extract([
        (entries['db/000003.log'], str(out / '000003.log')),
        (entries['db/CURRENT'], str(tmp_path / 'missing-dir' / 'CURRENT')),
    ])
    assert (out / '000003.log').read_bytes() == b'x' * 5000
    assert len(errors) == 1 and errors[0].startswith('db/CURRENT: write failed')
    assert not [n for n in os.listdir(out) if n.endswith('.part')]


def test_short_read_is_reported_and_nothing_written(tmp_path):
    path = tmp_path / 'a.zip'
    entries = _zip(path, {'db/000005.ldb': b'y' * 100_000})
    with open(path, 'r+b') as f:            # the archive ends inside the file's data
        f.truncate(50_000)
    dest = tmp_path / '000005.ldb'
    errors = _parallel_extract([(entries['db/000005.ldb'], str(dest))])
    assert len(errors) == 1 and errors[0].startswith('db/000005.ldb:')
    assert not dest.exists()


def test_already_extracted_needs_the_full_size(tmp_path):
    entries = _zip(tmp_path / 'a.zip', {'db/000005.ldb': b'z' * 1000})
    dest = tmp_path / '000005.ldb'
    assert not _already_extracted(str(dest), entries['db/000005.ldb'])
    dest.write_bytes(b'z' * 400)            # cut short by an interrupted write
    assert not _already_extracted(str(dest), entries['db/000005.ldb'])
    dest.write_bytes(b'z' * 1000)
    assert _already_extracted(str(dest), entries['db/000005.ldb'])


def _job(tmp_path, errors=()):
    return {'ui_path': 'Library/x/leveldb', 'extract_dir': str(tmp_path),
            'blob_extract_dir': None, 'to_fetch': [], 'errors': list(errors)}


def test_unresolved_file_fails_the_folder(tmp_path):
    with pytest.raises(LevelDbIndexError, match='not found in the archive'):
        run_leveldb_search_index(_job(tmp_path, ['Library/x/leveldb/000003.log: not found in the archive']))


def test_unreadable_store_fails_the_folder(tmp_path):
    (tmp_path / 'CURRENT').write_bytes(b'MANIFEST-000001\n')
    (tmp_path / '000003.log').write_bytes(b'\xff' * 64)
    (tmp_path / '000004.ldb').write_bytes(b'not a table')
    with pytest.raises(LevelDbIndexError):
        run_leveldb_search_index(_job(tmp_path))


def test_extract_dir_name_short_paths_unchanged():
    from leveldb_viewer import extract_dir_name
    assert extract_dir_name('data/data/com.app/app_webview/Local Storage/leveldb') == \
        'data_data_com.app_app_webview_Local_Storage_leveldb'


def test_extract_dir_name_long_paths_capped_and_distinct():
    """IOS17 (2026-11-05): a 194-character name put primary.ldb's files
    over Windows' 260-character path limit."""
    from leveldb_viewer import _MAX_EXTRACT_NAME, extract_dir_name
    base = ('mobile/Containers/Data/Application/E81161FB-2689-4DF6-B98B-C8F95B056440/'
            'Library/Application Support/PersistentCache/Users/7jlf2hrgerxu6n0pp8l35r5h0-user/')
    a = extract_dir_name(base + 'primary.ldb')
    b = extract_dir_name(base.replace('E81161FB', 'E81161FC') + 'primary.ldb')
    assert len(a) == len(b) == _MAX_EXTRACT_NAME
    assert a != b
    assert a.startswith('mobile_Containers_Data_Applica') and a.endswith('_primary.ldb')
    assert a == extract_dir_name(base + 'primary.ldb')       # stable across runs
