"""LevelDB search index on worker processes (F-167, leveldb_index_job and
LevelDbIndexWorker._run_on_pool): the same rows and the same failed
folders as decoding on the worker thread, the biggest folder first, a job
whose worker process failed decoded on the thread instead of dropped, and
a cancelled run stopping cleanly. The stores are real LevelDB log files
(one batch each), written byte by byte to the format ccl_leveldb reads."""
import importlib.util
import os
import sqlite3
import struct
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool

import pytest

import leveldb_index_job

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope='module')
def main():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    spec = importlib.util.spec_from_file_location('ffs_explorer_main', os.path.join(ROOT, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _write_store(folder, records, seq=1):
    """One LevelDB log file (000003.log) holding *records* [(key, value)]
    as one FULL record in one block (the CRC is not checked by the reader)."""
    os.makedirs(folder, exist_ok=True)
    batch = struct.pack('<QI', seq, len(records))
    for k, v in records:
        batch += b'\x01' + _varint(len(k)) + k + _varint(len(v)) + v
    assert len(batch) < 32768 - 7
    with open(os.path.join(folder, '000003.log'), 'wb') as f:
        f.write(struct.pack('<IHB', 0, len(batch), 1) + batch)


def _jobs(tmp_path):
    jobs = []
    for i in range(10):
        folder = tmp_path / 'leveldb_browse' / f'store{i}'
        recs = [(f'key{j}'.encode(), f'{{"store": {i}, "n": {j}, "text": "hello {i}-{j}"}}'.encode())
                for j in range(5 + 40 * (i % 3))]
        recs.append((b'binary', b'\x08\x96\x01'))   # 'bin': not indexed
        _write_store(str(folder), recs)
        jobs.append({'ui_path': f'data/data/app{i}/app_webview/Local Storage/leveldb',
                     'extract_dir': str(folder), 'blob_extract_dir': None,
                     'to_fetch': [], 'errors': []})
    bad = tmp_path / 'leveldb_browse' / 'bad'
    bad.mkdir(exist_ok=True)
    (bad / 'CURRENT').write_bytes(b'MANIFEST-000001\n')
    (bad / '000003.log').write_bytes(b'\xff' * 64)
    (bad / '000004.ldb').write_bytes(b'not a table')
    jobs.append({'ui_path': 'data/data/bad/leveldb', 'extract_dir': str(bad),
                 'blob_extract_dir': None, 'to_fetch': [], 'errors': []})
    jobs.append({'ui_path': 'data/data/missing/leveldb', 'extract_dir': str(bad),
                 'blob_extract_dir': None, 'to_fetch': [],
                 'errors': ['data/data/missing/leveldb/000003.log: not found in the archive']})
    return jobs


def _run(main, tmp_path, case, processes, monkeypatch, jobs=None, cancel_check=None):
    monkeypatch.setattr(main, '_LEVELDB_INDEX_PROCESSES', processes)
    case.mkdir(exist_ok=True)
    w = main.LevelDbIndexWorker(str(case), jobs or _jobs(tmp_path), 'test-version',
                                cancel_check=cancel_check)
    out = {}
    w.finished_index.connect(lambda failed, cancelled: out.update(failed=failed, cancelled=cancelled))
    w.run()                                   # on this thread: no event loop needed
    with sqlite3.connect(case / 'casecache.db') as db:
        rows = sorted(db.execute('SELECT folder_ui_path, record_index, display_name, '
                                 'searchable_text, content_type FROM leveldb_search_index'))
    return rows, out


def test_pool_gives_the_same_rows_and_failures_as_the_thread(main, tmp_path, monkeypatch):
    rows1, out1 = _run(main, tmp_path, tmp_path / 'case1', 1, monkeypatch)
    rows3, out3 = _run(main, tmp_path, tmp_path / 'case3', 3, monkeypatch)
    assert out1['cancelled'] is False and out3['cancelled'] is False
    assert rows1 == rows3
    assert len({r[0] for r in rows1}) == 10                 # every good store indexed
    assert sum(1 for r in rows1 if r[1] >= 0) == sum(5 + 40 * (i % 3) for i in range(10))
    assert sorted(p for p, _ in out1['failed']) == sorted(p for p, _ in out3['failed']) == \
        ['data/data/bad/leveldb', 'data/data/missing/leveldb']
    assert sorted(out1['failed']) == sorted(out3['failed'])  # the same reasons


def test_few_folders_stay_on_the_thread(main, tmp_path, monkeypatch):
    jobs = _jobs(tmp_path)[:3]
    monkeypatch.setattr(main, '_LEVELDB_INDEX_PROCESSES', 4)
    w = main.LevelDbIndexWorker(str(tmp_path), jobs, 'v')
    assert w._start_pool() is None


def test_biggest_folder_first(tmp_path):
    small, big = tmp_path / 'small', tmp_path / 'big'
    _write_store(str(small), [(b'a', b'x')])
    _write_store(str(big), [(b'a', b'x' * 5000)])
    jobs = [{'extract_dir': str(small), 'to_fetch': []}, {'extract_dir': str(big), 'to_fetch': []}]
    assert [j['extract_dir'] for j in sorted(jobs, key=leveldb_index_job.job_bytes, reverse=True)] == \
        [str(big), str(small)]


def test_index_job_returns_errors_as_values(tmp_path):
    status, reason = leveldb_index_job.index_job(
        {'ui_path': 'x/leveldb', 'extract_dir': str(tmp_path), 'blob_extract_dir': None,
         'to_fetch': [], 'errors': ['x/leveldb/000003.log: not found in the archive']})
    assert status == 'error' and 'not found in the archive' in reason


class _BrokenPool:
    """A pool whose every job fails as a dead worker process would."""
    def __init__(self):
        self.shut = False

    def submit(self, fn, job):
        f = Future()
        f.set_exception(BrokenProcessPool('worker died'))
        return f

    def shutdown(self, wait=True, cancel_futures=False):
        self.shut = True


def test_a_failed_worker_process_is_decoded_on_the_thread(main, tmp_path, monkeypatch):
    rows1, _ = _run(main, tmp_path, tmp_path / 'case1', 1, monkeypatch)
    case = tmp_path / 'case2'
    case.mkdir()
    w = main.LevelDbIndexWorker(str(case), _jobs(tmp_path), 'test-version')
    pool = _BrokenPool()
    failed = []
    db = main._open_cache_db(str(case))
    try:
        assert w._run_on_pool(pool, db, failed) is False
        rows = sorted(db.execute('SELECT folder_ui_path, record_index, display_name, '
                                 'searchable_text, content_type FROM leveldb_search_index'))
    finally:
        db.close()
    assert pool.shut
    assert rows == rows1
    assert sorted(p for p, _ in failed) == ['data/data/bad/leveldb', 'data/data/missing/leveldb']


def test_cancel_stops_the_pool_run(main, tmp_path, monkeypatch):
    calls = {'n': 0}

    def cancel():
        calls['n'] += 1
        return calls['n'] > 2

    rows, out = _run(main, tmp_path, tmp_path / 'case', 3, monkeypatch, cancel_check=cancel)
    assert out['cancelled'] is True
    assert len({r[0] for r in rows}) < 10


def test_cancel_ends_the_worker_processes(main, tmp_path, monkeypatch):
    """A cancelled run ends its worker processes (exit code -SIGTERM): the
    list is taken before pool.shutdown, which sets pool._processes to None.
    Read after it, there was nothing to end, and the workers of a run
    cancelled just before the app exited stayed alive."""
    import signal
    import time
    monkeypatch.setattr(main, '_LEVELDB_INDEX_PROCESSES', 2)
    case = tmp_path / 'case'
    case.mkdir()
    calls = {'n': 0}

    def cancel():
        calls['n'] += 1
        return calls['n'] > 4

    w = main.LevelDbIndexWorker(str(case), _jobs(tmp_path), 'v', cancel_check=cancel)
    pool = w._start_pool()
    seen = {}
    real_shutdown = pool.shutdown

    def shutdown(*a, **k):
        seen['procs'] = list((pool._processes or {}).values())
        return real_shutdown(*a, **k)
    pool.shutdown = shutdown
    db = main._open_cache_db(str(case))
    try:
        assert w._run_on_pool(pool, db, []) is True
    finally:
        db.close()
    assert seen['procs']
    deadline = time.monotonic() + 10
    while any(p.is_alive() for p in seen['procs']) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(p.is_alive() for p in seen['procs'])
    assert all(p.exitcode == -signal.SIGTERM for p in seen['procs'])
