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


def _renames_both(payload):
    """parse_directory's renames, and the full way's on the same records."""
    out = zip_index.parse_directory(payload)
    cd_off, cd_size = zip_index._cd_bounds(payload)
    recs = zip_index._parse_records(memoryview(payload)[cd_off:cd_off + cd_size], 0, cd_size)
    names = list(recs['names'])
    base = list(names)
    by_name = dict(zip(names, range(len(names))))
    rep = zip_index._name_repeated_captures(names, recs['cols']['header_offset'], by_name)
    full = zip_index._renames(base, recs['old'], names, by_name, recs['cols']['header_offset'],
                              repeated=rep, _full=True)
    fast = zip_index._renames_fast(base, recs['old'], names, by_name, rep)
    return out['renames'], full, fast


def _zip_payload(tmp_path, entries):
    p = tmp_path / 'r.zip'
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_STORED) as z:
        for n in entries:
            z.writestr(n, b'x')
    return p.read_bytes()


@pytest.mark.parametrize('entries,expect_fast', [
    (['a\\b.txt', 'c/d.txt', 'dir\\sub\\e.db'], True),                  # plain backslash names
    (['a\\b.txt', 'a/b.txt', 'x.txt'], False),                          # its other reading is a real name
    (['a\\b.txt', 'a\\b.txt', 'q.txt'], False),                         # the renamed name stored twice
    (['f\\g [capture 1 of 2].txt', 'h.txt'], False),                    # looks like a capture alias
    (['m\\n.txt', 'm/n.txt', 'm\\n.txt', 'z.db', 'z.db'], False),       # all of it at once
    (['p\\q.txt', 'r.db', 'r.db', 's/t'], True),                        # repeats elsewhere only
])
def test_renames_fast_way_equals_full_way(tmp_path, entries, expect_fast):
    got, full, fast = _renames_both(_zip_payload(tmp_path, entries))
    assert got == full
    assert (fast is not None) == expect_fast
