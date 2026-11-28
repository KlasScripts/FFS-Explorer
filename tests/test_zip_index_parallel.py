"""zip_index.parse_directory on worker processes gives the index the
serial loop gives, and the same error on a damaged directory (A-98)."""
import zipfile

import pytest

import zip_index

pytestmark = pytest.mark.filterwarnings('ignore:Duplicate name')


def _payload(tmp_path, n=900):
    p = tmp_path / 'a.zip'
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_STORED) as z:
        for i in range(n):
            z.writestr(f'dir{i % 7}/f{i}.txt', b'x' * (i % 13))
            if i % 50 == 0:
                z.writestr(f'dir{i % 7}/f{i}.txt', b'again')      # a path stored twice
            if i % 97 == 0:
                z.writestr(f'odd\\name{i}.bin', b'b')              # a backslash: A-80
    raw = p.read_bytes()
    cd_off = raw.rfind(b'PK\x01\x02', 0, raw.find(b'PK\x05\x06'))
    payload = raw                                                   # .zcd payload: the zip's bytes
    zcd = tmp_path / 'a.zcd'
    zcd.write_bytes(b'ZCD\x02' + payload)
    return payload, str(zcd)


def _same(a, b):
    assert {k: list(v) for k, v in a['cols'].items()} == {k: list(v) for k, v in b['cols'].items()}
    for k in ('names', 'origs', 'extras', 'renames'):
        assert a[k] == b[k], k
    assert list(a['table']) == list(b['table']) and list(a['order']) == list(b['order'])


def test_parallel_equals_serial(tmp_path, monkeypatch):
    payload, zcd = _payload(tmp_path)
    serial = zip_index.parse_directory(payload)
    monkeypatch.setattr(zip_index, 'PARALLEL_MIN_BYTES', 1)
    monkeypatch.setattr(zip_index, 'JOB_RECORDS', 64)

    def serial_not_used(*a, **k):
        raise AssertionError('parsed in this process, not on the workers')
    monkeypatch.setattr(zip_index, '_parse_records', serial_not_used)   # workers import their own
    par = zip_index.parse_directory(payload, zcd_path=zcd, payload_file_off=4, processes=3)
    _same(serial, par)
    assert serial['renames']                    # the backslash names were read


def test_damaged_directory_same_error(tmp_path, monkeypatch):
    payload, zcd = _payload(tmp_path, n=300)
    cd_off, _size = zip_index._cd_bounds(payload)
    bad = bytearray(payload)
    k = bad.find(b'PK\x01\x02', cd_off + 46 * 150)
    bad[k:k + 4] = b'XX\x01\x02'                # one record's signature broken
    bad = bytes(bad)
    with pytest.raises(zip_index.IndexError_) as e1:
        zip_index.parse_directory(bad)
    (tmp_path / 'b.zcd').write_bytes(b'ZCD\x02' + bad)
    monkeypatch.setattr(zip_index, 'PARALLEL_MIN_BYTES', 1)
    monkeypatch.setattr(zip_index, 'JOB_RECORDS', 32)
    with pytest.raises(zip_index.IndexError_) as e2:
        zip_index.parse_directory(bad, zcd_path=str(tmp_path / 'b.zcd'), payload_file_off=4,
                                  processes=3)
    assert str(e1.value) == str(e2.value)
