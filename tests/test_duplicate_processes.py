"""duplicate_scan.find_duplicate_groups hashing stored main-zip entries in
worker processes (processes > 1) gives exactly the results of the thread
path: groups of identical files, a CRC32 collision that MD5 tells apart,
compressed duplicates and files from a reader (unpacked from a nested
archive) mixed in, and a cancel before any result stays partial-safe."""
import zipfile
import zlib

import pytest

import duplicate_scan
import zip_cd_cache


def _crc_collision():
    # two different 8-byte strings with the same CRC32 (CRC32 is one-to-one on
    # 4-byte inputs, and linear, so random 8-byte ones; ~2^16 tries by the
    # birthday bound)
    import random
    rnd = random.Random(1)
    seen = {}
    for _ in range(1 << 20):
        b = rnd.getrandbits(64).to_bytes(8, 'little')
        c = zlib.crc32(b)
        if c in seen:
            return seen[c], b
        seen[c] = b
    raise AssertionError('no collision found')


@pytest.fixture
def case(tmp_path):
    zpath = tmp_path / 'EXTRACTION_FFS.zip'
    a, b = _crc_collision()
    with zipfile.ZipFile(zpath, 'w') as zf:
        for g in range(25):
            for k in range(3):
                zf.writestr(f'd/g{g}_{k}.bin', (b'group %d ' % g) * (g + 1))
        for u in range(15):
            zf.writestr(f'd/u{u}.bin', b'unique %d' % u)
        zf.writestr('d/crc_a', a)
        zf.writestr('d/crc_b', b)
        for k in range(2):
            zf.writestr(zipfile.ZipInfo(f'd/z{k}.txt'), b'compressed ' * 50,
                        compress_type=zipfile.ZIP_DEFLATED)
    c = tmp_path / 'case'
    c.mkdir()
    assert zip_cd_cache.save(str(zpath), str(c))
    infos = zip_cd_cache.load(str(zpath), str(c))
    cands = [(i.filename, i.filename, i, 0, 0) for i in infos]
    # a file from a nested archive, identical to group 0's files
    g0 = next(i for i in infos if i.filename == 'd/g0_0.bin')
    cands.append(('nested.zip/inner.bin', '', g0, 0, 0, '', lambda: b'group 0 '))
    return str(zpath), cands


def test_processes_give_the_same_groups(case, monkeypatch):
    zpath, cands = case
    monkeypatch.setattr(duplicate_scan, '_MIN_FOR_PROCESSES', 1)
    monkeypatch.setattr(duplicate_scan, '_PROCESS_BATCH', 7)
    threads = duplicate_scan.find_duplicate_groups(zpath, cands, processes=0)
    handed_back = []
    real = duplicate_scan._hash_in_processes

    def spy(*a, **k):
        left = real(*a, **k)
        handed_back.append(left)
        return left
    monkeypatch.setattr(duplicate_scan, '_hash_in_processes', spy)
    procs = duplicate_scan.find_duplicate_groups(zpath, cands, processes=2)
    # the pool ran: only the compressed pair and the nested file went to the threads
    assert len(handed_back) == 1 and len(handed_back[0]) == 3
    assert procs == threads
    assert len(threads) == 25 * 3 + 1 + 2          # groups, the nested copy, the compressed pair
    assert 'd/crc_a' not in threads and 'd/crc_b' not in threads
    assert threads['nested.zip/inner.bin']['group_size'] == 4


def test_cancel_before_results(case, monkeypatch):
    zpath, cands = case
    monkeypatch.setattr(duplicate_scan, '_MIN_FOR_PROCESSES', 1)
    out = duplicate_scan.find_duplicate_groups(zpath, cands, processes=2, cancel_check=lambda: True)
    assert out == {}
