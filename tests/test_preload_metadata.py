"""ffs_metadata.preload_metadata reads a Cellebrite metadata.msgpack before
the index exists: the same metadata the adapter would open, or None (then
the adapter opens it as before) - 2026-11-28."""
import os
import zipfile

import msgpack
import pytest

import ffs_metadata
import zip_cd_cache

pytestmark = pytest.mark.filterwarnings('ignore:Duplicate name')


def _case(tmp_path, entries, method=zipfile.ZIP_DEFLATED):
    zp = str(tmp_path / 'EXTRACTION_FFS.zip')
    with zipfile.ZipFile(zp, 'w', method) as z:
        for name, data in entries:
            z.writestr(name, data)
    case = str(tmp_path / 'case')
    os.makedirs(case)
    assert zip_cd_cache.save(zp, case)
    return zp, case


def _adapter_read(zp, case, name):
    infos = zip_cd_cache.load(zp, case)
    z = zip_cd_cache.CachedZipView(zp, infos)
    with z.open(name) as f:
        return msgpack.unpack(f)


@pytest.mark.parametrize('method', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_same_as_the_adapter(tmp_path, method):
    meta = {f'mobile/f{i}': {'size': i, 'mtime': i * 7} for i in range(500)}
    zp, case = _case(tmp_path, [('filesystem2/mobile/a', b'x'),
                                ('metadata1/metadata.msgpack', msgpack.packb({'old': {}})),
                                ('metadata2/metadata.msgpack', msgpack.packb(meta))], method)
    got = ffs_metadata.preload_metadata(zp, case)
    assert got[0] == 'metadata2/metadata.msgpack'           # metadata2 first, as the adapter
    assert got[1] == _adapter_read(zp, case, got[0]) == meta


def test_name_stored_twice_takes_the_one_the_index_names(tmp_path):
    zp, case = _case(tmp_path, [('metadata2/metadata.msgpack', msgpack.packb({'first': 1})),
                                ('metadata2/metadata.msgpack', msgpack.packb({'second': 2}))])
    got = ffs_metadata.preload_metadata(zp, case)
    assert got[1] == _adapter_read(zp, case, 'metadata2/metadata.msgpack') == {'second': 2}


def test_none_when_absent_or_damaged(tmp_path):
    zp, case = _case(tmp_path / 'a' if os.makedirs(tmp_path / 'a') is None else None,
                     [('filesystem1/x', b'y')])
    assert ffs_metadata.preload_metadata(zp, case) is None
    os.makedirs(tmp_path / 'b')
    zp, case = _case(tmp_path / 'b', [('metadata2/metadata.msgpack', msgpack.packb({'k': 1}))],
                     zipfile.ZIP_STORED)
    raw = bytearray(open(zp, 'rb').read())
    k = raw.find(msgpack.packb({'k': 1}))
    raw[k + 1] ^= 0xFF                                       # the data no longer matches its CRC
    open(zp, 'wb').write(bytes(raw))
    assert ffs_metadata.preload_metadata(zp, case) is None
