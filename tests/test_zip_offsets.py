"""zip_cd_cache.compute_data_offsets against hand-built zips whose exact data
offsets are known by construction.

Each archive is written byte by byte (not via zipfile) so the test controls
precisely the layouts real FFS zips have and the old single-delta probe got
wrong: a CD ZIP64 block on only the entries past the 4 GiB mark, ZIP64 blocks
in only SOME local headers, data descriptors, gaps between entries, and
non-UTF-8 (cp437) names. Every offset is checked against where the writer
actually put the data — never against another implementation.
"""
import os
import struct
import zlib

import pytest

import zip_cd_cache as zc


def _write_zip(path, entries):
    """entries: list of dicts with keys name (bytes), data (bytes), and
    optional utf8, local_zip64, cd_zip64, descriptor, gap_after.
    Returns {decoded name: true data offset}."""
    body = bytearray()
    cd = bytearray()
    expected = {}
    for e in entries:
        name, data = e['name'], e['data']
        flags = (0x800 if e.get('utf8') else 0) | (0x08 if e.get('descriptor') else 0)
        crc = zlib.crc32(data)
        local_extra = (struct.pack('<HHQQ', 1, 16, len(data), len(data))
                       if e.get('local_zip64') else b'')
        offset = len(body)
        lcrc, lsize = (0, 0) if e.get('descriptor') else (crc, len(data))
        body += struct.pack('<4sHHHHHIIIHH', b'PK\x03\x04', 20, flags, 0, 0, 0x21,
                            lcrc, lsize, lsize, len(name), len(local_extra))
        body += name + local_extra
        expected[name.decode('utf-8' if e.get('utf8') else 'cp437')] = len(body)
        body += data
        if e.get('descriptor'):
            body += struct.pack('<4sIII', b'PK\x07\x08', crc, len(data), len(data))
        body += b'\0' * e.get('gap_after', 0)
        cd_extra = struct.pack('<HHQ', 1, 8, offset) if e.get('cd_zip64') else b''
        cd += struct.pack('<4sHHHHHHIIIHHHHHII', b'PK\x01\x02', 20, 20, flags, 0, 0,
                          0x21, crc, len(data), len(data), len(name), len(cd_extra),
                          0, 0, 0, 0, 0xFFFFFFFF if e.get('cd_zip64') else offset)
        cd += name + cd_extra
    n = len(entries)
    eocd = struct.pack('<4sHHHHIIH', b'PK\x05\x06', 0, 0, n, n, len(cd), len(body), 0)
    with open(path, 'wb') as fh:
        fh.write(bytes(body) + bytes(cd) + eocd)
    return expected


def _load(tmp_path, entries):
    zip_path = str(tmp_path / 'ffs.zip')
    expected = _write_zip(zip_path, entries)
    case_dir = str(tmp_path / 'case')
    os.makedirs(case_dir)
    assert zc.save(zip_path, case_dir)
    infos = zc.load(zip_path, case_dir)
    assert infos is not None and len(infos) == len(entries)
    return zip_path, infos, expected


def _count_header_reads(monkeypatch):
    calls = []
    real = zc._header_offset_read

    def counting(fh, info):
        calls.append(info.filename)
        return real(fh, info)
    monkeypatch.setattr(zc, '_header_offset_read', counting)
    return calls


def _entries(n, **per_entry):
    return [{'name': f'private/var/mobile/f{i:04}.bin'.encode(),
             'data': os.urandom(40 + i % 13),
             **{k: f(i) for k, f in per_entry.items()}} for i in range(n)]


def test_cd_zip64_past_4gib_boundary(tmp_path, monkeypatch):
    # The real >4 GiB layout: entries past the boundary carry a CD ZIP64
    # block the local header lacks — in a 100 GB extraction that is nearly
    # every entry. The old 5-entry probe then usually sampled only that
    # group and put the few entries before the boundary 12 bytes off.
    zip_path, infos, expected = _load(tmp_path, _entries(400, cd_zip64=lambda i: i >= 10))
    reads = _count_header_reads(monkeypatch)
    assert zc.compute_data_offsets(zip_path, infos) == expected
    # Derived from the sidecar: only the one-off verification sample and the
    # last entry need a real header read — not one read per entry.
    assert len(reads) < 20


def test_local_only_zip64_mix_is_never_guessed(tmp_path):
    # ZIP64 blocks in only some LOCAL headers — invisible in the CD, so any
    # sampling approach could miss them. Deterministic and exact here.
    entries = _entries(60, local_zip64=lambda i: i % 6 == 0)
    zip_path, infos, expected = _load(tmp_path, entries)
    for _ in range(5):
        assert zc.compute_data_offsets(zip_path, infos) == expected


def test_subset_matches_full_list(tmp_path):
    # Callers pass subsets (export queue, header-scan candidates, a single
    # search hit). Neighbours come from the full sidecar layout, not the subset.
    zip_path, infos, expected = _load(tmp_path, _entries(100, cd_zip64=lambda i: i >= 50))
    subset = infos[3::17]
    assert zc.compute_data_offsets(zip_path, subset) == {i.filename: expected[i.filename]
                                                         for i in subset}
    one = infos[77]
    assert zc.compute_data_offsets(zip_path, [one]) == {one.filename: expected[one.filename]}


def test_data_descriptors_and_gaps_fall_back_to_headers(tmp_path):
    entries = _entries(30, descriptor=lambda i: i % 4 == 1, gap_after=lambda i: 7 if i == 10 else 0)
    zip_path, infos, expected = _load(tmp_path, entries)
    assert zc.compute_data_offsets(zip_path, infos) == expected


def test_cp437_names(tmp_path):
    entries = [{'name': b'caf\x82/men\x81.txt', 'data': b'x' * 50},     # no UTF-8 flag
               {'name': 'café/ü.txt'.encode(), 'data': b'y' * 50, 'utf8': True},
               {'name': b'plain.txt', 'data': b'z' * 50}]
    zip_path, infos, expected = _load(tmp_path, entries)
    assert zc.compute_data_offsets(zip_path, infos) == expected


def test_legacy_delta_argument_is_ignored(tmp_path):
    # Snapshots saved by older versions hold a single int delta.
    zip_path, infos, expected = _load(tmp_path, _entries(50, cd_zip64=lambda i: i >= 25))
    assert zc.probe_delta(zip_path, infos) is None
    assert zc.compute_data_offsets(zip_path, infos, delta=-12) == expected


def test_without_sidecar_layout_reads_headers(tmp_path):
    # infos that never went through load() (no layout registered) are still
    # resolved correctly, from each local header.
    import zipfile
    zip_path = str(tmp_path / 'raw.zip')
    expected = _write_zip(zip_path, _entries(20, local_zip64=lambda i: i % 3 == 0))
    infos = zipfile.ZipFile(zip_path).infolist()
    assert zc.compute_data_offsets(zip_path, infos) == expected


def test_cached_views_share_load_index(tmp_path):
    zip_path, infos, _ = _load(tmp_path, _entries(10))
    a = zc.CachedZipView(zip_path, infos)
    b = zc.CachedZipView(zip_path, infos)
    assert a._by_name is b._by_name
    assert a.getinfo(infos[4].filename) is infos[4]
    assert a.namelist() == [i.filename for i in infos]
