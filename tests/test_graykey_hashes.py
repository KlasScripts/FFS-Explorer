"""GrayKey's acquisition-time hashes (S2/S1/MD extra-field blocks,
ASSUMPTIONS.md A-73) and the duplicate scan's use of them.

Extra fields are built byte by byte in the exact shape measured on real
GrayKey zips (CTF26: S2 only; IOS18: S2 + S1 + MD), so the parser is checked
against known values, never against another parser.
"""
import hashlib
import struct
import zipfile
import zlib

from adapters import graykey
import duplicate_scan


def _block(tag: bytes, data: bytes) -> bytes:
    return tag + struct.pack('<H', len(data)) + data


def _info(name: str, data: bytes, extra: bytes) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.file_size = info.compress_size = len(data)
    info.CRC = zlib.crc32(data)
    info.extra = extra
    return info


def _ut(mtime=1_700_000_000):
    return _block(b'UT', bytes([1]) + struct.pack('<I', mtime))


def test_all_three_hashes_parsed_as_hex():
    data = b'example content'
    extra = (_ut() + _block(b'S2', hashlib.sha256(data).digest())
             + _block(b'S1', hashlib.sha1(data).digest())
             + _block(b'MD', hashlib.md5(data).digest())
             + _block(b'GK', b'\x01\x00'))
    meta = graykey._parse_entry(_info('/private/var/mobile/a.jpg', data, extra))
    assert meta['sha256'] == hashlib.sha256(data).hexdigest()
    assert meta['sha1'] == hashlib.sha1(data).hexdigest()
    assert meta['md5'] == hashlib.md5(data).hexdigest()


def test_sha256_only_archive_has_no_other_keys():
    data = b'android file'
    extra = _ut() + _block(b'S2', hashlib.sha256(data).digest())
    meta = graykey._parse_entry(_info('/data/data/x/f', data, extra))
    assert meta['sha256'] == hashlib.sha256(data).hexdigest()
    assert 'sha1' not in meta and 'md5' not in meta


def test_zero_placeholder_is_not_a_hash():
    # The local-header copy GrayKey writes before it knows the hash.
    extra = _ut() + _block(b'S2', bytes(32)) + _block(b'S1', bytes(20)) + _block(b'MD', bytes(16))
    meta = graykey._parse_entry(_info('/data/data/x/f', b'abc', extra))
    assert not {'sha256', 'sha1', 'md5'} & meta.keys()


def test_wrong_length_block_is_ignored():
    extra = _ut() + _block(b'S2', b'\x01' * 31)
    assert 'sha256' not in graykey._parse_entry(_info('/x', b'abc', extra))


def test_folders_carry_no_hashes():
    meta = graykey._parse_entry(_info('/data/data/x/', b'', _ut()))
    assert not {'sha256', 'sha1', 'md5'} & meta.keys()


# ── duplicate_scan with recorded SHA-256 ──

def _cand(path, data, sha='auto'):
    info = _info(path, data, b'')
    if sha == 'auto':
        sha = hashlib.sha256(data).hexdigest()
    return (path, path, info, 0, 0, sha)


def test_recorded_sha256_groups_without_reading(monkeypatch):
    def no_read(*a, **k):
        raise AssertionError('a file was read although every member had a recorded SHA-256')
    monkeypatch.setattr(duplicate_scan, 'ZipEntry', no_read)
    cands = [_cand('a/1.jpg', b'same'), _cand('b/1.jpg', b'same'), _cand('c/2.jpg', b'other')]
    res = duplicate_scan.find_duplicate_groups('unused.zip', cands)
    assert set(res) == {'a/1.jpg', 'b/1.jpg'}
    assert res['a/1.jpg']['md5'] == hashlib.sha256(b'same').hexdigest()
    assert res['a/1.jpg']['group_size'] == 2


def test_same_size_and_crc_but_different_sha256_are_not_duplicates(monkeypatch):
    monkeypatch.setattr(duplicate_scan, 'ZipEntry', None)
    a, b = _cand('a', b'x' * 10), _cand('b', b'x' * 10, sha='f' * 64)   # same size+CRC, other hash
    assert duplicate_scan.find_duplicate_groups('unused.zip', [a, b]) == {}


def test_group_with_a_member_lacking_a_hash_uses_md5(monkeypatch):
    class FakeEntry:
        def __init__(self, zip_path, phys, info):
            self.data = {'a': b'dup', 'b': b'dup'}[phys]

        def read(self):
            return self.data
    monkeypatch.setattr(duplicate_scan, 'ZipEntry', FakeEntry)
    res = duplicate_scan.find_duplicate_groups('unused.zip', [_cand('a', b'dup'), _cand('b', b'dup', sha='')])
    assert {v['md5'] for v in res.values()} == {hashlib.md5(b'dup').hexdigest()}


def test_five_tuple_candidates_still_accepted(monkeypatch):
    class FakeEntry:
        def __init__(self, zip_path, phys, info):
            pass

        def read(self):
            return b'q'
    monkeypatch.setattr(duplicate_scan, 'ZipEntry', FakeEntry)
    cands = [c[:5] for c in (_cand('a', b'q'), _cand('b', b'q'))]
    assert len(duplicate_scan.find_duplicate_groups('unused.zip', cands)) == 2
