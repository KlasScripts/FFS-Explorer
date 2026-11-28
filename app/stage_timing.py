"""How long each Process Case stage (and the first open of a case) takes,
and how much of the machine it uses: cores busy in the app and all its
worker processes, the most worker processes at once, and disk read/write
MB/s. Written to stage_timing.tsv in the case folder, one row per stage,
appended per run, so runs on different machines and worker settings can be
compared (owner request 2026-11-28: use every core and the SSD fully).

Measured every 0.5 s with psutil, the same way on Windows and Mac. What
the numbers mean:
  - cores: CPU seconds of the app and its worker processes per second of
    wall time (4.0 = four cores busy). A worker that ends between two
    samples loses its last part-second, so this reads a little low.
  - disk MB/s: the whole machine (psutil.disk_io_counters), so another
    program reading at the same time is counted too.
  - stages that run at the same time (the integrity hash runs beside the
    others) each show the app total while they run, not their own share.
Qt-free; nothing here changes what a stage does. Never raises: a timing
problem is not allowed to stop a run (an error is written as a row note)."""
from __future__ import annotations

import os
import platform
import threading
import time
from datetime import datetime, timezone

FILE_NAME = 'stage_timing.tsv'
COLUMNS = ['run_started_utc', 'stage', 'seconds', 'cores_avg', 'cores_peak', 'workers_peak',
           'read_mb_s_avg', 'read_mb_s_peak', 'write_mb_s_avg', 'read_gb', 'worker_setting',
           'logical_cores', 'physical_cores', 'ram_gb', 'os', 'note']
INTERVAL = 0.5


def machine() -> dict:
    """Core counts, RAM and OS, for every row."""
    out = {'logical_cores': os.cpu_count(), 'physical_cores': None, 'ram_gb': None,
           'os': f"{platform.system()} {platform.release()} {platform.machine()}"}
    try:
        import psutil
        out['physical_cores'] = psutil.cpu_count(logical=False)
        out['ram_gb'] = round(psutil.virtual_memory().total / 2 ** 30, 1)
    except Exception:
        pass
    return out


class _Stage:
    __slots__ = ('name', 't0', 't1', 'cpu', 'peak', 'workers', 'rd', 'rd_peak', 'wr', 'n')

    def __init__(self, name):
        self.name, self.t0, self.t1 = name, time.monotonic(), None
        self.cpu = self.peak = self.rd = self.rd_peak = self.wr = 0.0
        self.workers = self.n = 0


class StageLog:
    """begin(name) starts a stage; a stage started with sequential=True
    ends the sequential stage before it. end(name) ends one; close()
    ends the rest and writes the rows (once). case_dir may be set later
    (the first open knows it only after the case folder is chosen)."""

    def __init__(self, case_dir: str | None = None, worker_setting=None):
        self.case_dir = case_dir
        self.worker_setting = worker_setting
        self.started = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self._open: dict[str, _Stage] = {}
        self._done: list[_Stage] = []
        self._current: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._written = False
        self.error = ''
        try:
            import psutil
            self._psutil = psutil
            self._me = psutil.Process(os.getpid())
        except Exception as e:      # no psutil: wall times only
            self._psutil = None
            self.error = f'no CPU/disk figures ({e.__class__.__name__})'

    # ── stages ───────────────────────────────────────────────────────────
    def begin(self, name: str, sequential: bool = True) -> None:
        try:
            with self._lock:
                if name in self._open:
                    return
                if sequential:
                    if self._current and self._current in self._open:
                        self._end_locked(self._current)
                    self._current = name
                self._open[name] = _Stage(name)
            self._ensure_thread()
        except Exception as e:
            self.error = f'timing error ({e})'

    def end(self, name: str | None = None) -> None:
        try:
            with self._lock:
                name = name or self._current
                if name in self._open:
                    self._end_locked(name)
                if name == self._current:
                    self._current = None
        except Exception as e:
            self.error = f'timing error ({e})'

    def _end_locked(self, name):
        st = self._open.pop(name)
        st.t1 = time.monotonic()
        self._done.append(st)

    def rename(self, old: str, new: str) -> None:
        """Name a stage by what it turned out to be (a first open or a
        reopen is known only at its end)."""
        with self._lock:
            if old in self._open:
                st = self._open.pop(old)
                st.name = new
                self._open[new] = st
                if self._current == old:
                    self._current = new

    def is_open(self, name: str) -> bool:
        return name in self._open

    def close(self) -> str | None:
        """End every stage, stop sampling, write the rows. Returns the
        file written (None when there is no case folder or nothing to write)."""
        with self._lock:
            for name in list(self._open):
                self._end_locked(name)
            self._current = None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._written or not self._done or not self.case_dir:
            return None
        self._written = True
        return self._write()

    # ── sampling ─────────────────────────────────────────────────────────
    def _ensure_thread(self):
        if self._thread is None and self._psutil is not None:
            self._thread = threading.Thread(target=self._run, name='stage-timing', daemon=True)
            self._thread.start()

    def _cpu_now(self):
        ps = self._psutil
        procs = [self._me]
        try:
            procs += self._me.children(recursive=True)
        except ps.Error:
            pass
        times = {}
        for p in procs:
            try:
                t = p.cpu_times()
                times[p.pid] = t.user + t.system
            except ps.Error:
                pass
        return times

    def _disk_now(self):
        try:
            d = self._psutil.disk_io_counters()
            return (d.read_bytes, d.write_bytes) if d else None
        except Exception:
            return None

    def _run(self):
        try:
            last_t, last_cpu, last_disk = time.monotonic(), self._cpu_now(), self._disk_now()
            while not self._stop.wait(INTERVAL):
                now, cpu, disk = time.monotonic(), self._cpu_now(), self._disk_now()
                dt = now - last_t
                if dt <= 0:
                    continue
                busy = sum(max(0.0, v - last_cpu.get(pid, 0.0)) for pid, v in cpu.items()) / dt
                rd = wr = 0.0
                if disk and last_disk:
                    rd = max(0, disk[0] - last_disk[0]) / dt / 1e6
                    wr = max(0, disk[1] - last_disk[1]) / dt / 1e6
                with self._lock:
                    for st in self._open.values():
                        st.n += 1
                        st.cpu += busy
                        st.peak = max(st.peak, busy)
                        st.workers = max(st.workers, len(cpu) - 1)
                        st.rd += rd
                        st.rd_peak = max(st.rd_peak, rd)
                        st.wr += wr
                last_t, last_cpu, last_disk = now, cpu, disk
        except Exception as e:
            self.error = f'sampling stopped ({e})'

    # ── output ───────────────────────────────────────────────────────────
    def rows(self) -> list[dict]:
        m = machine()
        out = []
        for st in self._done:
            secs = (st.t1 or time.monotonic()) - st.t0
            n = st.n or None
            out.append({
                'run_started_utc': self.started, 'stage': st.name, 'seconds': round(secs, 2),
                'cores_avg': round(st.cpu / n, 2) if n else '',
                'cores_peak': round(st.peak, 2) if n else '',
                'workers_peak': st.workers if n else '',
                'read_mb_s_avg': round(st.rd / n) if n else '',
                'read_mb_s_peak': round(st.rd_peak) if n else '',
                'write_mb_s_avg': round(st.wr / n) if n else '',
                'read_gb': round(st.rd / n * secs / 1000, 2) if n else '',
                'worker_setting': self.worker_setting if self.worker_setting is not None else '',
                **m,
                'note': self.error or ('' if n else 'shorter than one sample'),
            })
        return out

    def _write(self) -> str | None:
        path = os.path.join(self.case_dir, FILE_NAME)
        try:
            new = not os.path.exists(path)
            with open(path, 'a', encoding='utf-8', newline='') as f:
                if new:
                    f.write('\t'.join(COLUMNS) + '\n')
                for r in self.rows():
                    f.write('\t'.join(str(r.get(c, '')).replace('\t', ' ') for c in COLUMNS) + '\n')
            return path
        except OSError:
            return None
