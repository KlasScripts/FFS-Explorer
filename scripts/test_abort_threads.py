"""Windows only: does startup_log's crash dump work when abort() comes from
a thread other than the GUI thread?

usage: venv\\Scripts\\python scripts\\test_abort_threads.py

startup_log's abort handler is Python code (a ctypes callback), so it
needs the GIL before it can write crash.dmp. The forced-abort tests so far
aborted on the main thread, which already holds the GIL. Here each
scenario runs in a fresh child process (LOCALAPPDATA pointed at a temp
folder, so the real log folder is untouched) and reports exit code, time
taken, and what reached crash.log / crash.dmp:

  main          abort() on the main thread (the case already tested)
  native_idle   abort() on a native thread-pool thread (no Python thread
                state), main thread idle in time.sleep (GIL free)
  native_busy   same, but the main thread holds the GIL for 8 s in a C
                call that never releases it
  worker_busy   abort() on a Python thread inside a GIL-releasing C call
                (like a worker inside zlib/Qt/ccl), main holds the GIL 8 s
  native_stuck  native thread aborts, main holds the GIL and never lets
                go (20 s timeout here): shows whether it hangs

Expected if all is well: exit code 3 every time, crash.dmp written,
"Native stack dump written to" in crash.log. *_busy should take about 8 s
(the handler waits for the GIL). native_stuck is the open question.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'app')

CHILD = r'''
import sys, time, ctypes, threading
sys.path.insert(0, APP)
import startup_log
startup_log.setup()
import faulthandler; faulthandler.cancel_dump_traceback_later()
crt = startup_log._system_crt('runtime')
k32 = ctypes.WinDLL('kernel32')
k32_gil = ctypes.PyDLL('kernel32')          # PyDLL: the GIL is HELD during the call
abort_addr = ctypes.cast(crt.abort, ctypes.c_void_p).value

def native_abort_in(ms):
    # A timer-queue callback runs on a Windows thread-pool thread that has
    # never run Python. abort() ignores the callback's two arguments.
    h = ctypes.c_void_p()
    k32.CreateTimerQueueTimer.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    assert k32.CreateTimerQueueTimer(ctypes.byref(h), None, abort_addr, None, ms, 0, 0)

def hold_gil(ms):
    k32_gil.Sleep(ms)

print('ready', flush=True)
if SCENARIO == 'main':
    crt.abort()
elif SCENARIO == 'native_idle':
    native_abort_in(500); time.sleep(30)
elif SCENARIO == 'native_busy':
    native_abort_in(500); hold_gil(8000); time.sleep(30)
elif SCENARIO == 'worker_busy':
    def worker():
        k32.Sleep(500)          # GIL released (WinDLL), like a C extension call
        crt.abort()             # CDLL: called with the GIL released
    threading.Thread(target=worker, name='worker-abort').start()
    time.sleep(0.05); hold_gil(8000); time.sleep(30)
elif SCENARIO == 'native_stuck':
    native_abort_in(500); hold_gil(0xFFFFFFFF)
print('NOT ABORTED', flush=True)
'''

SCENARIOS = ['main', 'native_idle', 'native_busy', 'worker_busy', 'native_stuck']


def run(scenario: str) -> dict:
    tmp = tempfile.mkdtemp(prefix=f'abort_{scenario}_')
    env = dict(os.environ, LOCALAPPDATA=tmp)
    code = f'APP = {APP!r}\nSCENARIO = {scenario!r}\n' + CHILD
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, '-c', code], env=env, timeout=20,
                           capture_output=True, text=True)
        rc, out = p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired as e:
        def _s(x):
            return x.decode(errors='replace') if isinstance(x, bytes) else (x or '')
        rc, out = 'HUNG (killed after 20 s)', (_s(e.stdout) + _s(e.stderr)).strip()
    secs = time.monotonic() - t0
    logs = os.path.join(tmp, 'FFS Explorer', 'logs')
    crash_txt = ''
    try:
        with open(os.path.join(logs, 'crash.log'), encoding='utf-8', errors='replace') as f:
            crash_txt = f.read()
    except OSError:
        pass
    dmp = os.path.join(logs, 'crash.dmp')
    dump_size = os.path.getsize(dmp) if os.path.exists(dmp) else 0
    reader = ''
    if dump_size:
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'read_crash_dump.py'), dmp],
                           capture_output=True, text=True, timeout=120)
        reader = '\n'.join((r.stdout or r.stderr).splitlines()[:12])
    return dict(scenario=scenario, rc=rc, secs=secs, out=out, dump_size=dump_size,
                py_stacks='Fatal Python error' in crash_txt or 'Current thread' in crash_txt,
                dump_line='Native stack dump written to' in crash_txt,
                folder=logs, reader=reader)


def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows only (the abort dump is installed only on win32).')
    only = sys.argv[1:] or SCENARIOS
    for s in only:
        r = run(s)
        print(f"=== {s}: exit {r['rc']}, {r['secs']:.1f} s, "
              f"Python stacks in crash.log: {r['py_stacks']}, "
              f"'dump written' line: {r['dump_line']}, crash.dmp: {r['dump_size']:,} bytes")
        if r['out']:
            print('   child output:', r['out'][:300].replace('\n', ' | '))
        if r['reader']:
            print(textwrap.indent(r['reader'], '   '))
        print('   logs:', r['folder'])
        print()


if __name__ == '__main__':
    main()
