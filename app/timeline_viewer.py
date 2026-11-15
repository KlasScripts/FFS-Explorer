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
        self._tl_around_at = None
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
        self._timeline_apply()
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
        if not getattr(self, '_case_dir', None) or self._tl_built_for != self._case_dir:
            return
        start, end = self._timeline_window_ms()
        if not self._tl_window_check.isChecked():
            self._tl_around_label.setVisible(False)
        script = self._tl_source.currentData()
        limit = 100_000
        with closing(_open_cache_db(self._case_dir)) as cache:
            rows, total = timeline.query(
                cache, start, end, [script] if script else None,
                self._tl_app.currentData(), self._tl_text.text().strip() or None,
                descending=self._tl_desc.isChecked(), limit=limit)
            n_all = cache.execute('SELECT COUNT(*) FROM timeline_events').fetchone()[0]
        self._tl_model.set_rows(rows)
        if total > len(rows):
            # Never silently capped (A-51).
            text = (f"Showing the first {len(rows):,} of {total:,} matching events — "
                    f"narrow the time window or filters to see the rest.")
        elif n_all == 0:
            text = ("No report in this case has Timeline times yet: see Sources… for "
                    "which reports have times and why they are not listed")
        else:
            text = f"{total:,} events"
            if total != n_all:
                text += f" (of {n_all:,} on the Timeline)"
        self._tl_status.setText(text + f" · times shown as {self._timestamp_mode_text()}")

    # ── pivot ────────────────────────────────────────────────────────────
    def _timeline_show_around(self, utc_ms: int, what: str = "") -> None:
        """Switch to the Timeline with a window of ±(Around) minutes on
        *utc_ms*; *what* says where the time came from."""
        def _go():
            half = self._tl_around.value() * 60_000
            for w, ms in ((self._tl_from, utc_ms - half), (self._tl_to, utc_ms + half)):
                w.blockSignals(True)
                w.setDateTime(QDateTime.fromMSecsSinceEpoch(int(ms), QTimeZone.utc()))
                w.blockSignals(False)
            self._tl_source.setCurrentIndex(0)
            self._tl_app.setCurrentIndex(0)
            self._tl_text.blockSignals(True)
            self._tl_text.clear()
            self._tl_text.blockSignals(False)
            self._tl_window_check.blockSignals(True)
            self._tl_window_check.setChecked(True)
            self._tl_window_check.blockSignals(False)
            self._tl_around_at = (utc_ms, what, self._tl_around.value())
            self._timeline_set_around_label()
            self._tl_around_label.setVisible(True)
            self._timeline_apply()
            # Select the first event at or after the pivot time.
            for r in range(self._tl_model.rowCount()):
                if self._tl_model.row(r)[_IDX['utc_ms']] >= utc_ms:
                    self._tl_view.selectRow(r)
                    self._tl_view.scrollTo(self._tl_model.index(r, 0),
                                           QAbstractItemView.ScrollHint.PositionAtCenter)
                    break
        self.center_tabs.setCurrentIndex(TIMELINE_TAB_INDEX)
        if self._tl_built_for == self._case_dir and not (
                self._tl_worker is not None and self._tl_worker.isRunning()):
            _go()
        else:
            self._timeline_ensure_built(then=_go)

    def _timeline_set_around_label(self) -> None:
        at = getattr(self, '_tl_around_at', None)
        if not at:
            return
        utc_ms, what, minutes = at
        self._tl_around_label.setText(
            f"Activity ±{minutes} min around {self._timeline_fmt_ms(utc_ms)}"
            + (f" — {what}" if what else ""))

    def _timeline_refresh_times(self) -> None:
        """The time setting changed (TimestampDisplayMixin): redraw the
        Time column, the pivot label and the status line in the new zone.
        The window inputs stay in UTC and say so."""
        if not hasattr(self, '_tl_model'):
            return
        self._tl_model.layoutChanged.emit()
        self._timeline_set_around_label()
        if getattr(self, '_case_dir', None) and self._tl_built_for == self._case_dir:
            self._timeline_apply()

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
        menu.addAction("Show activity around this time").triggered.connect(
            lambda _=False: self._timeline_show_around(
                r[_IDX['utc_ms']], f"{r[_IDX['source']]}: {r[_IDX['event']] or r[_IDX['meaning']]}"))
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
