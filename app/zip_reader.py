"""zip_reader.py — raw zip entry I/O primitives shared by header_scan and keyword_search."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterator

THREAD_WORKERS: int = min(8, os.cpu_count() or 4)


class ZipReader:
    """Raw byte reader for a single zip file, working directly from stored offsets.

    All reads bypass zipfile — entries in forensic FFS zips are stored uncompressed,
    so a seek + read is both correct and fastest.
    """

    def __init__(self, zip_path: str) -> None:
        self.zip_path = zip_path

    def read_at(self, offset: int, size: int, max_bytes: int | None = None) -> bytes:
        """Read up to max_bytes (or all of size) starting at offset.

        Opens and closes its own file handle — safe to call from threads.
        """
        read_len = min(size, max_bytes) if max_bytes is not None else size
        if read_len <= 0:
            return b''
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
