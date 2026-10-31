"""zip_cd_cache.py — local sidecar cache for ZIP central directory metadata.

On first open, save() seeks to the end of the network zip, locates the
central directory (CD) via the EOCD record, reads all CD bytes in a single
sequential pass, and writes them to a .zcd file in case_dir (the local
folder that also holds casedata.db — never on the network drive).

The .zcd is a minimal valid zip file: raw CD bytes followed by a rebuilt EOCD
(with cd_offset=0) so that zipfile.ZipFile(BytesIO(payload)) can parse it.

load() opens the .zcd and returns infolist().  The ZipInfo entries carry the
original header_offset values, so ZipEntry can seek directly into the network
file for data reads without ever re-reading the central directory.

save() is called on first open; load() is called immediately afterwards.
The network file is touched exactly once (the CD read during save).  All
metadata operations use the local copy.
"""

import hashlib
import io
import os
import re
import struct
import zipfile
from array import array
from collections.abc import Mapping
from bisect import bisect_right

_MAGIC = b'ZCD\x02'   # v1 (msgpack) silently invalidated by magic mismatch

_EOCD_SIG   = b'PK\x05\x06'
_EOCD64_SIG = b'PK\x06\x06'
_EOCD64_LOC = b'PK\x06\x07'

_EOCD_SIZE  = 22
_LOC64_SIZE = 20


# ── Cache path ────────────────────────────────────────────────────────────────

def cache_path(zip_path: str, case_dir: str) -> str:
    """Return the local .zcd path inside case_dir (never on the source drive)."""
    safe_name = os.path.basename(zip_path) + '.zcd'
    return os.path.join(case_dir, safe_name)


# ── Validity check ────────────────────────────────────────────────────────────

def is_valid(zip_path: str, case_dir: str) -> bool:
    """Return True if a .zcd cache exists in case_dir with the correct magic bytes."""
    try:
        with open(cache_path(zip_path, case_dir), 'rb') as f:
            return f.read(4) == _MAGIC
    except OSError:
        return False


# ── Internal: find and extract the CD from the source zip ────────────────────

def _read_chunks(fh, total: int, progress_cb=None) -> bytes:
    chunk = 1 << 23   # 8 MiB per read — larger requests amortise network latency
    buf = bytearray()
    done = 0
    while done < total:
        data = fh.read(min(chunk, total - done))
        if not data:
            break
        buf.extend(data)
        done += len(data)
        if progress_cb:
            progress_cb(done, total)
    return bytes(buf)


def _extract_cd_payload(zip_path: str, progress_cb=None) -> bytes:
    """Read the raw central directory and return .zcd payload bytes.

    The payload is: CD bytes + rebuilt end records (ZIP64 EOCD + locator +
    EOCD) with all offsets adjusted so the CD starts at byte 0.
    Raises zipfile.BadZipFile on truncated or non-seekable archives.
    """
    with open(zip_path, 'rb') as fh:
        # ── Locate EOCD ──────────────────────────────────────────────────────
        fh.seek(0, 2)
        file_size = fh.tell()
        search_size = min(file_size, 65535 + _EOCD_SIZE)
        fh.seek(file_size - search_size)
        tail = fh.read()
        pos = tail.rfind(_EOCD_SIG)
        if pos < 0:
            raise zipfile.BadZipFile("Cannot find EOCD signature")

        eocd_data   = tail[pos : pos + _EOCD_SIZE]
        eocd_offset = file_size - search_size + pos

        (_, _, _, entries_this, entries_total,
         cd_size_raw, cd_offset_raw, _) = struct.unpack_from('<4sHHHHIIH', eocd_data)

        is_zip64 = (cd_offset_raw == 0xFFFFFFFF
                    or entries_total == 0xFFFF
                    or cd_size_raw  == 0xFFFFFFFF)

        version_made = version_needed = 0
        entries_this64 = entries_this
        entries_total64 = entries_total
        cd_size   = cd_size_raw
        cd_offset = cd_offset_raw

        if is_zip64:
            # ── ZIP64 EOCD locator ────────────────────────────────────────────
            fh.seek(eocd_offset - _LOC64_SIZE)
            loc_data = fh.read(_LOC64_SIZE)
            if loc_data[:4] != _EOCD64_LOC:
                raise zipfile.BadZipFile("Cannot find ZIP64 EOCD locator")
            (_, _, zip64_eocd_offset, _) = struct.unpack_from('<4sIQI', loc_data)

            # ── ZIP64 EOCD ────────────────────────────────────────────────────
            fh.seek(zip64_eocd_offset)
            if fh.read(4) != _EOCD64_SIG:
                raise zipfile.BadZipFile("Cannot find ZIP64 EOCD")
            (zip64_rem,) = struct.unpack_from('<Q', fh.read(8))
            rest = fh.read(zip64_rem)
            (version_made, version_needed, _, _,
             entries_this64, entries_total64,
             cd_size, cd_offset) = struct.unpack_from('<HHIIQQQQ', rest)

        # ── Read CD in chunks ─────────────────────────────────────────────────
        fh.seek(cd_offset)
        cd_bytes = _read_chunks(fh, cd_size, progress_cb)
        if len(cd_bytes) != cd_size:
            raise zipfile.BadZipFile(
                f"Short read: expected {cd_size} CD bytes, got {len(cd_bytes)}")

    # ── Rebuild end records with cd_offset=0 ─────────────────────────────────
    if is_zip64:
        zip64_eocd_at = len(cd_bytes)
        new_zip64_eocd = struct.pack(
            '<4sQHHIIQQQQ',
            _EOCD64_SIG,
            44,               # remaining record size
            version_made, version_needed,
            0, 0,             # disk number, disk where CD starts
            entries_this64, entries_total64,
            cd_size,
            0,                # cd_offset = 0 in our payload
        )
        new_loc64 = struct.pack(
            '<4sIQI',
            _EOCD64_LOC,
            0,              # disk with ZIP64 EOCD
            zip64_eocd_at,  # offset of ZIP64 EOCD in payload
            1,              # total disks
        )
        new_eocd = struct.pack(
            '<4sHHHHIIH',
            _EOCD_SIG,
            0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF,
            0xFFFFFFFF, 0xFFFFFFFF,
            0,
        )
        return cd_bytes + new_zip64_eocd + new_loc64 + new_eocd
    else:
        count = min(entries_total, 0xFFFF)
        new_eocd = struct.pack(
            '<4sHHHHIIH',
            _EOCD_SIG,
            0, 0,           # disk number, disk where CD starts
            count, count,   # entries this disk, total entries
            cd_size,
            0,              # cd_offset = 0 in our payload
            0,              # comment length
        )
        return cd_bytes + new_eocd


# ── Save ──────────────────────────────────────────────────────────────────────

def save(zip_path: str, case_dir: str, progress_cb=None) -> bool:
    """Copy the raw central directory from zip_path to a .zcd file in case_dir.

    progress_cb(done_bytes, total_bytes) is called during the CD read.
    Returns True on success, False if the archive has no seekable CD.
    """
    try:
        payload = _extract_cd_payload(zip_path, progress_cb=progress_cb)
    except (zipfile.BadZipFile, OSError):
        return False

    with open(cache_path(zip_path, case_dir), 'wb') as fh:
        fh.write(_MAGIC)
        fh.write(payload)
    return True


# ── Load ──────────────────────────────────────────────────────────────────────

# In-memory memoization of load()'s own PARSED result, keyed by the real
# .zcd path (never bare zip_path -- the same archive can legitimately be
# opened into two different case_dirs in one session, each with its own
# .zcd file, and keying by zip_path alone would silently serve one case's
# parsed infolist to the other). Added 2026-09-15, found via direct user
# question ("do we not already have the data as part of the cache...")
# after profiling a real beach-ball report: the .zcd sidecar already
# avoids a NETWORK re-read of the central directory, but every call to
# load() was still re-parsing the same local bytes through
# zipfile.ZipFile() from scratch -- confirmed via cProfile to cost ~3.9s
# of pure CPU time for a real 830,298-entry archive. With 17+ independent
# call sites across this project (every keyword search, every "Interpret
# as SQL Record" click, every media thumbnail load with a fresh case_dir,
# every nested-archive extraction, ...), that cost was being paid over
# and over within a single session, not just once at load time.
# Invalidated by the real .zcd file's own (mtime, size) so a rebuilt
# cache is never served stale -- cheap to check (one os.stat) even on
# a cache hit. A memory tradeoff, not a free lunch: this keeps one
# 830k-entry ZipInfo list per distinct .zcd resident for the life of the
# process -- but every existing caller already independently builds and
# holds its own such list today (e.g. FastZipBrowser.self._zip_handle),
# so a single shared, memoized list is a wash at worst and a real
# reduction at best, not a new cost class.
#
# Each entry also keeps the {filename: ZipInfo} index zipfile itself builds
# while parsing (NameToInfo), so CachedZipView never has to rebuild one.
# Rebuilding it per view was ~0.3 s on a 700k-entry archive, paid once per
# nested archive extracted (found by SlowHens, KlasScripts/FFS-Explorer#2).
# Living here, it's invalidated together with the list it indexes.
_parsed_cache: dict[str, tuple[float, int, list, dict]] = {}


def load(zip_path: str, case_dir: str) -> list | None:
    """Return the archive's entries, or None if the cache is missing/corrupt.

    Since the file-index restructure the entries come from zip_index.py's
    mapped index (<zip>.ffsidx, built once from the .zcd, so the archive
    itself is not read again), not from parsing the .zcd with zipfile. They
    are zip_index.IndexEntry objects with the ZipInfo fields this app reads,
    identical entry for entry (names, capture names, offsets, sizes, CRC,
    flags, extra fields, dates; checked on all six test archives).

    Memoized in-memory per .zcd file for the life of the process — see
    _parsed_cache's own comment above for why this matters and how it's
    kept safe to invalidate."""
    path = cache_path(zip_path, case_dir)
    try:
        st = os.stat(path)
    except OSError:
        return None

    cached = _parsed_cache.get(path)
    if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    index = _open_index(zip_path, case_dir, path)
    if index is None:
        return None
    from zip_index import IndexEntry
    infos = [IndexEntry(index, i) for i in range(len(index))]
    by_name = _NameIndex(index, infos)
    _parsed_cache[path] = (st.st_mtime, st.st_size, infos, by_name)
    _register_layout(zip_path, infos)
    return infos


def _open_index(zip_path: str, case_dir: str, zcd_path: str):
    """The mapped index for this .zcd, built from it first when it is
    missing, older than the .zcd, or of an older format. None if the .zcd
    cannot be indexed (where zipfile would also have refused it)."""
    import zip_index
    path = zip_index.index_path(zip_path, case_dir)
    try:
        if os.path.getmtime(path) >= os.path.getmtime(zcd_path):
            return zip_index.ZipIndex(path)
    except (OSError, zip_index.IndexError_, ValueError, KeyError):
        pass
    try:
        with open(zcd_path, 'rb') as fh:
            if fh.read(4) != _MAGIC:
                return None
            payload = fh.read()
        parsed = zip_index.parse_directory(payload)
        zip_index.write(parsed, path, hashlib.sha256(payload).hexdigest())
        return zip_index.ZipIndex(path)
    except (OSError, zip_index.IndexError_, UnicodeDecodeError):
        return None


class _NameIndex(Mapping):
    """{name: entry} over the index's own name table: what zipfile's
    NameToInfo was, with the capture names (A-72), in the same order."""

    def __init__(self, index, infos):
        self._index = index
        self._infos = infos

    def __getitem__(self, name):
        i = self._index.find(name) if isinstance(name, str) else -1
        if i < 0:
            raise KeyError(name)
        return self._infos[i]

    def __contains__(self, name):
        return isinstance(name, str) and self._index.find(name) >= 0

    def __iter__(self):
        infos = self._infos
        return (infos[i].filename for i in self._index.order)

    def __len__(self):
        return len(self._index.order)


# ── Paths stored more than once (ASSUMPTIONS.md A-72) ─────────────────────────
#
# GrayKey can write the same path twice — it collects key databases in an
# early pass, then again in its full file-system walk — and the two copies
# can differ (IOS18: Mail's Envelope Index-wal changed in the 54 s between
# them). zipfile's name index keeps only the last, so the earlier copy used
# to be invisible. Every copy but the last now gets its own name, which every
# reader in the app already handles as an ordinary separate file. The marker
# goes before the extension and before a -wal/-shm/-journal suffix, so the
# file type is kept and a capture's database and sidecars still share one
# base name — the SQLite viewer's "<db path> + '-wal'" lookup pairs capture 1
# with capture 1. The last copy keeps the real name (what the app always
# showed). A path written once keeps its name even if its database was
# written twice, so parsers never lose a sidecar. Folder entries are left
# alone. orig_filename keeps the stored name for offset arithmetic.

_SIDECAR_SUFFIXES = ('-wal', '-shm', '-journal')
_CAPTURE_MARKER_RE = re.compile(r' \[capture \d+ of \d+\]')


def capture_alias(name: str, k: int, n: int) -> str:
    """'dir/sms.db-wal' -> 'dir/sms [capture 1 of 2].db-wal'."""
    suffix = next((s for s in _SIDECAR_SUFFIXES if name.endswith(s)), '')
    base = name[:len(name) - len(suffix)]
    folder, slash, leaf = base.rpartition('/')
    stem, dot, ext = leaf.rpartition('.')
    if not dot or not stem:                 # no extension, or a dotfile
        stem, ext = leaf, ''
    else:
        ext = '.' + ext
    return f"{folder}{slash}{stem} [capture {k} of {n}]{ext}{suffix}"


def is_capture_alias(name: str) -> bool:
    return bool(_CAPTURE_MARKER_RE.search(name))


def capture_marker(name: str) -> str:
    """The ' [capture k of n]' marker in *name*, or '' if it has none."""
    m = _CAPTURE_MARKER_RE.search(name)
    return m.group(0) if m else ''


def annotate_captures(ui_metadata: dict) -> None:
    """Set metadata 'capture' = "k of n" on every copy of a repeated path,
    the last (real-named) copy included. Read back from the names
    _name_repeated_captures gave the earlier copies — ZipInfo has __slots__,
    so nothing else can carry it — which also means it survives the load
    snapshot unchanged."""
    for key in [k for k in ui_metadata if is_capture_alias(k)]:
        m = _CAPTURE_MARKER_RE.search(key)
        k, n = (int(x) for x in re.findall(r'\d+', m.group(0)))
        if ui_metadata[key] is not None:
            ui_metadata[key]['capture'] = f"{k} of {n}"
        main = ui_metadata.get(key[:m.start()] + key[m.end():])
        if main is not None:
            main['capture'] = f"{n} of {n}"


def _name_repeated_captures(infos: list, by_name: dict) -> None:
    copies: dict[str, list] = {}
    for info in infos:
        if not info.filename.endswith('/'):
            copies.setdefault(info.filename, []).append(info)
    for name, group in copies.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda i: i.header_offset)
        n = len(group)
        for k, info in enumerate(group, 1):
            if k == n:
                break
            alias = capture_alias(name, k, n)
            if alias in by_name:          # a real entry already has that name
                continue
            info.filename = alias
            by_name[alias] = info
        by_name[name] = group[-1]


def _name_index(infos: list[zipfile.ZipInfo]) -> dict:
    """{filename: ZipInfo} for *infos* — the one load() already holds when
    *infos* is its memoized list (every real caller), else built fresh."""
    for cached in _parsed_cache.values():
        if cached[2] is infos:
            return cached[3]
    return {i.filename: i for i in infos}


# ── CachedZipView ─────────────────────────────────────────────────────────────

class CachedZipView:
    """zipfile.ZipFile-compatible view backed by a loaded .zcd infolist.

    Metadata operations (infolist, namelist, getinfo) never touch the network
    file.  Data reads (open) use ZipEntry's direct header_offset seek so that
    stored entries also avoid re-reading the central directory.  Compressed
    entries fall back to a full zipfile.ZipFile open (rare in FFS archives).
    """

    def __init__(self, zip_path: str, infos: list[zipfile.ZipInfo]) -> None:
        self._zip_path = zip_path
        self._infos    = infos
        self._by_name  = _name_index(infos)

    # ── ZipFile-compatible metadata interface ─────────────────────────────────

    def infolist(self) -> list[zipfile.ZipInfo]:
        return self._infos

    def namelist(self) -> list[str]:
        return list(self._by_name)

    def getinfo(self, name: str) -> zipfile.ZipInfo:
        return self._by_name[name]

    # ── Data reads ────────────────────────────────────────────────────────────

    def open(self, name_or_info) -> io.IOBase:
        """Return a readable BytesIO for the entry via ZipEntry's direct seek."""
        from zip_entry import ZipEntry
        info = (self._by_name[name_or_info]
                if isinstance(name_or_info, str) else name_or_info)
        entry = ZipEntry(self._zip_path, info.filename, info)
        return io.BytesIO(entry.read())

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


# ── Data offset resolution ────────────────────────────────────────────────────
#
# An entry's data starts at header_offset + 30 + local name length + local
# extra length — and the local extra length is NOT the central directory's.
# They differ by the ZIP64 block: any FFS zip over 4 GiB stores the real
# header offset of every entry past the 4 GiB mark in a CD ZIP64 block the
# local header doesn't have, so a single archive-wide "CD→local delta"
# (this function's old approach, probed from 5 random entries) was wrong
# for one whole group — keyword hits highlighted 12 bytes off, header scans
# read the wrong 16 bytes, exports shifted. Found by SlowHens
# (KlasScripts/FFS-Explorer#2), whose fix probed per ZIP64 group; that
# still trusted a random 5-entry sample, so a group mixing local-header
# shapes the CD can't see could still go wrong — and nondeterministically.
#
# Instead, the layout is read off the sidecar itself. Entries are stored
# back to back (sorted by header_offset), so for every entry
#     next_header_offset = data_offset + compress_size + trailer
# where data_offset = header_offset + 30 + name length + LOCAL extra length,
# and "trailer" is whatever the tool writes after the data: nothing, or a
# data descriptor (flag bit 3) the acquisition tool appends because it
# streams each file before knowing its CRC and size. Measured on real FFS
# zips (2026-10-20, ASSUMPTIONS.md A-15): Cellebrite writes a 24-byte ZIP64
# descriptor after EVERY entry; GrayKey writes a 16-byte descriptor followed
# by the file's own SHA-256 (48 bytes), or SHA-256 + SHA-1 + MD5 (84 bytes),
# on most entries. Both unknowns are learned per archive, then CHECKED
# against each other for every entry:
#   * the local extra length = the CD's own extra length + a delta that
#     depends only on the CD ZIP64 block's length (the local header lacks
#     the CD's header-offset field; Cellebrite's local header carries its
#     own ZIP64 sizes block) — one delta per ZIP64-block length;
#   * the trailer length — one per "has a data descriptor" class.
# Given the delta, the trailer an entry implies is exact; the entry is
# derived only if that equals the learned trailer for its class. Anything
# else — an unexpected local-header shape, a gap, a different descriptor
# size (a >4 GiB file's ZIP64 descriptor) — shows up as a trailer mismatch
# and is read from the entry's own local header instead (what ZipEntry does
# on every read anyway). Also read from their headers: the last entry (the
# .zcd doesn't keep the real CD start offset) and any entry whose stored
# name length can't be reproduced.
#
# Learned ONCE per archive per session from real local headers: the first
# and last entry plus an even spread of every (ZIP64-block length,
# descriptor) group, and an even spread of the whole archive. A group whose
# sample shows more than one delta, or a class with more than one trailer
# length, is never derived — slower for those entries, never wrong. The
# only way a derived offset can be wrong is two independent anomalies on
# the SAME entry cancelling exactly (an unseen header shape AND an unseen
# trailer size differing by the same number of bytes); none of 2.16 million
# real entries checked showed even one.

_SAMPLE_SPREAD = 64      # evenly spaced entries across the whole archive
_SAMPLE_PER_GROUP = 16   # evenly spaced per (ZIP64 length, descriptor) group


class _Layout:
    __slots__ = ('offsets', 'infos', 'trusted', 'delta_by_zip64', 'trailer_by_dd')

    def __init__(self, offsets, infos):
        self.offsets = offsets   # sorted array('Q') of every header_offset
        self.infos   = infos     # the full list it came from (for learning)
        self.trusted = None      # None = not yet learned, then True/False
        self.delta_by_zip64: dict[int, int] = {}   # CD ZIP64 block length -> local-CD extra delta
        self.trailer_by_dd: dict[bool, int] = {}   # has data descriptor -> bytes after the data


_layouts: dict[str, _Layout] = {}


def _layout_key(zip_path: str) -> str:
    return os.path.normcase(os.path.abspath(zip_path))


def _register_layout(zip_path: str, infos: list[zipfile.ZipInfo]) -> None:
    """Record the header-offset layout of a FULL infolist (load() only —
    a subset would make neighbouring entries look adjacent)."""
    offs = array('Q', sorted({i.header_offset for i in infos}))
    _layouts[_layout_key(zip_path)] = _Layout(offs, infos)


def _name_len(info: zipfile.ZipInfo) -> int:
    """Byte length of the name as stored: UTF-8 when general-purpose bit 11
    is set, else cp437 (what zipfile decoded it with). -1 if the stored bytes
    can't be reproduced."""
    name = getattr(info, 'orig_filename', None) or info.filename   # the stored name
    if name.isascii():
        return len(name)        # same bytes in either encoding — the common case
    enc = 'utf-8' if info.flag_bits & 0x800 else 'cp437'
    try:
        return len(name.encode(enc, errors='surrogatepass'))
    except UnicodeEncodeError:
        return -1


def _zip64_len(extra: bytes) -> int:
    """Length of the ZIP64 (0x0001) block's data in an extra field, 0 if
    absent. -1 if the extra field is malformed (never derived)."""
    off = 0
    n = len(extra)
    while off + 4 <= n:
        tag, size = struct.unpack_from('<HH', extra, off)
        if tag == 0x0001:
            return size
        off += 4 + size
    return 0 if off == n else -1


def _header_offset_read(fh, info: zipfile.ZipInfo) -> int:
    fh.seek(info.header_offset + 26)
    fname_len, extra_len = struct.unpack('<HH', fh.read(4))
    return info.header_offset + 30 + fname_len + extra_len


def _learn_layout(fh, layout: _Layout) -> bool:
    """Read real local headers for a deterministic sample and record, per
    group, the one delta / trailer length they show (see the section
    comment). Returns False when nothing usable was learned."""
    order = sorted(layout.infos, key=lambda i: i.header_offset)
    nxt = {a.header_offset: b.header_offset for a, b in zip(order, order[1:])}
    picks: dict[int, zipfile.ZipInfo] = {}

    def spread(seq, k):
        n = len(seq)
        for s in range(min(k, n)):
            info = seq[(s * (n - 1)) // max(k - 1, 1)]
            picks[info.header_offset] = info

    spread(order, _SAMPLE_SPREAD)
    groups: dict[tuple, list] = {}
    for info in order:
        groups.setdefault((_zip64_len(info.extra), bool(info.flag_bits & 0x08)), []).append(info)
    for group in groups.values():
        picks[group[0].header_offset] = group[0]
        picks[group[-1].header_offset] = group[-1]
        spread(group, _SAMPLE_PER_GROUP)

    deltas: dict[int, set] = {}
    trailers: dict[bool, set] = {}
    for info in picks.values():
        z64 = _zip64_len(info.extra)
        name_len = _name_len(info)
        if z64 < 0 or name_len < 0:
            continue
        data_off = _header_offset_read(fh, info)
        deltas.setdefault(z64, set()).add(
            data_off - info.header_offset - 30 - name_len - len(info.extra))
        if info.header_offset in nxt:
            trailers.setdefault(bool(info.flag_bits & 0x08), set()).add(
                nxt[info.header_offset] - data_off - info.compress_size)
    layout.delta_by_zip64 = {k: next(iter(v)) for k, v in deltas.items() if len(v) == 1}
    layout.trailer_by_dd = {k: next(iter(v)) for k, v in trailers.items()
                            if len(v) == 1 and next(iter(v)) >= 0}
    return bool(layout.delta_by_zip64 and layout.trailer_by_dd)


def probe_delta(zip_path: str, infos: list[zipfile.ZipInfo]) -> None:
    """Obsolete — offsets are derived from the sidecar layout now. Kept so
    existing callers (and snapshots that stored its result) keep working;
    compute_data_offsets() ignores whatever they pass back in."""
    return None


def compute_data_offsets(
    zip_path: str,
    infos: list[zipfile.ZipInfo],
    delta=None,
) -> dict[str, int]:
    """Return {filename: data_offset} for each ZipInfo in *infos*.

    Pure arithmetic over the sidecar layout for nearly every entry (see the
    section comment above); the rest are read from their own local headers.
    *delta* is accepted for backward compatibility and ignored."""
    if not infos:
        return {}

    layout = _layouts.get(_layout_key(zip_path))
    result: dict[str, int] = {}
    unresolved: list[zipfile.ZipInfo] = []
    try:
        with open(zip_path, 'rb') as fh:
            if layout is not None and layout.trusted is None:
                layout.trusted = _learn_layout(fh, layout)
                if not layout.trusted:
                    print(f"[zip_cd_cache] no consistent layout learned for {zip_path} "
                          "— reading every local header (correct, just slower)")
            if layout is not None and layout.trusted:
                # Inlined: this loop runs over every stored entry of an
                # 800k-entry archive when the keyword-search index is built.
                offs, n_offs = layout.offsets, len(layout.offsets)
                delta_by_zip64, trailer_by_dd = layout.delta_by_zip64, layout.trailer_by_dd
                for info in infos:
                    trailer = trailer_by_dd.get(bool(info.flag_bits & 0x08))
                    extra = info.extra
                    delta = delta_by_zip64.get(_zip64_len(extra) if extra else 0)
                    name = info.filename
                    stored = info.orig_filename
                    name_len = len(stored) if stored.isascii() else _name_len(info)
                    k = bisect_right(offs, info.header_offset)
                    if trailer is None or delta is None or name_len < 0 or k >= n_offs:
                        unresolved.append(info)
                        continue
                    local_extra = len(extra) + delta
                    data_off = info.header_offset + 30 + name_len + local_extra
                    if (0 <= local_extra <= 0xFFFF
                            and offs[k] - data_off - info.compress_size == trailer):
                        result[name] = data_off
                    else:
                        unresolved.append(info)
            else:
                unresolved = list(infos)
            for info in sorted(unresolved, key=lambda i: i.header_offset):
                result[info.filename] = _header_offset_read(fh, info)
    except OSError:
        pass
    return result


# ── CD hash ───────────────────────────────────────────────────────────────────

def sidecar_hash(zip_path: str, case_dir: str) -> str | None:
    """Return SHA-256 of the existing .zcd sidecar, or None if absent/unreadable."""
    zcd = cache_path(zip_path, case_dir)
    try:
        with open(zcd, 'rb') as f:
            f.read(4)   # skip magic
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def extract_cd_hash(zip_path: str) -> str | None:
    """Return SHA-256 of the CD extracted directly from zip_path (no sidecar).

    Returns None if the archive has no seekable central directory (e.g. it is
    corrupted or truncated) or if the file cannot be read.
    """
    try:
        return hashlib.sha256(_extract_cd_payload(zip_path)).hexdigest()
    except Exception:
        return None


def compute_hash(zip_path: str, case_dir: str) -> str | None:
    """Return SHA-256 of the CD payload — sidecar first, raw ZIP fallback."""
    h = sidecar_hash(zip_path, case_dir)
    return h if h is not None else extract_cd_hash(zip_path)
