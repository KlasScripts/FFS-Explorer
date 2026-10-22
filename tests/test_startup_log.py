"""startup_log: what it records, the stall snapshot, and the size limits.
Each case runs in a fresh interpreter with HOME/LOCALAPPDATA pointed at a temp
folder, so faulthandler/excepthook state never leaks between tests and nothing
is written to the real user log folder."""
import os
import subprocess
import sys
import textwrap

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')


def _run(tmp_path, body: str, timeout=60):
    env = dict(os.environ, HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path),
               XDG_STATE_HOME=str(tmp_path))
    code = f"import sys; sys.path.insert(0, {APP!r})\nimport startup_log\n" + textwrap.dedent(body)
    subprocess.run([sys.executable, '-c', code], env=env, timeout=timeout,
                   capture_output=True)
    sys.path.insert(0, APP)
    import startup_log
    env_backup = dict(os.environ)
    try:
        os.environ.update(HOME=str(tmp_path), LOCALAPPDATA=str(tmp_path),
                          XDG_STATE_HOME=str(tmp_path))
        return startup_log.log_dir()
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def _read(d, name):
    with open(os.path.join(d, name), encoding='utf-8') as f:
        return f.read()


def test_steps_and_uncaught_errors_are_logged(tmp_path):
    d = _run(tmp_path, """
        import threading
        startup_log.setup()
        startup_log.step('building main window')
        t = threading.Thread(target=lambda: 1 / 0, name='worker-x'); t.start(); t.join()
        startup_log.window_shown()
        raise RuntimeError('boom at startup')
    """)
    log = _read(d, startup_log_name())
    assert 'FFS Explorer starting' in log
    assert 'building main window' in log
    assert 'Uncaught exception in thread worker-x' in log and 'ZeroDivisionError' in log
    assert 'RuntimeError: boom at startup' in log
    assert 'exiting' in log


def test_stall_writes_stack_snapshot(tmp_path):
    d = _run(tmp_path, """
        import time
        startup_log.WATCHDOG_SECONDS = 1
        startup_log.setup()
        def stuck_building_window():
            time.sleep(3)
        stuck_building_window()
    """)
    crash = _read(d, 'crash.log')
    assert 'stuck_building_window' in crash          # where the main thread was


def test_previous_crash_snapshot_kept_once(tmp_path):
    for _ in range(2):
        d = _run(tmp_path, """
            import time
            startup_log.WATCHDOG_SECONDS = 1
            startup_log.setup()
            time.sleep(2)
        """)
    assert os.path.getsize(os.path.join(d, 'crash.prev.log')) > 0
    assert sorted(f for f in os.listdir(d) if f.startswith('crash')) == ['crash.log', 'crash.prev.log']


def test_repeats_collapsed_and_file_rotates(tmp_path):
    d = _run(tmp_path, """
        startup_log.setup()
        for _ in range(10_000):
            startup_log.message('warning', 'Qt warning: same thing again')
        for i in range(20_000):
            startup_log.message('warning', f'distinct message {i:06d} ' + 'x' * 120)
    """, timeout=120)
    log = _read(d, startup_log_name())
    assert log.count('same thing again') <= 6           # 5 writes + the exit summary
    assert 'repeated 9,995 more times' in log or os.path.exists(os.path.join(d, startup_log_name() + '.1'))
    files = [f for f in os.listdir(d) if f.startswith('ffs-explorer.log')]
    assert len(files) <= 4                               # current + 3 backups
    assert all(os.path.getsize(os.path.join(d, f)) <= 1_100_000 for f in files)


def startup_log_name():
    sys.path.insert(0, APP)
    import startup_log
    return startup_log.LOG_NAME
