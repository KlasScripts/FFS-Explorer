"""zip_index: every entry reads as zipfile reads it (plus the capture naming,
A-72), except that a backslash in a name is never turned into "/".

Each zip is built here, so the expected entries are known; the reference is
CPython's own zipfile on the same bytes, not this module's output."""
import io
import os
import struct
import sys
import zipfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import zip_cd_cache as zc  # noqa: E402
import zip_index as zi  # noqa: E402

FIELDS = ("filename", "orig_filename", "header_offset", "file_size", "compress_size", "CRC",
          "compress_type", "flag_bits", "external_attr")


def _payload(zip_bytes: bytes) -> bytes:
    """The .zcd payload for an in-memory zip (as zip_cd_cache.save makes it)."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
        f.write(zip_bytes)
    try:
        return zc._extract_cd_payload(f.name)
    finally:
        os.unlink(f.name)


def _reference(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        infos, by_name = zf.infolist(), zf.NameToInfo
    zc._name_repeated_captures(infos, by_name)
    return infos, by_name


def _index(tmp_path, payload: bytes) -> zi.ZipIndex:
    path = str(tmp_path / "t.ffsidx")
    zi.write(zi.parse_directory(payload), path, "0" * 64)
    return zi.ZipIndex(path)


def _same_as_zipfile(tmp_path, zip_bytes: bytes):
    payload = _payload(zip_bytes)
    infos, by_name = _reference(payload)
    ix = _index(tmp_path, payload)
    assert len(ix) == len(infos)
    for k, x in enumerate(infos):
        e = ix.entry(k)
        for f in FIELDS:
            assert getattr(e, f) == getattr(x, f), (k, f)
        assert e.extra == x.extra and e.date_time == x.date_time and e.is_dir() == x.is_dir()
    pos = {id(x): k for k, x in enumerate(infos)}
    assert all(ix.find(n) == pos[id(x)] for n, x in by_name.items())
    assert [ix.name(i) for i in ix.order] == list(by_name)
    assert ix.find("not/in/the/archive") == -1
    return ix


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def test_plain_names_folders_and_utf8(tmp_path):
    _same_as_zipfile(tmp_path, _zip([("Dump/", b""), ("Dump/data/a.db", b"x" * 100),
                                     ("Dump/data/bé×.txt", b"y"), ("Dump/data/日本.jpg", b"z")]))


def test_a_path_written_twice_gets_capture_names(tmp_path):
    raw = _zip([("d/sms.db", b"first"), ("d/sms.db-wal", b"w1"), ("d/sms.db", b"second"),
                ("d/sms.db-wal", b"w2"), ("d/other", b"o")])
    ix = _same_as_zipfile(tmp_path, raw)
    names = {ix.name(i) for i in range(len(ix))}
    assert {"d/sms [capture 1 of 2].db", "d/sms [capture 1 of 2].db-wal", "d/sms.db", "d/sms.db-wal"} <= names


def _patch_record(raw: bytes, name: bytes, *, flags=None, extra=None):
    """Rewrite one central-directory record's flags and/or extra field."""
    out = bytearray(raw)
    pos = raw.find(b"PK\x01\x02")
    while pos >= 0:
        nlen, elen, clen = struct.unpack_from("<HHH", raw, pos + 28)
        if raw[pos + 46:pos + 46 + nlen] == name:
            break
        pos = raw.find(b"PK\x01\x02", pos + 46 + nlen + elen + clen)
    assert pos >= 0
    if flags is not None:
        struct.pack_into("<H", out, pos + 8, flags)
    if extra is not None:
        old_elen = struct.unpack_from("<H", out, pos + 30)[0]
        start = pos + 46 + struct.unpack_from("<H", out, pos + 28)[0]
        out[start:start + old_elen] = extra
        struct.pack_into("<H", out, pos + 30, len(extra))
        eocd = out.rfind(b"PK\x05\x06")
        cd_size = struct.unpack_from("<I", out, eocd + 12)[0]
        struct.pack_into("<I", out, eocd + 12, cd_size + len(extra) - old_elen)
    return bytes(out)


def test_utf8_name_without_the_flag_is_read_as_utf8(tmp_path):
    """GrayKey writes UTF-8 names without flag bit 11 (A-80). zipfile reads
    them as cp437 ("1170-├ù-2436.png"); the index reads UTF-8 and records
    the old name for case_names."""
    name = "Library/chronod/1170-×-2436.png"
    raw = _patch_record(_zip([(name, b"a"), ("other.txt", b"b")]), name.encode(), flags=0)
    ix = _index(tmp_path, _payload(raw))
    garbled = name.encode().decode("cp437")
    assert ix.name(0) == name and ix.orig_name(0) == name and ix.find(name) == 0
    assert ix.find(garbled) == -1 and ix.stored_name_len[0] == len(name.encode())
    assert ix.renames() == [(garbled, name)]


def test_cp437_bytes_without_the_flag_stay_cp437(tmp_path):
    """Bytes that are not valid UTF-8 are read as cp437, exactly as zipfile."""
    raw = _zip([("plain/cafe.txt", b"a"), ("other.txt", b"b")])
    raw = raw.replace(b"plain/cafe.txt", b"plain/caf\x82.txt")
    raw = _patch_record(raw, b"plain/caf\x82.txt", flags=0)
    ix = _same_as_zipfile(tmp_path, raw)
    assert ix.name(0) == "plain/café.txt" and ix.renames() == []


def test_unicode_path_extra_field_replaces_the_name(tmp_path):
    raw = _zip([("plain/cafe.txt", b"a"), ("other.txt", b"b")])
    raw = raw.replace(b"plain/cafe.txt", b"plain/caf\x82.txt")
    stored = b"plain/caf\x82.txt"
    raw = _patch_record(raw, stored, flags=0)
    up = "plain/unicode-name.txt".encode("utf-8")
    field = struct.pack("<HHBL", 0x7075, 5 + len(up), 1, zlib.crc32(stored)) + up
    ix = _same_as_zipfile(tmp_path, _patch_record(raw, stored, extra=field))
    assert ix.find("plain/unicode-name.txt") >= 0


def test_null_byte_cuts_the_name(tmp_path):
    raw = _zip([("evil.txt\u0000.jpg", b"x")])
    ix = _same_as_zipfile(tmp_path, raw)
    assert ix.name(0) == "evil.txt"


def test_zip64_extra_field_supplies_sizes_and_offset(tmp_path):
    raw = _zip([("a", b"1" * 10), ("b", b"2" * 20)])
    # Mark b's sizes and offset as ZIP64 and give the real values in the
    # extra field, as an archive over 4 GiB stores them.
    pos = raw.find(b"PK\x01\x02", raw.find(b"PK\x01\x02") + 1)
    csize, usize = struct.unpack_from("<II", raw, pos + 20)
    hoff = struct.unpack_from("<I", raw, pos + 42)[0]
    out = bytearray(raw)
    struct.pack_into("<II", out, pos + 20, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", out, pos + 42, 0xFFFFFFFF)
    raw = _patch_record(bytes(out), b"b", extra=struct.pack("<HHQQQ", 1, 24, usize, csize, hoff))
    ix = _same_as_zipfile(tmp_path, raw)
    assert (ix.file_size[1], ix.compress_size[1], ix.header_offset[1]) == (usize, csize, hoff)


def test_backslash_is_kept_on_every_platform(tmp_path):
    """iOS Mail stores Gmail folders as '[Gmail]/\\Sent.imapmbox'. zipfile on
    Windows turned that backslash into '/'; the index never does, and
    records the Windows name (and its folder) for case_names."""
    base = "fs/Library/Mail/X/[Gmail]/"
    name = base + "\\Sent.imapmbox/Info.plist"
    payload = _payload(_zip([(base + "\\Sent.imapmbox/", b""), (name, b"p")]))
    ix = _index(tmp_path, payload)
    assert ix.name(1) == name and ix.find(name) == 1
    assert sorted(ix.renames()) == [(base + "/Sent.imapmbox", base + "\\Sent.imapmbox"),
                                    (base + "/Sent.imapmbox/Info.plist", name)]


def test_load_serves_index_entries_and_builds_the_index_once(tmp_path):
    zpath = tmp_path / "a.zip"
    zpath.write_bytes(_zip([("Dump/x.db", b"1"), ("Dump/y.db", b"2")]))
    case = tmp_path / "case"
    case.mkdir()
    assert zc.save(str(zpath), str(case))
    infos = zc.load(str(zpath), str(case))
    assert [i.filename for i in infos] == ["Dump/x.db", "Dump/y.db"]
    assert os.path.exists(zi.index_path(str(zpath), str(case)))
    view = zc.CachedZipView(str(zpath), infos)
    assert view.getinfo("Dump/y.db") is infos[1] and view.namelist() == ["Dump/x.db", "Dump/y.db"]
    assert view.open("Dump/x.db").read() == b"1"
