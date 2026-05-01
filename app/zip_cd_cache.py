"""zip_cd_cache.py — local sidecar cache for ZIP central directory metadata.

On first open, the caller saves the infolist to a .zcd msgpack file next to
the zip.  On subsequent opens, CachedZipView loads the .zcd and provides a
zipfile.ZipFile-compatible interface for metadata operations without touching
the network file at all.

For actual file data reads (plist, msgpack, hex viewer etc.), CachedZipView
uses ZipEntry's direct-seek path — seeking straight to header_offset in the
real file rather than re-reading the central directory.

Cache is automatically invalidated when the zip's file size or mtime changes.
"""

import io
import os
import struct
import zipfile

import msgpack

from zip_entry import ZipEntry

_MAGIC   = b'ZCD\x01'
_HDR_FMT = '<Qd'                      # file_size (uint64) + mtime (float64)
_HDR_SZ  = struct.calcsize(_HDR_FMT)


# ── Cache path ────────────────────────────────────────────────────────────────

def cache_path(zip_path: str, case_dir: str) -> str:
    """Return the local .zcd path inside case_dir (never on the source drive)."""
    safe_name = os.path.basename(zip_path) + '.zcd'
    return os.path.join(case_dir, safe_name)


# ── Validity check ────────────────────────────────────────────────────────────

def is_valid(zip_path: str, case_dir: str) -> bool:
    """Return True if a valid, up-to-date .zcd cache exists in case_dir."""
    cp = cache_path(zip_path, case_dir)
    try:
        zst = os.stat(zip_path)
        with open(cp, 'rb') as f:
            if f.read(4) != _MAGIC:
                return False
            size, mtime = struct.unpack(_HDR_FMT, f.read(_HDR_SZ))
            return size == zst.st_size and abs(mtime - zst.st_mtime) < 2
    except OSError:
        return False


# ── Save ──────────────────────────────────────────────────────────────────────

def save(zip_path: str, case_dir: str, infolist: list[zipfile.ZipInfo],
         progress_cb=None) -> None:
    """Serialise *infolist* to a .zcd file in case_dir (local drive).

    *progress_cb(done, total)* is called every 5 000 entries if provided.
    """
    total = len(infolist)
    rows: list = []
    for i, info in enumerate(infolist):
        rows.append((
            info.filename,
            info.file_size,
            info.compress_size,
            info.compress_type,
            info.header_offset,
            info.extra,
            list(info.date_time),
        ))
        if progress_cb and i % 5_000 == 0:
            progress_cb(i, total)

    zst    = os.stat(zip_path)
    header = _MAGIC + struct.pack(_HDR_FMT, zst.st_size, zst.st_mtime)
    with open(cache_path(zip_path, case_dir), 'wb') as fh:
        fh.write(header)
        fh.write(msgpack.packb(rows, use_bin_type=True))

    if progress_cb:
        progress_cb(total, total)


# ── Load ──────────────────────────────────────────────────────────────────────

def load(zip_path: str, case_dir: str) -> list[zipfile.ZipInfo] | None:
    """Return the cached ZipInfo list, or None if the cache is missing/stale."""
    if not is_valid(zip_path, case_dir):
        return None
    try:
        with open(cache_path(zip_path, case_dir), 'rb') as fh:
            fh.read(4 + _HDR_SZ)   # skip magic + header
            rows = msgpack.unpackb(fh.read(), raw=False)
        infos: list[zipfile.ZipInfo] = []
        for fn, fs, cs, ct, ho, ex, dt in rows:
            info               = zipfile.ZipInfo(fn)
            info.file_size     = fs
            info.compress_size = cs
            info.compress_type = ct
            info.header_offset = ho
            info.extra         = bytes(ex) if ex else b''
            info.date_time     = tuple(dt)
            infos.append(info)
        return infos
    except Exception:
        return None


# ── CachedZipView ─────────────────────────────────────────────────────────────

class CachedZipView:
    """zipfile.ZipFile-compatible view backed by a .zcd cache.

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
        """Return a readable BytesIO for the entry.

        Uses ZipEntry's direct-seek path for stored entries — no central
        directory access needed.  Falls back to zipfile for compressed entries.
        """
        info = (self._by_name[name_or_info]
                if isinstance(name_or_info, str) else name_or_info)
        entry = ZipEntry(self._zip_path, info.filename, info)
        return io.BytesIO(entry.read())

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass
