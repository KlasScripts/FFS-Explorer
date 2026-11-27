"""Worker-process side of the LevelDB/IndexedDB search index (Process Case,
LevelDbIndexWorker in ffs-explorer.py, F-167). The decode is pure Python
(ccl_leveldb, ccl_chromium_indexeddb), so on the worker thread it ran on
one core: Android 15 CTF25, 384 stores, 7.9 s on one thread, 4.3 s on 8
processes, rows identical store by store (2026-11-26).

The worker thread copies each store's files out of the archive first (on
its threads, as before); a job sent here has nothing left to fetch, so a
worker process never reads the archive. The decode itself is
leveldb_viewer.run_leveldb_search_index, unchanged: this module is kept
apart so that the decode logic's version hash
(leveldb_viewer.leveldb_decode_logic_version) is not changed by how the
work is spread. Qt-free."""

_lowered = False


def index_job(job: dict) -> tuple:
    """('ok', rows) or ('error', reason) for one already-extracted job.
    Errors come back as values, so a failure of the process itself (which
    raises in the caller) is told apart and the job read again there."""
    global _lowered
    if not _lowered:
        import child_priority
        child_priority.lower_this_process()
        _lowered = True
    from leveldb_viewer import run_leveldb_search_index, LevelDbIndexError
    try:
        return 'ok', run_leveldb_search_index(job)
    except LevelDbIndexError as exc:
        return 'error', str(exc)
    except Exception as exc:
        return 'error', f"{type(exc).__name__}: {exc}"


def job_bytes(job: dict) -> int:
    """About how many bytes a job decodes: its files still to fetch plus
    those already in its extract folders. Used to start the biggest first,
    so one large store (a Spotify cache: 3.6 of the 7.9 s above) runs
    alongside the rest instead of after them."""
    import os
    total = sum(getattr(entry, 'file_size', 0) or 0 for entry, _dest in job.get('to_fetch') or [])
    fetching = {dest for _entry, dest in job.get('to_fetch') or []}
    for root in (job.get('extract_dir'), job.get('blob_extract_dir')):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                if path in fetching:
                    continue
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
    return total
