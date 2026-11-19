"""freeze_log: a stretch with no beats is logged as one freeze with its
length and where the 'GUI' thread was (and what else was busy); regular
beats log nothing; nothing is logged after stop(); the exit watchdog ends a
process stuck after "exiting" and writes its stacks, and leaves crash.log
empty on a normal exit."""
import importlib
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

import freeze_log

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')


@pytest.fixture
def fl(monkeypatch):
    m = importlib.reload(freeze_log)
    monkeypatch.setattr(m, 'THRESHOLD_S', 0.3)
    monkeypatch.setattr(m, 'SAMPLE_S', 0.05)
    yield m
    m.stop()


def _beat_for(m, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        m.beat()
        time.sleep(0.02)


def _busy_gui_work(seconds):
    end = time.monotonic() + seconds
    x = 0
    while time.monotonic() < end:
        x += 1
    return x


def _run_as_gui(m, body):
    """Run *body* on a thread that start()s the logger (so it is the 'GUI
    thread'); returns when it ends."""
    t = threading.Thread(target=lambda: (m.start(None), body()))
    t.start()
    t.join(30)


def test_freeze_logged_with_stack(fl, caplog):
    caplog.set_level(logging.INFO, logger='ffs_explorer.startup')
    _run_as_gui(fl, lambda: (_beat_for(fl, 0.4), _busy_gui_work(0.9), _beat_for(fl, 0.4)))
    # the longest freeze: on a busy machine a short extra one can also be
    # logged, from this thread itself being scheduled late
    rec = max(fl.recent, key=lambda r: r['seconds'])
    assert 0.6 < rec['seconds'] < 1.5
    assert rec['samples'] >= 3
    assert rec['stack'][0][2] == '_busy_gui_work'
    assert 'GUI froze' in caplog.text and '_busy_gui_work' in caplog.text
    assert 'freezes over' in fl.summary()


def test_regular_beats_log_nothing(fl, monkeypatch):
    # 1 s, not 0.3 s: under CPU load the beating thread itself can be
    # scheduled a few hundred ms late (seen once in 12 loaded runs)
    monkeypatch.setattr(fl, 'THRESHOLD_S', 1.0)
    _run_as_gui(fl, lambda: _beat_for(fl, 1.0))
    assert fl.recent == [] and fl.summary() is None


def test_other_busy_thread_reported(fl):
    stop = threading.Event()

    def _competing_worker():
        while not stop.is_set():
            sum(range(1000))
    w = threading.Thread(target=_competing_worker, name='ffs-caseopen_0')
    w.start()
    try:
        _run_as_gui(fl, lambda: (_beat_for(fl, 0.2), _busy_gui_work(0.8), _beat_for(fl, 0.3)))
    finally:
        stop.set(); w.join()
    others = max(fl.recent, key=lambda r: r['seconds'])['others']
    assert any(name == 'ffs-caseopen_0' and any(fn == '_competing_worker' for _f, _n, fn in st)
               for name, st, _k in others)


def test_nothing_after_stop(fl):
    _run_as_gui(fl, lambda: (_beat_for(fl, 0.2), fl.stop(), time.sleep(0.8)))
    assert fl.recent == []


def test_event_loop_stack_recognised():
    assert freeze_log._in_event_loop((('ffs-explorer.py', 18083, 'main'), ('ffs-explorer.py', 18090, '<module>')))
    assert not freeze_log._in_event_loop((('x.py', 1, 'reload_tree'), ('ffs-explorer.py', 1, 'main')))


_CHILD = r"""
import sys, time
sys.path.insert(0, {app!r})
import freeze_log
f = open({crash!r}, 'a')
freeze_log.arm_exit_watchdog(f, {secs})
if {hang}:
    time.sleep(30)
"""


@pytest.mark.parametrize('hang', [True, False])
def test_exit_watchdog(tmp_path, hang):
    crash = str(tmp_path / 'crash.log')
    code = _CHILD.format(app=APP, crash=crash, secs=1.0, hang=hang)
    t = time.monotonic()
    p = subprocess.run([sys.executable, '-c', code], timeout=20)
    took = time.monotonic() - t
    text = open(crash).read()
    if hang:
        assert p.returncode != 0 and took < 10
        assert 'Timeout' in text and '<module>' in text
    else:
        assert p.returncode == 0 and text == ''


def test_beat_arms_nothing(fl, monkeypatch):
    """beat() only records the time: until 2026-11-19 it re-armed
    faulthandler.dump_traceback_later every second on the GUI thread
    (Windows crashes in beat() after long freezes)."""
    import faulthandler
    calls = []
    monkeypatch.setattr(faulthandler, 'dump_traceback_later', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(faulthandler, 'dump_traceback', lambda *a, **k: calls.append(a))
    _run_as_gui(fl, lambda: _beat_for(fl, 1.5))
    assert calls == []


def test_long_freeze_dumps_every_thread_once(fl, monkeypatch, tmp_path):
    monkeypatch.setattr(fl, 'HARD_S', 0.8)
    crash = open(tmp_path / 'crash.log', 'w+')
    t = threading.Thread(target=lambda: (fl.start(crash), _beat_for(fl, 0.3),
                                         _busy_gui_work(1.6), _beat_for(fl, 0.4)))
    t.start()
    t.join(30)
    crash.flush()
    text = open(tmp_path / 'crash.log').read()
    assert text.count('GUI not responding for') == 1
    assert '_busy_gui_work' in text and 'Thread 0x' in text
    rec = max(fl.recent, key=lambda r: r['seconds'])
    assert rec['seconds'] > 1.2
