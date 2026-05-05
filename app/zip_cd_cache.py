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

import io
import os
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
    chunk = 1 << 20   # 1 MiB per read
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

def load(zip_path: str, case_dir: str) -> list[zipfile.ZipInfo] | None:
    """Return the cached ZipInfo list, or None if the cache is missing/corrupt."""
    if not is_valid(zip_path, case_dir):
        return None
    try:
        with open(cache_path(zip_path, case_dir), 'rb') as fh:
            fh.read(4)       # skip magic
            payload = fh.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            return zf.infolist()
    except Exception:
        return None


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
        self._by_name  = {i.filename: i for i in infos}

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
