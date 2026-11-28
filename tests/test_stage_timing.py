"""stage_timing: one row per stage in stage_timing.tsv, a sequential stage
ends the one before it, an overlapping one (the integrity hash) runs on,
and worker processes' CPU is counted."""
import csv
import multiprocessing as mp
import os
import time

import stage_timing


def _spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def _rows(case):
    with open(os.path.join(case, stage_timing.FILE_NAME), encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def test_stages_rows_and_worker_cpu(tmp_path):
    log = stage_timing.StageLog(str(tmp_path), 'auto: 2 search/metadata/leveldb, 2 sweep')
    log.begin('integrity_hash', sequential=False)
    log.begin('a')
    time.sleep(0.6)
    log.begin('b')                               # ends a
    assert not log.is_open('a') and log.is_open('integrity_hash')
    ctx = mp.get_context('spawn')
    procs = [ctx.Process(target=_spin, args=(1.6,)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    log.end('integrity_hash')
    log.end()
    assert log.close() == os.path.join(str(tmp_path), stage_timing.FILE_NAME)
    assert log.close() is None                   # written once
    rows = {r['stage']: r for r in _rows(str(tmp_path))}
    assert set(rows) == {'a', 'b', 'integrity_hash'}
    assert 0.5 < float(rows['a']['seconds']) < 1.5
    # two spinning worker processes: well over one core while b ran
    assert float(rows['b']['cores_peak']) > 1.3, rows['b']
    assert int(rows['b']['workers_peak']) >= 2
    assert rows['b']['logical_cores'] == str(os.cpu_count())
    assert rows['b']['physical_cores'] not in ('', 'None')
    assert rows['b']['worker_setting'].startswith('auto')


def test_appends_and_no_case_dir(tmp_path):
    for _ in range(2):
        log = stage_timing.StageLog(str(tmp_path))
        log.begin('x')
        log.close()
    assert [r['stage'] for r in _rows(str(tmp_path))] == ['x', 'x']
    log = stage_timing.StageLog(None)
    log.begin('open_case')
    log.rename('open_case', 'open_case (new case)')
    assert log.is_open('open_case (new case)')
    assert log.close() is None                   # no folder: nothing written
