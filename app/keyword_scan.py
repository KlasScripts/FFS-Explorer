"""keyword_scan.py — the byte matching behind Keyword Search, run in worker
processes. Qt-free.

Why processes (2026-11-06): matching (bytes.lower + one find per encoding)
holds Python's GIL, so the search's four threads matched on one core
between them (~560 MB/s on the test PC, against ~2,100 MB/s the archive
on Z: reads at), and the GUI thread, which needs the GIL back after every
Qt call, froze for seconds at a time while a common word's hits arrived.
A worker process reads its own files straight from the archive by offset
(stored entries, so no decompression) and returns only the hits.

A hit never crosses from one archive file into the next: each file is
searched on its own. Within a file larger than one read (CHUNK), the last
pattern-length-minus-one bytes of each read are carried into the next, so
a match split across two reads is still found, once.
"""

from __future__ import annotations

CHUNK = 4 * 1024 * 1024
CTX_BYTES = 40          # bytes either side of a hit, for the context column
JOB_BYTES = 32 * 1024 * 1024
JOB_FILES = 2000


def make_patterns(keyword: str) -> list:
    """[(lowercase pattern bytes, length, encoding), ...] for a keyword."""
    patterns = []
    for enc in ('utf-8', 'utf-16-le', 'utf-16-be'):
        pat = keyword.encode(enc, errors='replace')
        patterns.append((pat.lower(), len(pat), enc))
    return patterns


def iter_pattern_hits(data_lower: bytes, patterns: list):
    """Yield (idx, pat_len, enc) for every pattern occurrence in data_lower."""
    for pat, pat_len, enc in patterns:
        idx = 0
        while True:
            idx = data_lower.find(pat, idx)
            if idx == -1:
                break
            yield idx, pat_len, enc
            idx += pat_len


def _decode_ctx(data: bytes, enc: str) -> str:
    if enc == 'utf-8':
        return data.decode('utf-8', errors='replace')
    if len(data) % 2:
        data = data[1:]
    return data.decode(enc, errors='replace')


def search_blocks(blocks, patterns: list, ctx_bytes: int = CTX_BYTES) -> list:
    """Search one file given as successive byte blocks; returns
    [(offset in file, context), ...] in the order found."""
    overlap = max(pat_len for _, pat_len, _ in patterns) - 1
    results = []
    leftover = b''
    base = 0
    for raw in blocks:
        block = leftover + raw if leftover else raw
        block_lower = block.lower()
        for idx, pat_len, enc in iter_pattern_hits(block_lower, patterns):
            # A match lying wholly inside the carried-over bytes was
            # already found in the previous block.
            if leftover and idx + pat_len <= len(leftover):
                continue
            cs = max(0, idx - ctx_bytes)
            ce = min(len(block), idx + pat_len + ctx_bytes)
            hit_text = block[idx:idx + pat_len].decode(enc, errors='replace')
            results.append((base + idx,
                            _decode_ctx(block[cs:idx], enc) + f'[{hit_text}]'
                            + _decode_ctx(block[idx + pat_len:ce], enc)))
        # (a block shorter than the overlap is carried whole)
        leftover = block[-overlap:] if overlap > 0 else b''
        base += len(block) - len(leftover)
    return results


def make_jobs(entries: list, job_bytes: int = JOB_BYTES, job_files: int = JOB_FILES) -> list:
    """Group (name, data_offset, size) entries into jobs of about
    *job_bytes* / *job_files*, in archive order so each job reads forward."""
    jobs, cur, cur_bytes = [], [], 0
    for e in sorted(entries, key=lambda e: e[1]):
        if cur and (cur_bytes + e[2] > job_bytes or len(cur) >= job_files):
            jobs.append(cur)
            cur, cur_bytes = [], 0
        cur.append(e)
        cur_bytes += e[2]
    if cur:
        jobs.append(cur)
    return jobs


def search_job(zip_path: str, entries: list, patterns: list,
               ctx_bytes: int = CTX_BYTES, chunk: int = CHUNK) -> tuple[list, int]:
    """Worker-process entry point: search each (name, data_offset, size)
    entry of the archive. Returns ([(name, hits), ...] for files with
    hits, number of files that could not be read)."""
    out, unreadable = [], 0
    with open(zip_path, 'rb', buffering=0) as f:
        for name, offset, size in entries:
            def blocks(offset=offset, size=size):
                pos = 0
                while pos < size:
                    f.seek(offset + pos)
                    data = f.read(min(chunk, size - pos))
                    if not data:
                        return
                    yield data
                    pos += len(data)
            try:
                hits = search_blocks(blocks(), patterns, ctx_bytes)
            except OSError:
                unreadable += 1
                continue
            if hits:
                out.append((name, hits))
    return out, unreadable
