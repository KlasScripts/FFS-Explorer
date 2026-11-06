"""startup_log.py — a small, bounded log of startup and errors.

Added 2026-10-22 after a real report: on a work PC just updated to Windows 11
the exe sat in Task Manager with no window and no error, because the Windows
build has no console (console=False) and nothing was written anywhere.

What it records (and nothing else, so a normal launch is ~20 lines):
- each startup step with a timestamp, so a stall shows where it stopped;
- uncaught exceptions on any thread, with the full traceback;
- Qt warnings and errors (previously printed to a console that doesn't exist).

How it stays small:
- `ffs-explorer.log`: rotated at 1 MB, 3 old copies kept (≈4 MB at most).
- An identical message is written at most `_REPEAT_LIMIT` times per session;
  the number suppressed is logged at exit.
- `crash.log` (faulthandler): written only on a hard crash, or when the main
  window hasn't appeared within `WATCHDOG_SECONDS` — then it holds a stack
  snapshot of every thread showing exactly where startup is stuck. One
  previous copy is kept as `crash.prev.log`.

Started on the first lines of ffs-explorer.py, before the heavy imports,
so a stall while loading modules is logged too (2026-10-23). If the
per-user folder can't be written, the temp folder and then a 'logs'
folder next to the exe are tried; the first line then says so.

Qt-free on purpose: it is set up before QApplication exists. Every call is
wrapped so a logging failure can never stop the app from starting.
"""

import atexit
import faulthandler
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback

LOG_NAME = 'ffs-explorer.log'
CRASH_NAME = 'crash.log'
MAX_BYTES = 1_000_000
BACKUPS = 3
WATCHDOG_SECONDS = 60
_REPEAT_LIMIT = 5

_logger = logging.getLogger('ffs_explorer.startup')
_log_dir: str | None = None
_crash_file = None
_t0 = time.monotonic()
_seen: dict[str, int] = {}
_seen_lock = threading.Lock()


def log_dir() -> str:
    """Per-user folder for the logs: %LOCALAPPDATA%\\FFS Explorer\\logs on
    Windows, ~/Library/Logs/FFS Explorer on macOS, XDG state dir elsewhere."""
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return os.path.join(base, 'FFS Explorer', 'logs')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Logs/FFS Explorer')
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(base, 'FFS Explorer', 'logs')


def _candidate_dirs() -> list[str]:
    """Where to try writing the log, in order: the normal per-user folder,
    then the temp folder, then next to the exe (a locked-down PC can refuse
    the first)."""
    out = [log_dir()]
    try:
        import tempfile
        out.append(os.path.join(tempfile.gettempdir(), 'FFS Explorer', 'logs'))
    except Exception:
        pass
    out.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'logs'))
    return out


def active_log_dir() -> str | None:
    """The folder this session is actually logging to (None before setup
    or if no folder could be written)."""
    return _log_dir


def setup() -> str | None:
    """Start logging for this session. Returns the log folder, or None if no
    candidate folder could be written (the app then runs without a log).
    Safe to call more than once; later calls return the same folder."""
    global _log_dir, _crash_file
    if _log_dir is not None:
        return _log_dir
    d = None
    for cand in _candidate_dirs():
        try:
            os.makedirs(cand, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(cand, LOG_NAME), maxBytes=MAX_BYTES,
                backupCount=BACKUPS, encoding='utf-8')
        except Exception:
            continue
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(message)s'))
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False
        d = _log_dir = cand
        break
    if d is None:
        return None
    crash = _open_crash_file(d)
    if crash is not None:
        try:
            _route_c_stderr(crash)
            # Before faulthandler: it keeps this as the handler to pass an
            # abort on to once it has written the Python stacks.
            _install_abort_dump(os.path.splitext(crash)[0] + '.dmp')
            faulthandler.enable(file=_crash_file, all_threads=True)
            faulthandler.dump_traceback_later(WATCHDOG_SECONDS, repeat=False, file=_crash_file)
        except Exception:
            _crash_file = None
    _install_excepthooks()
    atexit.register(_at_exit)
    _logger.info('=' * 60)
    _logger.info('FFS Explorer starting  pid=%s', os.getpid())
    _logger.info('%s', _environment())
    if _crt_path or _abort_handler is not None:
        _logger.info('crash dump on abort: %s (C runtime %s)',
                     'on' if _abort_handler is not None else 'OFF', _crt_path or 'unknown')
    if d != log_dir():
        _logger.warning('could not write to %s; logging here instead', log_dir())
    return d


def step(message: str) -> None:
    """Log one startup step with the seconds since setup()."""
    try:
        _logger.info('[+%.2fs] %s', time.monotonic() - _t0, message)
    except Exception:
        pass


def window_shown() -> None:
    """The main window is up: stop the 'stuck at startup' watchdog. Call it
    once the event loop is running (QTimer.singleShot(0, ...) after show())."""
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
    step('main window shown, event loop running')


def message(level: str, text: str) -> None:
    """Log a message (e.g. a Qt warning), at most _REPEAT_LIMIT times for an
    identical text per session."""
    try:
        with _seen_lock:
            n = _seen.get(text, 0) + 1
            _seen[text] = n
        if n <= _REPEAT_LIMIT:
            _logger.log(logging.ERROR if level == 'error' else logging.WARNING,
                        '%s%s', text, '  (further repeats not logged)' if n == _REPEAT_LIMIT else '')
    except Exception:
        pass


def _environment() -> str:
    try:
        frozen = getattr(sys, 'frozen', False)
        exe = sys.executable
        built = ''
        if frozen:
            built = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(exe)))
        from version import app_version
        version = app_version() or 'version unknown'
        # 'files dated': the exe's modified time, i.e. when it was unzipped,
        # not when it was built; the version says which release it is.
        return (f"FFS Explorer {version} | {platform.platform()} | "
                f"python {platform.python_version()} | "
                f"{'frozen, files dated ' + built if frozen else 'source run'} | {exe}")
    except Exception:
        return 'environment unavailable'


def _open_crash_file(d: str) -> str | None:
    """Open this session's crash log in folder *d* (sets `_crash_file`) and
    return its path, or None if none could be opened.

    The previous non-empty crash.log becomes crash.prev.log first. If that
    fails (another running copy of the app holds crash.log open, as on
    2026-11-06), this session writes to crash-<pid>.log instead: crash
    logging used to be skipped entirely then, so a crash in the second
    copy left no record."""
    global _crash_file
    crash = os.path.join(d, CRASH_NAME)
    try:
        if os.path.exists(crash) and os.path.getsize(crash) > 0:
            os.replace(crash, os.path.join(d, 'crash.prev.log'))
    except OSError:
        crash = os.path.join(d, f'crash-{os.getpid()}.log')
    try:
        # Append, not 'w': the C stderr reopened onto the same file
        # (_route_c_stderr) appends too, and a 'w' handle would write over it.
        _crash_file = open(crash, 'a', encoding='utf-8')
    except OSError:
        _crash_file = None
        return None
    return crash


_crt_path: str | None = None   # which C runtime file the handlers went to


def _system_crt(part: str):
    """The C runtime that Python and Qt actually use, as a ctypes library
    (*part*: 'runtime' or 'stdio').

    Not ctypes.CDLL('ucrtbase'): the frozen build also ships its own
    _internal/ucrtbase.dll, and loading by that name picked the bundled
    copy, a second, separate C runtime. Python, faulthandler and Qt use
    the system one (they link through the api-ms-win-crt API sets), so a
    signal handler or stderr reopened in the bundled copy never saw
    anything (v0.50.35, 2026-11-06: two ucrtbase.dll loaded; a forced
    abort wrote no dump). Loading by the API-set name resolves to the same
    copy they use."""
    global _crt_path
    import ctypes
    lib = ctypes.CDLL(f'api-ms-win-crt-{part}-l1-1-0')
    if _crt_path is None:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(lib._handle), buf, 1024)
            _crt_path = buf.value
        except Exception:
            pass
    return lib


def _route_c_stderr(path: str) -> None:
    """Windowed Windows exe only: reopen the C runtime's stderr onto
    *path* (crash.log, appended to). Qt, shiboken and the C runtime print
    the reason for an abort there; without a console it went nowhere, and
    crash.log held only faulthandler's "Fatal Python error: Aborted" and
    the stacks (IOS17 open, v0.50.31, 2026-11-05). Redirecting file
    descriptor 2 is not enough: in a GUI process that stream has no
    descriptor at all, so it is reopened by name."""
    if sys.platform != 'win32' or sys.stderr is not None:
        return
    try:
        import ctypes
        ucrt = _system_crt('stdio')
        ucrt.__acrt_iob_func.restype = ctypes.c_void_p
        ucrt._wfreopen.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p)
        ucrt._wfreopen.restype = ctypes.c_void_p
        ucrt._wfreopen(path, 'a', ucrt.__acrt_iob_func(2))
    except Exception:
        pass


_abort_handler = None   # kept alive: the C runtime holds only a raw pointer


def _install_abort_dump(dump_path: str) -> None:
    """Windows only: when the process aborts, write a small minidump
    (thread stacks and the loaded-module list, no heap) to *dump_path*.

    Why: two aborts in v0.50.31/34 (2026-11-05/06) left only "Fatal Python
    error: Aborted" and the Python stacks; the GUI thread was at the first
    line of an ordinary slot, so the code that called abort() was native
    (Qt, the C++ runtime or an extension) and invisible to faulthandler.
    The dump's stack names it.

    faulthandler is enabled after this and passes SIGABRT on to the
    previous handler, so the order on an abort is: Python stacks into
    crash.log, then this dump, then exit (code 3, as before). Nothing runs
    unless the process is already aborting."""
    global _abort_handler
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        from ctypes import wintypes
        ucrt = _system_crt('runtime')
        k32 = ctypes.WinDLL('kernel32')
        dbghelp = ctypes.WinDLL('dbghelp')
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        dbghelp.MiniDumpWriteDump.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE,
                                              ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_void_p)
        handler_type = ctypes.CFUNCTYPE(None, ctypes.c_int)
        GENERIC_WRITE, CREATE_ALWAYS = 0x40000000, 2
        MINIDUMP_FLAGS = 0x1000   # MiniDumpWithThreadInfo; stacks only, no heap
        invalid = wintypes.HANDLE(-1).value

        def on_abort(_sig):
            try:
                h = k32.CreateFileW(dump_path, GENERIC_WRITE, 0, None, CREATE_ALWAYS, 0, None)
                if h and h != invalid:
                    ok = dbghelp.MiniDumpWriteDump(k32.GetCurrentProcess(),
                                                   k32.GetCurrentProcessId(), h,
                                                   MINIDUMP_FLAGS, None, None, None)
                    k32.CloseHandle(h)
                    if _crash_file is not None:
                        _crash_file.write(f"\nNative stack dump {'written to' if ok else 'FAILED:'} "
                                          f"{dump_path}\n")
                        _crash_file.flush()
            except Exception:
                pass
            ucrt._exit(3)

        _abort_handler = handler_type(on_abort)
        ucrt.signal.restype = ctypes.c_void_p
        ucrt.signal.argtypes = (ctypes.c_int, handler_type)
        SIGABRT = 22
        ucrt.signal(SIGABRT, _abort_handler)
    except Exception:
        _abort_handler = None


def _install_excepthooks() -> None:
    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            _logger.error('Uncaught exception:\n%s',
                          ''.join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        previous(exc_type, exc, tb)

    def _thread_hook(args):
        try:
            _logger.error('Uncaught exception in thread %s:\n%s',
                          getattr(args.thread, 'name', '?'),
                          ''.join(traceback.format_exception(
                              args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def _at_exit() -> None:
    try:
        repeated = {t: n - _REPEAT_LIMIT for t, n in _seen.items() if n > _REPEAT_LIMIT}
        for text, extra in sorted(repeated.items(), key=lambda kv: -kv[1])[:20]:
            _logger.info('repeated %s more times: %s', f'{extra:,}', text[:200])
        step('exiting')
        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
