"""How many workers each kind of stage uses (Preferences ▸ Performance,
owner request 2026-11-28: use every core and the SSD fully).

Two settings, because the stages are limited by different things:
  - CPU-heavy: the embedded-media sweep, the media metadata stage, LevelDB
    indexing. They parse and decode, and keep getting faster with more
    worker processes until the machine's cores are busy.
  - Disk-heavy: Keyword Search (owner, 2026-11-28: it reads the whole
    archive), duplicate hashing and the header scan's readers. Past some
    number of readers at once the disk is the limit and more only queue.

0 means automatic:
  - CPU: the physical cores, one left for the window. Physical, not
    logical: a second thread on a core (SMT/Hyper-Threading) adds far less
    than a core to this work, and a process per logical core only adds
    memory and start-up time.
  - Disk: the physical cores, at most DISK_AUTO_MAX (see its comment).
A number set by the examiner is used as it is, up to the logical cores.
Qt-free; psutil gives the physical count, os.cpu_count() without it."""
from __future__ import annotations

import os

CPU_PREF = 'worker_processes'      # the key the single setting had before 2026-11-28
DISK_PREF = 'disk_workers'
# Disk-heavy stages, automatic: the physical cores, at most DISK_AUTO_MAX.
# Measured 2026-11-28 on a Mac (M5, 10 cores, internal SSD), IOS17 case
# (ASSUMPTIONS.md A-98): duplicate hashing of every file 14.8 s on 1 worker,
# 7.4 on 2, 5.9 on 4, 5.4 on 8, 5.0 on 10, the disk never above ~0.8 GB/s;
# Keyword Search 33 s on 1, 8.4 on 4, 4.6 on 10. Nothing here stopped
# gaining below the core count. The owner found 12 best for Keyword Search on
# a 20-core (40-thread) Threadripper (2026-11-28, from memory, not measured
# here), hence the most.
DISK_AUTO_MAX = 12


def logical_cores() -> int:
    return os.cpu_count() or 1


def physical_cores() -> int:
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:
        pass
    return logical_cores()


def auto_cpu() -> int:
    return max(1, physical_cores() - 1)


def auto_disk() -> int:
    return max(1, min(DISK_AUTO_MAX, physical_cores()))


def resolve(cpu_pref, disk_pref) -> tuple[int, int]:
    """(cpu, disk) worker counts from the two settings (0 or None: automatic)."""
    def one(v, auto):
        try:
            v = int(v or 0)
        except (TypeError, ValueError):
            v = 0
        return auto if v <= 0 else max(1, min(v, logical_cores()))
    return one(cpu_pref, auto_cpu()), one(disk_pref, auto_disk())
