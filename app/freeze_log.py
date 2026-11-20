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
says so. A freeze of HARD_S or more also has every thread's stack written to
crash.log, by the watcher thread itself while it holds the lock
(faulthandler.dump_traceback), so the other threads are stopped while their
frames are read.

Two more facts make a freeze line say WHY (added 2026-11-20, after the
v0.50.50 Windows test, where most lines named the Process Case dialog's
exec() line as if it were the cause):
- Which call the GUI thread is in (_callee: read from the bytecode, so it
  works in the frozen build, which has no source). A GUI thread inside
  app.exec() or a dialog's exec() is running no Python: Qt is busy, or it
  is waiting for Python's lock to call the beat.
- How long the watcher thread itself could not run. It sleeps SAMPLE_S
  and needs Python's lock to wake; waking far later means another thread
  held the lock, or no CPU was free. Which: each thread's own CPU time
  (Windows GetThreadTimes, pthread CPU clocks elsewhere; not on macOS) is
  read at every wake, so the thread that used the CPU while the watcher
  was shut out is named with where it is now and the calls on that source
  line (e.g. "_enc, json.dumps, encode, zlib.compress"); if no thread here
  used it, the machine had no CPU free. A holder can't be sampled during
  the hold itself: by the time the watcher runs, the call has ended.

beat() only records the time. Until 2026-11-19 it re-armed
faulthandler.dump_traceback_later every second: each call joins the previous
watchdog OS thread and starts a new one on the GUI thread, and when it fired
it read every thread's frames WITHOUT the lock while worker threads ran.
Windows (x64 emulated, v0.50.48): 4 of 5 crashes during Process Case and
first open had the GUI thread in beat() right after a long freeze, and the
freeze log showed it stuck 1-4 s on the re-arm lines.

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
_HELD_DEPTH = 6        # innermost frames shown for a thread that held the lock
_LATE_S = 0.25         # the watcher waking this much late counts as "could not run"
_QT_WAITS = ('exec', 'exec_')   # calls that run Qt's event loop

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
    'dumped': False,      # this freeze's stacks are in crash.log
    'callees': {},        # GUI stack -> the call its innermost frame is in
    'starved': 0.0,       # seconds of this freeze the watcher could not run
    'max_gap': 0.0,       # the longest of those stretches
    'held': Counter(),    # (thread, stack, line's calls) -> CPU s used while shut out
    'idle_cpu': 0.0,      # of 'starved', the part no thread here used the CPU
    'cpu': {},            # every thread's CPU seconds at the last wake
    'missed': [],         # freezes beat() saw that the watcher did not (s each)
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
    as a freeze or dumped."""
    global _crash_file
    with _lock:
        _state['stopped'] = True
        _state['in_freeze'] = False
    _crash_file = None


def beat() -> None:
    """The GUI thread is alive (called from a QTimer). Only records the
    time: nothing is armed and no thread is started here. A gap since the
    last beat that the watcher is not already timing is appended (a float)
    for it to log: when other threads hold Python's lock, the GUI thread can
    get the lock now and then while the watcher gets none for many seconds
    (19.5 s during Windows extraction, 2026-11-20), so several freezes can
    pass that it never sees running."""
    now = time.monotonic()
    if now - _state['last'] >= THRESHOLD_S and not _state['in_freeze']:
        missed = _state['missed']
        if len(missed) < 200:
            missed.append(now - _state['last'])
    _state['last'] = now


def _frames(frame, depth: int) -> tuple:
    out = []
    while frame is not None and len(out) < depth:
        co = frame.f_code
        out.append((os.path.basename(co.co_filename), frame.f_lineno, co.co_name))
        frame = frame.f_back
    return tuple(out)


def _busy(stack: tuple) -> bool:
    return bool(stack) and stack[0][0] not in _IDLE_FILES


_callee_cache: dict = {}


def _callee(frame) -> str | None:
    """The call *frame* is in right now, as written ('json.dumps',
    'dlg.exec'), or None if it isn't in a call or that can't be told.

    Read from the bytecode, not the source (a frozen build has none): the
    instruction at f_lasti is the CALL, and the loads that name the called
    thing start where the CALL's own source span starts (Python 3.11+
    instruction positions)."""
    try:
        return _callee_at(frame.f_code, frame.f_lasti)
    except Exception:
        return None


def _callee_at(code, lasti: int) -> str | None:
    """_callee for the instruction at offset *lasti* of *code*."""
    try:
        key = (code, lasti)
        if key in _callee_cache:
            return _callee_cache[key]
        import dis
        ins = list(dis.get_instructions(code))
        name = None
        call = next((i for i in ins if i.offset == lasti), None)
        if (call is not None and call.opname.startswith('CALL')
                and not call.opname.startswith('CALL_INTRINSIC') and call.positions):
            start = (call.positions.lineno, call.positions.col_offset)
            parts = []
            for i in ins:
                if i.offset >= lasti:
                    break
                if not i.positions or (i.positions.lineno, i.positions.col_offset) != start:
                    continue
                if i.opname in ('LOAD_GLOBAL', 'LOAD_NAME', 'LOAD_FAST', 'LOAD_DEREF',
                                'LOAD_FAST_CHECK', 'LOAD_FAST_BORROW'):
                    parts = [str(i.argval)]            # a new chain starts here
                elif i.opname in ('LOAD_ATTR', 'LOAD_METHOD') and parts:
                    parts.append(str(i.argval))
            if parts:
                name = '.'.join(parts[-3:])
        if len(_callee_cache) > 512:
            _callee_cache.clear()
        _callee_cache[key] = name
        return name
    except Exception:
        return None


def _qt_wait(stack: tuple, callee: str | None) -> bool:
    """The GUI thread is inside Qt's event loop (app.exec() or a dialog's
    exec()) with no Python of its own running."""
    if callee and callee.rsplit('.', 1)[-1] in _QT_WAITS:
        return True
    return _in_event_loop(stack) or (bool(stack) and all(fn == '<module>' for _f, _n, fn in stack))


def _sample(frames: dict, names: dict) -> None:
    me = threading.get_ident()
    main = frames.get(_state['main'])
    if main is not None:
        st = _frames(main, _STACK_DEPTH)
        _state['samples'].append(st)
        if st not in _state['callees']:
            _state['callees'][st] = _callee(main)
    for ident, fr in frames.items():
        if ident in (me, _state['main']):
            continue
        st = _frames(fr, _OTHER_DEPTH)
        if _busy(st):
            _state['others'][(names.get(ident, '?'), st)] += 1


def _line_calls(frame) -> str | None:
    """The calls on *frame*'s current source line, in order ('_enc,
    json.dumps, encode, zlib.compress'), from the bytecode."""
    try:
        import dis
        line = frame.f_lineno
        names = []
        for i in dis.get_instructions(frame.f_code):
            if (i.opname.startswith('CALL') and not i.opname.startswith('CALL_INTRINSIC')
                    and i.positions and i.positions.lineno == line):
                n = _callee_at(frame.f_code, i.offset)
                if n and n not in names:
                    names.append(n)
        return ', '.join(names[:6]) or None
    except Exception:
        return None


_kernel32 = None
_thread_handles: dict = {}


def _thread_cpu(threads) -> dict:
    """{ident: CPU seconds the thread has used}, for *threads*
    (threading.enumerate()); {} where the platform can't tell."""
    global _kernel32
    out = {}
    try:
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            if _kernel32 is None:
                k = ctypes.WinDLL('kernel32', use_last_error=True)
                k.OpenThread.restype = wintypes.HANDLE
                k.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                k.GetThreadTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
                k.CloseHandle.argtypes = (wintypes.HANDLE,)
                _kernel32 = k
            k = _kernel32
            alive = set()
            for t in threads:
                tid = t.native_id
                if not tid:
                    continue
                alive.add(tid)
                h = _thread_handles.get(tid)
                if h is None:
                    h = k.OpenThread(0x0800, False, tid)   # THREAD_QUERY_LIMITED_INFORMATION
                    if not h:
                        continue
                    _thread_handles[tid] = h
                ft = [wintypes.FILETIME() for _ in range(4)]
                if k.GetThreadTimes(h, *[ctypes.byref(f) for f in ft]):
                    kern = (ft[2].dwHighDateTime << 32) | ft[2].dwLowDateTime
                    user = (ft[3].dwHighDateTime << 32) | ft[3].dwLowDateTime
                    out[t.ident] = (kern + user) / 1e7
            for tid in [d for d in _thread_handles if d not in alive]:
                k.CloseHandle(_thread_handles.pop(tid))
        elif hasattr(time, 'pthread_getcpuclockid'):
            for t in threads:
                try:
                    out[t.ident] = time.clock_gettime(time.pthread_getcpuclockid(t.ident))
                except (OSError, OverflowError, TypeError):
                    pass
    except Exception:
        return {}
    return out


def _note_held(frames: dict, names: dict, cpu: dict, late: float) -> None:
    """The watcher just woke *late* s late: the threads whose CPU time grew
    meanwhile are the ones that ran (held Python's lock); none means no CPU
    was free for this process."""
    me = threading.get_ident()
    prev = _state['cpu']
    used = 0.0
    for ident, now in cpu.items():
        if ident == me or ident not in prev:
            continue
        d = now - prev[ident]
        if d < 0.1 or d < 0.2 * late:
            continue
        used += d
        fr = frames.get(ident)
        st = _frames(fr, _HELD_DEPTH) if fr is not None else ()
        name = 'GUI thread' if ident == _state['main'] else names.get(ident, '?')
        _state['held'][(name, st, _line_calls(fr) if fr is not None else None)] += d
    if cpu and prev and used < 0.3 * late:
        _state['idle_cpu'] += late


def _begin_freeze() -> None:
    _state['dumped'] = False
    _state['freeze_start'] = _state['last']
    _state['samples'] = []
    _state['others'] = Counter()
    _state['callees'] = {}
    _state['starved'] = 0.0
    _state['max_gap'] = 0.0
    _state['held'] = Counter()
    _state['idle_cpu'] = 0.0
    _state['same_stretch'] = None


def _note_late(frames: dict, names: dict, cpu: dict, late: float) -> None:
    _state['starved'] += late
    _state['max_gap'] = max(_state['max_gap'], late)
    _note_held(frames, names, cpu, late)


def _log_missed(frames, names, cpu, late: float) -> None:
    """Log the freezes beat() saw that the watcher never caught running
    (the GUI thread got the lock back first), oldest first. What the
    watcher saw on this late wake goes with the longest of them; the
    others say they were in the same stretch. Only between freezes (or
    right before one starts): it uses the per-freeze state."""
    gaps = list(_state['missed'])
    _state['missed'].clear()
    if not gaps:
        return
    longest = max(range(len(gaps)), key=gaps.__getitem__)
    for i, seconds in enumerate(gaps):
        _begin_freeze()
        if i != longest and len(gaps) > 1:
            _state['same_stretch'] = gaps[longest]
        elif frames is not None and late >= _LATE_S:
            _note_late(frames, names, cpu, late)
        _end_freeze(seconds)


def _watch() -> None:
    mine = _state       # a module reload (tests) replaces it: this watcher then ends
    t_prev = time.monotonic()
    while True:
        time.sleep(SAMPLE_S)
        try:
            with _lock:
                now = time.monotonic()
                late = now - t_prev - SAMPLE_S
                t_prev = now
                if _state is not mine or _state['stopped']:
                    return
                frames = sys._current_frames()
                threads = threading.enumerate()
                names = {t.ident: t.name for t in threads}
                cpu = _thread_cpu(threads)
                gap = now - _state['last']
                if gap >= THRESHOLD_S:
                    if not _state['in_freeze']:
                        _log_missed(None, None, None, 0.0)   # earlier ones first
                        _begin_freeze()
                        _state['in_freeze'] = True
                    if late >= _LATE_S:
                        _note_late(frames, names, cpu, late)
                    _sample(frames, names)
                    if gap >= HARD_S and not _state['dumped']:
                        _state['dumped'] = True
                        _dump_all(gap)
                elif _state['in_freeze']:
                    _end_freeze(_state['last'] - _state['freeze_start'])
                elif _state['missed']:
                    _log_missed(frames, names, cpu, late)
                _state['cpu'] = cpu
                del frames
        except Exception:
            pass


def _dump_all(gap: float) -> None:
    """Every thread's stack to crash.log, from the watcher thread while it
    holds the interpreter lock (the other threads are stopped meanwhile)."""
    f = _crash_file
    if f is None:
        return
    try:
        f.write(f"\nGUI not responding for {gap:.0f} s ({time.strftime('%Y-%m-%d %H:%M:%S')}); "
                "every thread's stack:\n")
        f.flush()
        faulthandler.dump_traceback(file=f, all_threads=True)
        f.flush()
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
        rec['callee'] = _state['callees'].get(stack)
        rec['qt_wait'] = _qt_wait(stack, rec['callee'])
    rec['others'] = [(name, st, n) for (name, st), n in _state['others'].most_common(4)]
    rec['starved'] = min(_state['starved'], seconds)
    rec['max_gap'] = _state['max_gap']
    rec['held'] = [(name, st, call, s) for (name, st, call), s in _state['held'].most_common(3)]
    rec['idle_cpu'] = min(_state['idle_cpu'], _state['starved'])
    rec['same_stretch'] = _state.get('same_stretch')
    del recent[:-19]
    recent.append(rec)
    if _state['logged'] >= MAX_LOGGED:
        return
    _state['logged'] += 1
    try:
        # Lock starvation: the watcher itself could not run for most of it.
        starved = rec['starved'] >= 0.5 * seconds
        if samples and rec['qt_wait']:
            where = (f'GUI thread in Qt, no Python running ({n} of {len(samples)} samples, '
                     f'waiting in {rec["callee"] or "exec"} at {_fmt(rec["stack"][:2])})')
            cause = (': waiting for Python\'s lock (see below)' if starved else
                     ': Qt itself was busy (drawing, layout, a large model reset) or the '
                     'system delayed the timer (e.g. macOS App Nap on a hidden window)')
            where += cause
        elif samples:
            call = f' in {rec["callee"]}' if rec['callee'] else ''
            where = (f'GUI thread{call} at ({n} of {len(samples)} samples): '
                     f'{_fmt(rec["stack"])}')
        elif starved:
            where = ('no GUI stack sampled: the watcher could not run until the freeze '
                     'was over (see below)')
        elif rec['same_stretch']:
            where = ('no GUI stack sampled: one of several freezes while the watcher could '
                     f'not run (see the {rec["same_stretch"]:.2f} s one for what ran)')
        else:
            where = ('no Python stack sampled: the GUI thread held the interpreter '
                     'lock inside one C call (Qt, SQLite, msgpack …) the whole time')
        lines = [f'GUI froze {seconds:.2f} s; {where}']
        if starved:
            why = ("another thread held Python's lock, or no CPU was free"
                   if not rec['held'] and not rec['idle_cpu'] else
                   "a thread held Python's lock (named below)" if rec['held'] else
                   'no thread of this process ran: no CPU was free for it')
            shut = _state['starved']
            if rec['max_gap'] > seconds + 0.2:
                # the watcher was shut out longer than this freeze lasted:
                # the GUI thread got the lock now and then, the watcher not
                lines.append(f'    the watcher thread could not run for {rec["max_gap"]:.1f} s '
                             f'up to the end of it (longer than the freeze; any other freezes '
                             f'in that time are logged as well): {why}')
            else:
                lines.append(f'    the watcher thread could not run for {rec["starved"]:.1f} s '
                             f'of it: {why}')
            if rec['held'] and rec['idle_cpu'] >= 0.5:
                lines.append(f'    for {rec["idle_cpu"]:.1f} s of that no thread here ran: '
                             'no CPU was free')
        else:
            shut = _state['starved']
        for name, st, call, s in rec['held']:
            on_line = f'; calls on that line: {call}' if call else ''
            lines.append(f'    ran {s:.1f} s of CPU in the {shut:.1f} s the watcher could not '
                         f'run: {name}, now at {_fmt(st)}{on_line}')
        if seconds >= HARD_S and _crash_file is not None:
            lines.append('    every thread\'s stack at %.0f s is in crash.log' % HARD_S)
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
        stop()
        if crash_file is None:
            return
        # No header written now: a normal exit must leave crash.log empty.
        # faulthandler's own dump starts "Timeout (0:00:20)!".
        faulthandler.dump_traceback_later(seconds, repeat=False, file=crash_file, exit=True)
        _logger.info('exit watchdog: if still running in %.0f s, the stacks go to '
                     'crash.log ("Timeout") and the process is ended', seconds)
    except Exception:
        pass
