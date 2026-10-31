"""zip_index.py — the archive's file directory as one compact, memory-mapped index.

Replaces parsing the .zcd sidecar through Python's zipfile, which built a
ZipInfo object per entry (about 767 bytes each: 517 MB and 1.3 s for the
674,000 entries of the CTF23 test archive) every time a case was opened.
Here the central directory is parsed once, by this module, into columns of
plain numbers plus pools of names and extra fields, written to one file in
the case folder (<zip name>.ffsidx), and mapped back on every later open:
nothing is parsed again, and the operating system loads only the pages used.

Entries read the same as zipfile reads them, field by field, with one
deliberate difference. Per CPython's zipfile (3.14):
  * a name is UTF-8 when general-purpose flag bit 11 is set, else cp437;
  * a name is cut at its first NUL byte;
  * a Unicode Path extra field (0x7075, version 1) whose CRC matches the
    stored name's bytes replaces the name;
  * the ZIP64 extra field (0x0001) supplies file size (when it is
    0xFFFFFFFF or 0xFFFFFFFFFFFFFFFF), compressed size and header offset
    (when 0xFFFFFFFF), in that order;
  * date_time comes from the DOS date and time words.
The difference: zipfile on Windows also turns every backslash in a name into
"/". This index does not, on any platform. FFS archives always separate
folders with "/", and a backslash is part of a real name (iOS Mail stores
Gmail folders as "[Gmail]/\\Sent.imapmbox"), so the Windows rule turned real
names into wrong paths. On macOS and Linux nothing changes.

Paths stored more than once (ASSUMPTIONS.md A-72) get the same names
zip_cd_cache._name_repeated_captures gave them: every copy but the last
becomes "<name> [capture k of n]", unless a real entry already has that name.

Qt-free. zip_cd_cache.load() returns this index's entries, so every caller
of the sidecar sees the same names and fields as before.
"""

from __future__ import annotations

import mmap
import os
import struct
import zlib
from array import array

_MAGIC = b"FFSIDX\x00\x02"
_HEADER = struct.Struct("<8sQ")              # magic, number of sections
_SECTION = struct.Struct("<16sQQ")           # name, offset, length
_CD_RECORD = struct.Struct("<4s6H3L5H2L")
_CD_SIG = b"PK\x01\x02"
_MAX_EXTRACT_VERSION = 63                    # zipfile.MAX_EXTRACT_VERSION

# Column name -> array type code. Order is the file order.
_COLUMNS = (
    ("header_offset", "Q"), ("compress_size", "Q"), ("file_size", "Q"),
    ("crc", "I"), ("flag_bits", "H"), ("compress_type", "H"),
    ("dos_time", "H"), ("dos_date", "H"), ("external_attr", "I"),
    ("stored_name_len", "H"),                # name length in bytes, as stored
    ("name_start", "Q"), ("name_len", "I"),  # final name in the names pool (UTF-8)
    ("orig_start", "Q"), ("orig_len", "I"),  # zipfile's orig_filename (UTF-8)
    ("orig_in_names", "B"),                  # 1: orig_start points into the names pool
    ("extra_start", "Q"), ("extra_len", "H"),
)


class IndexError_(Exception):
    """The directory could not be indexed the way zipfile would read it."""


def _sanitize(name: str) -> str:
    """zipfile._sanitize_filename without the Windows backslash rule (see
    the module docstring): cut at the first NUL."""
    nul = name.find("\x00")
    return name[:nul] if nul >= 0 else name


def _cd_bounds(payload: bytes) -> tuple[int, int]:
    """(start, size) of the central directory inside a .zcd payload: the
    directory bytes followed by end records whose CD offset is 0."""
    eocd = len(payload) - 22
    if eocd < 0 or payload[eocd:eocd + 4] != b"PK\x05\x06":
        raise IndexError_("no end-of-central-directory record")
    (_sig, _d, _ds, _e1, total, cd_size, cd_off, _cl) = struct.unpack_from("<4sHHHHIIH", payload, eocd)
    if cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF or total == 0xFFFF:
        loc = eocd - 20
        if payload[loc:loc + 4] != b"PK\x06\x07":
            raise IndexError_("no ZIP64 end-of-central-directory locator")
        z64 = struct.unpack_from("<4sIQI", payload, loc)[2]
        if payload[z64:z64 + 4] != b"PK\x06\x06":
            raise IndexError_("no ZIP64 end-of-central-directory record")
        cd_size, cd_off = struct.unpack_from("<QQ", payload, z64 + 40)
    return cd_off, cd_size


def parse_directory(payload: bytes) -> dict:
    """Columns, pools and the name table for a .zcd payload. Pure: the same
    payload always gives the same index."""
    cd_off, cd_size = _cd_bounds(payload)
    cd = memoryview(payload)[cd_off:cd_off + cd_size]
    cols = {name: array(code) for name, code in _COLUMNS}
    names = bytearray()
    origs = bytearray()
    extras = bytearray()
    final_names: list[str] = []
    origs_list: list[str] = []
    unpack = _CD_RECORD.unpack_from
    pos, end = 0, len(cd)
    while pos < end:
        if end - pos < 46:
            raise IndexError_("truncated central directory")
        (sig, _cv, ext_ver, flags, method, t, d, crc, csize, usize,
         nlen, elen, clen, _disk, _iattr, eattr, hoff) = unpack(cd, pos)
        if sig != _CD_SIG:
            raise IndexError_("bad central directory record signature")
        if (ext_ver & 0xFF) > _MAX_EXTRACT_VERSION:   # zipfile reads only this byte
            raise IndexError_(f"zip file version {(ext_ver & 0xFF) / 10:.1f}")
        p = pos + 46
        raw_name = bytes(cd[p:p + nlen])
        p += nlen
        extra = bytes(cd[p:p + elen])
        orig = raw_name.decode("utf-8" if flags & 0x800 else "cp437")
        name = _sanitize(orig)
        # Extra fields, as zipfile._decodeExtra reads them.
        e = extra
        name_crc = None
        while len(e) >= 4:
            tp, ln = struct.unpack("<HH", e[:4])
            if ln + 4 > len(e):
                raise IndexError_(f"corrupt extra field {tp:04x} (size={ln})")
            if tp == 0x0001:
                data = e[4:ln + 4]
                try:
                    if usize in (0xFFFF_FFFF_FFFF_FFFF, 0xFFFF_FFFF):
                        usize, = struct.unpack("<Q", data[:8])
                        data = data[8:]
                    if csize == 0xFFFF_FFFF:
                        csize, = struct.unpack("<Q", data[:8])
                        data = data[8:]
                    if hoff == 0xFFFF_FFFF:
                        hoff, = struct.unpack("<Q", data[:8])
                except struct.error:
                    raise IndexError_("corrupt zip64 extra field") from None
            elif tp == 0x7075:
                data = e[4:ln + 4]
                try:
                    up_version, up_crc = struct.unpack("<BL", data[:5])
                except struct.error:
                    raise IndexError_("corrupt unicode path extra field (0x7075)") from None
                if name_crc is None:
                    name_crc = zlib.crc32(raw_name)
                if up_version == 1 and up_crc == name_crc:
                    try:
                        up = data[5:].decode("utf-8")
                    except UnicodeDecodeError:
                        raise IndexError_("corrupt unicode path extra field (0x7075)") from None
                    if up:
                        name = _sanitize(up)
            e = e[ln + 4:]
        cols["header_offset"].append(hoff)
        cols["compress_size"].append(csize)
        cols["file_size"].append(usize)
        cols["crc"].append(crc)
        cols["flag_bits"].append(flags)
        cols["compress_type"].append(method)
        cols["dos_time"].append(t)
        cols["dos_date"].append(d)
        cols["external_attr"].append(eattr)
        cols["stored_name_len"].append(nlen)
        origs_list.append(orig)
        cols["extra_start"].append(len(extras))
        cols["extra_len"].append(elen)
        extras += extra
        final_names.append(name)
        pos = p + elen + clen
    # zipfile's NameToInfo (last entry with a name wins, first-seen order),
    # then the capture renaming, exactly as zip_cd_cache did on ZipInfo.
    by_name: dict[str, int] = {}
    for i, name in enumerate(final_names):
        by_name[name] = i
    _name_repeated_captures(final_names, cols["header_offset"], by_name)
    for i, name in enumerate(final_names):
        nb = name.encode("utf-8", "surrogatepass")
        cols["name_start"].append(len(names))
        cols["name_len"].append(len(nb))
        names += nb
        o = origs_list[i]
        if o == name:                         # the usual case: share the name's bytes
            cols["orig_start"].append(cols["name_start"][i])
            cols["orig_len"].append(len(nb))
            cols["orig_in_names"].append(1)
        else:
            ob = o.encode("utf-8", "surrogatepass")
            cols["orig_start"].append(len(origs))
            cols["orig_len"].append(len(ob))
            cols["orig_in_names"].append(0)
            origs += ob
    return {"cols": cols, "names": bytes(names), "origs": bytes(origs), "extras": bytes(extras),
            "table": _hash_table(by_name), "order": array("I", by_name.values())}


def _name_repeated_captures(names: list[str], header_offsets, by_name: dict) -> None:
    """zip_cd_cache._name_repeated_captures, on names and a {name: index}
    dict instead of ZipInfo objects (see A-72)."""
    from zip_cd_cache import capture_alias
    copies: dict[str, list[int]] = {}
    for i, name in enumerate(names):
        if not name.endswith("/"):
            copies.setdefault(name, []).append(i)
    for name, group in copies.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda i: header_offsets[i])
        n = len(group)
        for k, i in enumerate(group, 1):
            if k == n:
                break
            alias = capture_alias(name, k, n)
            if alias in by_name:              # a real entry already has that name
                continue
            names[i] = alias
            by_name[alias] = i
        by_name[name] = group[-1]


def _hash_table(by_name: dict[str, int]) -> array:
    """Open-addressing table: slot -> entry index, keyed by the CRC-32 of the
    UTF-8 name (stable across runs, unlike Python's hash()). Only the entry
    by_name maps a name to is reachable, as with zipfile's NameToInfo."""
    size = 1
    while size < max(len(by_name), 1) * 2:
        size <<= 1
    table = array("i", [-1]) * size
    mask = size - 1
    for name, i in by_name.items():
        h = zlib.crc32(name.encode("utf-8", "surrogatepass")) & mask
        while table[h] != -1:
            h = (h + 1) & mask
        table[h] = i
    return table


# ── File format ───────────────────────────────────────────────────────────────

def index_path(zip_path: str, case_dir: str) -> str:
    return os.path.join(case_dir, os.path.basename(zip_path) + ".ffsidx")


def write(parsed: dict, path: str, cd_sha256: str) -> None:
    """Write *parsed* to *path* (via a temporary file, so a crash never
    leaves half an index)."""
    sections = [("cd_sha256", cd_sha256.encode("ascii")), ("names", parsed["names"]),
                ("origs", parsed["origs"]), ("extras", parsed["extras"]),
                ("table", parsed["table"].tobytes()), ("order", parsed["order"].tobytes())]
    sections += [(name, parsed["cols"][name].tobytes()) for name, _code in _COLUMNS]
    head = _HEADER.size + _SECTION.size * len(sections)
    offset = (head + 7) & ~7
    table, blobs = [], []
    for name, blob in sections:
        table.append(_SECTION.pack(name.encode("ascii"), offset, len(blob)))
        pad = (-len(blob)) & 7                # keep every section 8-byte aligned
        blobs.append(blob + b"\x00" * pad)
        offset += len(blob) + pad
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_HEADER.pack(_MAGIC, len(sections)))
        f.writelines(table)
        f.write(b"\x00" * (((head + 7) & ~7) - head))
        f.writelines(blobs)
    os.replace(tmp, path)


class ZipIndex:
    """A mapped .ffsidx: len(), name(i), find(name), the columns, extra(i)."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        mv = memoryview(self._mm)
        magic, n = _HEADER.unpack_from(self._mm, 0)
        if magic != _MAGIC:
            raise IndexError_("not an FFS Explorer index (or an older format)")
        sec = {}
        for k in range(n):
            name, off, length = _SECTION.unpack_from(self._mm, _HEADER.size + k * _SECTION.size)
            sec[name.rstrip(b"\x00").decode("ascii")] = mv[off:off + length]
        for name, code in _COLUMNS:
            setattr(self, name, sec[name].cast(code))
        self.cd_sha256 = bytes(sec["cd_sha256"]).decode("ascii")
        self._names, self._origs, self._extras = sec["names"], sec["origs"], sec["extras"]
        self._table = sec["table"].cast("i")
        self.order = sec["order"].cast("I")     # namelist order: zipfile's, then captures
        self._mask = len(self._table) - 1
        self._n = len(self.header_offset)

    def __len__(self) -> int:
        return self._n

    def name(self, i: int) -> str:
        s = self.name_start[i]
        return str(self._names[s:s + self.name_len[i]], "utf-8", "surrogatepass")

    def orig_name(self, i: int) -> str:
        s = self.orig_start[i]
        pool = self._names if self.orig_in_names[i] else self._origs
        return str(pool[s:s + self.orig_len[i]], "utf-8", "surrogatepass")

    def extra(self, i: int) -> bytes:
        s = self.extra_start[i]
        return bytes(self._extras[s:s + self.extra_len[i]])

    def find(self, name: str) -> int:
        """Index of the entry *name* maps to (the last with that name), or -1."""
        nb = name.encode("utf-8", "surrogatepass")
        h = zlib.crc32(nb) & self._mask
        table, starts, lens, pool = self._table, self.name_start, self.name_len, self._names
        while True:
            i = table[h]
            if i == -1:
                return -1
            s = starts[i]
            if lens[i] == len(nb) and pool[s:s + lens[i]] == nb:
                return i
            h = (h + 1) & self._mask

    def date_time(self, i: int) -> tuple:
        t, d = self.dos_time[i], self.dos_date[i]
        return ((d >> 9) + 1980, (d >> 5) & 0xF, d & 0x1F, t >> 11, (t >> 5) & 0x3F, (t & 0x1F) * 2)

    def entry(self, i: int) -> "IndexEntry":
        return IndexEntry(self, i)

    def close(self) -> None:
        for attr in [name for name, _c in _COLUMNS] + ["_names", "_origs", "_extras", "_table", "order"]:
            v = getattr(self, attr, None)
            if isinstance(v, memoryview):
                v.release()
        self._mm.close()
        self._fh.close()


class IndexEntry:
    """One entry, read from the index on demand. Has the zipfile.ZipInfo
    attributes this app reads (filename, orig_filename, header_offset,
    file_size, compress_size, CRC, compress_type, flag_bits, extra,
    date_time, external_attr, is_dir()). filename can be reassigned, as
    zip_cd_cache's capture naming did on ZipInfo."""

    __slots__ = ("filename", "orig_filename", "header_offset", "file_size", "compress_size",
                 "CRC", "compress_type", "flag_bits", "external_attr", "_index", "_i")

    def __init__(self, index: ZipIndex, i: int):
        self._index, self._i = index, i
        self.filename = index.name(i)
        # Same string object when the original name is the final name.
        self.orig_filename = self.filename if index.orig_in_names[i] else index.orig_name(i)
        self.header_offset = index.header_offset[i]
        self.file_size = index.file_size[i]
        self.compress_size = index.compress_size[i]
        self.CRC = index.crc[i]
        self.compress_type = index.compress_type[i]
        self.flag_bits = index.flag_bits[i]
        self.external_attr = index.external_attr[i]

    @property
    def extra(self) -> bytes:
        return self._index.extra(self._i)

    @property
    def date_time(self) -> tuple:
        return self._index.date_time(self._i)

    @property
    def stored_name_len(self) -> int:
        return self._index.stored_name_len[self._i]

    def is_dir(self) -> bool:
        return self.filename.endswith("/")

    def __repr__(self) -> str:
        return f"<IndexEntry {self.filename!r} offset={self.header_offset}>"
