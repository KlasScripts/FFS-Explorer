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
    try:
        crash = os.path.join(d, CRASH_NAME)
        if os.path.exists(crash) and os.path.getsize(crash) > 0:
            os.replace(crash, os.path.join(d, 'crash.prev.log'))
        _crash_file = open(crash, 'w', encoding='utf-8')
        faulthandler.enable(file=_crash_file, all_threads=True)
        faulthandler.dump_traceback_later(WATCHDOG_SECONDS, repeat=False, file=_crash_file)
    except Exception:
        _crash_file = None
    _install_excepthooks()
    atexit.register(_at_exit)
    _logger.info('=' * 60)
    _logger.info('FFS Explorer starting  pid=%s', os.getpid())
    _logger.info('%s', _environment())
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
        return (f"{platform.platform()} | python {platform.python_version()} | "
                f"{'frozen build ' + built if frozen else 'source run'} | {exe}")
    except Exception:
        return 'environment unavailable'


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
