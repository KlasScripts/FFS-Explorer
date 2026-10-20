"""The direct-seek readers return exactly what zipfile returns, with fewer
file opens: read_nested_entry (cached per-archive index), _LazyZipSlice
(one handle per slice) and ZipEntry (one open on first read)."""
import builtins
import gzip
import io
import os
import zipfile

import pytest

import zip_reader
from zip_entry import ZipEntry
from zip_reader import read_nested_entry


@pytest.fixture
def count_opens(monkeypatch):
    """Count builtins.open calls made on one path."""
    real_open = builtins.open
    counts = {}

    def counting_open(file, *args, **kwargs):
        counts[str(file)] = counts.get(str(file), 0) + 1
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', counting_open)
    return counts


def _repacked_zip(path, n=300):
    """A nested archive as nested_archive.extract_one() writes it: ZIP_STORED."""
    data = {f'assets/sub/f{i:04}.bin': os.urandom(20 + i % 50) for i in range(n)}
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as z:
        for name, blob in data.items():
            z.writestr(name, blob)
    return data


def test_nested_entries_match_zipfile(tmp_path):
    p = str(tmp_path / 'nested.zip')
    data = _repacked_zip(p)
    for name, blob in data.items():
        assert read_nested_entry(p, name) == blob


def test_nested_index_parsed_once(tmp_path, monkeypatch):
    p = str(tmp_path / 'nested.zip')
    data = _repacked_zip(p)
    parses = []
    real = zipfile.ZipFile

    class CountingZipFile(real):
        def __init__(self, file, *a, **k):
            parses.append(file)
            super().__init__(file, *a, **k)
    monkeypatch.setattr(zipfile, 'ZipFile', CountingZipFile)
    for name in data:
        read_nested_entry(p, name)
    assert parses.count(p) == 1   # was one full central-directory parse per entry


def test_nested_missing_entry_and_deflated(tmp_path):
    p = str(tmp_path / 'mixed.zip')
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('stored', b's' * 100, compress_type=zipfile.ZIP_STORED)
        z.writestr('deflated', b'd' * 1000, compress_type=zipfile.ZIP_DEFLATED)
    assert read_nested_entry(p, 'stored') == b's' * 100
    assert read_nested_entry(p, 'deflated') == b'd' * 1000   # zipfile fallback
    assert read_nested_entry(p, 'no-such-entry') is None


def test_nested_gzip_blob_returned_whole(tmp_path):
    # gzip sources are stored as their raw decompressed bytes, not a zip.
    p = str(tmp_path / 'blob.bin')
    raw = gzip.decompress(gzip.compress(b'plain decompressed content' * 10))
    open(p, 'wb').write(raw)
    assert read_nested_entry(p, 'anything') == raw


def test_nested_index_invalidated_on_reextraction(tmp_path):
    p = str(tmp_path / 'nested.zip')
    _repacked_zip(p, n=5)
    read_nested_entry(p, 'assets/sub/f0000.bin')
    with zipfile.ZipFile(p, 'w', compression=zipfile.ZIP_STORED) as z:
        z.writestr('replaced.txt', b'new content, different size')
    os.utime(p, (1, 1))   # force a different mtime even on coarse filesystems
    assert read_nested_entry(p, 'replaced.txt') == b'new content, different size'
    assert read_nested_entry(p, 'assets/sub/f0000.bin') is None


def test_nested_index_lru_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(zip_reader, '_NESTED_INDEX_MAX', 3)
    zip_reader._nested_index.clear()
    for i in range(6):
        p = str(tmp_path / f'n{i}.zip')
        _repacked_zip(p, n=2)
        read_nested_entry(p, 'assets/sub/f0000.bin')
    assert len(zip_reader._nested_index) == 3


def test_lazy_zip_slice_reads_and_opens_once(tmp_path, count_opens):
    from media_metadata_scan import _LazyZipSlice
    p = str(tmp_path / 'ffs.zip')
    payload = os.urandom(10_000)
    with zipfile.ZipFile(p, 'w', compression=zipfile.ZIP_STORED) as z:
        z.writestr('pad', b'x' * 123)
        z.writestr('video.mov', payload)
    info = zipfile.ZipFile(p).getinfo('video.mov')
    entry = ZipEntry(p, info.filename, info)
    data_offset = entry.data_offset
    count_opens.clear()
    with _LazyZipSlice(p, data_offset, info.file_size) as s:
        chunks = [s.read(1000) for _ in range(5)]
        s.seek(9_000)
        tail = s.read()
        s.seek(-10, io.SEEK_END)
        last = s.read(100)        # clamped to the entry's own end
    assert b''.join(chunks) == payload[:5000]
    assert tail == payload[9000:]
    assert last == payload[-10:]
    assert count_opens.get(p) == 1


def test_zip_entry_first_read_single_open(tmp_path, count_opens):
    p = str(tmp_path / 'ffs.zip')
    payload = os.urandom(5000)
    with zipfile.ZipFile(p, 'w', compression=zipfile.ZIP_STORED) as z:
        z.writestr('a/b.db', payload)
    info = zipfile.ZipFile(p).getinfo('a/b.db')
    count_opens.clear()
    e = ZipEntry(p, info.filename, info)
    assert e.read() == payload
    assert count_opens.get(p) == 1          # was 2: header, then data
    assert e.read(100) == payload[:100]
    assert e.read_at(4000, 50) == payload[4000:4050]
    assert e.data_offset == ZipEntry(p, info.filename, info).data_offset
