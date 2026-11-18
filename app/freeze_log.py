"""freeze_log.py — log every time the window stops responding, and why.

Added 2026-11-18: a work PC reported the app "a bit sluggish" with no way to
tell which job caused it. Each freeze of the GUI thread longer than
THRESHOLD_S is written to the startup log (ffs-explorer.log) when it ends,
with how long it lasted and where the GUI thread's Python code was while it
was stuck (sampled every SAMPLE_S, the most common stack shown), plus what the
other busy threads were running, since they compete with the GUI thread for
Python's one lock (A-81). Only code locations are logged, never values, so
no case data reaches the log.

How it works: the GUI thread calls beat() from a QTimer every BEAT_S. A
daemon thread notices when the beats stop and samples the GUI thread's stack
(sys._current_frames). It needs the interpreter lock to do that, so a freeze
spent inside one long C call that holds the lock gets no sample; the log then
says so. For freezes of HARD_S or more (and a total hang) faulthandler, which
runs without the lock, writes every thread's stack to crash.log: beat()
re-arms it about once a second.

Also: arm_exit_watchdog() for the exit, so a process that logged "exiting"
but never ended writes its stacks to crash.log and is ended (a work-PC
report, 2026-11-18: an instance stayed open with its window after "exiting").

Qt-free; every call is wrapped so it can never stop the app.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from collections import Counter

THRESHOLD_S = 0.5      # a freeze is logged when the GUI thread misses beats this long
BEAT_S = 0.1           # the GUI timer's interval
SAMPLE_S = 0.1         # stack sampling interval during a freeze
HARD_S = 15.0          # faulthandler dump (all threads, no lock needed) after this
EXIT_S = 20.0          # exit watchdog: end the process this long after "exiting"
MAX_LOGGED = 200       # freezes written to the log per session (all are counted)
_STACK_DEPTH = 14      # innermost frames shown for the GUI thread
_OTHER_DEPTH = 3       # innermost frames shown for each other busy thread

_logger = logging.getLogger('ffs_explorer.startup')

# Frames in these files mean a thread is idle (waiting), not competing.
_IDLE_FILES = ('threading.py', 'queue.py', 'selectors.py', 'thread.py',
               'process.py', 'connection.py', 'socketserver.py', 'socket.py')

_state = {
    'last': 0.0,          # monotonic time of the last beat
    'main': None,         # GUI thread ident
    'samples': [],        # stacks sampled during the current freeze
    'others': Counter(),  # other busy threads' stacks during it
    'in_freeze': False,
    'freeze_start': 0.0,
    'armed_at': 0.0,      # when faulthandler was last re-armed
    'count': 0, 'total': 0.0, 'longest': 0.0, 'logged': 0,
    'started': False, 'stopped': False,
}
_lock = threading.Lock()
recent: list[dict] = []   # the last few freezes (for tests and diagnostics)
_crash_file = None


def start(crash_file=None) -> None:
    """Start watching. Call on the GUI thread once the event loop runs, then
    call beat() from a QTimer every BEAT_S. *crash_file*: an open text file
    for the faulthandler dumps of long freezes (startup_log's crash.log)."""
    global _crash_file
    try:
        if _state['started']:
            return
        _state['started'] = True
        _state['main'] = threading.get_ident()
        _state['last'] = time.monotonic()
        _crash_file = crash_file
        threading.Thread(target=_watch, name='ffs-freeze-watch', daemon=True).start()
        _logger.info('freeze logger on: freezes over %.1f s are logged', THRESHOLD_S)
    except Exception:
        pass


def stop() -> None:
    """The app is quitting: beats stop for good; nothing more is logged
    as a freeze and beat() no longer re-arms the faulthandler dump."""
    global _crash_file
    with _lock:
        _state['stopped'] = True
        _state['in_freeze'] = False
    _crash_file = None


def beat() -> None:
    """The GUI thread is alive (called from a QTimer)."""
    try:
        if _state['stopped']:
            return
        now = time.monotonic()
        _state['last'] = now
        if _crash_file is not None and now - _state['armed_at'] >= 1.0:
            _state['armed_at'] = now
            faulthandler.dump_traceback_later(HARD_S, repeat=False, file=_crash_file)
    except Exception:
        pass


def _frames(frame, depth: int) -> tuple:
    out = []
    while frame is not None and len(out) < depth:
        co = frame.f_code
        out.append((os.path.basename(co.co_filename), frame.f_lineno, co.co_name))
        frame = frame.f_back
    return tuple(out)


def _busy(stack: tuple) -> bool:
    return bool(stack) and stack[0][0] not in _IDLE_FILES


def _sample() -> None:
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    me = threading.get_ident()
    main = frames.get(_state['main'])
    if main is not None:
        _state['samples'].append(_frames(main, _STACK_DEPTH))
    for ident, fr in frames.items():
        if ident in (me, _state['main']):
            continue
        st = _frames(fr, _OTHER_DEPTH)
        if _busy(st):
            _state['others'][(names.get(ident, '?'), st)] += 1


def _watch() -> None:
    while True:
        time.sleep(SAMPLE_S)
        try:
            with _lock:
                if _state['stopped']:
                    return
                gap = time.monotonic() - _state['last']
                if gap >= THRESHOLD_S:
                    if not _state['in_freeze']:
                        _state['in_freeze'] = True
                        _state['freeze_start'] = _state['last']
                        _state['samples'] = []
                        _state['others'] = Counter()
                    _sample()
                elif _state['in_freeze']:
                    _end_freeze(_state['last'] - _state['freeze_start'])
        except Exception:
            pass


def _fmt(stack: tuple) -> str:
    return ' <- '.join(f'{f}:{n} {fn}' for f, n, fn in stack)


def _in_event_loop(stack: tuple) -> bool:
    """The GUI thread's stack is only main() (and the module), i.e. it is
    inside app.exec() with no Python callback running."""
    names = [fn for _f, _n, fn in stack]
    return bool(names) and names[0] == 'main' and all(n in ('main', '<module>') for n in names)


def _end_freeze(seconds: float) -> None:
    _state['in_freeze'] = False
    _state['count'] += 1
    _state['total'] += seconds
    _state['longest'] = max(_state['longest'], seconds)
    samples = _state['samples']
    rec = {'seconds': seconds, 'samples': len(samples)}
    if samples:
        stack, n = Counter(samples).most_common(1)[0]
        rec['stack'] = stack
        rec['share'] = n / len(samples)
    rec['others'] = [(name, st, n) for (name, st), n in _state['others'].most_common(4)]
    del recent[:-19]
    recent.append(rec)
    if _state['logged'] >= MAX_LOGGED:
        return
    _state['logged'] += 1
    try:
        if samples and _in_event_loop(rec['stack']):
            where = (f'GUI thread in Qt\'s event loop, no Python running ({n} of {len(samples)} '
                     'samples): Qt itself was busy (drawing, layout, a large model reset) '
                     'or the system delayed the timer (e.g. macOS App Nap on a hidden window)')
        elif samples:
            where = (f'GUI thread at ({n} of {len(samples)} samples): '
                     f'{_fmt(rec["stack"])}')
        else:
            where = ('no Python stack sampled: the GUI thread held the interpreter '
                     'lock inside one C call (Qt, SQLite, msgpack …) the whole time')
        lines = [f'GUI froze {seconds:.2f} s; {where}']
        if seconds >= HARD_S and _crash_file is not None:
            lines.append('    every thread\'s stack at %.0f s is in crash.log ("Timeout")' % HARD_S)
        for name, st, k in rec['others']:
            lines.append(f'    busy meanwhile: {name} ({k} samples): {_fmt(st)}')
        _logger.warning('%s', '\n'.join(lines))
    except Exception:
        pass


def summary() -> str | None:
    """One line for the end of the session, or None if nothing froze."""
    if not _state['count']:
        return None
    return (f"GUI freezes over {THRESHOLD_S:.1f} s this session: {_state['count']}, "
            f"{_state['total']:.1f} s in all, longest {_state['longest']:.1f} s")


def arm_exit_watchdog(crash_file, seconds: float = EXIT_S) -> None:
    """Called once the app has logged "exiting": if the process is still
    alive *seconds* later (stuck while Python and Qt shut down), faulthandler
    writes every thread's stack to *crash_file* and ends the process. Runs
    in faulthandler's own C thread, so it works while Python is finalising."""
    try:
        stop()                         # beat() must no longer re-arm the dump
        if crash_file is None:
            return
        # No header written now: a normal exit must leave crash.log empty.
        # faulthandler's own dump starts "Timeout (0:00:20)!".
        faulthandler.dump_traceback_later(seconds, repeat=False, file=crash_file, exit=True)
        _logger.info('exit watchdog: if still running in %.0f s, the stacks go to '
                     'crash.log ("Timeout") and the process is ended', seconds)
    except Exception:
        pass
