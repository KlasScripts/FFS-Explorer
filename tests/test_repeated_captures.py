"""Paths a zip stores more than once (ASSUMPTIONS.md A-72): every copy but the
last gets its own name, data offsets stay exact, and a capture's database and
WAL keep sharing one base name. Archives are written byte by byte (the writer
from test_zip_offsets), in the shapes measured on IOS18: a database family
captured in two passes, and a keychain whose WAL was captured only once."""
import os

import zip_cd_cache as zc
from test_zip_offsets import _write_zip


def _load(tmp_path, entries):
    zip_path = str(tmp_path / 'ffs.zip')
    _write_zip(zip_path, entries)
    case_dir = str(tmp_path / 'case')
    os.makedirs(case_dir)
    assert zc.save(zip_path, case_dir)
    return zip_path, zc.load(zip_path, case_dir)


def _read(zip_path, infos, name):
    off = zc.compute_data_offsets(zip_path, infos)
    info = next(i for i in infos if i.filename == name)
    with open(zip_path, 'rb') as fh:
        fh.seek(off[name])
        return fh.read(info.file_size)


MAIL = 'private/var/mobile/Library/Mail/Envelope Index'
KEYCHAIN = 'private/var/Keychains/keychain-2.db'


def _ios18_shape():
    return [
        {'name': KEYCHAIN.encode() + b'-shm', 'data': b'K-SHM'},
        {'name': KEYCHAIN.encode() + b'-wal', 'data': b'K-WAL'},
        {'name': KEYCHAIN.encode(), 'data': b'KEYCHAIN'},
        {'name': MAIL.encode(), 'data': b'MAIL-DB'},                 # early pass
        {'name': MAIL.encode() + b'-wal', 'data': b'WAL-EARLY'},
        {'name': b'private/var/other.txt', 'data': b'x' * 30},
        {'name': KEYCHAIN.encode(), 'data': b'KEYCHAIN'},           # full walk
        {'name': MAIL.encode(), 'data': b'MAIL-DB'},
        {'name': MAIL.encode() + b'-shm', 'data': b'SHM-LATE'},
        {'name': MAIL.encode() + b'-wal', 'data': b'WAL-LATER-AND-LONGER'},
    ]


def test_earlier_copies_get_their_own_names(tmp_path):
    zip_path, infos = _load(tmp_path, _ios18_shape())
    names = [i.filename for i in infos]
    assert len(names) == len(set(names))
    assert MAIL + ' [capture 1 of 2]' in names
    assert MAIL + ' [capture 1 of 2]-wal' in names
    assert 'private/var/Keychains/keychain-2 [capture 1 of 2].db' in names
    assert MAIL in names and MAIL + '-wal' in names and MAIL + '-shm' in names
    # stored names untouched, for offset arithmetic
    alias = next(i for i in infos if i.filename == MAIL + ' [capture 1 of 2]-wal')
    assert alias.orig_filename == MAIL + '-wal'


def test_each_capture_reads_its_own_bytes(tmp_path):
    zip_path, infos = _load(tmp_path, _ios18_shape())
    assert _read(zip_path, infos, MAIL + ' [capture 1 of 2]-wal') == b'WAL-EARLY'
    assert _read(zip_path, infos, MAIL + '-wal') == b'WAL-LATER-AND-LONGER'
    # the "<db> + '-wal'" sidecar lookup pairs captures with themselves
    db1 = MAIL + ' [capture 1 of 2]'
    assert _read(zip_path, infos, db1 + '-wal') == b'WAL-EARLY'
    assert db1 + '-shm' not in {i.filename for i in infos}


def test_a_wal_written_once_keeps_its_real_name(tmp_path):
    zip_path, infos = _load(tmp_path, _ios18_shape())
    names = {i.filename for i in infos}
    assert KEYCHAIN + '-wal' in names
    assert not any('keychain-2 [capture' in n and n.endswith('-wal') for n in names)


def test_capture_alias_naming():
    assert zc.capture_alias('a/sms.db', 1, 2) == 'a/sms [capture 1 of 2].db'
    assert zc.capture_alias('a/sms.db-wal', 1, 2) == 'a/sms [capture 1 of 2].db-wal'
    assert zc.capture_alias('a/Envelope Index-shm', 2, 3) == 'a/Envelope Index [capture 2 of 3]-shm'
    assert zc.capture_alias('a/.hidden', 1, 2) == 'a/.hidden [capture 1 of 2]'
    assert zc.capture_alias('noslash.txt', 1, 2) == 'noslash [capture 1 of 2].txt'
    assert zc.is_capture_alias('a/sms [capture 1 of 2].db')
    assert not zc.is_capture_alias('a/sms.db')


def test_annotate_captures_marks_every_copy():
    meta = {'d/sms.db': {}, 'd/sms [capture 1 of 2].db': {}, 'd/x': {}}
    zc.annotate_captures(meta)
    assert meta['d/sms.db']['capture'] == '2 of 2'
    assert meta['d/sms [capture 1 of 2].db']['capture'] == '1 of 2'
    assert 'capture' not in meta['d/x']


def test_archive_without_repeats_is_unchanged(tmp_path):
    entries = [{'name': f'f{i}.bin'.encode(), 'data': os.urandom(20)} for i in range(20)]
    zip_path, infos = _load(tmp_path, entries)
    assert [i.filename for i in infos] == [f'f{i}.bin' for i in range(20)]
    assert all(i.filename == i.orig_filename for i in infos)


def test_parser_run_log_warning_lines():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from artifact_viewer import _earlier_capture_warnings
    earlier = {'/d/sms.db': ['/d/sms [capture 1 of 2].db'],
               '/d/sms.db-wal': ['/d/sms [capture 1 of 2].db-wal']}
    # reading only the database still warns about its WAL's earlier capture
    lines = _earlier_capture_warnings({'/d/sms.db'}, earlier)
    assert len(lines) == 2
    assert 'd/sms.db exists (capture 1 of 2)' in lines[0]
    assert lines[0].endswith('sms [capture 1 of 2].db')
    assert 'sms.db-wal' in lines[1]
    assert _earlier_capture_warnings({'/d/other.db'}, earlier) == []
