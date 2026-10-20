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
import random
import struct
import zipfile

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
_parsed_cache: dict[str, tuple[float, int, list]] = {}


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
    except Exception:
        return None

    _parsed_cache[path] = (st.st_mtime, st.st_size, infos)
    return infos


# ── CachedZipView ─────────────────────────────────────────────────────────────

# {filename: ZipInfo} index per infolist, keyed by the list's identity.
# load() hands every caller the SAME memoized list, but each CachedZipView
# used to rebuild this dict from scratch — ~128 ms for a 708k-entry archive,
# paid once per nested archive (93% of the nested-archive stage's time on a
# real iOS 16 case). The entry holds a strong reference to the list, so an
# id() can't be reused while its entry exists. Bounded: an ad-hoc list
# passed by a caller only ever displaces older entries.
_name_index_cache: list[tuple[list, dict]] = []
_NAME_INDEX_MAX = 4


def _name_index(infos: list[zipfile.ZipInfo]) -> dict:
    for cached_infos, index in _name_index_cache:
        if cached_infos is infos:
            return index
    index = {i.filename: i for i in infos}
    _name_index_cache.insert(0, (infos, index))
    del _name_index_cache[_NAME_INDEX_MAX:]
    return index


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
# extra length. The local extra length is NOT the central-directory one: in
# every real FFS archive checked (8 archives, 1.9M entries — Cellebrite iOS
# and Android, GrayKey iOS and Android) the only difference is the ZIP64
# block (tag 0x0001), which the CD carries only for entries that need it
# (header_offset past 4 GiB, or always) while the local header carries its
# own fixed-size one or none. So the CD→local gap ("delta") is constant
# within a group of entries sharing the same CD ZIP64 block length, but it
# differs BETWEEN groups (e.g. 0 vs −12, or +20 vs +8 on Cellebrite iOS).
# A single archive-wide delta was therefore wrong for one whole group —
# keyword-search hits highlighted 12 bytes away from the real match, header
# scans read the wrong 16 bytes, exports were shifted. Deltas are now
# probed and applied per group; a group whose sample disagrees falls back
# to reading each of its entries' local headers.

_PROBE_N = 5   # entries sampled per ZIP64 group


def _zip64_len(extra: bytes) -> int:
    """Length of the ZIP64 (0x0001) block in *extra*, 0 if absent."""
    off = 0
    while off + 4 <= len(extra):
        tag, size = struct.unpack_from('<HH', extra, off)
        if tag == 0x0001:
            return size
        off += 4 + size
    return 0


def _name_len(info: zipfile.ZipInfo) -> int:
    """Byte length of the entry's name as stored in the archive — UTF-8 when
    general-purpose flag bit 11 is set, otherwise cp437 (what zipfile decoded
    it with)."""
    enc = 'utf-8' if info.flag_bits & 0x800 else 'cp437'
    try:
        return len(info.filename.encode(enc, errors='surrogatepass'))
    except UnicodeEncodeError:
        return -1   # can't reproduce the stored bytes -> never matches a probe


def _group_by_zip64(infos: list[zipfile.ZipInfo]) -> dict[int, list[zipfile.ZipInfo]]:
    groups: dict[int, list[zipfile.ZipInfo]] = {}
    for info in infos:
        groups.setdefault(_zip64_len(info.extra), []).append(info)
    return groups


def _probe_group(fh, group: list[zipfile.ZipInfo]) -> int | None:
    """The CD→local extra delta shared by a sample of *group*, or None if the
    sample disagrees or a stored name length can't be reproduced."""
    delta: int | None = None
    for info in random.sample(group, min(_PROBE_N, len(group))):
        fh.seek(info.header_offset + 26)
        local_fname_len, local_extra_len = struct.unpack('<HH', fh.read(4))
        if local_fname_len != _name_len(info):
            return None
        d = local_extra_len - len(info.extra)
        if delta is None:
            delta = d
        elif d != delta:
            return None
    return delta


def probe_delta(zip_path: str, infos: list[zipfile.ZipInfo]) -> dict[int, int] | None:
    """Return {CD ZIP64 block length: delta} for every group in *infos*, or
    None if any group's sample disagrees (callers then fall back to per-entry
    seeks for that group). Call once at archive-open time and pass the result
    to compute_data_offsets()."""
    if not infos:
        return None
    deltas: dict[int, int] = {}
    try:
        with open(zip_path, 'rb') as fh:
            for key, group in _group_by_zip64(infos).items():
                d = _probe_group(fh, group)
                if d is None:
                    return None
                deltas[key] = d
    except OSError:
        return None
    return deltas


def compute_data_offsets(
    zip_path: str,
    infos: list[zipfile.ZipInfo],
    delta: dict[int, int] | None = None,
) -> dict[str, int]:
    """Return {filename: data_offset} for each ZipInfo in *infos*.

    *delta* is probe_delta()'s per-group map. Any group it doesn't cover —
    including every group when *delta* is a legacy single int saved by an
    older version, which was unreliable — is probed here; a group whose
    probe disagrees is resolved by reading each entry's local header.
    """
    if not infos:
        return {}
    known = delta if isinstance(delta, dict) else {}

    result: dict[str, int] = {}
    try:
        with open(zip_path, 'rb') as fh:
            for key, group in _group_by_zip64(infos).items():
                d = known.get(key)
                if d is None:
                    d = _probe_group(fh, group)
                unresolved = group
                if d is not None:
                    unresolved = []
                    for info in group:
                        name_len = _name_len(info)
                        if name_len < 0:
                            unresolved.append(info)
                            continue
                        result[info.filename] = (
                            info.header_offset + 30 + name_len + len(info.extra) + d)
                for info in sorted(unresolved, key=lambda i: i.header_offset):
                    fh.seek(info.header_offset + 26)
                    fname_len, extra_len = struct.unpack('<HH', fh.read(4))
                    result[info.filename] = info.header_offset + 30 + fname_len + extra_len
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
