"""Background child processes (the first-open parse, the zip index build,
the iOS system check) run below normal priority, so the window is always
served first when the CPUs are busy. On a 4-CPU test machine the window
still stuttered up to 2 s while the index process ran at full speed
(2026-11-22). When nothing else wants the CPU it changes nothing, so the
work is not slower. Qt-free."""
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
