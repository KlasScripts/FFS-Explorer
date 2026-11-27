"""A cancelled run ends its worker processes (exit code -SIGTERM) in
media metadata (scan_all), keyword search (KeywordSearchWorker._search)
and the embedded-media sweep (EmbeddedMediaScanWorker._run_jobs). Each
takes the pool's process list before pool.shutdown, which sets
pool._processes to None: read after it, as keyword search and the sweep
did before 2026-11-27, there was nothing to end, and the workers of a run
cancelled just before the app exited stayed alive. Media metadata did not
end them at all. The same fix and test as the LevelDB index
(test_leveldb_index_pool.test_cancel_ends_the_worker_processes)."""
import concurrent.futures
import importlib.util
import os
import signal
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def seen(monkeypatch):
    """Replace ProcessPoolExecutor (each pool imports it when it starts)
    with one that records its worker processes as shutdown is called."""
    got = {'procs': []}

    class Recording(concurrent.futures.ProcessPoolExecutor):
        def shutdown(self, *a, **k):
            got['procs'] += list((self._processes or {}).values())
            return super().shutdown(*a, **k)
    monkeypatch.setattr(concurrent.futures, 'ProcessPoolExecutor', Recording)
    return got


def _assert_ended(procs):
    assert procs, 'no worker processes were started'
    deadline = time.monotonic() + 10
    while any(p.is_alive() for p in procs) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(p.is_alive() for p in procs)
    assert all(p.exitcode == -signal.SIGTERM for p in procs)


def test_media_metadata_cancel_ends_the_workers(seen, tmp_path, monkeypatch):
    import media_metadata_scan as mms
    monkeypatch.setattr(mms, 'METADATA_PROCESSES', 2)
    monkeypatch.setattr(mms, 'PROCESS_MIN_FILES', 1)
    monkeypatch.setattr(mms, 'JOB_FILES', 1)
    local = [(f'x/{i}.jpg', str(tmp_path / f'{i}.jpg'), 'jpg') for i in range(20)]
    calls = {'n': 0}

    def cancel():
        calls['n'] += 1
        return calls['n'] > 1
    mms.scan_all(str(tmp_path / 'none.zip'), [], local, cancel_check=cancel)
    _assert_ended(seen['procs'])


@pytest.fixture(scope='module')
def main():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    spec = importlib.util.spec_from_file_location('ffs_explorer_main', os.path.join(ROOT, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_keyword_search_cancel_ends_the_workers(seen, tmp_path, monkeypatch):
    import keyword_search as ks
    monkeypatch.setattr(ks, 'SEARCH_PROCESSES', 2)
    stop = threading.Event()
    stop.set()                                # cancelled at the first check
    fake = type('W', (), {
        'zip_path': str(tmp_path / 'none.zip'), '_CTX_BYTES': 64, '_b64': None,
        '_phone': None, '_stop': stop, '_report_png_notes': lambda self: None,
        'status_update': type('S', (), {'emit': lambda self, t: None})(),
    })()
    entries = [(f'f{i}', i * 10_000_000, 10_000_000) for i in range(6)]
    list(ks.KeywordSearchWorker._search(fake, entries, [b'needle']))
    _assert_ended(seen['procs'])


def test_embedded_media_cancel_ends_the_workers(main, seen, tmp_path, monkeypatch):
    monkeypatch.setattr(main, '_EMBEDDED_MEDIA_PROCESSES', 2)
    fake = type('W', (), {'isInterruptionRequested': lambda self: True})()
    jobs = [('sqlite', f'x/{i}.db', None, None, None, None, None, None) for i in range(6)]
    list(main.EmbeddedMediaScanWorker._run_jobs(fake, jobs))
    _assert_ended(seen['procs'])
