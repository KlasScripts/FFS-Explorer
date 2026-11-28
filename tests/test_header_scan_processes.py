"""header_scan.scan_entries on worker processes gives what it gives on
threads (owner request 2026-11-28: header scan on every core)."""
import zipfile

import header_scan
import zip_cd_cache


def _zip(path):
    blobs = {
        'a.jpg': b'\xff\xd8\xff\xe0' + b'\x00' * 100,
        'b.png': b'\x89PNG\r\n\x1a\n' + b'\x00' * 100,
        'c.txt': b'plain text ' * 50,
        'd.txt': b'text at first ' * 400 + b'\x00\x01\x02 then binary',   # text check reads on
        'e.bin': bytes(range(256)) * 4,
        'f.sqlite': b'SQLite format 3\x00' + b'\x00' * 200,
        'empty': b'',
    }
    names = []
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        for i in range(60):
            for name, data in blobs.items():
                n = f'{i}/{name}'
                z.writestr(n, data)
                names.append(n)
    return names


def _candidates(path):
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    offsets = zip_cd_cache.compute_data_offsets(path, infos) if hasattr(
        zip_cd_cache, 'compute_data_offsets') else None
    out = []
    with open(path, 'rb') as f:
        for info in infos:
            f.seek(info.header_offset + 26)
            n, m = int.from_bytes(f.read(2), 'little'), int.from_bytes(f.read(2), 'little')
            out.append((info.filename, info.header_offset + 30 + n + m, info.file_size))
    return out


def test_processes_equal_threads(tmp_path, monkeypatch):
    path = str(tmp_path / 'a.zip')
    _zip(path)
    cands = _candidates(path)
    threads = header_scan.scan_entries(path, cands, processes=1)
    monkeypatch.setattr(header_scan, 'PROCESS_MIN_FILES', 10)
    monkeypatch.setattr(header_scan, 'JOB_FILES', 37)
    left = []
    procs = header_scan.scan_entries(path, cands, processes=3,
                                     progress_cb=left.append)
    assert procs == threads
    assert left and left[-1] == 0
    assert {v for v in threads.values()} >= {'Text'}
    assert '0/d.txt' not in threads          # binary after its text start
