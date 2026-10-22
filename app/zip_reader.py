"""zip_reader.py — raw zip entry I/O primitives shared by header_scan and keyword_search."""

import os
import struct
import threading
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterator

THREAD_WORKERS: int = min(8, os.cpu_count() or 4)


# Per repacked nested archive: {entry name: (data_offset, size)} for STORED
# entries, or None when the file isn't a zip at all (a gzip source stored as
# its raw decompressed blob). Re-parsing a repacked zip's central directory
# on every single entry read made reading all N entries cost O(N²) — 54 s
# for a 4,000-entry archive vs 0.05 s reading it with one parse. These files
# are written once by nested_archive.extract_one() (always ZIP_STORED) and
# never modified, so one parse per file, invalidated by (mtime, size) in
# case one is ever re-extracted, is safe. LRU-bounded so a case with
# thousands of nested archives doesn't keep every index resident.
_NESTED_INDEX_MAX = 64
_nested_index: 'OrderedDict[str, tuple[float, int, dict | None]]' = OrderedDict()
_nested_lock = threading.Lock()


def _nested_entry_index(stored_path: str) -> dict | None:
    st = os.stat(stored_path)
    with _nested_lock:
        cached = _nested_index.get(stored_path)
        if cached is not None and cached[0] == st.st_mtime and cached[1] == st.st_size:
            _nested_index.move_to_end(stored_path)
            return cached[2]
    try:
        with zipfile.ZipFile(stored_path, 'r') as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        index = None
    else:
        index = {}
        with open(stored_path, 'rb') as fh:
            for info in sorted(infos, key=lambda i: i.header_offset):
                if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 0x1:
                    continue          # never written by extract_one; left to zipfile
                fh.seek(info.header_offset)
                header = fh.read(30)
                if len(header) < 30 or header[:4] != b'PK\x03\x04':
                    continue
                name_len, extra_len = struct.unpack_from('<HH', header, 26)
                index[info.filename] = (info.header_offset + 30 + name_len + extra_len,
                                        info.file_size)
    with _nested_lock:
        _nested_index[stored_path] = (st.st_mtime, st.st_size, index)
        _nested_index.move_to_end(stored_path)
        while len(_nested_index) > _NESTED_INDEX_MAX:
            _nested_index.popitem(last=False)
    return index


def read_nested_entry(stored_path: str, entry_path: str) -> bytes | None:
    """Read one entry's bytes from an extracted nested-archive file.

    stored_path is either a repackaged ZIP (for ZIP sources) or a raw
    decompressed blob (for gzip sources).  Tries ZIP first; if the file is
    not a ZIP at all, falls back to returning the whole blob.
    Returns None on any read error, including an entry name not in the zip.

    A STORED entry is read by direct seek from a cached per-archive index
    (see _nested_entry_index); anything else falls back to zipfile.
    """
    try:
        index = _nested_entry_index(stored_path)
        if index is None:
            with open(stored_path, 'rb') as f:
                return f.read()
        loc = index.get(entry_path)
        if loc is not None:
            data_offset, size = loc
            with open(stored_path, 'rb') as f:
                f.seek(data_offset)
                data = f.read(size)
            return data if len(data) == size else None
        with zipfile.ZipFile(stored_path, 'r') as zf:
            return zf.read(entry_path)
    except Exception:
        return None


# One open handle per (worker thread, archive), reused for every read that
# thread makes — added 2026-10-22 after a report that bulk stages were slower
# on Windows than macOS from the same kind of SSD: each read used to open the
# archive afresh (19,183 opens for IOS17's media metadata stage), and on
# Windows every open of a 30-40 GB file goes through Defender's real-time
# scanning and other file-system filters. Every read still seeks to its own
# offset first, and threads never share a handle, so results can't change.
# A worker thread's handles close when the thread ends (thread-local storage
# is released). The main (GUI) thread keeps opening per read: it only does
# occasional single reads, and a handle held open there would stop the
# archive being moved or renamed in Windows Explorer while the app runs.
_tls = threading.local()


def thread_handle(path: str):
    """An open 'rb' handle on *path* for the calling thread, or None on the
    main thread (callers then open and close their own)."""
    if threading.current_thread() is threading.main_thread():
        return None
    handles = getattr(_tls, 'handles', None)
    if handles is None:
        handles = _tls.handles = {}
    fh = handles.get(path)
    if fh is None or fh.closed:
        fh = open(path, 'rb')
        handles[path] = fh
    return fh


class ZipReader:
    """Raw byte reader for a single zip file, working directly from stored offsets.

    All reads bypass zipfile — entries in forensic FFS zips are stored uncompressed,
    so a seek + read is both correct and fastest.
    """

    def __init__(self, zip_path: str) -> None:
        self.zip_path = zip_path

    def read_at(self, offset: int, size: int, max_bytes: int | None = None) -> bytes:
        """Read up to max_bytes (or all of size) starting at offset.

        Safe to call from threads: a worker thread reuses its own handle
        (thread_handle), the main thread opens and closes one.
        """
        read_len = min(size, max_bytes) if max_bytes is not None else size
        if read_len <= 0:
            return b''
        fh = thread_handle(self.zip_path)
        if fh is not None:
            fh.seek(offset)
            return fh.read(read_len)
        with open(self.zip_path, 'rb') as fh:
            fh.seek(offset)
            return fh.read(read_len)

    def read_chunked_at(
        self,
        offset: int,
        size: int,
        chunk_size: int = 1 * 1024 * 1024,
        stop_fn: Callable[[], bool] | None = None,
    ) -> Iterator[bytes]:
        """Yield successive chunks of an entry.

        stop_fn() is checked before each chunk — return True to abort.
        Opens its own file handle so it can be called from threads.
        """
        if size <= 0:
            return
        with open(self.zip_path, 'rb') as fh:
            fh.seek(offset)
            pos = 0
            while pos < size:
                if stop_fn and stop_fn():
                    return
                read_len = min(chunk_size, size - pos)
                chunk = fh.read(read_len)
                if not chunk:
                    return
                yield chunk
                pos += read_len

    def run_parallel(
        self,
        items: list,
        work_fn: Callable,
        progress_cb: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        batch_size: int = 200,
    ):
        """Submit items to a thread pool, yield (item, result) as futures complete.

        work_fn(item) -> any
        progress_cb(done, total) called after each batch and once at the end.
        cancel_check() -> bool halts processing when True.
        """
        total = len(items)
        with ThreadPoolExecutor(max_workers=THREAD_WORKERS) as pool:
            futures = {pool.submit(work_fn, item): item for item in items}
            for done, fut in enumerate(as_completed(futures), 1):
                if cancel_check and cancel_check():
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                yield futures[fut], fut.result()
                if progress_cb and done % batch_size == 0:
                    progress_cb(done, total)
        if progress_cb:
            progress_cb(total, total)
