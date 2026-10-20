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
import struct
import zipfile
from array import array
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


def load(zip_path: str, case_dir: str) -> list[zipfile.ZipInfo] | None:
    """Return the cached ZipInfo list, or None if the cache is missing/corrupt.

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

    try:
        with open(path, 'rb') as fh:
            if fh.read(4) != _MAGIC:
                return None
            payload = fh.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            infos = zf.infolist()
            by_name = zf.NameToInfo
    except Exception:
        return None

    _parsed_cache[path] = (st.st_mtime, st.st_size, infos, by_name)
    _register_layout(zip_path, infos)
    return infos


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
# Instead, the layout is read off the sidecar itself: entries are stored
# back to back, so an entry's data ENDS where the next entry's local header
# begins (sorted by header_offset), and therefore STARTS at
#     next_header_offset - compress_size
# — exact per entry, deterministic, zero archive I/O. Not derivable, so
# resolved by reading the entry's own local header instead (exactly what
# ZipEntry does on every read anyway):
#   * the last entry (the .zcd doesn't keep the real CD start offset),
#   * entries with a data descriptor (flag bit 3 — trailer size varies),
#   * any result implying an impossible local extra length (a gap between
#     entries, or a hand-built CD),
#   * any entry whose stored name length can't be reproduced.
# The derived layout is spot-checked ONCE per archive per session against
# real local headers (deterministic sample, every ZIP64 group covered); a
# single mismatch disables it for that archive and everything is read from
# local headers — slower, never wrong. The check also records which
# CD→local extra deltas genuinely occur; a derived offset implying any OTHER
# delta (a gap after that entry, or a local-header shape the sample never
# hit) is read from its header rather than trusted.

_VERIFY_SPREAD = 8   # evenly spaced entries checked, plus first/last of each group


class _Layout:
    __slots__ = ('offsets', 'infos', 'trusted', 'deltas')

    def __init__(self, offsets, infos):
        self.offsets = offsets   # sorted array('Q') of every header_offset
        self.infos   = infos     # the full list it came from (for verification)
        self.trusted = None      # None = not yet verified, then True/False
        self.deltas: set[int] = set()   # CD→local extra deltas confirmed real


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
    name = info.filename
    if name.isascii():
        return len(name)        # same bytes in either encoding — the common case
    enc = 'utf-8' if info.flag_bits & 0x800 else 'cp437'
    try:
        return len(name.encode(enc, errors='surrogatepass'))
    except UnicodeEncodeError:
        return -1


def _zip64_len(extra: bytes) -> int:
    off = 0
    while off + 4 <= len(extra):
        tag, size = struct.unpack_from('<HH', extra, off)
        if tag == 0x0001:
            return size
        off += 4 + size
    return 0


def _derived_offset(layout: _Layout, info: zipfile.ZipInfo,
                    require_known_delta: bool = True) -> int | None:
    """Data offset from the sidecar layout alone, or None if not derivable
    (or, once verified, if it implies a delta no real header showed)."""
    if info.flag_bits & 0x08:
        return None
    name_len = _name_len(info)
    if name_len < 0:
        return None
    k = bisect_right(layout.offsets, info.header_offset)
    if k >= len(layout.offsets):
        return None                              # last entry
    data_off = layout.offsets[k] - info.compress_size
    local_extra = data_off - info.header_offset - 30 - name_len
    if not 0 <= local_extra <= 0xFFFF:
        return None
    if require_known_delta and local_extra - len(info.extra) not in layout.deltas:
        return None
    return data_off


def _header_offset_read(fh, info: zipfile.ZipInfo) -> int:
    fh.seek(info.header_offset + 26)
    fname_len, extra_len = struct.unpack('<HH', fh.read(4))
    return info.header_offset + 30 + fname_len + extra_len


def _verify_layout(fh, layout: _Layout) -> bool:
    """Compare derived offsets with real local headers for a deterministic
    sample: evenly spread entries plus the first and last of every CD ZIP64
    group (the group boundary is exactly where the old bug lived)."""
    order = sorted(layout.infos, key=lambda i: i.header_offset)
    picks: dict[int, zipfile.ZipInfo] = {}
    n = len(order)
    for s in range(_VERIFY_SPREAD):
        info = order[(s * (n - 1)) // max(_VERIFY_SPREAD - 1, 1)]
        picks[info.header_offset] = info
    seen: dict[int, list] = {}
    for info in order:
        seen.setdefault(_zip64_len(info.extra), []).append(info)
    for group in seen.values():
        picks[group[0].header_offset] = group[0]
        picks[group[-1].header_offset] = group[-1]
    for info in picks.values():
        derived = _derived_offset(layout, info, require_known_delta=False)
        if derived is None:
            continue
        if derived != _header_offset_read(fh, info):
            return False
        layout.deltas.add(derived - info.header_offset - 30
                          - _name_len(info) - len(info.extra))
    return bool(layout.deltas)


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
                layout.trusted = _verify_layout(fh, layout)
                if not layout.trusted:
                    print(f"[zip_cd_cache] sidecar layout of {zip_path} did not "
                          "match its local headers — reading headers per entry")
            if layout is not None and layout.trusted:
                # _derived_offset() inlined: this loop runs over every stored
                # entry of an 800k-entry archive at load and on every export.
                offs, n_offs, deltas = layout.offsets, len(layout.offsets), layout.deltas
                for info in infos:
                    if info.flag_bits & 0x08:
                        unresolved.append(info)
                        continue
                    name = info.filename
                    name_len = len(name) if name.isascii() else _name_len(info)
                    k = bisect_right(offs, info.header_offset)
                    if name_len < 0 or k >= n_offs:
                        unresolved.append(info)
                        continue
                    data_off = offs[k] - info.compress_size
                    local_extra = data_off - info.header_offset - 30 - name_len
                    if 0 <= local_extra <= 0xFFFF and local_extra - len(info.extra) in deltas:
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
