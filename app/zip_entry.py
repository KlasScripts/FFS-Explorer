"""zip_entry.py — lightweight wrapper around a single zip archive entry.

ZipEntry is the object passed to every viewer.  It encapsulates the two
read strategies so callers never have to know which one applies:

* ZIP_STORED   → seek directly into the raw zip file (zero decompression).
* ZIP_DEFLATED → fall back to zipfile.ZipFile.open() (standard decompress).
  Never hit for the main FFS archive, which is always stored uncompressed.
"""

import struct
import zipfile

from zip_reader import thread_handle


class ZipEntry:
    """A handle to one file inside a zip archive."""

    __slots__ = ('zip_path', 'physical_path', '_file_size',
                 '_compress_type', '_header_offset', '_data_offset')

    def __init__(self, zip_path: str, physical_path: str,
                 zinfo: zipfile.ZipInfo) -> None:
        self.zip_path      = zip_path
        self.physical_path = physical_path
        self._file_size    = zinfo.file_size
        self._compress_type = zinfo.compress_type
        # data_offset is resolved lazily from the local header
        self._header_offset = zinfo.header_offset
        self._data_offset: int | None = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def file_size(self) -> int:
        return self._file_size

    @property
    def is_stored(self) -> bool:
        return self._compress_type == zipfile.ZIP_STORED

    # ── Offset resolution ─────────────────────────────────────────────────────

    @property
    def data_offset(self) -> int:
        """Byte offset of the raw entry data within the zip file.

        Computed once from the local file header
        (header_offset + 30 + filename_len + extra_len)."""
        if self._data_offset is None:
            fh = thread_handle(self.zip_path)
            if fh is not None:
                self._resolve_data_offset(fh)
            else:
                with open(self.zip_path, 'rb') as f:
                    self._resolve_data_offset(f)
        return self._data_offset

    def _resolve_data_offset(self, f) -> int:
        """Resolve data_offset from the local header using the already-open
        handle *f* (so a first read() costs one open, not two)."""
        if self._data_offset is None:
            f.seek(self._header_offset + 26)
            fname_len, extra_len = struct.unpack('<HH', f.read(4))
            self._data_offset = self._header_offset + 30 + fname_len + extra_len
        return self._data_offset

    # ── Reading ───────────────────────────────────────────────────────────────

    def read(self, limit: int | None = None) -> bytes:
        """Return up to *limit* bytes from the start of the entry.

        Uses a direct file seek for STORED entries; falls back to
        zipfile decompression for compressed entries."""
        size = self._file_size if limit is None else min(self._file_size, limit)
        if self.is_stored:
            # A worker thread reuses its own handle (zip_reader.thread_handle:
            # one open per thread, not per file — the cost that made bulk
            # stages slow on Windows); seeks every time, so it's safe.
            fh = thread_handle(self.zip_path)
            if fh is not None:
                fh.seek(self._resolve_data_offset(fh))
                return fh.read(size)
            with open(self.zip_path, 'rb') as f:
                f.seek(self._resolve_data_offset(f))
                return f.read(size)
        with zipfile.ZipFile(self.zip_path) as z:
            with z.open(self.physical_path) as f:
                return f.read(size) if limit is not None else f.read()

    def read_at(self, offset: int, limit: int) -> bytes:
        """Read *limit* bytes starting at *offset* within the entry data.

        Only supported for ZIP_STORED entries — raises ValueError otherwise.
        Used by the hex viewer to page in additional data on scroll."""
        if not self.is_stored:
            raise ValueError("read_at requires a ZIP_STORED entry")
        fh = thread_handle(self.zip_path)
        if fh is not None:
            fh.seek(self._resolve_data_offset(fh) + offset)
            return fh.read(limit)
        with open(self.zip_path, 'rb') as f:
            f.seek(self._resolve_data_offset(f) + offset)
            return f.read(limit)
