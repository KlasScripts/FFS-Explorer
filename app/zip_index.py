"""zip_index.py — the archive's file directory as one compact, memory-mapped index.

Replaces parsing the .zcd sidecar through Python's zipfile, which built a
ZipInfo object per entry (about 767 bytes each: 517 MB and 1.3 s for the
674,000 entries of the CTF23 test archive) every time a case was opened.
Here the central directory is parsed once, by this module, into columns of
plain numbers plus pools of names and extra fields, written to one file in
the case folder (<zip name>.ffsidx), and mapped back on every later open:
nothing is parsed again, and the operating system loads only the pages used.

Entries read the same as zipfile reads them, field by field, with two
deliberate differences (ASSUMPTIONS.md A-80). Per CPython's zipfile (3.14):
  * a name is UTF-8 when general-purpose flag bit 11 is set, else cp437;
  * a name is cut at its first NUL byte;
  * a Unicode Path extra field (0x7075, version 1) whose CRC matches the
    stored name's bytes replaces the name;
  * the ZIP64 extra field (0x0001) supplies file size (when it is
    0xFFFFFFFF or 0xFFFFFFFFFFFFFFFF), compressed size and header offset
    (when 0xFFFFFFFF), in that order;
  * date_time comes from the DOS date and time words.
The differences:
  * Without flag bit 11, a name whose bytes are valid UTF-8 is read as
    UTF-8, not cp437. GrayKey writes UTF-8 names without the flag (IOS18
    CTF25: all 58 non-ASCII names), which cp437 garbled ("1170-├ù-2436"
    for "1170-×-2436").
  * zipfile on Windows also turns every backslash in a name into "/". This
    index does not, on any platform. FFS archives always separate folders
    with "/", and a backslash is part of a real name (iOS Mail stores Gmail
    folders as "[Gmail]/\\Sent.imapmbox"), so the Windows rule turned real
    names into wrong paths.
Each name these change is recorded with the name older versions gave it
(renames()), so case_names.py can update a case made by an older version.

Paths stored more than once (ASSUMPTIONS.md A-72) get the same names
zip_cd_cache._name_repeated_captures gave them: every copy but the last
becomes "<name> [capture k of n]", unless a real entry already has that name.

Qt-free. zip_cd_cache.load() returns this index's entries, so every caller
of the sidecar sees the same names and fields as before.
"""

from __future__ import annotations

import json
import mmap
import os
import struct
import zlib
from array import array

_MAGIC = b"FFSIDX\x00\x03"   # 3: name rule A-80 and the renames section
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


def _decode(raw: bytes, flags: int) -> str:
    """A stored name as text (ASSUMPTIONS.md A-80): UTF-8 when flag bit 11
    is set; without the flag, UTF-8 when the bytes are valid UTF-8 (GrayKey
    writes UTF-8 names without the flag), else cp437."""
    if flags & 0x800 or raw.isascii():
        return raw.decode("utf-8")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp437")


def _old_names(raw: bytes, flags: int, unicode_path: str | None) -> tuple[str, str]:
    """The name older versions of this app gave an entry (zipfile's own
    reading): (on macOS/Linux, on Windows). The Windows one also had every
    backslash turned into "/"."""
    if unicode_path:
        posix = _sanitize(unicode_path)
    else:
        posix = _sanitize(raw.decode("utf-8" if flags & 0x800 else "cp437"))
    return posix, posix.replace("\\", "/")


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
    old: dict[int, tuple[str, str]] = {}      # entry -> its names in older versions, when different
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
        orig = _decode(raw_name, flags)
        name = _sanitize(orig)
        unicode_path = None
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
                        unicode_path = up
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
        if "\\" in name or (not flags & 0x800 and not raw_name.isascii()):
            olds = _old_names(raw_name, flags, unicode_path)
            if olds != (name, name):
                old[len(final_names)] = olds
        final_names.append(name)
        pos = p + elen + clen
    # zipfile's NameToInfo (last entry with a name wins, first-seen order),
    # then the capture renaming, exactly as zip_cd_cache did on ZipInfo.
    base_names = list(final_names) if old else None
    by_name: dict[str, int] = {}
    for i, name in enumerate(final_names):
        by_name[name] = i
    _name_repeated_captures(final_names, cols["header_offset"], by_name)
    renames = _renames(base_names, old, final_names, by_name, cols["header_offset"]) if old else []
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
            "table": _hash_table(by_name), "order": array("I", by_name.values()),
            "renames": renames}


def _renames(base_names: list[str], old: dict, final_names: list[str], by_name: dict,
             header_offsets) -> list[tuple[str, str]]:
    """[(name in older versions, name now)] for every entry whose name the
    A-80 rule changed, once for the macOS/Linux reading and once for the
    Windows one, plus each folder above it whose name changed (a folder
    with no entry of its own is still a path in the case data). Pairs
    are dropped when the old name is ambiguous: it is also a name some
    entry has now, or it stood for two different names."""
    found: dict[str, set] = {}
    for variant in (0, 1):
        names = list(base_names)
        for i, pair in old.items():
            names[i] = pair[variant]
        names_by = {}
        for i, name in enumerate(names):
            names_by[name] = i
        _name_repeated_captures(names, header_offsets, names_by)
        for i, (was, now) in enumerate(zip(names, final_names)):
            if was != now:
                for a, b in _with_folders(was.rstrip("/"), now.rstrip("/")):
                    found.setdefault(a, set()).add(b)
    current = set(by_name)
    current |= {n.rstrip("/") for n in by_name}
    return sorted((a, next(iter(b))) for a, b in found.items()
                  if len(b) == 1 and a not in current)


def _with_folders(was: str, now: str) -> list[tuple[str, str]]:
    """(was, now) and the changed folders above them, paired from the
    deepest level up. A folder only in the old name (a backslash read as a
    folder break) has no pair and is left out."""
    k = 0
    while k < min(len(was), len(now)) and was[k] == now[k]:
        k += 1
    start = was.rfind("/", 0, k) + 1          # first changed level
    pairs = [(was, now)]
    w_dirs = [was[:j] for j in range(start, len(was)) if was[j] == "/" and j > start]
    n_dirs = [now[:j] for j in range(start, len(now)) if now[j] == "/" and j > start]
    for a, b in zip(reversed(w_dirs), reversed(n_dirs)):
        if a != b:
            pairs.append((a, b))
    return pairs


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
                ("table", parsed["table"].tobytes()), ("order", parsed["order"].tobytes()),
                ("renames", json.dumps(parsed["renames"], ensure_ascii=False).encode("utf-8"))]
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
        self._renames = bytes(sec["renames"])
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

    def renames(self) -> list[tuple[str, str]]:
        """[(name in older versions of this app, name now)] (A-80)."""
        return [tuple(p) for p in json.loads(self._renames.decode("utf-8"))]

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
