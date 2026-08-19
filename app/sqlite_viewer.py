"""sqlite_viewer.py — SqliteViewerMixin: a generic SQLite database browser.

Adds a "Database" tab to the File Preview panel.  When a .db/.sqlite file is
selected, the database is extracted from the archive to a temp copy, its tables
are listed (with row counts), and the chosen table's rows are browsed in a paged
grid with a text filter and CSV export.

When the database ships with a WAL sidecar, the table is shown as a unified
net-change view: the pre-WAL (last-checkpointed) table with the rows the WAL
added / deleted / modified highlighted in place.

Reuses ArtifactTableModel (lazy, paged, DB-backed virtual model) and
ArtifactFilterWorker (background LIKE filter) from artifact_viewer.
"""

import csv
import os
import shutil
import sqlite3
import tempfile

from PySide6.QtWidgets import (
    QWidget, QLabel, QLineEdit, QComboBox, QHBoxLayout, QVBoxLayout,
    QTableView, QPushButton, QFileDialog, QCheckBox,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QColor, QBrush

from artifact_viewer import ArtifactTableModel, ArtifactFilterWorker


# A file is treated as a database by its content, not its name: every SQLite
# database begins with this 16-byte header.  This catches SQLite files that
# carry an unusual extension or none at all (common in iOS extractions) and
# avoids trying to open non-SQLite files that merely end in .db.
_SQLITE_MAGIC = b'SQLite format 3\x00'

# Cap for list-mode fallback (WITHOUT ROWID tables) so a pathological table
# can't exhaust memory.
_LIST_MODE_CAP = 50_000

# Above this row count a table is not diffed (both sides would be held in
# memory at once).
_DIFF_ROW_CAP = 200_000

# Row background by WAL change type (alpha-blended so they read on light/dark).
_DIFF_BG = {
    'ADDED':    QColor(46, 160, 67, 70),    # green
    'DELETED':  QColor(198, 40, 40, 70),    # red  (matches the app's #c62828)
    'MODIFIED': QColor(255, 193, 7, 45),     # amber
}
_DIFF_CELL_CHANGED = QColor(255, 193, 7, 130)   # stronger amber for changed cells


class SqliteDiffModel(QAbstractTableModel):
    """In-memory model for the WAL net-change view of one table.

    Each row is (status, values, changed_cols):
      status       — 'ADDED' | 'DELETED' | 'MODIFIED'
      values       — display values; for MODIFIED rows changed cells read
                     "old → new"
      changed_cols — set of 0-based *table* column indices that differ
                     (used to paint the individual changed cells)
    A leading "Change" column shows the status.  Supports a synchronous text
    filter over the in-memory rows.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: list = []      # includes the leading "Change" column
        self._rows:    list = []      # [(status, values, changed_cols), …]
        self._view:    list = []      # indices into _rows (filtered order)

    def load(self, table_columns: list, rows: list) -> None:
        self.beginResetModel()
        self._columns = ["Change"] + list(table_columns)
        self._rows    = rows
        self._view    = list(range(len(rows)))
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._columns = []
        self._rows    = []
        self._view    = []
        self.endResetModel()

    @property
    def total_rows(self) -> int:
        return len(self._rows)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._view)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        status, vals, changed = self._rows[self._view[index.row()]]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return status
            v = vals[col - 1] if (col - 1) < len(vals) else None
            return '' if v is None else str(v)

        if role == Qt.ItemDataRole.BackgroundRole:
            if status == 'MODIFIED' and col > 0 and (col - 1) in changed:
                return QBrush(_DIFF_CELL_CHANGED)
            base = _DIFF_BG.get(status)
            return QBrush(base) if base else None

        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._columns[section] if section < len(self._columns) else ''
        return section + 1


class SqlitePreviewWorker(QThread):
    """Extracts a SQLite db (+ WAL/SHM sidecars) to a temp copy and counts
    every table's rows — off the GUI thread.  Both the extraction write and
    the per-table SELECT COUNT(*) loop can take seconds on a large database
    (e.g. a case on a network share), and this previously ran synchronously
    in _load_sqlite_preview on every file click."""
    done = Signal(dict)

    def __init__(self, raw: bytes, name: str, wal: bytes | None, shm: bytes | None):
        super().__init__()
        self._raw  = raw
        self._name = name
        self._wal  = wal
        self._shm  = shm

    def run(self):
        tmpdir = tempfile.mkdtemp(prefix="ffs_sqlite_")
        db_path = os.path.join(tmpdir, self._name)
        try:
            with open(db_path, 'wb') as f:
                f.write(self._raw)
        except Exception as exc:
            self.done.emit({'error': f"Could not extract {self._name}: {exc}", 'tmpdir': tmpdir})
            return

        # Pull WAL/SHM sidecars alongside so the latest committed pages show.
        # A real -wal/-shm never starts with the SQLite main-db magic; guards
        # against readers that fall back to the whole db blob when the
        # sidecar entry doesn't exist (e.g. gzip-sourced nested archives).
        wrote_wal = False
        for suffix, sidecar in (('-wal', self._wal), ('-shm', self._shm)):
            if sidecar is None or sidecar[:16] == _SQLITE_MAGIC:
                continue
            try:
                with open(db_path + suffix, 'wb') as f:
                    f.write(sidecar)
                if suffix == '-wal':
                    wrote_wal = True
            except Exception:
                pass

        # When a WAL is present, keep a pristine copy of the main file alone
        # so the GUI thread can compute the pre-WAL (last-checkpointed) state.
        has_wal = wrote_wal
        before_path = None
        if wrote_wal:
            before_dir = os.path.join(tmpdir, 'before')
            os.makedirs(before_dir, exist_ok=True)
            before_path = os.path.join(before_dir, self._name)
            try:
                with open(before_path, 'wb') as f:
                    f.write(self._raw)
            except Exception:
                has_wal = False
                before_path = None

        if self.isInterruptionRequested():
            self.done.emit({'cancelled': True, 'tmpdir': tmpdir})
            return

        try:
            conn = sqlite3.connect(db_path)
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                counts = {}
                for t in tables:
                    if self.isInterruptionRequested():
                        self.done.emit({'cancelled': True, 'tmpdir': tmpdir})
                        return
                    try:
                        counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    except Exception:
                        counts[t] = 0
            finally:
                conn.close()   # never hand a cross-thread sqlite3 connection to the GUI thread
        except Exception:
            self.done.emit({'error': f"{self._name} is not a valid SQLite database.",
                             'tmpdir': tmpdir})
            return

        self.done.emit({
            'tmpdir': tmpdir, 'db_path': db_path, 'before_path': before_path,
            'has_wal': has_wal, 'name': self._name, 'tables': tables, 'counts': counts,
        })


class SqliteViewerMixin:

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _setup_sqlite_tab(self) -> QWidget:
        """Build the Database preview tab and initialise state.  Returns the
        QWidget to be added to self.preview_tabs."""
        self._sql_tmpdir:       str | None = None
        self._sql_db_path:      str | None = None
        self._sql_before_path:  str | None = None   # main file alone (pre-WAL)
        self._sql_meta_conn:    sqlite3.Connection | None = None
        self._sql_filter_worker = None
        self._sql_preview_worker = None
        self._sql_list_mode     = False          # True for WITHOUT ROWID tables
        self._sql_has_wal       = False
        self._sql_diff_mode     = False
        self._sql_affected_only = False          # WAL view: hide unchanged rows
        self._sql_affected_counts = None         # {table: affected count}, lazy
        self._sql_tables        = []             # all table names, display order
        self._sql_model         = ArtifactTableModel()
        self._sql_diff_model    = SqliteDiffModel()

        # ── Controls row ────────────────────────────────────────────────────
        self._sql_table_combo = QComboBox()
        self._sql_table_combo.setMinimumWidth(220)
        self._sql_table_combo.currentIndexChanged.connect(self._on_sql_table_changed)

        self._sql_affected_check = QCheckBox("WAL updates only")
        self._sql_affected_check.setToolTip(
            "Show only the rows the WAL added, deleted, or modified "
            "(hides unchanged rows).")
        self._sql_affected_check.toggled.connect(self._on_sql_affected_toggled)
        self._sql_affected_check.setVisible(False)

        self._sql_filter_input = QLineEdit()
        self._sql_filter_input.setPlaceholderText("Filter rows…")
        self._sql_filter_input.setClearButtonEnabled(True)
        self._sql_filter_timer = QTimer(self)
        self._sql_filter_timer.setSingleShot(True)
        self._sql_filter_timer.setInterval(250)
        self._sql_filter_timer.timeout.connect(self._apply_sql_filter)
        self._sql_filter_input.textChanged.connect(
            lambda _t: self._sql_filter_timer.start())

        self._sql_export_btn = QPushButton("Export CSV")
        self._sql_export_btn.clicked.connect(self._export_sql_table_csv)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Table:"))
        controls.addWidget(self._sql_table_combo)
        controls.addWidget(self._sql_affected_check)
        controls.addWidget(self._sql_filter_input, stretch=1)
        controls.addWidget(self._sql_export_btn)

        # ── Table view ──────────────────────────────────────────────────────
        self._sql_table_view = QTableView()
        self._sql_table_view.setModel(self._sql_model)
        self._sql_table_view.setAlternatingRowColors(True)
        self._sql_table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._sql_table_view.horizontalHeader().setStretchLastSection(True)
        self._sql_table_view.verticalHeader().setVisible(False)

        self._sql_status_label = QLabel("No database selected")

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(controls)
        layout.addWidget(self._sql_table_view, stretch=1)
        layout.addWidget(self._sql_status_label)

        self._set_sql_controls_enabled(False)
        return tab

    def _set_sql_controls_enabled(self, enabled: bool) -> None:
        self._sql_table_combo.setEnabled(enabled)
        self._sql_filter_input.setEnabled(enabled)
        self._sql_export_btn.setEnabled(enabled)
        self._sql_affected_check.setEnabled(enabled)

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load_sqlite_preview(self, ui_path: str, raw: bytes | None = None,
                             sidecar_reader=None) -> None:
        """Extract *ui_path* (a SQLite db) plus any WAL/SHM sidecars to a temp
        copy, then enumerate its tables.  Shows a message on any failure.

        *raw* may be supplied for entries that aren't in the main archive
        (e.g. files inside an extracted nested archive).

        *sidecar_reader* is an optional callable ``(suffix) -> bytes | None``
        used to fetch the WAL/SHM sidecars from the same source as the db.
        When omitted and the db came from the main archive, sidecars are read
        from there via _read_zip_bytes; when omitted for a supplied *raw*,
        no sidecars are loaded."""
        self._clear_sqlite_preview()

        name = os.path.basename(ui_path)
        if raw is None:
            raw = self._read_zip_bytes(ui_path)
            if sidecar_reader is None:
                sidecar_reader = lambda suffix: self._read_zip_bytes(ui_path + suffix)
        if raw is None:
            self._sql_status_label.setText(f"Could not read {name} from the archive.")
            return

        # Fetch sidecar bytes here (touches the shared zip handle — stays on
        # the GUI thread); the worker decides whether each is a real sidecar
        # and does the actual (slow) extraction + per-table counting.
        wal = shm = None
        if sidecar_reader is not None:
            for suffix, var in (('-wal', 'wal'), ('-shm', 'shm')):
                try:
                    sidecar = sidecar_reader(suffix)
                except Exception:
                    sidecar = None
                if suffix == '-wal':
                    wal = sidecar
                else:
                    shm = sidecar

        self._set_sql_controls_enabled(False)
        self._sql_status_label.setText(f"Loading {name}…")
        self._stop_sqlite_preview_worker()
        self._sql_preview_worker = SqlitePreviewWorker(raw, name, wal, shm)
        self._sql_preview_worker.done.connect(self._on_sqlite_preview_ready)
        self._sql_preview_worker.start()

    def _stop_sqlite_preview_worker(self) -> None:
        worker = self._sql_preview_worker
        if worker is None:
            return
        if worker.isRunning():
            try:
                worker.done.disconnect()
            except (RuntimeError, TypeError):
                pass
            worker.requestInterruption()
            worker.wait()
        self._sql_preview_worker = None

    def _on_sqlite_preview_ready(self, result: dict) -> None:
        self._sql_preview_worker = None

        if result.get('cancelled') or result.get('error'):
            tmpdir = result.get('tmpdir')
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
            if result.get('error'):
                self._sql_status_label.setText(result['error'])
            return

        self._sql_tmpdir      = result['tmpdir']
        self._sql_db_path     = result['db_path']
        self._sql_before_path = result['before_path']
        self._sql_has_wal     = result['has_wal']
        name   = result['name']
        tables = result['tables']
        counts = result['counts']

        if not tables:
            self._sql_status_label.setText(f"{name} contains no tables.")
            return

        try:
            self._sql_meta_conn = sqlite3.connect(self._sql_db_path)
        except Exception:
            self._sql_status_label.setText(f"{name} is not a valid SQLite database.")
            self._clear_sqlite_preview()
            return

        self._sql_table_counts = counts
        self._sql_tables = list(tables)
        self._sql_table_combo.blockSignals(True)
        self._sql_table_combo.clear()
        for t in tables:
            self._sql_table_combo.addItem(f"{t}  ({counts[t]:,} rows)", t)
        self._sql_table_combo.setCurrentIndex(0)
        self._sql_table_combo.blockSignals(False)

        # A WAL is always shown as the unified change view; the text filter
        # doesn't apply there, so hide it.  The "Affected only" toggle takes
        # its place in that view.
        self._sql_diff_mode = self._sql_has_wal
        self._sql_affected_only = False
        self._sql_affected_counts = None
        self._sql_affected_check.blockSignals(True)
        self._sql_affected_check.setChecked(False)
        self._sql_affected_check.setVisible(self._sql_has_wal)
        self._sql_affected_check.blockSignals(False)
        self._sql_filter_input.setVisible(not self._sql_has_wal)

        self._set_sql_controls_enabled(True)
        total = sum(counts.values())
        wal_note = "  ·  WAL present — showing changes inline" if self._sql_has_wal else ""
        self._sql_status_label.setText(
            f"{name} — {len(tables)} table{'s' if len(tables) != 1 else ''} · "
            f"{total:,} rows{wal_note}")
        self._on_sql_table_changed(0)

    def _on_sql_table_changed(self, idx: int) -> None:
        if idx < 0 or self._sql_db_path is None:
            return
        table = self._sql_table_combo.itemData(idx)
        if not table:
            return

        self._cancel_sql_filter_worker()
        self._sql_filter_input.blockSignals(True)
        self._sql_filter_input.clear()
        self._sql_filter_input.blockSignals(False)

        if self._sql_diff_mode:
            self._show_table_diff(table)
        else:
            self._load_table_normal(table)

    def _load_table_normal(self, table: str) -> None:
        """Browse all rows of *table* (paged for rowid tables)."""
        if self._sql_table_view.model() is not self._sql_model:
            self._sql_table_view.setModel(self._sql_model)

        try:
            cols = [r[1] for r in self._sql_meta_conn.execute(
                f'PRAGMA table_info("{table}")')]
        except Exception as exc:
            self._sql_status_label.setText(f"Could not read columns: {exc}")
            return

        # Try rowid mode (handles big tables via the model's page cache).
        try:
            ids = [r[0] for r in self._sql_meta_conn.execute(
                f'SELECT rowid FROM "{table}" ORDER BY rowid')]
            self._sql_list_mode = False
            self._sql_model.load_from_db(
                sqlite3.connect(self._sql_db_path), table, cols, ids)
            shown = len(ids)
        except Exception:
            # WITHOUT ROWID tables have no rowid — fall back to in-memory list.
            try:
                rows = list(self._sql_meta_conn.execute(
                    f'SELECT * FROM "{table}" LIMIT {_LIST_MODE_CAP}'))
            except Exception as exc:
                self._sql_status_label.setText(f"Could not load table: {exc}")
                return
            self._sql_list_mode = True
            self._sql_model.load_rows(cols, rows)
            shown = len(rows)

        self._sql_resize_columns()
        total = self._sql_table_counts.get(table, shown)
        suffix = f" (showing first {shown:,})" if shown < total else ""
        self._sql_status_label.setText(f'{table} — {total:,} rows{suffix}')

    # ── WAL net-change diff ──────────────────────────────────────────────────────

    def _read_keyed(self, conn: sqlite3.Connection, table: str) -> tuple[list, dict]:
        """Return (columns, {key: values}) for *table* on *conn*.

        Keyed by rowid for ordinary tables, or by primary-key columns for
        WITHOUT ROWID tables (falling back to the whole row when there is no
        primary key).  Returns ([], {}) when the table does not exist."""
        try:
            info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        except Exception:
            return [], {}
        if not info:
            return [], {}
        columns = [r[1] for r in info]
        pk_cols = [r[1] for r in sorted((x for x in info if x[5]), key=lambda x: x[5])]

        try:
            cur = conn.execute(f'SELECT rowid, * FROM "{table}"')
            use_rowid = True
        except Exception:
            cur = conn.execute(f'SELECT * FROM "{table}"')
            use_rowid = False

        m: dict = {}
        if use_rowid:
            for r in cur:
                m[('rid', r[0])] = tuple(r[1:])
        elif pk_cols:
            pk_idx = [columns.index(c) for c in pk_cols]
            for r in cur:
                m[('pk', tuple(r[i] for i in pk_idx))] = tuple(r)
        else:
            for r in cur:
                m[('row', tuple(r))] = tuple(r)
        return columns, m

    def _compute_table_diff(self, table: str):
        """Return (columns, rows, (before, deleted, modified, added)) for the
        unified WAL view of *table*.

        The whole pre-WAL table is shown in its original order: unchanged rows
        plain, modified rows once with changed cells rendered "old → new",
        deleted rows kept in place (flagged DELETED), and rows the WAL added
        appended at the end (flagged ADDED)."""
        before_conn = sqlite3.connect(self._sql_before_path)
        try:
            before_cols, before_map = self._read_keyed(before_conn, table)
        finally:
            before_conn.close()
        after_cols, after_map = self._read_keyed(self._sql_meta_conn, table)

        columns = after_cols or before_cols
        ncols   = len(columns)

        def pad(vals):
            vals = list(vals)
            return vals[:ncols] + [None] * (ncols - len(vals))

        def cell(v):
            return 'NULL' if v is None else str(v)

        rows = []
        deleted = modified = added = 0

        # Walk the original (pre-WAL) table in order.
        for k, bv in before_map.items():
            av = after_map.get(k)
            if av is None:
                deleted += 1
                rows.append(('DELETED', pad(bv), set()))
            elif av != bv:
                modified += 1
                bv, av = pad(bv), pad(av)
                changed, vals = set(), []
                for i in range(ncols):
                    if bv[i] != av[i]:
                        changed.add(i)
                        vals.append(f"{cell(bv[i])} → {cell(av[i])}")
                    else:
                        vals.append(av[i])
                rows.append(('MODIFIED', vals, changed))
            else:
                rows.append(('', pad(av), set()))

        # Rows the WAL introduced have no place in the original order — append.
        for k, av in after_map.items():
            if k not in before_map:
                added += 1
                rows.append(('ADDED', pad(av), set()))

        return columns, rows, (len(before_map), deleted, modified, added)

    def _show_table_diff(self, table: str) -> None:
        if self._sql_table_counts.get(table, 0) > _DIFF_ROW_CAP:
            self._sql_status_label.setText(
                f'{table} — too large to diff (> {_DIFF_ROW_CAP:,} rows); '
                f'showing all rows instead')
            self._load_table_normal(table)
            return
        try:
            columns, rows, (before, deleted, modified, added) = \
                self._compute_table_diff(table)
        except Exception as exc:
            self._sql_status_label.setText(f"Could not diff {table}: {exc}")
            self._load_table_normal(table)
            return

        if self._sql_affected_only:
            rows = [r for r in rows if r[0]]      # keep only ADDED/DELETED/MODIFIED

        self._sql_diff_model.load(columns, rows)
        if self._sql_table_view.model() is not self._sql_diff_model:
            self._sql_table_view.setModel(self._sql_diff_model)
        self._sql_resize_columns()

        if self._sql_affected_only:
            self._sql_status_label.setText(
                f'{table} — {deleted + modified + added:,} WAL updates · '
                f'{deleted:,} deleted · {modified:,} modified · {added:,} added')
        else:
            self._sql_status_label.setText(
                f'{table} — {before:,} rows before WAL · '
                f'{deleted:,} deleted · {modified:,} modified · {added:,} added')

    # ── "Affected only" toggle (WAL view) ────────────────────────────────────────

    def _on_sql_affected_toggled(self, checked: bool) -> None:
        self._sql_affected_only = checked
        if checked and self._sql_affected_counts is None:
            self._compute_affected_counts()
        self._rebuild_table_combo()
        if self._sql_table_combo.count() == 0:        # nothing the WAL touched
            self._sql_diff_model.clear()
            self._sql_status_label.setText("No tables were updated by the WAL")
            return
        self._on_sql_table_changed(self._sql_table_combo.currentIndex())

    def _compute_affected_counts(self) -> None:
        """Diff every table once to learn how many rows each had affected by the
        WAL, so the table dropdown can show which tables changed.  Cached."""
        counts = {}
        for i in range(self._sql_table_combo.count()):
            t = self._sql_table_combo.itemData(i)
            if not t:
                continue
            if self._sql_table_counts.get(t, 0) > _DIFF_ROW_CAP:
                counts[t] = None                 # too large to diff
                continue
            try:
                _cols, _rows, (_before, d, m, a) = self._compute_table_diff(t)
                counts[t] = d + m + a
            except Exception:
                counts[t] = None
        self._sql_affected_counts = counts

    def _rebuild_table_combo(self) -> None:
        """Repopulate the table dropdown for the current mode.  In "WAL updates
        only" mode it shows each table's update count and drops tables the WAL
        never touched; otherwise it lists every table with its row count."""
        combo = self._sql_table_combo
        keep  = combo.currentData()
        counts = self._sql_affected_counts or {}
        combo.blockSignals(True)
        combo.clear()
        for t in self._sql_tables:
            if self._sql_affected_only:
                n = counts.get(t)
                if n == 0:                        # untouched — hide it
                    continue
                text = f"{t}  ({n:,} updates)" if n is not None else f"{t}  (?)"
            else:
                text = f"{t}  ({self._sql_table_counts.get(t, 0):,} rows)"
            combo.addItem(text, t)
        idx = combo.findData(keep)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _sql_resize_columns(self, max_width: int = 320) -> None:
        """Estimate column widths from the first 200 visible rows of the
        currently displayed model."""
        model  = self._sql_table_view.model()
        fm     = self._sql_table_view.fontMetrics()
        n_cols = model.columnCount()
        n_rows = min(200, model.rowCount())
        for col in range(n_cols):
            header = model.headerData(col, Qt.Orientation.Horizontal) or ''
            w = fm.horizontalAdvance(str(header)) + 24
            for row in range(n_rows):
                text = model.data(model.index(row, col)) or ''
                w = max(w, fm.horizontalAdvance(text) + 12)
            self._sql_table_view.setColumnWidth(col, min(w, max_width))

    # ── Filtering ──────────────────────────────────────────────────────────────

    def _apply_sql_filter(self) -> None:
        if self._sql_diff_mode:   # filter is hidden in the unified WAL view
            return
        term  = self._sql_filter_input.text()
        total = self._sql_model.total_rows
        if not term:
            self._cancel_sql_filter_worker()
            self._sql_model.clear_filter()
            self._update_sql_filter_status(total, total)
            return

        # List-mode fallback: filter synchronously in Python.
        if self._sql_list_mode or self._sql_model._conn is None:
            self._cancel_sql_filter_worker()
            count = self._sql_model.filter_rows_inmem(term, -1)
            self._update_sql_filter_status(count, total)
            return

        # DB mode: run the LIKE on a background thread.
        self._cancel_sql_filter_worker()
        worker = ArtifactFilterWorker(
            self._sql_db_path,
            self._sql_model._table,
            self._sql_model._columns,
            term, -1,
        )
        worker.done.connect(self._on_sql_filter_done)
        self._sql_filter_worker = worker
        worker.start()

    def _on_sql_filter_done(self, rowids: list, where: str, wargs: list) -> None:
        self._sql_model.apply_rowids(rowids, where, wargs)
        self._update_sql_filter_status(len(rowids), self._sql_model.total_rows)

    def _update_sql_filter_status(self, shown: int, total: int) -> None:
        table = self._sql_table_combo.currentData() or ''
        if shown < total:
            self._sql_status_label.setText(
                f'{table} — {shown:,} of {total:,} rows (filtered)')
        else:
            self._sql_status_label.setText(f'{table} — {total:,} rows')

    def _cancel_sql_filter_worker(self) -> None:
        if self._sql_filter_worker and self._sql_filter_worker.isRunning():
            try:
                self._sql_filter_worker.done.disconnect()
            except Exception:
                pass
        self._sql_filter_worker = None

    # ── Export ─────────────────────────────────────────────────────────────────

    def _export_sql_table_csv(self) -> None:
        model = self._sql_table_view.model()
        cols  = list(getattr(model, '_columns', []))
        if not cols:
            return
        table = self._sql_table_combo.currentData() or 'table'
        default = f"{table}_wal_changes.csv" if self._sql_diff_mode else f"{table}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Table to CSV", default, "CSV Files (*.csv)")
        if not path:
            return

        nrows = model.rowCount()
        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                for row in range(nrows):
                    writer.writerow([
                        model.data(model.index(row, c)) for c in range(len(cols))
                    ])
        except Exception as exc:
            self._sql_status_label.setText(f"Export failed: {exc}")
            return
        self._sql_status_label.setText(f"Exported {nrows:,} rows to {path}")

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def _clear_sqlite_preview(self) -> None:
        self._stop_sqlite_preview_worker()
        self._cancel_sql_filter_worker()
        for model in (getattr(self, '_sql_model', None),
                      getattr(self, '_sql_diff_model', None)):
            try:
                model.clear()
            except Exception:
                pass
        if self._sql_meta_conn is not None:
            try:
                self._sql_meta_conn.close()
            except Exception:
                pass
            self._sql_meta_conn = None
        self._sql_table_combo.blockSignals(True)
        self._sql_table_combo.clear()
        self._sql_table_combo.blockSignals(False)
        self._sql_affected_check.blockSignals(True)
        self._sql_affected_check.setChecked(False)
        self._sql_affected_check.setVisible(False)
        self._sql_affected_check.blockSignals(False)
        self._sql_filter_input.setVisible(True)
        if self._sql_table_view.model() is not self._sql_model:
            self._sql_table_view.setModel(self._sql_model)
        self._sql_table_counts = {}
        self._sql_list_mode = False
        self._sql_has_wal   = False
        self._sql_diff_mode = False
        self._sql_affected_only = False
        self._sql_affected_counts = None
        self._sql_tables = []
        self._set_sql_controls_enabled(False)
        if self._sql_tmpdir and os.path.isdir(self._sql_tmpdir):
            shutil.rmtree(self._sql_tmpdir, ignore_errors=True)
        self._sql_tmpdir      = None
        self._sql_db_path     = None
        self._sql_before_path = None
        self._sql_status_label.setText("No database selected")
