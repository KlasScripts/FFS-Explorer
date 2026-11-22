"""Background child processes (the first-open parse, the zip index build,
the iOS system check): they run below normal priority, so the window is
always served first when the CPUs are busy (on a 4-CPU test machine the
window still stuttered up to 2 s while the index process ran at full
speed, 2026-11-22; with nothing else wanting the CPU it changes nothing),
and end at once when their work is saved (end_now). Qt-free."""
import os
import sys


def lower_this_process() -> None:
    """Below-normal priority for the calling process. Never raises."""
    try:
        if sys.platform == 'win32':
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            kernel32 = ctypes.windll.kernel32
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(5)
    except Exception:
        pass


def end_now(code: int = 0) -> None:
    """End this child process at once, its work saved and reported: Python's
    normal exit frees every object first, which for the parse child's
    2-million-entry structures took most of the 10 s the window waits for it
    (proc.join after 'done', 2026-11-22). Logs are flushed first."""
    try:
        import logging
        logging.shutdown()
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)

