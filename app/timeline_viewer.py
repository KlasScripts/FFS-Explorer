"""timeline_viewer.py — TimelineViewerMixin: the Timeline centre tab.

One table of timed events across the artifact reports (built by
timeline.py into casecache.db), with what each time means, the source's
known catch, and a link back to the report row. "Show activity around this
time" from a report row (or a Timeline row) sets a time window here.

Nothing is corrected: times are as each source stored them.
"""
import re
from contextlib import closing

from PySide6.QtCore import (Qt, QAbstractTableModel, QModelIndex, QThread, Signal,
                            QDateTime, QTimeZone)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableView, QComboBox, QLineEdit, QCheckBox, QSpinBox,
                               QDateTimeEdit, QMenu, QDialog, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView)

import timeline
from db_utils import _open_cache_db, _open_results_db

TIMELINE_TAB_INDEX = 4

_HEADERS = ["Time", "What this time means", "Source", "App", "Event", "Detail",
            "Record", "Caution", "Raw value"]
_IDX = {c: i for i, c in enumerate(timeline.COLUMNS)}
_HMS = re.compile(r'(\d{2}:\d{2}:\d{2})')


class TimelineBuildWorker(QThread):
    """Rebuilds the event table if anything it is built from changed. Owns
    its own database connections (a worker never shares the GUI's)."""
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, case_dir: str, modules: dict, force: bool = False):
        super().__init__()
        self.case_dir = case_dir
        self._case_dir = case_dir
        self._modules = modules
        self._force = force

    def run(self):
        try:
            with closing(_open_results_db(self._case_dir)) as res, \
                    closing(_open_cache_db(self._case_dir)) as cache:
                fp = timeline.fingerprint(res, self._modules)
                if not self._force and timeline.stored_fingerprint(cache) == fp:
                    self.done.emit({'rebuilt': False})
                    return
                out = timeline.build(res, cache, self._modules,
                                     cancelled=self.isInterruptionRequested)
                out['rebuilt'] = True
                self.done.emit(out)
        except Exception as exc:     # shown to the examiner, never swallowed
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class TimelineTableModel(QAbstractTableModel):
    def __init__(self, fmt_ms):
        super().__init__()
        self._rows = []
        self._fmt_ms = fmt_ms

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row(self, r):
        return self._rows[r]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        if role == Qt.ItemDataRole.DisplayRole:
            return str(section + 1)
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = _HEADERS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == "Time":
                return self._fmt_ms(r[_IDX['utc_ms']])
            if col == "Raw value":
                return f"{r[_IDX['raw']]} ({r[_IDX['unit']]}) · {r[_IDX['field']]}"
            key = {"What this time means": 'meaning', "Source": 'source', "App": 'app',
                   "Event": 'event', "Detail": 'detail', "Record": 'record',
                   "Caution": 'caution'}[col]
            return r[_IDX[key]] or ''
        if role == Qt.ItemDataRole.ToolTipRole and col in ("Detail", "Caution", "Record",
                                                           "What this time means"):
            return self.data(index) or None
        if role == Qt.ItemDataRole.ForegroundRole and col == "Record" and r[_IDX['record']]:
            from PySide6.QtGui import QColor
            return QColor('#c0392b')
        return None


class TimelineViewerMixin:
    """Mixed into FastZipBrowser (ffs-explorer.py)."""

    # ── setup ────────────────────────────────────────────────────────────
    def _setup_timeline_tab(self) -> QWidget:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        top = QHBoxLayout()
        self._tl_status = QLabel("Open a case to build the Timeline.")
        top.addWidget(self._tl_status)
        top.addStretch()
        self._tl_sources_btn = QPushButton("Sources…")
        self._tl_sources_btn.setToolTip(
            "Which reports are on the Timeline, which have times whose meaning is "
            "not written yet, and how many values were blank or not a time")
        self._tl_sources_btn.clicked.connect(self._timeline_show_sources)
        top.addWidget(self._tl_sources_btn)
        self._tl_rebuild_btn = QPushButton("Rebuild")
        self._tl_rebuild_btn.setToolTip("Build the Timeline again from the reports")
        self._tl_rebuild_btn.clicked.connect(lambda: self._timeline_ensure_built(force=True))
        top.addWidget(self._tl_rebuild_btn)
        lay.addLayout(top)

        bar = QHBoxLayout()
        self._tl_window_check = QCheckBox("Only between (UTC)")
        self._tl_window_check.toggled.connect(self._timeline_apply)
        bar.addWidget(self._tl_window_check)
        utc = QTimeZone.utc()
        self._tl_from = QDateTimeEdit()
        self._tl_to = QDateTimeEdit()
        for w in (self._tl_from, self._tl_to):
            w.setTimeZone(utc)
            w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            w.setCalendarPopup(True)
            w.editingFinished.connect(self._timeline_apply)
        bar.addWidget(self._tl_from)
        bar.addWidget(QLabel("and"))
        bar.addWidget(self._tl_to)
        bar.addWidget(QLabel("  Around: ±"))
        self._tl_around = QSpinBox()
        self._tl_around.setRange(1, 24 * 60)
        self._tl_around.setValue(5)
        self._tl_around.setSuffix(" min")
        self._tl_around.setToolTip("Width of \"Show activity around this time\"")
        bar.addWidget(self._tl_around)
        bar.addWidget(QLabel("  Source:"))
        self._tl_source = QComboBox()
        self._tl_source.currentIndexChanged.connect(self._timeline_apply)
        bar.addWidget(self._tl_source)
        bar.addWidget(QLabel("App:"))
        self._tl_app = QComboBox()
        self._tl_app.setMinimumContentsLength(18)
        self._tl_app.currentIndexChanged.connect(self._timeline_apply)
        bar.addWidget(self._tl_app)
        self._tl_text = QLineEdit()
        self._tl_text.setPlaceholderText("Filter text…")
        self._tl_text.setClearButtonEnabled(True)
        self._tl_text.returnPressed.connect(self._timeline_apply)
        self._tl_text.textChanged.connect(lambda t: self._timeline_apply() if not t else None)
        bar.addWidget(self._tl_text, 1)
        self._tl_desc = QCheckBox("Newest first")
        self._tl_desc.toggled.connect(self._timeline_apply)
        bar.addWidget(self._tl_desc)
        lay.addLayout(bar)

        self._tl_around_label = QLabel("")
        self._tl_around_label.setStyleSheet("color: #e07b00;")
        self._tl_around_label.setVisible(False)
        lay.addWidget(self._tl_around_label)

        note = QLabel(
            "Times are as each source stored them; nothing is corrected. "
            "Only reports whose times have a written meaning are listed (see Sources…). "
            "Caution gives the source's known catch. Double-click a row to open it in its report.")
        note.setWordWrap(True)
        note.setStyleSheet("color: grey;")
        lay.addWidget(note)

        self._tl_model = TimelineTableModel(self._timeline_fmt_ms)
        self._tl_view = QTableView()
        self._tl_view.setModel(self._tl_model)
        self._tl_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._tl_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tl_view.setWordWrap(False)
        self._tl_view.verticalHeader().setDefaultSectionSize(22)
        self._tl_view.horizontalHeader().setStretchLastSection(True)
        self._tl_view.doubleClicked.connect(self._timeline_open_row)
        self._tl_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tl_view.customContextMenuRequested.connect(self._timeline_context_menu)
        for i, w in enumerate((235, 300, 190, 190, 170, 320, 120, 300)):
            self._tl_view.setColumnWidth(i, w)
        lay.addWidget(self._tl_view, 1)

        self._tl_worker = None
        self._tl_after_build = []
        self._tl_built_for = None
        return tab

    # ── formatting ───────────────────────────────────────────────────────
    def _timeline_fmt_ms(self, utc_ms) -> str:
        """The case's time display (UTC / handset / ...) with milliseconds,
        so events in the same second keep a visible order."""
        try:
            utc_ms = int(utc_ms)
        except (TypeError, ValueError):
            return ''
        secs, ms = divmod(utc_ms, 1000)
        text = self.format_ts(secs) if secs else self.format_ts(0.001)
        return _HMS.sub(lambda m: f"{m.group(1)}.{ms:03d}", text, count=1)

    # ── case lifecycle ───────────────────────────────────────────────────
    def _refresh_timeline_tab(self) -> None:
        """Case opened or reports re-run: the Timeline is rebuilt (if its
        inputs changed) the next time it is shown."""
        if not hasattr(self, '_tl_model'):
            return
        self._tl_built_for = None
        # Never leave another case's (or an older run's) rows on screen: a
        # double-click would open a row number in the wrong report.
        self._tl_model.set_rows([])
        self._tl_marker = None
        self._tl_around_label.setVisible(False)
        self._tl_status.setText("The Timeline is rebuilt from the reports when you open this tab.")
        if self.center_tabs.currentIndex() == TIMELINE_TAB_INDEX:
            self._timeline_ensure_built()

    def _timeline_on_shown(self) -> None:
        if self._tl_built_for != self._case_dir:
            self._timeline_ensure_built()

    def _timeline_modules(self) -> dict:
        from artifact_runner import list_artifacts
        platform = 'android' if self._is_android_archive() else 'ios'
        return dict(list_artifacts(platform))

    def _timeline_ensure_built(self, force: bool = False, then=None) -> None:
        """Start the build worker (a no-op build if nothing changed); *then*
        runs after it finishes."""
        if then is not None:
            self._tl_after_build.append(then)
        if not self._case_dir:
            self._tl_status.setText("Open a case to build the Timeline.")
            return
        if self._tl_worker is not None and self._tl_worker.isRunning():
            return
        self._tl_status.setText("Building the Timeline from the reports…")
        self._tl_rebuild_btn.setEnabled(False)
        worker = TimelineBuildWorker(self._case_dir, self._timeline_modules(), force)
        worker.done.connect(self._timeline_on_built)
        worker.failed.connect(self._timeline_on_failed)
        # Kept referenced until the thread has really finished (Qt aborts
        # if a running QThread is garbage-collected).
        self._tl_old_workers = [w for w in getattr(self, '_tl_old_workers', [])
                                if w.isRunning()] + [worker]
        self._tl_worker = worker
        worker.start()

    def _timeline_on_built(self, out: dict) -> None:
        worker, self._tl_worker = self._tl_worker, None
        self._tl_rebuild_btn.setEnabled(True)
        if out.get('cancelled'):
            return
        if worker is not None and worker.case_dir != self._case_dir:
            # Built for a case that is no longer open: build this one.
            self._timeline_ensure_built()
            return
        self._tl_built_for = self._case_dir
        self._timeline_fill_combos()
        self._timeline_load()
        pending, self._tl_after_build = self._tl_after_build, []
        for fn in pending:
            fn()

    def _timeline_on_failed(self, msg: str) -> None:
        self._tl_worker = None
        self._tl_rebuild_btn.setEnabled(True)
        self._tl_after_build = []
        self._tl_status.setText(f"The Timeline could not be built: {msg}")

    def _timeline_stop_worker(self) -> None:
        for w in getattr(self, '_tl_old_workers', []):
            if w.isRunning():
                w.requestInterruption()
                w.wait(5000)

    # ── filters and query ────────────────────────────────────────────────
    def _timeline_fill_combos(self) -> None:
        with closing(_open_cache_db(self._case_dir)) as cache:
            sources = timeline.source_list(cache)
            apps = timeline.apps(cache)
        on = sorted({(s[1], s[0]) for s in sources
                     if s[3] in timeline.HAS_EVENTS and s[4]})
        for combo, items in ((self._tl_source, [(lbl, sn) for lbl, sn in on]),
                             (self._tl_app, [(a, a) for a in apps])):
            keep = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All", None)
            for text, data in items:
                combo.addItem(text, data)
            i = combo.findData(keep) if keep is not None else 0
            combo.setCurrentIndex(max(i, 0))
            combo.blockSignals(False)

    def _timeline_window_ms(self):
        if not self._tl_window_check.isChecked():
            return None, None
        return (self._tl_from.dateTime().toMSecsSinceEpoch(),
                self._tl_to.dateTime().toMSecsSinceEpoch())

    def _timeline_apply(self, *_a) -> None:
        """A filter was changed by hand: a "went to" / "around" note no
        longer describes what is listed, so it is cleared."""
        self._tl_marker = None
        self._tl_around_label.setVisible(False)
        self._timeline_load()

    def _timeline_load(self, center_ms=None) -> None:
        """Query and show the events the filters select. With *center_ms*
        and more matches than the cap, the rows shown are centred on that
        time (and the status says which part of the list it is)."""
        if not getattr(self, '_case_dir', None) or self._tl_built_for != self._case_dir:
            return
        start, end = self._timeline_window_ms()
        script = self._tl_source.currentData()
        scripts = [script] if script else None
        app = self._tl_app.currentData()
        text = self._tl_text.text().strip() or None
        desc = self._tl_desc.isChecked()
        limit = 100_000
        offset = 0
        with closing(_open_cache_db(self._case_dir)) as cache:
            total = timeline.count(cache, start, end, scripts, app, text)
            if center_ms is not None and total > limit:
                before = timeline.count_before(cache, center_ms, start, end, scripts,
                                               app, text, descending=desc)
                offset = max(0, min(before - limit // 2, total - limit))
            rows, total = timeline.query(cache, start, end, scripts, app, text,
                                         descending=desc, limit=limit, offset=offset)
            n_all = cache.execute('SELECT COUNT(*) FROM timeline_events').fetchone()[0]
        self._tl_model.set_rows(rows)
        if total > len(rows):
            # Never silently capped (A-51).
            if offset:
                status = (f"Showing events {offset + 1:,}–{offset + len(rows):,} of {total:,} "
                          f"(the part around the time you went to) — narrow the time "
                          f"window or filters to see the rest.")
            else:
                status = (f"Showing the first {len(rows):,} of {total:,} matching events — "
                          f"narrow the time window or filters to see the rest.")
        elif n_all == 0:
            status = ("No report in this case has Timeline times yet: see Sources… for "
                      "which reports have times and why they are not listed")
        else:
            status = f"{total:,} events"
            if total != n_all:
                status += f" (of {n_all:,} on the Timeline)"
        self._tl_status.setText(status + f" · times shown as {self._timestamp_mode_text()}")

    # ── pivot / go to ────────────────────────────────────────────────────
    def _timeline_add_time_menu(self, menu, times) -> bool:
        """Add a "🕒 Timeline" submenu to *menu* for a row's times.
        *times*: [(label, utc_ms, what)] — *label* names the time (a column),
        *what* says where it came from (shown on the Timeline). Each time
        gets "Go to … in the Timeline" and "Activity ±1 day around …".
        Returns False (nothing added) if no time is usable."""
        times = [(lbl, int(ms), what) for lbl, ms, what in times if ms]
        if not times:
            return False
        sub = menu.addMenu("🕒 Timeline")
        for lbl, ms, what in times:
            act = sub.addAction(f"Go to {lbl} ({self._timeline_fmt_ms(ms)}) in the Timeline")
            act.triggered.connect(lambda _=False, ms=ms, what=what: self._timeline_go_to(ms, what))
        sub.addSeparator()
        for lbl, ms, what in times:
            act = sub.addAction(f"Activity ±1 day around {lbl}")
            act.triggered.connect(lambda _=False, ms=ms, what=what:
                                  self._timeline_show_around(ms, what, minutes=24 * 60))
        return True

    def _timeline_file_times(self, ui_path: str) -> list:
        """[(label, utc_ms, what)] for a file's own times (the File
        Browser's time columns: Created/Changed/Modified as this archive
        has them), read from the archive metadata — never from the
        displayed text, which is in the case's time setting. Accessed is
        left out (owner decision, 2026-11-15): phones often don't update
        it, so it is not trustworthy enough for the Timeline."""
        meta = (getattr(self, 'full_metadata', None) or {}).get(ui_path) or {}
        name = ui_path.rsplit('/', 1)[-1] if ui_path else ''
        out = []
        for header, key in getattr(self, '_time_cols', ()):
            if key == 'atime':
                continue
            ms = timeline.file_time_ms(meta.get(key))
            if ms:
                out.append((header, ms, f"{header} of {name} (file system)"))
        return out

    def _timeline_when_ready(self, fn) -> None:
        self.center_tabs.setCurrentIndex(TIMELINE_TAB_INDEX)
        if self._tl_built_for == self._case_dir and not (
                self._tl_worker is not None and self._tl_worker.isRunning()):
            fn()
        else:
            self._timeline_ensure_built(then=fn)

    def _timeline_reset_filters(self, window_ms=None) -> None:
        """Clear source/app/text; set the UTC window to *window_ms*
        (start, end), or switch the window off for None. No reload."""
        for combo in (self._tl_source, self._tl_app):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self._tl_text.blockSignals(True)
        self._tl_text.clear()
        self._tl_text.blockSignals(False)
        if window_ms is not None:
            for w, ms in zip((self._tl_from, self._tl_to), window_ms):
                w.blockSignals(True)
                w.setDateTime(QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc()))
                w.blockSignals(False)
        self._tl_window_check.blockSignals(True)
        self._tl_window_check.setChecked(window_ms is not None)
        self._tl_window_check.blockSignals(False)

    def _timeline_select_nearest(self, utc_ms: int):
        """Select and centre the event nearest *utc_ms* (either list
        order). Returns that event's utc_ms, or None if nothing is listed."""
        best, best_d = None, None
        for r in range(self._tl_model.rowCount()):
            d = abs(self._tl_model.row(r)[_IDX['utc_ms']] - utc_ms)
            if best_d is None or d < best_d:
                best, best_d = r, d
        if best is None:
            return None
        self._tl_view.selectRow(best)
        self._tl_view.scrollTo(self._tl_model.index(best, 0),
                               QAbstractItemView.ScrollHint.PositionAtCenter)
        return self._tl_model.row(best)[_IDX['utc_ms']]

    def _timeline_show_around(self, utc_ms: int, what: str = "", minutes=None) -> None:
        """Switch to the Timeline with a window of ±*minutes* (default: the
        tab's Around setting) on *utc_ms*; *what* says where the time came
        from."""
        def _go():
            m = int(minutes or self._tl_around.value())
            self._timeline_reset_filters((utc_ms - m * 60_000, utc_ms + m * 60_000))
            self._tl_marker = {'kind': 'around', 'ms': utc_ms, 'what': what, 'minutes': m}
            self._timeline_load(center_ms=utc_ms)
            self._tl_marker['nearest'] = self._timeline_select_nearest(utc_ms)
            self._timeline_set_around_label()
        self._timeline_when_ready(_go)

    def _timeline_go_to(self, utc_ms: int, what: str = "") -> None:
        """Switch to the Timeline with no filters and select the event
        nearest *utc_ms*; the note says how far that event is from it."""
        def _go():
            self._timeline_reset_filters(None)
            self._tl_marker = {'kind': 'goto', 'ms': utc_ms, 'what': what}
            self._timeline_load(center_ms=utc_ms)
            self._tl_marker['nearest'] = self._timeline_select_nearest(utc_ms)
            self._timeline_set_around_label()
        self._timeline_when_ready(_go)

    @staticmethod
    def _timeline_span_text(ms: int) -> str:
        ms = abs(int(ms))
        if ms < 1000:
            return f"{ms} ms"
        secs = ms // 1000
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        parts = [f"{d} d" if d else "", f"{h} h" if h else "", f"{m} min" if m else "",
                 f"{s} s" if s and not d else ""]
        return " ".join(p for p in parts if p) or "0 s"

    def _timeline_set_around_label(self) -> None:
        mk = getattr(self, '_tl_marker', None)
        if not mk:
            self._tl_around_label.setVisible(False)
            return
        when = self._timeline_fmt_ms(mk['ms'])
        src = f" — {mk['what']}" if mk.get('what') else ""
        if mk['kind'] == 'around':
            m = mk['minutes']
            width = (f"{m // 1440} day{'s' if m // 1440 != 1 else ''}" if m % 1440 == 0
                     else f"{m // 60} h" if m % 60 == 0 else f"{m} min")
            text = f"Activity ±{width} around {when}{src}"
        else:
            text = f"Went to {when}{src}"
        near = mk.get('nearest')
        if near is None:
            text += ". No event is listed."
        elif near == mk['ms']:
            text += ". Selected: an event at exactly this time."
        else:
            gap = self._timeline_span_text(near - mk['ms'])
            text += (f". Selected: the nearest event, {gap} "
                     f"{'after' if near > mk['ms'] else 'before'} this time.")
        self._tl_around_label.setText(text)
        self._tl_around_label.setVisible(True)

    def _timeline_refresh_times(self) -> None:
        """The time setting changed (TimestampDisplayMixin): redraw the
        Time column, the note and the status line in the new zone. The
        window inputs stay in UTC and say so."""
        if not hasattr(self, '_tl_model'):
            return
        self._tl_model.layoutChanged.emit()
        self._timeline_set_around_label()
        if getattr(self, '_case_dir', None) and self._tl_built_for == self._case_dir:
            mk = getattr(self, '_tl_marker', None)
            self._timeline_load(center_ms=mk['ms'] if mk else None)

    def _timeline_open_row(self, index) -> None:
        if not index.isValid():
            return
        r = self._tl_model.row(index.row())
        self._art_jump_to_report_row(r[_IDX['script']], r[_IDX['report_rowid']])

    def _timeline_context_menu(self, pos) -> None:
        index = self._tl_view.indexAt(pos)
        if not index.isValid():
            return
        r = self._tl_model.row(index.row())
        menu = QMenu(self._tl_view)
        menu.addAction("Open in report").triggered.connect(
            lambda _=False, ix=index: self._timeline_open_row(ix))
        what = f"{r[_IDX['source']]}: {r[_IDX['event']] or r[_IDX['meaning']]}"
        menu.addAction("Show activity around this time (±Around)").triggered.connect(
            lambda _=False: self._timeline_show_around(r[_IDX['utc_ms']], what))
        self._timeline_add_time_menu(menu, [("this event", r[_IDX['utc_ms']], what)])
        menu.exec(self._tl_view.viewport().mapToGlobal(pos))

    # ── sources ──────────────────────────────────────────────────────────
    def _timeline_show_sources(self) -> None:
        if not self._case_dir:
            return
        with closing(_open_cache_db(self._case_dir)) as cache:
            rows = timeline.source_list(cache)
        dlg = QDialog(self)
        dlg.setWindowTitle("Timeline sources")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            "Every report in this case with a time column, and what happened to it. "
            "A report not run on this case is not listed unless it declares Timeline times."))
        heads = ["Source", "Time column", "Status", "Events", "Blank", "Zero",
                 "Not a time", "Outside 2000–2100 (kept, labelled)", "Note"]
        table = QTableWidget(len(rows), len(heads))
        table.setHorizontalHeaderLabels(heads)
        for i, s in enumerate(rows):
            _script, label, field, status, ev, blank, zero, bad, odd, note = s
            vals = [label, field, status] + ['' if v is None else f"{v:,}"
                                             for v in (ev, blank, zero, bad, odd)] + [note or '']
            for j, v in enumerate(vals):
                table.setItem(i, j, QTableWidgetItem(v))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(table)
        dlg.resize(1200, 500)
        dlg.exec()
