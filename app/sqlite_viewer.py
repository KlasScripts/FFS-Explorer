"""sqlite_viewer.py — SqliteViewerMixin: a generic SQLite database browser.

Adds a "Database" tab to the File Preview panel.  When a .db/.sqlite file is
selected, the database is extracted from the archive to a temp copy, its tables
are listed (with row counts), and the chosen table's rows are browsed in a paged
grid with a text filter and CSV export.

When the database ships with a WAL sidecar, the table is shown as a unified
net-change view: the pre-WAL (last-checkpointed) table with the rows the WAL
added / deleted / modified highlighted in place. Below those come recovered
versions that are not in the live table: WAL HISTORY (an older version in a
WAL frame, such as a row added and deleted again, or an earlier edit),
JOURNAL (a page as it was before a change, from a rollback journal), and
CARVED (a deleted record still in free space on this table's own pages).
CARVED (freed page) comes from a page no table owns any more: it is placed
in this table only because the column count matches, so the table is not
certain. CARVED (pattern) was found by its record-header pattern, has no
rowid, and shows [cut off] for columns that were not recovered (never a
blank that could be read as NULL). A table the WAL dropped is still listed, its rows DELETED. A
database with no WAL shows the plain paged table unless a table has
JOURNAL or CARVED rows.

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
    QTableView, QPushButton, QFileDialog, QCheckBox, QDialog, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QAbstractTableModel, QModelIndex
from PySide6.QtGui import QColor, QBrush

from artifact_viewer import ArtifactTableModel, ArtifactFilterWorker
from zip_cd_cache import capture_alias, capture_marker


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
    'WAL HISTORY': QColor(66, 133, 244, 55),          # blue
    'WAL HISTORY (older WAL)': QColor(66, 133, 244, 30),
    'JOURNAL':  QColor(0, 150, 136, 50),     # teal
    'CARVED':   QColor(156, 39, 176, 45),    # purple
    'CARVED (freed page)': QColor(156, 39, 176, 25),
    'CARVED (pattern)':    QColor(156, 39, 176, 25),
}
_DIFF_CELL_CHANGED = QColor(255, 193, 7, 130)   # stronger amber for changed cells

# Hover text on the Change column. Ten words or fewer each.
_DIFF_TIP = {
    'ADDED':    "Only in the WAL, not yet in the main file.",
    'DELETED':  "In the main file, removed by the WAL.",
    'MODIFIED': "Changed by the WAL. Cells show old → new.",
    'WAL HISTORY': "Older version in a WAL frame of this table's page.",
    'WAL HISTORY (older WAL)': "Earlier WAL round. Page may have held another table.",
    'JOURNAL':  "Older page from rollback journal. May be another table's.",
    'CARVED':   "Deleted record from free space on this table's pages.",
    'CARVED (freed page)': "Freed page. Table not certain: matched by column count.",
    'CARVED (pattern)': "Found by record pattern, no rowid. [cut off] = missing.",
}

_JOURNAL_MAGIC = b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7'

# A column a pattern-carved record was cut off before. Not NULL: the value
# was not recovered, so the cell must not look like a stored empty value.
_CUT_OFF = '[cut off]'


def _journal_pages(data: bytes) -> list[tuple[int, bytes]]:
    """(page number, page image) for each record in a rollback journal.

    The header is 28 bytes: magic, record count, nonce, original page
    count, sector size, page size. Records start at the sector size and
    are page number (4), the page as it was before the change, and a
    checksum (4). Records are read to the end of the file, so the count
    in the header (often 0 in a journal left behind) is not relied on.
    """
    import struct
    if len(data) < 28 or data[:8] != _JOURNAL_MAGIC:
        return []
    _n, _nonce, _orig, sector, page_size = struct.unpack('>IIIII', data[8:28])
    if not page_size or not sector or page_size & (page_size - 1):
        return []
    out = []
    off = sector
    step = page_size + 8
    while off + step <= len(data):
        page_no = struct.unpack('>I', data[off:off + 4])[0]
        if page_no:
            out.append((page_no, data[off + 4:off + 4 + page_size]))
        off += step
    return out


class CellChange:
    """A MODIFIED cell: the pre-WAL and final raw values. Shown "old → new",
    and equal to that text, so callers and tests can compare it as before."""

    __slots__ = ('old', 'new')

    def __init__(self, old, new):
        self.old, self.new = old, new

    def text(self, fmt=None) -> str:
        def one(v):
            if v is None:
                return 'NULL'
            return fmt(v) if fmt else str(v)
        return f"{one(self.old)} → {one(self.new)}"

    def __str__(self) -> str:
        return self.text()

    def __eq__(self, other):
        if isinstance(other, CellChange):
            return (self.old, self.new) == (other.old, other.new)
        return isinstance(other, str) and other == str(self)

    __hash__ = None


class SqliteDiffModel(QAbstractTableModel):
    """In-memory model for the WAL net-change view of one table.

    Each row is (status, values, changed_cols):
      status       — 'ADDED' | 'DELETED' | 'MODIFIED' | a recovered type
      values       — raw values; a MODIFIED row's changed cells are a
                     CellChange, shown "old → new"
      changed_cols — set of 0-based *table* column indices that differ
                     (used to paint the individual changed cells)
    A leading "Change" column shows the status. Columns in set_epoch()'s
    units are shown as dates and times, with the header text it gives.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: list = []      # includes the leading "Change" column
        self._rows:    list = []      # [(status, values, changed_cols), …]
        self._view:    list = []      # indices into _rows (filtered order)
        self._ts_units: dict = {}     # table column name -> epoch code
        self._ts_fmt = None           # (raw value, code) -> text
        self._labels: dict = {}       # table column name -> header text

    def load(self, table_columns: list, rows: list) -> None:
        self.beginResetModel()
        self._columns = ["Change"] + list(table_columns)
        self._rows    = rows
        self._view    = list(range(len(rows)))
        self._ts_units, self._ts_fmt, self._labels = {}, None, {}
        self.endResetModel()

    def set_epoch(self, units: dict, fmt, labels: dict) -> None:
        self._ts_units = dict(units or {})
        self._ts_fmt = fmt if units else None
        self._labels = dict(labels or {})
        if self._columns:
            self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(self._columns) - 1)
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(max(0, len(self._view) - 1), len(self._columns) - 1))

    def clear(self) -> None:
        self.beginResetModel()
        self._columns = []
        self._rows    = []
        self._view    = []
        self._ts_units, self._ts_fmt, self._labels = {}, None, {}
        self.endResetModel()

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Header click: order the rows by the raw value, so a converted
        time column sorts by time. A changed cell sorts by its new value.
        Blank sorts first; numbers before text; anything else by its text."""
        if not (0 <= column < len(self._columns)):
            return

        def key(i):
            status, vals, _changed = self._rows[i]
            if column == 0:
                value = status
            else:
                value = vals[column - 1] if column - 1 < len(vals) else None
            if isinstance(value, CellChange):
                value = value.new
            if value is None or value == '':
                return (0, 0, '')
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return (1, value, '')
            return (2, 0, str(value))

        self.layoutAboutToBeChanged.emit()
        self._view.sort(key=key, reverse=order == Qt.SortOrder.DescendingOrder)
        self.layoutChanged.emit()

    def _display(self, value, name: str) -> str:
        code = self._ts_units.get(name)
        fmt = (lambda v: self._ts_fmt(v, code)) if code and self._ts_fmt else None
        if isinstance(value, CellChange):
            return value.text(fmt)
        if value is None:
            return ''
        return fmt(value) if fmt else str(value)

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
            return self._display(v, self._columns[col])

        if role == Qt.ItemDataRole.ToolTipRole and col == 0:
            return _DIFF_TIP.get(status)

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
            if section >= len(self._columns):
                return ''
            name = self._columns[section]
            return self._labels.get(name, name) if section else name
        return section + 1


class SqlitePreviewWorker(QThread):
    """Extracts a SQLite db (+ WAL/SHM sidecars) to a temp copy and counts
    every table's rows — off the GUI thread.  Both the extraction write and
    the per-table SELECT COUNT(*) loop can take seconds on a large database
    (e.g. a case on a network share), and this previously ran synchronously
    in _load_sqlite_preview on every file click."""
    done = Signal(dict)

    def __init__(self, raw: bytes, name: str, wal: bytes | None, shm: bytes | None,
                 journal: bytes | None = None):
        super().__init__()
        self._raw  = raw
        self._name = name
        self._wal  = wal
        self._shm  = shm
        self._journal = journal

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
        # so the GUI thread can compute the pre-WAL (last-checkpointed) state,
        # and the WAL bytes themselves: closing a connection on db_path
        # checkpoints the WAL into that copy and deletes its -wal.
        has_wal = wrote_wal
        before_path = None
        wal_copy = None
        if wrote_wal:
            before_dir = os.path.join(tmpdir, 'before')
            os.makedirs(before_dir, exist_ok=True)
            before_path = os.path.join(before_dir, self._name)
            wal_copy = os.path.join(tmpdir, 'wal-frames')
            try:
                with open(before_path, 'wb') as f:
                    f.write(self._raw)
                with open(wal_copy, 'wb') as f:
                    f.write(self._wal)
            except Exception:
                has_wal = False
                before_path = None
                wal_copy = None

        # The main file as it came out of the archive, for carving, and the
        # rollback journal. The journal is never put beside db_path: SQLite
        # would roll it back into the copy on open.
        main_copy = os.path.join(tmpdir, 'main-file')
        journal_copy = None
        try:
            with open(main_copy, 'wb') as f:
                f.write(self._raw)
        except Exception:
            main_copy = None
        if self._journal and self._journal[:8] == _JOURNAL_MAGIC:
            journal_copy = os.path.join(tmpdir, 'journal-pages')
            try:
                with open(journal_copy, 'wb') as f:
                    f.write(self._journal)
            except Exception:
                journal_copy = None

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

        # A table the WAL dropped exists only in the pre-WAL copy.
        dropped = []
        if before_path:
            try:
                before = sqlite3.connect(f'file:{before_path}?immutable=1', uri=True)
                try:
                    for (t,) in before.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                        if t not in counts:
                            dropped.append(t)
                            counts[t] = 0
                finally:
                    before.close()
            except Exception:
                dropped = []
        tables = tables + dropped

        self.done.emit({
            'tmpdir': tmpdir, 'db_path': db_path, 'before_path': before_path,
            'wal_copy': wal_copy, 'dropped': dropped,
            'main_copy': main_copy, 'journal_copy': journal_copy,
            'has_wal': has_wal, 'name': self._name, 'tables': tables, 'counts': counts,
        })


class EpochFormatsDialog(QDialog):
    """Which columns of one table are times, and in which epoch format.

    One row per column: an example value from the table, the format (or
    "Not a time"), the example as it would be shown, and who set it.
    Changing a format updates the example straight away, so a wrong guess
    is easy to see. changes() is the columns whose format was changed.
    """

    _SET_BY = {'yours': "Your setting", 'built-in': "Built-in", 'guessed': "Guessed", None: ""}

    def __init__(self, parent, table: str, rows: list, fmt):
        import epoch_guess
        super().__init__(parent)
        self.setWindowTitle(f"Epoch Formats — {table}")
        self.resize(940, 540)
        self.cleared = False
        self._fmt = fmt
        self._initial = {column: code for column, _s, code, _src in rows}
        self._source = {column: source for column, _s, _code, source in rows}
        # Converted columns first, then the rest in table order.
        self._rows = sorted(rows, key=lambda r: (not r[2], rows.index(r)))

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Columns shown as a date and time in this table. A guess comes from the "
            "column's own numbers; a built-in setting ships with FFS Explorer. Pick a "
            "format to see the example converted. \"Not a time\" shows the raw number. "
            "Your choices are saved for this database path, table and column, and "
            "apply to every case on this install.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._grid = QTableWidget(len(self._rows), 5)
        self._grid.setHorizontalHeaderLabels(
            ["Column", "Example value", "Format", "Shown as", "Set by"])
        self._grid.verticalHeader().setVisible(False)
        self._combos = []
        for i, (column, sample, code, source) in enumerate(self._rows):
            self._grid.setItem(i, 0, self._cell(column))
            self._grid.setItem(i, 1, self._cell("(no value)" if sample is None else str(sample)))
            combo = QComboBox()
            combo.addItem("Not a time", "")
            for key in epoch_guess.FORMATS:
                combo.addItem(epoch_guess.label(key), key)
            combo.setCurrentIndex(max(0, combo.findData(code or "")))
            combo.currentIndexChanged.connect(lambda _i, row=i: self._update(row))
            self._grid.setCellWidget(i, 2, combo)
            self._combos.append(combo)
            self._grid.setItem(i, 3, self._cell(""))
            self._grid.setItem(i, 4, self._cell(""))
            self._update(i)
        header = self._grid.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._grid.setColumnWidth(1, 200)
        layout.addWidget(self._grid, stretch=1)

        buttons = QHBoxLayout()
        clear = QPushButton("Clear My Settings for This Table")
        clear.setToolTip("Go back to the built-in settings and the guess.")
        clear.clicked.connect(self._clear)
        buttons.addWidget(clear)
        buttons.addStretch()
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        layout.addLayout(buttons)

    @staticmethod
    def _cell(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _update(self, row: int) -> None:
        column, sample, _code, _source = self._rows[row]
        code = self._combos[row].currentData() or ""
        if sample is None:
            shown = ""
        elif code:
            shown = self._fmt(sample, code)
        else:
            shown = str(sample)
        self._grid.item(row, 3).setText(shown)
        changed = code != (self._initial.get(column) or "")
        source = 'yours' if changed else self._source.get(column)
        self._grid.item(row, 4).setText(self._SET_BY.get(source, ""))

    def _clear(self) -> None:
        self.cleared = True
        self.accept()

    def changes(self) -> dict:
        """{column: code} for each column whose format was changed; ""
        means the examiner marked it as not a time."""
        out = {}
        for row, (column, _sample, _code, _source) in enumerate(self._rows):
            code = self._combos[row].currentData() or ""
            if code != (self._initial.get(column) or ""):
                out[column] = code
        return out


class SqliteViewerMixin:

    # ── Setup ──────────────────────────────────────────────────────────────────

    def _setup_sqlite_tab(self) -> QWidget:
        """Build the Database preview tab and initialise state.  Returns the
        QWidget to be added to self.preview_tabs."""
        self._sql_tmpdir:       str | None = None
        self._sql_db_path:      str | None = None
        self._sql_before_path:  str | None = None   # main file alone (pre-WAL)
        self._sql_wal_copy:     str | None = None   # WAL bytes, for WAL HISTORY rows
        self._sql_main_copy:    str | None = None   # main file as extracted, for CARVED rows
        self._sql_journal_copy: str | None = None   # rollback journal, for JOURNAL rows
        self._sql_dropped:      set = set()         # tables only in the pre-WAL copy
        self._sql_meta_conn:    sqlite3.Connection | None = None
        self._sql_filter_worker = None
        self._sql_preview_worker = None
        self._sql_list_mode     = False          # True for WITHOUT ROWID tables
        self._sql_has_wal       = False
        self._sql_diff_mode     = False
        self._sql_affected_only = False          # WAL view: hide unchanged rows
        self._sql_affected_counts = None         # {table: affected count}, lazy
        self._sql_tables        = []             # all table names, display order
        self._sql_ui_path       = ''             # archive path of the open database
        self._sql_epoch         = {}             # {table: {column: (code, source)}}
        self._sql_epoch_upper   = None           # latest plausible time (s)
        self._sql_model         = ArtifactTableModel()
        self._sql_diff_model    = SqliteDiffModel()

        # ── Controls row ────────────────────────────────────────────────────
        self._sql_table_combo = QComboBox()
        self._sql_table_combo.setMinimumWidth(220)
        self._sql_table_combo.currentIndexChanged.connect(self._on_sql_table_changed)

        self._sql_affected_check = QCheckBox("Changes only")
        self._sql_affected_check.setToolTip(
            "Show only changed and recovered rows: WAL, journal, carved.")
        self._sql_affected_check.toggled.connect(self._on_sql_affected_toggled)
        self._sql_affected_check.setVisible(False)

        self._sql_filter_input = QLineEdit()
        self._sql_filter_input.setPlaceholderText("Filter rows (stored values: a date column is searched as its raw number)…")
        self._sql_filter_input.setClearButtonEnabled(True)
        self._sql_filter_timer = QTimer(self)
        self._sql_filter_timer.setSingleShot(True)
        self._sql_filter_timer.setInterval(250)
        self._sql_filter_timer.timeout.connect(self._apply_sql_filter)
        self._sql_filter_input.textChanged.connect(
            lambda _t: self._sql_filter_timer.start())

        self._sql_export_btn = QPushButton("Export CSV")
        self._sql_export_btn.clicked.connect(self._export_sql_table_csv)

        self._sql_epoch_btn = QPushButton("Epoch Formats…")
        self._sql_epoch_btn.setToolTip(
            "Choose which columns are times, and their epoch format.")
        self._sql_epoch_btn.clicked.connect(self._open_epoch_formats_dialog)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Table:"))
        controls.addWidget(self._sql_table_combo)
        controls.addWidget(self._sql_affected_check)
        controls.addWidget(self._sql_filter_input, stretch=1)
        controls.addWidget(self._sql_epoch_btn)
        controls.addWidget(self._sql_export_btn)

        # ── Table view ──────────────────────────────────────────────────────
        self._sql_table_view = QTableView()
        self._sql_table_view.setModel(self._sql_model)
        self._sql_table_view.setAlternatingRowColors(True)
        self._sql_table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._sql_table_view.horizontalHeader().setStretchLastSection(True)
        self._sql_table_view.verticalHeader().setVisible(False)
        # Click a header to sort. No indicator at first, so a table opens in
        # its stored order (the change view's order is part of the meaning).
        self._sql_table_view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._sql_table_view.setSortingEnabled(True)

        self._sql_status_label = QLabel("No database selected")
        # Which capture of a database the zip stores more than once — its
        # own label, since the status line above is rewritten per table.
        self._sql_capture_label = QLabel()
        self._sql_capture_label.setWordWrap(True)
        self._sql_capture_label.setVisible(False)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(controls)
        layout.addWidget(self._sql_table_view, stretch=1)
        layout.addWidget(self._sql_status_label)
        layout.addWidget(self._sql_capture_label)

        self._set_sql_controls_enabled(False)
        return tab

    def _set_sql_controls_enabled(self, enabled: bool) -> None:
        self._sql_table_combo.setEnabled(enabled)
        self._sql_filter_input.setEnabled(enabled)
        self._sql_export_btn.setEnabled(enabled)
        self._sql_epoch_btn.setEnabled(enabled)
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
        self._sql_ui_path = ui_path
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
        wal = shm = journal = None
        if sidecar_reader is not None:
            for suffix in ('-wal', '-shm', '-journal'):
                try:
                    sidecar = sidecar_reader(suffix)
                except Exception:
                    sidecar = None
                if suffix == '-wal':
                    wal = sidecar
                elif suffix == '-shm':
                    shm = sidecar
                else:
                    journal = sidecar

        note = self._capture_note(ui_path, wal)
        self._sql_capture_label.setText(note)
        self._sql_capture_label.setVisible(bool(note))
        self._set_sql_controls_enabled(False)
        self._sql_status_label.setText(f"Loading {name}…")
        self._stop_sqlite_preview_worker()
        self._sql_preview_worker = SqlitePreviewWorker(raw, name, wal, shm, journal)
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
        self._sql_wal_copy    = result.get('wal_copy')
        self._sql_main_copy   = result.get('main_copy')
        self._sql_journal_copy = result.get('journal_copy')
        self._sql_dropped     = set(result.get('dropped') or ())
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
        self._sql_tables = sorted(tables, key=lambda t: (-counts.get(t, 0), t.casefold()))
        self._sql_table_combo.blockSignals(True)
        self._sql_table_combo.clear()
        for t in self._sql_tables:
            self._sql_table_combo.addItem(self._sql_table_label(t), t)
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
        self._sql_affected_check.setVisible(True)
        self._sql_affected_check.blockSignals(False)
        self._sql_filter_input.setVisible(not self._sql_has_wal)

        self._set_sql_controls_enabled(True)
        total = sum(counts.values())
        wal_note = "  ·  WAL present — showing changes inline" if self._sql_has_wal else ""
        self._sql_status_label.setText(
            f"{name} — {len(tables)} table{'s' if len(tables) != 1 else ''} · "
            f"{total:,} rows{wal_note}")
        self._on_sql_table_changed(0)

    def _sql_table_label(self, table: str) -> str:
        if table in self._sql_dropped:
            return f"{table}  (dropped in WAL)"
        return f"{table}  ({self._sql_table_counts.get(table, 0):,} rows)"

    def _capture_note(self, ui_path: str, wal) -> str:
        """Status-line note for a copy of a database the zip stores more than
        once (ASSUMPTIONS.md A-72): which capture this is, and — when this
        copy has no WAL of its own but another capture of it does — say so
        rather than borrowing that other capture's WAL, which would mix two
        moments of the database."""
        meta = getattr(self, 'full_metadata', None) or {}
        cap = (meta.get(ui_path) or {}).get('capture')
        if not cap:
            return ''
        note = (f"Capture {cap} of this database — the zip holds more than one "
                "copy (ASSUMPTIONS.md A-72).")
        if wal:
            if (meta.get(ui_path + '-wal') or {}).get('capture'):
                return note + " Using the WAL from the same capture."
            return note + " Using the only WAL captured (it was written once)."
        k, n = (int(x) for x in cap.split(' of '))
        marker = capture_marker(ui_path)
        main = ui_path.replace(marker, '') if marker else ui_path
        others = [main] + [capture_alias(main, j, n) for j in range(1, n)]
        with_wal = [o for o in others if o != ui_path and (o + '-wal') in meta]
        if with_wal:
            which = (meta.get(with_wal[0]) or {}).get('capture', '')
            note += (f" No WAL was captured with this copy; capture {which} "
                     "has one (not borrowed, so the two moments aren't mixed).")
        return note

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
        # A newly opened table starts in stored order, not the last sort.
        self._sql_table_view.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

        if self._sql_diff_mode:
            self._show_table_diff(table)
        elif not self._show_table_diff(table, only_if_recovered=True):
            self._load_table_normal(table)

    def _load_table_normal(self, table: str) -> None:
        """Browse all rows of *table* (paged for rowid tables)."""
        if self._sql_table_view.model() is not self._sql_model:
            self._sql_table_view.setModel(self._sql_model)
        self._sql_filter_input.setVisible(not self._sql_has_wal)

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

        units, labels = self._sql_epoch_units(table, cols)
        self._sql_model.set_timestamp_formatting(units, self._sql_format_epoch)
        self._sql_model.set_header_labels(labels)
        self._sql_resize_columns()
        total = self._sql_table_counts.get(table, shown)
        suffix = f" (showing first {shown:,})" if shown < total else ""
        self._sql_status_label.setText(
            f'{table} — {total:,} rows{suffix}{self._sql_epoch_note(units)}')

    # ── Epoch columns ────────────────────────────────────────────────────────────

    def _sql_store_path(self) -> str:
        """The open database's path with an iOS container GUID replaced by
        the bundle id, so a saved choice applies to every install."""
        path = self._sql_ui_path or ''
        adapter = getattr(self, '_adapter', None)
        if adapter is not None and hasattr(adapter, 'canonical_app_path'):
            try:
                path = adapter.canonical_app_path(
                    path, getattr(self, 'guid_to_bundle', None) or {},
                    getattr(self, 'folder_map', None))
            except Exception:
                pass
        return path

    def _sql_upper_time(self) -> float:
        """Latest plausible time for the guess: the extraction time when the
        case records one, else now (plus a day)."""
        if self._sql_epoch_upper is None:
            import epoch_guess
            upper = None
            try:
                import device_timezone
                zip_path = getattr(self, 'zip_path', None)
                got = device_timezone.detect_acquisition_offset(zip_path) if zip_path else None
                if got:
                    from datetime import timedelta, timezone
                    offset_h, acquired_local = got
                    upper = (acquired_local - timedelta(hours=offset_h)).replace(
                        tzinfo=timezone.utc).timestamp() + 86400
            except Exception:
                upper = None
            self._sql_epoch_upper = upper or epoch_guess.default_upper()
        return self._sql_epoch_upper

    def _sql_column_sample(self, table: str, column: str, limit: int = 500) -> list:
        try:
            return [r[0] for r in self._sql_meta_conn.execute(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT {limit}')]
        except Exception:
            return []

    def _sql_epoch_choices(self, table: str, columns: list) -> dict:
        """{column: (code, source)} for *table*: your saved choice, then a
        built-in setting, then a guess. source is "yours", "built-in" or
        "guessed"; code "" means not a time. Cached per table."""
        if table in self._sql_epoch:
            return self._sql_epoch[table]
        import epoch_guess
        import epoch_store
        path = self._sql_store_path()
        upper = self._sql_upper_time()
        found = {}
        for column in columns:
            code, source = epoch_store.lookup(path, table, column)
            if code is None:
                code = epoch_guess.guess(self._sql_column_sample(table, column), upper)
                source = 'guessed' if code else None
            if code is not None or source:
                found[column] = (code or '', source)
        self._sql_epoch[table] = found
        return found

    def _sql_epoch_units(self, table: str, columns: list) -> tuple[dict, dict]:
        """({column: code}, {column: header text}) for converted columns."""
        import epoch_guess
        units, labels = {}, {}
        for column, (code, source) in self._sql_epoch_choices(table, columns).items():
            if code:
                units[column] = code
                labels[column] = f"{column}  (converted: {epoch_guess.label(code)}, {source})"
        return units, labels

    def _sql_epoch_note(self, units: dict) -> str:
        if not units:
            return ''
        n = len(units)
        return (f" · {n} column{'s' if n != 1 else ''} shown as date and time"
                " — raw values in Epoch Formats…")

    def _sql_format_epoch(self, raw, code: str) -> str:
        """*raw* as a date and time, in the case's display setting. A zero
        stays "0": apps write 0 for "not set", not for 1 January 1970."""
        import epoch_guess
        if epoch_guess.number(raw) == 0:
            return str(raw)
        seconds = epoch_guess.to_unix(raw, code)
        if seconds is None:
            return str(raw)
        fmt = getattr(self, 'format_ts', None)
        if fmt is not None:
            return fmt(seconds)
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (OverflowError, OSError, ValueError):
            return str(raw)

    def _sql_refresh_times(self) -> None:
        """Repaint after the UTC / local time setting changes."""
        view = getattr(self, '_sql_table_view', None)
        if view is not None:
            view.viewport().update()

    def _open_epoch_formats_dialog(self) -> None:
        table = self._sql_table_combo.currentData()
        if not table or self._sql_meta_conn is None:
            return
        import epoch_store
        try:
            columns = [r[1] for r in self._sql_meta_conn.execute(f'PRAGMA table_info("{table}")')]
        except Exception:
            return
        choices = self._sql_epoch_choices(table, columns)
        rows = []
        for column in columns:
            sample = next((v for v in self._sql_column_sample(table, column, 50)
                           if v not in (None, '', 0, b'')), None)
            code, source = choices.get(column, ('', None))
            rows.append((column, sample, code, source))
        dialog = EpochFormatsDialog(self, table, rows, self._sql_format_epoch)
        if not dialog.exec():
            return
        path = self._sql_store_path()
        if dialog.cleared:
            epoch_store.save_user(path, table, {c: None for c in columns})
        else:
            epoch_store.save_user(path, table, dialog.changes())
        self._sql_epoch.pop(table, None)
        self._on_sql_table_changed(self._sql_table_combo.currentIndex())

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

    def _sql_file_bytes(self, path: str | None) -> bytes:
        if not path or not os.path.isfile(path):
            return b''
        with open(path, 'rb') as f:
            return f.read()

    def _recovered_rows(self, table: str, columns: list,
                        before_map: dict, after_map: dict) -> list:
        """Row versions that are not in the live table, from three places.

        WAL HISTORY: an older version in a WAL frame for one of this table's
        pages. "(older WAL)" when the frame's salt is from an earlier WAL
        generation. JOURNAL: an older version in a rollback-journal page,
        which holds the page as it was before a change. CARVED: a deleted
        record still in the main file, in free space on this table's pages
        or on a freed page (sqlite_carve, the same carving the reports use).
        A freed page can come from another table, so a carved row must have
        this table's column count and a rowid no live row uses, the same
        rule the reports apply. An ordinary single-row delete overwrites the
        cell's length and rowid, so free space is also searched by the
        table's record-header signature (learned from live rows, needs five
        or more). Those rows have no rowid: the id is left blank, and a row
        cut off by the end of the free range keeps only the columns read.

        A version equal to the pre-WAL or final row is not repeated. Cells
        that overflow onto another page are skipped by the decoder, so a
        cut-off row never shows as an edit. Rowid tables only.
        """
        import struct
        from sqlite_carve import (
            iter_wal_frames, decode_leaf_page_cells, walk_table_leaf_pages,
            _wal_latest_page_images, rowid_alias_column, parse_db_header,
            carve_unallocated_region, carve_freed_pages,
            sample_header_signature, carve_by_header_signature, _text_plausible)
        if any(k[0] != 'rid' for k in list(before_map) + list(after_map)):
            return []
        main = self._sql_file_bytes(self._sql_main_copy)
        wal = self._sql_file_bytes(self._sql_wal_copy)
        journal = self._sql_file_bytes(self._sql_journal_copy)
        try:
            header = parse_db_header(main)
            page_size = header['page_size']
        except ValueError:
            header = None
            page_size = struct.unpack('>I', wal[8:12])[0] if len(wal) >= 32 else 0
        if not page_size:
            return []
        wal_images = _wal_latest_page_images(wal, page_size) if wal else {}

        # Leaves of this table before and after the WAL; the after copy is
        # db_path, which closing the counting connection checkpointed.
        leaves, id_col, raws = set(), None, []
        sources = ((self._sql_main_copy, None), (self._sql_db_path, self._sql_meta_conn))
        for path, conn in sources:
            raw = self._sql_file_bytes(path)
            if not raw:
                continue
            own = conn is None
            if own:
                conn = sqlite3.connect(f'file:{path}?immutable=1', uri=True)
            try:
                row = conn.execute(
                    "SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                if row and row[0]:
                    own_leaves = walk_table_leaf_pages(raw, page_size, row[0], wal_images)
                    leaves |= set(own_leaves)
                    raws.append((raw, own_leaves))
                    id_col = id_col or rowid_alias_column(conn, table)
            except Exception:
                pass
            finally:
                if own:
                    conn.close()
        ncols = len(columns)
        id_idx = columns.index(id_col) if id_col in columns else None
        live = {k[1] for k in after_map}
        seen, out = set(), []

        def empty(vals):
            return all(v in (None, '', b'') for i, v in enumerate(vals) if i != id_idx)

        def keep(status, cell, carved=False):
            values = list(cell['values'])
            if len(values) != ncols:
                return
            if carved and (cell['rowid'] in live or empty(values)):
                return
            if id_idx is not None:
                values[id_idx] = cell['rowid']
            version = tuple(values)
            key = ('rid', cell['rowid'])
            if before_map.get(key) == version or after_map.get(key) == version:
                return
            if (cell['rowid'], version) in seen:
                return
            seen.add((cell['rowid'], version))
            out.append((status, values, set()))

        def page_cells(image, page_no):
            try:
                return decode_leaf_page_cells(image, page_no, 100 if page_no == 1 else 0)
            except (ValueError, IndexError):
                return []

        if wal and leaves:
            salt = wal[16:24]
            for frame in iter_wal_frames(wal, page_size):
                if frame['page'] not in leaves:
                    continue
                off = frame['frame_offset']
                status = ('WAL HISTORY' if wal[off + 8:off + 16] == salt
                          else 'WAL HISTORY (older WAL)')
                for cell in page_cells(frame['image'], frame['page']):
                    keep(status, cell)

        if journal and leaves:
            for page_no, image in _journal_pages(journal):
                if page_no in leaves:
                    for cell in page_cells(image, page_no):
                        keep('JOURNAL', cell)

        for raw, own_leaves in raws:
            try:
                cells = carve_unallocated_region(raw, page_size, own_leaves)
                own_header = parse_db_header(raw)
                # A freed page is no longer any table's: its rows are
                # placed here only because the column count matches.
                cells += [dict(c, _status='CARVED (freed page)')
                          for c in carve_freed_pages(raw, page_size, own_header)]
            except Exception:
                continue
            for cell in cells:
                keep(cell.get('_status', 'CARVED'), cell, carved=True)

        # No rowid survives these, so compare everything except the id.
        def without_id(vals):
            return tuple(v for i, v in enumerate(vals) if i != id_idx)
        live_rows = list(before_map.values()) + list(after_map.values())
        prefixes: dict = {}

        def known_prefix(n):
            if n not in prefixes:
                prefixes[n] = {without_id(list(v)[:n]) for v in live_rows}
            return prefixes[n]
        for raw, own_leaves in raws:
            try:
                signature = sample_header_signature(raw, page_size, own_leaves)
                cells = carve_by_header_signature(raw, page_size, own_leaves, signature)
            except Exception:
                continue
            for cell in cells:
                values = list(cell['values'])[:ncols]
                read = len(values)
                values += [None] * (ncols - read)
                if empty(values) or any(isinstance(v, str) and not _text_plausible(v) for v in values):
                    continue
                if id_idx is not None:
                    values[id_idx] = None
                # A cut-off record matches a live row when the columns it
                # has agree; the missing ones are not compared.
                version = without_id(values[:read])
                if version in known_prefix(read) or ('sig', read, version) in seen:
                    continue
                seen.add(('sig', read, version))
                values[read:] = [_CUT_OFF] * (ncols - read)
                if id_idx is not None and id_idx >= read:
                    values[id_idx] = None
                out.append(('CARVED (pattern)', values, set()))
        return out

    def _compute_table_diff(self, table: str):
        """Return (columns, rows, counts) for the change view of *table*.

        counts is {'before', 'deleted', 'modified', 'added', 'history',
        'journal', 'carved'}. The whole pre-WAL table is shown in its original
        order: unchanged rows plain, modified rows once with changed cells
        rendered "old → new", deleted rows kept in place (DELETED), rows the
        WAL added appended (ADDED), then recovered versions (WAL HISTORY,
        JOURNAL, CARVED). With no WAL, before and after are the same file."""
        after_cols, after_map = self._read_keyed(self._sql_meta_conn, table)
        if self._sql_before_path:
            before_conn = sqlite3.connect(self._sql_before_path)
            try:
                before_cols, before_map = self._read_keyed(before_conn, table)
            finally:
                before_conn.close()
        else:
            before_cols, before_map = after_cols, after_map

        columns = after_cols or before_cols
        ncols   = len(columns)

        def pad(vals):
            vals = list(vals)
            return vals[:ncols] + [None] * (ncols - len(vals))

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
                        vals.append(CellChange(bv[i], av[i]))
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

        try:
            recovered = self._recovered_rows(table, columns, before_map, after_map)
        except Exception:
            recovered = []
        rows.extend((status, pad(vals), changed) for status, vals, changed in recovered)

        def count(prefix):
            return sum(1 for status, _v, _c in recovered if status.startswith(prefix))

        return columns, rows, {
            'before': len(before_map), 'deleted': deleted, 'modified': modified,
            'added': added, 'history': count('WAL HISTORY'),
            'journal': count('JOURNAL'), 'carved': count('CARVED'),
        }

    def _show_table_diff(self, table: str, only_if_recovered: bool = False) -> bool:
        """Show the change view. With only_if_recovered (a database with no
        WAL), show it only when the table has JOURNAL or CARVED rows, and
        return False so the caller shows the plain paged table instead."""
        if self._sql_table_counts.get(table, 0) > _DIFF_ROW_CAP:
            if only_if_recovered:
                return False
            self._sql_status_label.setText(
                f'{table} — too large to diff (> {_DIFF_ROW_CAP:,} rows); '
                f'showing all rows instead')
            self._load_table_normal(table)
            return True
        try:
            columns, rows, n = self._compute_table_diff(table)
        except Exception as exc:
            if only_if_recovered:
                return False
            self._sql_status_label.setText(f"Could not diff {table}: {exc}")
            self._load_table_normal(table)
            return True
        changed = sum(n[k] for k in ('deleted', 'modified', 'added', 'history', 'journal', 'carved'))
        if only_if_recovered and not changed and not self._sql_affected_only:
            return False

        if self._sql_affected_only:
            rows = [r for r in rows if r[0]]      # keep only rows with a Change value

        self._sql_diff_model.load(columns, rows)
        units, labels = self._sql_epoch_units(table, columns)
        self._sql_diff_model.set_epoch(units, self._sql_format_epoch, labels)
        if self._sql_table_view.model() is not self._sql_diff_model:
            self._sql_table_view.setModel(self._sql_diff_model)
        self._sql_filter_input.setVisible(False)
        self._sql_resize_columns()

        dropped = ' · table dropped in WAL' if table in self._sql_dropped else ''
        parts = []
        if self._sql_has_wal:
            parts += [f"{n['deleted']:,} deleted", f"{n['modified']:,} modified",
                      f"{n['added']:,} added", f"{n['history']:,} WAL history"]
        if self._sql_journal_copy:
            parts.append(f"{n['journal']:,} journal")
        parts.append(f"{n['carved']:,} carved")
        changes = ' · '.join(parts) + dropped + self._sql_epoch_note(units)
        if self._sql_affected_only:
            self._sql_status_label.setText(f'{table} — {changed:,} changes · {changes}')
        elif self._sql_has_wal:
            self._sql_status_label.setText(
                f"{table} — {n['before']:,} rows before WAL · {changes}")
        else:
            self._sql_status_label.setText(f"{table} — {n['before']:,} rows · {changes}")
        return True

    # ── "Affected only" toggle (WAL view) ────────────────────────────────────────

    def _on_sql_affected_toggled(self, checked: bool) -> None:
        self._sql_affected_only = checked
        if checked and self._sql_affected_counts is None:
            self._compute_affected_counts()
        self._rebuild_table_combo()
        if self._sql_table_combo.count() == 0:        # nothing changed or recovered
            self._sql_diff_model.clear()
            self._sql_status_label.setText("No changed or recovered rows in any table")
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
                _cols, _rows, n = self._compute_table_diff(t)
                counts[t] = sum(n[k] for k in (
                    'deleted', 'modified', 'added', 'history', 'journal', 'carved'))
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
        order = self._sql_tables
        if self._sql_affected_only:
            order = sorted(order, key=lambda t: (-(counts.get(t) or 0), t.casefold()))
        for t in order:
            if self._sql_affected_only:
                n = counts.get(t)
                if n == 0:                        # untouched — hide it
                    continue
                text = f"{t}  ({n:,} changes)" if n is not None else f"{t}  (?)"
            else:
                text = self._sql_table_label(t)
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
        """Write the table as shown. A column shown as a date and time is
        written twice: the stored number under the column's own name, then
        the converted text under its labelled header, so a wrong conversion
        never replaces the stored value in an exported file."""
        model = self._sql_table_view.model()
        if model is None or not model.columnCount():
            return
        diff = model is self._sql_diff_model
        names, converted = [], []
        for c in range(model.columnCount()):
            if diff:
                name = model._columns[c]
            else:
                real = model._real_col(c)
                name = model._columns[real] if real is not None else ''
            units = model._ts_units
            names.append(name)
            converted.append(name in units)
        header = []
        for c, name in enumerate(names):
            header.append(name)
            if converted[c]:
                header.append(model.headerData(c, Qt.Orientation.Horizontal) or name)
        table = self._sql_table_combo.currentData() or 'table'
        default = f"{table}_wal_changes.csv" if self._sql_diff_mode else f"{table}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Table to CSV", default, "CSV Files (*.csv)")
        if not path:
            return

        def stored(row, c):
            if diff:
                if c == 0:
                    return model.data(model.index(row, 0))
                _status, vals, _changed = model._rows[model._view[row]]
                v = vals[c - 1] if c - 1 < len(vals) else None
                return str(v) if isinstance(v, CellChange) else v
            return model.row_dict(row).get(names[c])

        nrows = model.rowCount()
        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                for row in range(nrows):
                    out = []
                    for c in range(len(names)):
                        out.append(stored(row, c) if converted[c] else model.data(model.index(row, c)))
                        if converted[c]:
                            out.append(model.data(model.index(row, c)))
                    writer.writerow(out)
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
        self._sql_wal_copy    = None
        self._sql_main_copy   = None
        self._sql_journal_copy = None
        self._sql_dropped     = set()
        self._sql_epoch       = {}
        self._sql_epoch_upper = None
        self._sql_status_label.setText("No database selected")
