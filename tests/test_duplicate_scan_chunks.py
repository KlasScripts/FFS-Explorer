"""duplicate_scan hashes main-archive files in chunks, on several threads
(2026-11-01). Checked against hashlib on the real bytes of a real stored zip,
with a chunk size smaller than the files so the chunk loop really runs."""
import hashlib
import os
import zipfile

import duplicate_scan


def _zip(tmp_path, files: dict) -> tuple[str, list]:
    path = str(tmp_path / 'ffs.zip')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    return path, [(i.filename, i.filename, i, 0, 0) for i in infos]


def test_chunked_md5_matches_whole_file(tmp_path, monkeypatch):
    monkeypatch.setattr(duplicate_scan, '_CHUNK', 5)
    same = b'0123456789abcdefghij-photo'           # 26 bytes: 6 chunks
    path, cands = _zip(tmp_path, {'a/1.jpg': same, 'b/1.jpg': same, 'c/2.jpg': b'x' * 26})
    res = duplicate_scan.find_duplicate_groups(path, cands)
    assert set(res) == {'a/1.jpg', 'b/1.jpg'}
    assert res['a/1.jpg'] == {'md5': hashlib.md5(same).hexdigest(), 'size': 26, 'group_size': 2}


def test_many_groups_on_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(duplicate_scan, '_CHUNK', 7)
    files = {}
    for g in range(60):                      # 60 groups of 3 identical files
        data = os.urandom(40 + g)
        for k in range(3):
            files[f'g{g}/f{k}.bin'] = data
    path, cands = _zip(tmp_path, files)
    res = duplicate_scan.find_duplicate_groups(path, cands)
    assert len(res) == 180
    for name, v in res.items():
        assert v['md5'] == hashlib.md5(files[name]).hexdigest() and v['group_size'] == 3


def test_file_cut_short_is_never_reported(tmp_path):
    same = b'A' * 64
    path, cands = _zip(tmp_path, {'a': same, 'b': same})
    # Cut the archive inside the second file's data: its bytes can no
    # longer be read in full, so it can't be confirmed as a duplicate.
    second = max(cands, key=lambda c: c[2].header_offset)[2]
    with open(path, 'r+b') as f:
        f.truncate(second.header_offset + 30 + len(second.filename) + 10)
    assert duplicate_scan.find_duplicate_groups(path, cands) == {}


def test_cancel_returns_only_whole_groups(tmp_path):
    files = {f'g{g}/f{k}': bytes([g]) * 50 for g in range(20) for k in range(2)}
    path, cands = _zip(tmp_path, files)
    calls = [0]

    def cancel():
        calls[0] += 1
        return calls[0] > 15
    res = duplicate_scan.find_duplicate_groups(path, cands, cancel_check=cancel)
    assert len(res) % 2 == 0 and len(res) < 40
    for name, v in res.items():
        assert v['group_size'] == 2
        assert sum(1 for w in res.values() if w['md5'] == v['md5']) == 2
