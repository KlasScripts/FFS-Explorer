"""artifact_viewer.py — ArtifactRunnerWorker, ArtifactRunnerDialog, and ArtifactViewerMixin."""

import os
import pathlib
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QLabel, QLineEdit, QComboBox, QHBoxLayout, QVBoxLayout,
    QTableView, QTreeView, QPlainTextEdit, QStackedWidget, QSplitter,
    QScrollArea, QCheckBox, QPushButton, QDialog, QMessageBox,
)
from PySide6.QtCore import Qt, QThread, Signal, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont

from db_utils import _open_results_db, start_run_log, complete_run_log, load_last_run
from highlight_delegate import HighlightDelegate


# ── DB-backed virtual table model ─────────────────────────────────────────────

class ArtifactTableModel(QAbstractTableModel):
    """Virtual model backed by SQLite — only visible rows are fetched.

    Supports two modes:
      DB mode   — opened via load_from_db(); holds an open sqlite3.Connection.
                  Rows are fetched on demand using a page cache keyed by rowid.
                  Memory use is bounded to _MAX_PAGES × _PAGE_SIZE rows at once.
      List mode — opened via load_rows(); stores a plain list for small tables
                  (e.g. the Exported Files view) where a DB connection is not needed.

    The model owns its connection and closes it on clear() / load_from_db().
    """

    _PAGE_SIZE = 300   # rows fetched per cache page
    _MAX_PAGES = 12    # evict LRU page above this limit

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conn:    sqlite3.Connection | None = None
        self._table:   str  = ''
        self._columns: list = []
        self._rowids:  list = []   # current view order (all or filtered/sorted)
        self._all_ids: list = []   # unfiltered rowids (used by clear_filter)
        self._rows:    list = []   # list mode only
        self._cache:   dict = {}   # page_num -> [tuple, …]
        self._lru:     list = []   # LRU eviction order
        # Retained filter clause so sort() can re-apply it
        self._where: str  = ''
        self._wargs: list = []

    # ── Connection ────────────────────────────────────────────────────────

    def _close_conn(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ── Loading ───────────────────────────────────────────────────────────

    def load_from_db(self, conn: sqlite3.Connection, table: str,
                     columns: list, rowids: list) -> None:
        """DB mode: rows fetched on demand; conn is owned by this model."""
        self.beginResetModel()
        self._close_conn()
        self._conn    = conn
        self._table   = table
        self._columns = list(columns)
        self._rowids  = list(rowids)
        self._all_ids = list(rowids)
        self._rows    = []
        self._cache.clear()
        self._lru.clear()
        self._where = ''
        self._wargs = []
        self.endResetModel()

    def load_rows(self, columns: list, rows: list) -> None:
        """List mode: small in-memory dataset (e.g. exported-files view)."""
        self.beginResetModel()
        self._close_conn()
        self._columns = list(columns)
        self._rows    = list(rows)
        self._rowids  = list(range(len(rows)))
        self._all_ids = list(self._rowids)
        self._table   = ''
        self._cache.clear()
        self._lru.clear()
        self._where = ''
        self._wargs = []
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._close_conn()
        self._columns = []
        self._rowids  = []
        self._all_ids = []
        self._rows    = []
        self._table   = ''
        self._cache.clear()
        self._lru.clear()
        self._where = ''
        self._wargs = []
        self.endResetModel()

    # ── Filter / sort ─────────────────────────────────────────────────────

    def apply_rowids(self, rowids: list, where: str = '', wargs: list = None) -> None:
        """Update the visible row set (result of filtering or sorting)."""
        self.beginResetModel()
        self._rowids = list(rowids)
        self._where  = where
        self._wargs  = wargs or []
        self._cache.clear()
        self._lru.clear()
        self.endResetModel()

    def clear_filter(self) -> None:
        self.apply_rowids(self._all_ids)

    def filter_rows_inmem(self, term: str, col_idx: int) -> int:
        """List-mode synchronous filter. Returns visible count."""
        t = term.casefold()
        if col_idx < 0:
            ids = [i for i, row in enumerate(self._rows)
                   if any(t in str(v).casefold() for v in row)]
        else:
            ids = [i for i, row in enumerate(self._rows)
                   if t in str(row[col_idx]).casefold()]
        self.apply_rowids(ids)
        return len(ids)

    # ── Page cache ────────────────────────────────────────────────────────

    def _fetch_page(self, page_num: int) -> list:
        start = page_num * self._PAGE_SIZE
        batch = self._rowids[start: start + self._PAGE_SIZE]
        if not batch:
            return []

        if self._conn is None:
            # List mode: batch contains plain list indices
            return [self._rows[i] for i in batch if i < len(self._rows)]

        # DB mode: fetch by rowid, preserving _rowids order
        ph = ','.join('?' * len(batch))
        try:
            by_id = {r[0]: r[1:] for r in self._conn.execute(
                f'SELECT rowid, * FROM "{self._table}" WHERE rowid IN ({ph})', batch)}
        except Exception:
            by_id = {}
        empty = (None,) * len(self._columns)
        return [by_id.get(rid, empty) for rid in batch]

    def _get_row(self, model_row: int):
        pg  = model_row // self._PAGE_SIZE
        off = model_row  % self._PAGE_SIZE
        if pg not in self._cache:
            if len(self._cache) >= self._MAX_PAGES:
                old = self._lru.pop(0)
                del self._cache[old]
            self._cache[pg] = self._fetch_page(pg)
            self._lru.append(pg)
        page = self._cache[pg]
        return page[off] if off < len(page) else None

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def total_rows(self) -> int:
        return len(self._all_ids)

    @property
    def visible_rows(self) -> int:
        return len(self._rowids)

    # ── QAbstractTableModel interface ─────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rowids)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._get_row(index.row())
        if row is None:
            return ''
        val = row[index.column()] if index.column() < len(row) else None
        return str(val) if val is not None else ''

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section] if section < len(self._columns) else None
        return str(section + 1)

    def sort(self, col: int, order: Qt.SortOrder) -> None:
        """Re-query rowids from SQLite using ORDER BY — fast C-level sort."""
        if not self._conn or not self._columns or col >= len(self._columns):
            return
        direction = 'DESC' if order == Qt.SortOrder.DescendingOrder else 'ASC'
        col_name  = self._columns[col]
        try:
            if self._where:
                sql  = (f'SELECT rowid FROM "{self._table}" WHERE {self._where}'
                        f' ORDER BY "{col_name}" {direction}')
                args = self._wargs
            else:
                sql  = f'SELECT rowid FROM "{self._table}" ORDER BY "{col_name}" {direction}'
                args = []
            new_ids = [r[0] for r in self._conn.execute(sql, args)]
        except Exception:
            return
        self.beginResetModel()
        self._rowids = new_ids
        self._cache.clear()
        self._lru.clear()
        self.endResetModel()


# ── Background SQL filter worker ──────────────────────────────────────────────

class ArtifactFilterWorker(QThread):
    """Runs a SQLite LIKE filter on a background thread.

    Opens its own read-only connection so the model's main-thread connection
    is never touched from the worker thread."""

    done = Signal(list, str, list)   # (rowids, where_clause, where_args)

    def __init__(self, db_path: str, table: str, columns: list,
                 term: str, col_idx: int, parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self._table   = table
        self._columns = columns
        self._pattern = f'%{term}%'
        self._col_idx = col_idx

    def run(self) -> None:
        p = self._pattern
        if self._col_idx < 0:
            where = ' OR '.join(f'"{c}" LIKE ?' for c in self._columns)
            args  = [p] * len(self._columns)
        else:
            where = f'"{self._columns[self._col_idx]}" LIKE ?'
            args  = [p]
        try:
            conn   = sqlite3.connect(self._db_path, timeout=5)
            rowids = [r[0] for r in conn.execute(
                f'SELECT rowid FROM "{self._table}" WHERE {where}', args)]
            conn.close()
        except Exception:
            rowids = []
        self.done.emit(rowids, where, args)


# ── Tree node role sentinels ──────────────────────────────────────────────────
_ART_GROUP  = "__art_group__:"
_ART_REPORT = "__art_report__:"
_ART_SCRIPT = "__art_script__:"
_ART_SOURCE = "__art_source__:"
_ART_FILES  = "__art_files__:"


class ArtifactRunnerWorker(QThread):
    log  = Signal(str)
    done = Signal()

    def __init__(self, selected, zip_path, adapter, streaming_index, case_dir):
        super().__init__()
        self._selected        = selected
        self._zip_path        = zip_path
        self._adapter         = adapter
        self._streaming_index = streaming_index
        self._case_dir        = case_dir

    def run(self):
        from artifact_runner import run_artifact
        from artifact_db import write_artifact_results

        try:
            case_conn = _open_results_db(self._case_dir)
        except Exception as exc:
            self.log.emit(f"Could not open results database: {exc}")
            self.done.emit()
            return

        zip_obj = None
        if self._streaming_index is None:
            try:
                zip_obj = zipfile.ZipFile(self._zip_path, 'r')
            except Exception as exc:
                self.log.emit(f"Could not open archive: {exc}")
                case_conn.close()
                self.done.emit()
                return

        try:
            for script_name, module in self._selected:
                label = getattr(module, 'name', script_name)
                self.log.emit(f"Running: {label}…")
                run_id = None
                try:
                    run_id = start_run_log(case_conn, f'artifact_{script_name}')
                except Exception:
                    pass
                rows, error = run_artifact(
                    script_name, module,
                    self._zip_path, self._adapter,
                    case_dir=self._case_dir,
                    zip_obj=zip_obj,
                    streaming_index=self._streaming_index,
                )
                if error:
                    self.log.emit(f"  Error: {error}")
                else:
                    count = write_artifact_results(case_conn, script_name, rows)
                    self.log.emit(f"  Done — {count} rows written.")
                    if run_id is not None:
                        try:
                            complete_run_log(case_conn, run_id,
                                             processed=count, output_rows=count)
                        except Exception:
                            pass
        except Exception as exc:
            self.log.emit(f"\nUnexpected error: {exc}")
        finally:
            case_conn.close()
            if zip_obj:
                zip_obj.close()

        self.log.emit("\nAll selected parsers finished.")
        self.done.emit()


class ArtifactRunnerDialog(QDialog):
    parsers_completed = Signal()

    def __init__(self, zip_path, zip_names, adapter, streaming_index, case_dir,
                 is_android, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Run Artifact Parsers")
        self.setMinimumSize(480, 540)
        self._worker = None

        platform = 'android' if is_android else 'ios'

        from artifact_runner import list_artifacts
        all_artifacts = list_artifacts(platform)

        def _exists(candidates):
            if streaming_index is not None:
                return any(c in streaming_index for c in candidates)
            return any(c in zip_names for c in candidates)

        def _mod_matches(mod):
            if hasattr(mod, 'app_path') and hasattr(mod, 'files'):
                app_base = mod.app_path.strip('/')
                return any(
                    _exists(adapter.user_candidates(f"{app_base}/{sub.lstrip('/')}"))
                    for sub in mod.files.values()
                )
            return any(
                _exists(adapter.user_candidates(ui_path))
                for ui_path in getattr(mod, 'target_paths', [])
            )

        available = [
            (script_name, mod)
            for script_name, mod in all_artifacts
            if _mod_matches(mod)
        ]

        layout = QVBoxLayout(self)

        if not available:
            layout.addWidget(QLabel("No artifact parsers matched files in the loaded archive."))
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.reject)
            layout.addWidget(close_btn)
            return

        layout.addWidget(QLabel(f"Parsers matched ({platform.upper()}) — select to run:"))

        # Load run history for all available parsers in one DB open
        run_history: dict[str, dict] = {}
        if case_dir:
            try:
                with closing(_open_results_db(case_dir)) as rdb:
                    for _sn, _ in available:
                        _last = load_last_run(rdb, f'artifact_{_sn}')
                        if _last:
                            run_history[_sn] = _last
            except Exception:
                pass

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(6, 6, 6, 6)
        inner_layout.setSpacing(5)
        self._checkboxes: list[tuple[QCheckBox, str, object]] = []
        for script_name, mod in available:
            cb = QCheckBox(getattr(mod, 'name', script_name))
            cb.setChecked(True)
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(cb)
            last = run_history.get(script_name)
            if last:
                rows   = last['output_rows'] or 0
                run_at = (last['run_at'] or '')[:10]
                if last['complete']:
                    info_text  = f"last run {run_at} · {rows:,} rows"
                    info_color = 'grey'
                else:
                    info_text  = f"last run {run_at} · incomplete"
                    info_color = '#b8860b'
                info_lbl = QLabel(info_text)
                info_lbl.setStyleSheet(
                    f"color: {info_color}; font-size: 11px; font-style: italic;")
                row.addWidget(info_lbl)
            row.addStretch()
            inner_layout.addLayout(row)
            self._checkboxes.append((cb, script_name, mod))
        inner_layout.addStretch()
        scroll.setWidget(inner)
        layout.addWidget(scroll)

        layout.addWidget(QLabel("Log:"))
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(170)
        self._log.setFont(QFont("Courier", 10))
        layout.addWidget(self._log)

        btn_row = QHBoxLayout()
        self._run_btn   = QPushButton("Run")
        self._close_btn = QPushButton("Close")
        self._run_btn.clicked.connect(self._run)
        self._close_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

        self._zip_path        = zip_path
        self._adapter         = adapter
        self._streaming_index = streaming_index
        self._case_dir        = case_dir

    def _run(self):
        selected = [
            (sn, mod)
            for cb, sn, mod in self._checkboxes
            if cb.isChecked()
        ]
        if not selected:
            return

        self._run_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._log.clear()

        self._worker = ArtifactRunnerWorker(
            selected, self._zip_path, self._adapter,
            self._streaming_index, self._case_dir,
        )
        self._worker.log.connect(self._log.appendPlainText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self):
        self._run_btn.setEnabled(True)
        self._close_btn.setEnabled(True)
        self.parsers_completed.emit()

    def closeEvent(self, event):
        if self._worker and self._worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)


class ArtifactViewerMixin:
    """Mixin providing the Artifact Viewer tab and runner dialog for FastZipBrowser."""

    def _setup_artifact_tab(self) -> QWidget:
        """Build the Artifact Viewer tab — own tree on the left, content on the right."""
        self._art_tree_model = QStandardItemModel()
        self._art_tree_model.setHorizontalHeaderLabels(["Device Artifacts"])

        self._art_tree_view = QTreeView()
        self._art_tree_view.setModel(self._art_tree_model)
        self._art_tree_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self._art_tree_view.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self._art_tree_view.setHeaderHidden(False)
        self._art_tree_view.setMinimumWidth(200)
        self._art_tree_view.setMaximumWidth(320)
        self._art_tree_view.clicked.connect(self._on_art_tree_clicked)

        # ── Right side: stacked widget ────────────────────────────────────────
        self._art_placeholder = QLabel("Select a Report or Script from the tree.")
        self._art_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_placeholder.setStyleSheet("color: grey; font-style: italic;")

        # Report page — filter bar + virtual table view
        report_page = QWidget()
        report_layout = QVBoxLayout(report_page)
        report_layout.setContentsMargins(0, 0, 0, 0)
        report_layout.setSpacing(4)

        report_filter_row = QHBoxLayout()
        self._art_filter_input = QLineEdit()
        self._art_filter_input.setPlaceholderText("Filter…")
        self._art_filter_input.returnPressed.connect(self._apply_art_filter)
        self._art_filter_col = QComboBox()
        self._art_filter_col.addItem("All Columns")
        self._art_filter_col.currentIndexChanged.connect(self._apply_art_filter)
        self._art_filter_btn = QPushButton("Filter")
        self._art_filter_btn.setFixedWidth(60)
        self._art_filter_btn.clicked.connect(self._apply_art_filter)
        self._art_row_label = QLabel()
        report_filter_row.addWidget(QLabel("Filter:"))
        report_filter_row.addWidget(self._art_filter_input, 1)
        report_filter_row.addWidget(self._art_filter_col)
        report_filter_row.addWidget(self._art_filter_btn)
        report_filter_row.addWidget(self._art_row_label)
        report_layout.addLayout(report_filter_row)

        self._art_table_model   = ArtifactTableModel()
        self._art_active_filter = ''
        self._art_filter_worker: ArtifactFilterWorker | None = None

        self._art_report_view = QTableView()
        self._art_report_view.setModel(self._art_table_model)
        self._art_report_view.setSortingEnabled(True)
        self._art_report_view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._art_report_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._art_report_view.setAlternatingRowColors(True)
        self._art_report_view.setWordWrap(True)
        self._art_report_view.horizontalHeader().setStretchLastSection(True)
        self._art_report_view.verticalHeader().hide()
        self._art_report_view.verticalHeader().setDefaultSectionSize(80)
        self._art_highlight_delegate = HighlightDelegate(
            lambda: self._art_active_filter)
        self._art_report_view.setItemDelegate(self._art_highlight_delegate)
        report_layout.addWidget(self._art_report_view)

        # Script page — read-only monospace text editor
        self._art_script_view = QPlainTextEdit()
        self._art_script_view.setReadOnly(True)
        self._art_script_view.setFont(QFont("Courier", 11))

        self._art_stack = QStackedWidget()
        self._art_stack.addWidget(self._art_placeholder)  # 0
        self._art_stack.addWidget(report_page)             # 1
        self._art_stack.addWidget(self._art_script_view)  # 2

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._art_tree_view)
        splitter.addWidget(self._art_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        tab = QWidget()
        QHBoxLayout(tab).addWidget(splitter)
        tab.layout().setContentsMargins(4, 4, 4, 4)
        return tab

    def _refresh_artifact_tab(self):
        """Rebuild the artifact tree from completed parsers in the case DB."""
        if not hasattr(self, '_art_tree_model'):
            return
        from artifact_db import list_completed_artifacts
        from artifact_runner import list_artifacts

        self._cancel_art_filter_worker()
        self._art_tree_model.clear()
        self._art_tree_model.setHorizontalHeaderLabels(["Device Artifacts"])
        self._art_table_model.clear()   # closes any open SQLite connection
        self._art_active_filter = ''
        self._art_stack.setCurrentIndex(0)

        if not self._case_dir:
            return
        try:
            with closing(_open_results_db(self._case_dir)) as case_conn:
                completed = list_completed_artifacts(case_conn)
        except Exception:
            return

        if not completed:
            placeholder = QStandardItem("No parsers run yet")
            placeholder.setEditable(False)
            self._art_tree_model.invisibleRootItem().appendRow(placeholder)
            return

        platform = 'android' if self._is_android_archive() else 'ios'
        modules  = {sn: mod for sn, mod in list_artifacts(platform)}

        def _item(text, role_val=None):
            it = QStandardItem(text)
            it.setEditable(False)
            it.setCheckable(False)
            if role_val is not None:
                it.setData(role_val, Qt.ItemDataRole.UserRole)
            return it

        for script_name in completed:
            mod   = modules.get(script_name)
            label = getattr(mod, 'name', script_name) if mod else script_name
            group = _item(label, _ART_GROUP + script_name)
            group.setFont(QFont("Arial", weight=QFont.Weight.Bold))
            group.appendRow(_item("Report",         _ART_REPORT + script_name))
            group.appendRow(_item("Script",         _ART_SCRIPT + script_name))
            group.appendRow(_item("Source in ZIP",  _ART_SOURCE + script_name))
            group.appendRow(_item("Exported Files", _ART_FILES  + script_name))
            self._art_tree_model.invisibleRootItem().appendRow(group)

        self._art_tree_view.expandAll()

    def _on_art_tree_clicked(self, index):
        role_val = index.data(Qt.ItemDataRole.UserRole)
        if not role_val:
            return
        if role_val.startswith(_ART_REPORT):
            self._art_show_report(role_val[len(_ART_REPORT):])
        elif role_val.startswith(_ART_SCRIPT):
            self._art_show_script(role_val[len(_ART_SCRIPT):])
        elif role_val.startswith(_ART_SOURCE):
            self._art_goto_source(role_val[len(_ART_SOURCE):])
        elif role_val.startswith(_ART_FILES):
            self._art_show_files(role_val[len(_ART_FILES):])

    # ── Report display ────────────────────────────────────────────────────────

    def _setup_report_filter_ui(self, columns: list):
        """Reset filter controls for a newly loaded report."""
        self._cancel_art_filter_worker()
        self._art_active_filter = ''
        self._art_filter_input.clear()
        self._art_filter_col.blockSignals(True)
        self._art_filter_col.clear()
        self._art_filter_col.addItem("All Columns")
        for col in columns:
            self._art_filter_col.addItem(col)
        self._art_filter_col.blockSignals(False)
        self._art_filter_btn.setEnabled(True)

    def _art_resize_columns(self, max_width: int = 320):
        """Estimate column widths by sampling the first 200 visible rows."""
        fm     = self._art_report_view.fontMetrics()
        n_cols = self._art_table_model.columnCount()
        n_rows = min(200, self._art_table_model.rowCount())
        for col in range(n_cols):
            header = self._art_table_model.headerData(col, Qt.Orientation.Horizontal) or ''
            w = fm.horizontalAdvance(str(header)) + 24
            for row in range(n_rows):
                text = self._art_table_model.data(
                    self._art_table_model.index(row, col)) or ''
                w = max(w, fm.horizontalAdvance(text) + 12)
            self._art_report_view.setColumnWidth(col, min(w, max_width))

    def _art_show_report(self, script_name: str):
        self._cancel_art_filter_worker()
        table = f'artifact_{script_name}'
        try:
            conn = _open_results_db(self._case_dir)
            cur  = conn.execute(f'SELECT * FROM "{table}" LIMIT 0')
            cols = [d[0] for d in cur.description]
            ids  = [r[0] for r in conn.execute(
                f'SELECT rowid FROM "{table}" ORDER BY rowid')]
        except Exception as exc:
            self.status_bar.showMessage(f"Could not load report: {exc}")
            try:
                conn.close()
            except Exception:
                pass
            return

        # Transfer connection ownership to the model
        self._art_table_model.load_from_db(conn, table, cols, ids)
        self._setup_report_filter_ui(cols)
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(ids):,} rows")
        self._art_stack.setCurrentIndex(1)

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _apply_art_filter(self):
        term    = self._art_filter_input.text()
        col_idx = self._art_filter_col.currentIndex() - 1   # -1 = all columns
        total   = self._art_table_model.total_rows

        self._art_active_filter = term

        if not term:
            self._cancel_art_filter_worker()
            self._art_table_model.clear_filter()
            self._art_row_label.setText(f"{total:,} rows")
            return

        # List-mode (small datasets, e.g. exported files): filter in Python
        if self._art_table_model._conn is None:
            self._cancel_art_filter_worker()
            count = self._art_table_model.filter_rows_inmem(term, col_idx)
            self._art_row_label.setText(f"{count:,} of {total:,} rows")
            return

        # DB mode: run SQLite LIKE on a background thread
        self._cancel_art_filter_worker()
        self._art_filter_btn.setEnabled(False)
        self._art_row_label.setText(f"Filtering {total:,} rows…")

        db_path = os.path.join(self._case_dir, 'caseresults.db')
        worker  = ArtifactFilterWorker(
            db_path,
            self._art_table_model._table,
            self._art_table_model._columns,
            term, col_idx,
        )
        worker.done.connect(self._on_art_filter_done)
        self._art_filter_worker = worker
        worker.start()

    def _on_art_filter_done(self, rowids: list, where: str, wargs: list):
        self._art_table_model.apply_rowids(rowids, where, wargs)
        total = self._art_table_model.total_rows
        self._art_row_label.setText(f"{len(rowids):,} of {total:,} rows")
        self._art_filter_btn.setEnabled(True)

    def _cancel_art_filter_worker(self):
        if self._art_filter_worker and self._art_filter_worker.isRunning():
            try:
                self._art_filter_worker.done.disconnect()
            except Exception:
                pass
        self._art_filter_worker = None

    # ── Script / source / files views ─────────────────────────────────────────

    def _art_show_script(self, script_name: str):
        from artifact_runner import _ARTIFACTS_DIR
        platform    = 'android' if self._is_android_archive() else 'ios'
        script_path = os.path.join(_ARTIFACTS_DIR, platform, f"{script_name}.py")
        if not os.path.isfile(script_path):
            self.status_bar.showMessage(f"Script not found: {script_path}")
            return
        try:
            text = pathlib.Path(script_path).read_text(encoding='utf-8')
        except Exception as exc:
            self.status_bar.showMessage(f"Could not read script: {exc}")
            return
        self._art_script_view.setPlainText(text)
        self._art_stack.setCurrentIndex(2)
        self.status_bar.showMessage(f"Script: {script_path}")

    def _art_goto_source(self, script_name: str):
        from artifact_runner import list_artifacts
        platform = 'android' if self._is_android_archive() else 'ios'
        modules  = {sn: mod for sn, mod in list_artifacts(platform)}
        mod      = modules.get(script_name)
        if not mod:
            return
        if hasattr(mod, 'app_path') and hasattr(mod, 'files'):
            first_sub = next(iter(mod.files.values()), None)
            if not first_sub:
                return
            first_path = mod.app_path.strip('/') + '/' + first_sub.lstrip('/')
        else:
            target_paths = getattr(mod, 'target_paths', [])
            if not target_paths:
                return
            first_path = target_paths[0]
        parent = '/'.join(first_path.split('/')[:-1])
        self.center_tabs.setCurrentIndex(0)
        self.navigate_tree_to_path(parent)

    def _art_show_files(self, script_name: str):
        from artifact_runner import list_artifacts, safe_folder_name
        platform    = 'android' if self._is_android_archive() else 'ios'
        modules     = {sn: mod for sn, mod in list_artifacts(platform)}
        mod         = modules.get(script_name)
        parser_name = getattr(mod, 'name', script_name) if mod else script_name
        folder      = os.path.join(self._case_dir, 'artifact_parser_files',
                                   safe_folder_name(parser_name))
        if not os.path.isdir(folder):
            self.status_bar.showMessage(f"No exported files for {parser_name}")
            return

        columns = ["Name", "Size (Bytes)", "Modified", "Full Path"]
        rows    = []
        # scandir caches stat info per entry — one directory pass instead of
        # three stat calls per file.
        with os.scandir(folder) as it:
            for entry in sorted(it, key=lambda e: e.name):
                if not entry.is_file():
                    continue
                st    = entry.stat()
                mtime = datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                rows.append((entry.name, f"{st.st_size:,}", mtime, entry.path))

        self._art_table_model.load_rows(columns, rows)
        self._setup_report_filter_ui(columns)
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(rows)} file(s)")
        self._art_stack.setCurrentIndex(1)
        self.status_bar.showMessage(f"Exported files — {folder}")

    def _open_artifact_runner(self):
        if not self.zip_path:
            return
        if not self._case_dir:
            QMessageBox.information(self, "No Case Folder",
                                    "A case folder is required to store results.\n"
                                    "Use File → Process Archive… to set one first.")
            return
        dlg = ArtifactRunnerDialog(
            zip_path=self.zip_path,
            zip_names=self.zip_names,
            adapter=self._adapter,
            streaming_index=self._streaming_index,
            case_dir=self._case_dir,
            is_android=self._is_android_archive(),
            parent=self,
        )
        dlg.parsers_completed.connect(self._refresh_artifact_tab)
        dlg.exec()
