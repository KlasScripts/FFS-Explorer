"""Work the window waits for, or does itself, stays short (2026-11-21):
the search-index load stops when asked, the hex reader stops between
chunks instead of being killed, the last search hits are added in slices,
and a report sort runs off the window thread."""
import os
import time
from contextlib import closing

import msgpack
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def test_search_index_load_stops_when_asked(tmp_path):
    import keyword_search as ks
    from db_utils import _open_cache_db, save_blob
    entries = [(f'f/{i}.bin', i * 10, 10) for i in range(200_000)]
    with closing(_open_cache_db(str(tmp_path))) as db:
        save_blob(db, 'search_entries', ks._SEARCH_ENTRIES_VERSION,
                  msgpack.packb(entries, use_bin_type=True))
    w = ks.SearchIndexWorker('unused.zip', case_dir=str(tmp_path))
    assert len(w._load_from_db()) == 200_000
    w.stop()
    assert w._load_from_db() is None             # stopped at the first 20k check
    got = []
    w.entries_ready.connect(got.append)
    w.run()
    assert got == []                             # nothing handed to the window


def test_hex_reader_is_set_aside_not_waited_for():
    from hex_viewer import HexViewerMixin

    class Worker:
        def isRunning(self):
            return True

        def wait(self):
            raise AssertionError('the window waited for the hex reader')

        def terminate(self):
            raise AssertionError('the hex reader was killed')

    class Window(HexViewerMixin):
        def __init__(self):
            self.retired = []
            self._hex_worker = Worker()

        def _retire_worker(self, w):
            self.retired.append(w)

    win = Window()
    old = win._hex_worker
    win._stop_hex_worker()
    assert win.retired == [old] and win._hex_worker is None


def test_case_open_never_terminates_a_thread():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'ffs-explorer.py'), encoding='utf-8').read()
    assert '_hex_worker.terminate()' not in src


def test_last_hits_go_in_in_slices_then_the_search_finishes():
    """Each slice of _flush_live_hits gives the window back within about
    _FLUSH_SLICE_S; the finish step runs once, after the last hit."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import keyword_search as ks

    class View:
        def setUpdatesEnabled(self, v):
            pass

    class Win:
        _FLUSH_SLICE_S = ks.KeywordSearchMixin._FLUSH_SLICE_S
        for _n in ('_flush_live_hits', '_after_hits_added', '_run_after_hits_added',
                   '_on_db_loader_rows'):
            locals()[_n] = getattr(ks.KeywordSearchMixin, _n)

        def __init__(self):
            self.search_results_view = View()
            self._live_hit_buffer = []
            self._live_hit_flush_scheduled = False
            self._after_hits_added_cbs = []
            self.added = 0
            self.slices = []

        def _search_add_hit(self, *a, **k):
            self.added += 1
            t = time.perf_counter()       # a hit costs a little window time
            while time.perf_counter() - t < 0.00002:
                pass

        def _search_apply_counts(self):
            pass

    w = Win()
    finished = []
    for start in range(0, 60_000, 200):     # the saved-search loader's batches
        w._on_db_loader_rows([(f'f{i}', i, 'ctx') for i in range(start, start + 200)])
    w._after_hits_added(lambda: finished.append(w.added))
    assert w.added == 0 and finished == []  # nothing added while the batches arrived
    longest, deadline = 0.0, time.monotonic() + 60
    while not finished and time.monotonic() < deadline:
        t = time.perf_counter()
        app.processEvents()
        longest = max(longest, time.perf_counter() - t)
    assert finished == [60_000]
    assert longest < 0.25, f'one slice held the window {longest:.2f} s'


def _report_model(tmp_path, n=200_000):
    import random
    import sqlite3
    from artifact_viewer import ArtifactTableModel
    db = str(tmp_path / 'caseresults.db')
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE "artifact_x" (message TEXT, ts INTEGER)')
    rnd = random.Random(1)
    c.executemany('INSERT INTO "artifact_x" VALUES (?,?)',
                  ((f'm{rnd.randrange(10**9):09d}', i) for i in range(n)))
    c.commit()
    c.close()
    m = ArtifactTableModel()
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    ids = [r[0] for r in conn.execute('SELECT rowid FROM "artifact_x"')]
    m.load_from_db(conn, 'artifact_x', ['message', 'ts'], ids)
    return m, db


def _wait(app, cond, secs=60):
    deadline = time.monotonic() + secs
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def test_report_sort_runs_off_the_window_thread_and_keeps_the_selection(tmp_path):
    import sqlite3
    from PySide6.QtCore import Qt, QItemSelectionModel
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    m, db = _report_model(tmp_path)
    sel = QItemSelectionModel(m)
    picked = m._rowids[12_345]
    sel.setCurrentIndex(m.index(12_345, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect)
    busy = []
    m.sort_busy.connect(busy.append)
    t = time.perf_counter()
    m.sort(0, Qt.SortOrder.AscendingOrder)
    assert time.perf_counter() - t < 0.1          # the window is not held
    assert m._rowids[12_345] == picked            # old order until it is done
    _wait(app, lambda: busy == [True, False])
    want = [r[0] for r in sqlite3.connect(db).execute(
        'SELECT rowid FROM "artifact_x" ORDER BY "message" ASC')]
    assert m._rowids == want
    cur = sel.currentIndex()
    assert cur.isValid() and m._rowids[cur.row()] == picked   # still on the same record


def test_a_filter_during_a_sort_wins(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    m, _db = _report_model(tmp_path, n=100_000)
    m.sort(0, Qt.SortOrder.DescendingOrder)
    m.apply_rowids([5, 3, 1])                     # a filter result lands first
    _wait(app, lambda: not m._sort_workers)
    app.processEvents()
    assert m._rowids == [5, 3, 1]


def test_file_browser_lists_are_kept_until_its_rows_change():
    """Switching to the Media Browser checked every File Browser row each
    time: 10 s on 2.19 million rows (2026-11-21). The list is now kept by
    the model until its rows change (added, filtered, sorted)."""
    import importlib.util
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location('ffs_main_pauses', os.path.join(root, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    model = m.FileTableModel(['Name', 'Size (Bytes)'])
    model.append_rows_batch([([f'f{i:06d}.jpg', str(i)], f'd/f{i:06d}.jpg', -1) for i in range(50_000)])
    calls = []

    def compute():
        calls.append(1)
        return tuple(r[1] for r in model._rows)
    first = model.cached('media', 'k', compute)
    again = model.cached('media', 'k', compute)
    assert again is first and len(calls) == 1                 # reused while unchanged
    assert model.row_of_path('d/f049999.jpg') == 49_999
    model.sort(1, Qt.SortOrder.DescendingOrder)                # rows reordered
    assert model.cached('media', 'k', compute)[0] == 'd/f049999.jpg' and len(calls) == 2
    assert model.row_of_path('d/f049999.jpg') == 0
    assert model.cached('media', 'other key', compute) and len(calls) == 3
    model.set_filter('')                                       # rows replaced
    assert model.cached('media', 'other key', compute) and len(calls) == 4


def test_media_flags_and_path_list_kept_as_rows_arrive():
    """The media list and the path list are kept as rows come in, so a
    tab switch or a selection only reads them; a sort rebuilds them once;
    nothing is counted twice."""
    import importlib.util
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location('ffs_main_flags', os.path.join(root, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    model = m.FileTableModel(['Name', 'Size (Bytes)'])
    model.append_rows_batch([(['dir', '0'], 'd', 3)])                      # a folder, before the flagger
    model.set_row_flagger(lambda r: r[2] < 0 and r[1].endswith('.jpg'), 'k')
    for start in range(0, 3000, 500):
        model.append_rows_batch([([f'f{i}', '1'], f'd/f{i:04d}.{"jpg" if i % 3 == 0 else "txt"}', -1)
                                 for i in range(start, start + 500)])
    media, files = model.flagged_rows()
    assert files == 3000 and len(media) == 1000 and media[0] == 'd/f0000.jpg'
    assert model.row_of_path('d/f2999.txt') == 3000 and len(model._path_list) == 3001
    model.sort(1, Qt.SortOrder.DescendingOrder)
    media2, files2 = model.flagged_rows()
    assert files2 == 3000 and sorted(media2) == sorted(media)
    assert list(media2) == [r[1] for r in model._rows if r[1].endswith('.jpg')]   # in the new order
    assert model.row_of_path('d/f2997.jpg') == [r[1] for r in model._rows].index('d/f2997.jpg')
    model.set_filter('')                                                   # rows put back
    assert model.flagged_rows()[1] == 3000 and len(model._path_list) == 3001
