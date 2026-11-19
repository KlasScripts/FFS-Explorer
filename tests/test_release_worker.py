"""A QThread's result signal is sent from inside run(), so the thread is
often still running when its handler drops the last reference; Python then
frees it and Qt aborts ("QThread: Destroyed while thread '' is still
running": Windows, LevelDB indexing, 2026-11-19). FastZipBrowser.
_release_worker keeps it until finished. Run in a child process so an abort
is an exit code, with a control showing the old way aborts."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD = r'''
import gc, importlib.util, os, sys, time
sys.path.insert(0, {root!r}); sys.path.insert(0, os.path.join({root!r}, 'app'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QThread, Signal, QTimer, QEventLoop
app = QCoreApplication([])
spec = importlib.util.spec_from_file_location('m', os.path.join({root!r}, 'ffs-explorer.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class W(QThread):
    done = Signal()
    def run(self):
        self.done.emit()
        time.sleep(0.5)          # still running after the result is out

class Holder:
    _retired_workers = []
    _release_worker = m.FastZipBrowser._release_worker
    _reap_retired_worker = m.FastZipBrowser._reap_retired_worker

h = Holder(); h.w = W()
def on_done():
    if {use_release}:
        h._release_worker(h.w)
    h.w = None
    gc.collect()
h.w.done.connect(on_done)
h.w.start()
loop = QEventLoop(); QTimer.singleShot(1500, loop.quit); loop.exec()
gc.collect()
print('reaped' if not h._retired_workers else 'not reaped', flush=True)
'''


def _run(use_release):
    code = _CHILD.format(root=ROOT, use_release=use_release)
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)


def test_release_keeps_the_thread_until_it_ends():
    p = _run(True)
    assert p.returncode == 0 and 'reaped' in p.stdout, p.stderr[-500:]


def test_dropping_it_the_old_way_aborts():
    p = _run(False)
    assert p.returncode != 0 or 'Destroyed while thread' in p.stderr
