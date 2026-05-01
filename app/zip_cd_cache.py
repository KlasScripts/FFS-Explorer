"""zip_cd_cache.py — local sidecar cache for ZIP central directory metadata.

On first open, the caller saves the infolist to a .zcd msgpack file next to
the zip.  On subsequent opens, CachedZipView loads the .zcd and provides a
zipfile.ZipFile-compatible interface for metadata operations without touching
the network file at all.

For actual file data reads (plist, msgpack, hex viewer etc.), CachedZipView
uses ZipEntry's direct-seek path — seeking straight to header_offset in the
real file rather than re-reading the central directory.

These archives are forensic evidence and are never modified, so no
mtime/size validity check is performed — presence of the .zcd is sufficient.
"""

import io
import os
import zipfile

import msgpack

from zip_entry import ZipEntry

_MAGIC = b'ZCD\x01'


# ── Cache path ────────────────────────────────────────────────────────────────

def cache_path(zip_path: str, case_dir: str) -> str:
    """Return the local .zcd path inside case_dir (never on the source drive)."""
    safe_name = os.path.basename(zip_path) + '.zcd'
    return os.path.join(case_dir, safe_name)


# ── Validity check ────────────────────────────────────────────────────────────

def is_valid(zip_path: str, case_dir: str) -> bool:
    """Return True if a .zcd cache exists in case_dir with the correct magic."""
    try:
        with open(cache_path(zip_path, case_dir), 'rb') as f:
            return f.read(4) == _MAGIC
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

    with open(cache_path(zip_path, case_dir), 'wb') as fh:
        fh.write(_MAGIC)
        fh.write(msgpack.packb(rows, use_bin_type=True))

    if progress_cb:
        progress_cb(total, total)


# ── Load ──────────────────────────────────────────────────────────────────────

def load(zip_path: str, case_dir: str) -> list[zipfile.ZipInfo] | None:
    """Return the cached ZipInfo list, or None if the cache is missing."""
    if not is_valid(zip_path, case_dir):
        return None
    try:
        with open(cache_path(zip_path, case_dir), 'rb') as fh:
            fh.read(4)   # skip magic
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
