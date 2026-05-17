"""keyword_search.py — keyword search worker, dialogs, and FastZipBrowser mixin."""

import html
import os
import re
import sqlite3
import struct
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import msgpack

from adapters import FfsAdapter
from db_utils import (_open_cache_db, _open_results_db, OldSchemaError, save_blob, load_blob,
                      load_bookmark_groups, load_bookmark_entries)
from zip_cd_cache import load as _zcd_load, compute_data_offsets as _compute_data_offsets

_SEARCH_ENTRIES_VERSION = '1'

# Separator used to encode scope into the DB cache key.
# \x00 cannot appear in a user-typed search term.
_SCOPE_SEP = '\x00'


def _encode_search_key(term: str, scope_label: str) -> str:
    """Return the DB key for (term, scope_label).  'all files' scope uses just term."""
    if scope_label == 'all files':
        return term
    return f"{term}{_SCOPE_SEP}{scope_label}"


def _decode_search_key(db_key: str) -> tuple:
    """Return (term, scope_label) from a DB key."""
    if _SCOPE_SEP in db_key:
        term, scope_label = db_key.split(_SCOPE_SEP, 1)
        return term, scope_label
    return db_key, 'all files'
from PySide6.QtWidgets import (
    QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QVBoxLayout, QHBoxLayout, QTreeView, QDialog, QProgressBar,
    QPlainTextEdit, QMenu, QApplication, QStyle, QStyledItemDelegate,
    QHeaderView,
)
from PySide6.QtGui import (
    QStandardItemModel, QStandardItem, QFont, QFontDatabase,
    QTextDocument, QTextCharFormat,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSortFilterProxyModel


# ── Shared zip-entry scanner ─────────────────────────────────────────────────

def _build_zip_entries(zip_path: str, streaming_index, stop,
                       case_dir: str | None = None,
                       delta: int | None = None) -> list:
    """Return list of (name, data_offset, file_size) for all STORED entries.
    *stop* is a threading.Event; set it to abort early.
    *case_dir*, when set, allows using the local .zcd sidecar to avoid
    reading the central directory from the network."""
    entries = []
    if streaming_index is not None:
        for name in streaming_index.namelist():
            try:
                entry = streaming_index.get_entry(name)
                if entry.is_stored and entry.file_size > 0:
                    entries.append((name, entry.data_offset, entry.file_size))
            except Exception:
                pass
        return entries

    # Use the local .zcd sidecar when available — avoids a full network CD read.
    infolist = None
    if case_dir:
        try:
            infolist = _zcd_load(zip_path, case_dir)
        except Exception:
            pass

    try:
        if infolist is not None:
            stored = [
                info for info in infolist
                if info.compress_type == zipfile.ZIP_STORED and info.file_size > 0
            ]
        else:
            with zipfile.ZipFile(zip_path, 'r') as z:
                stored = [
                    info for info in z.infolist()
                    if info.compress_type == zipfile.ZIP_STORED and info.file_size > 0
                ]
    except Exception:
        return entries

    offsets = _compute_data_offsets(zip_path, stored, delta=delta)
    for info in stored:
        if stop.is_set():
            return entries
        if info.filename in offsets:
            entries.append((info.filename, offsets[info.filename], info.file_size))
    return entries


# ── SearchIndexWorker ─────────────────────────────────────────────────────────

class SearchIndexWorker(QThread):
    """Build (or restore from DB) the zip entry index in the background.

    Emits entries_ready once the list is available so the mixin can cache it
    before the first search is ever started.
    """
    entries_ready = Signal(list)   # list of (name, data_offset, file_size)

    def __init__(self, zip_path: str, streaming_index=None,
                 case_dir: str | None = None, delta: int | None = None,
                 parent=None):
        super().__init__(parent)
        self.zip_path        = zip_path
        self.streaming_index = streaming_index
        self.case_dir        = case_dir
        self.delta           = delta
        self._stop           = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # Try the DB cache first (keyed by zip file size for cheap validation)
        if self.case_dir:
            cached = self._load_from_db()
            if cached is not None:
                self.entries_ready.emit(cached)
                return

        entries = _build_zip_entries(self.zip_path, self.streaming_index, self._stop,
                                     case_dir=self.case_dir, delta=self.delta)
        if self._stop.is_set():
            return

        if self.case_dir:
            self._save_to_db(entries)

        self.entries_ready.emit(entries)

    def _load_from_db(self) -> list | None:
        try:
            db = _open_cache_db(self.case_dir)
            try:
                raw = load_blob(db, 'search_entries', _SEARCH_ENTRIES_VERSION)
            finally:
                db.close()
            if raw is None:
                return None
            rows = msgpack.unpackb(raw, raw=False)
            return [tuple(r) for r in rows] if rows else None
        except Exception:
            return None

    def _save_to_db(self, entries: list) -> None:
        try:
            raw = msgpack.packb(entries, use_bin_type=True)
            db  = _open_cache_db(self.case_dir)
            try:
                save_blob(db, 'search_entries', _SEARCH_ENTRIES_VERSION, raw)
            finally:
                db.close()
        except Exception:
            pass


# ── KeywordSearchWorker ───────────────────────────────────────────────────────

class KeywordSearchWorker(QThread):
    """Search all STORED entries in a zip for a keyword using multiple threads.

    Emits:
        result_found(filename, offset_in_file, context_str)
        progress(files_done, files_total)
        finished(total_hits)
    """

    result_found  = Signal(str, int, str)   # name, offset-in-file, context
    progress      = Signal(int, int)        # done, total
    finished      = Signal(int, int, int, bool)  # total hits, files_done, files_total, stopped
    status_update = Signal(str)             # free-text status line

    _CHUNK     = 1 * 1024 * 1024   # 1 MB read chunks
    _CTX_BYTES = 40                # bytes either side of hit for context

    def __init__(self, zip_path: str, keyword: str,
                 streaming_index=None, entries=None, scope="all",
                 exclude_prefixes: tuple = (), parent=None):
        super().__init__(parent)
        self.zip_path          = zip_path
        self.keyword           = keyword.encode('utf-8', errors='replace')
        self.streaming_index   = streaming_index
        self._stop             = threading.Event()
        self._prebuilt_entries = entries
        self._scope            = scope
        self._exclude_prefixes = exclude_prefixes
        self.entries: list     = []
        self._patterns: list   = []
        for _enc in ('utf-8', 'utf-16-le', 'utf-16-be'):
            _pat = keyword.encode(_enc, errors='replace')
            self._patterns.append((_pat.lower(), len(_pat), _enc))

    def stop(self):
        self._stop.set()

    def _build_entries(self) -> list:
        return _build_zip_entries(self.zip_path, self.streaming_index, self._stop)

    def run(self):
        if self._prebuilt_entries is not None:
            entries = self._prebuilt_entries
        else:
            self.status_update.emit("Preparing search index…")
            full_entries = self._build_entries()
            self.entries = full_entries
            if self._scope == "app_data":
                entries = [e for e in full_entries if "mobile/Containers" in e[0]]
            elif self._exclude_prefixes:
                entries = [e for e in full_entries
                           if not any(e[0].lstrip('/').startswith(p)
                                      for p in self._exclude_prefixes)]
            else:
                entries = full_entries
        self.status_update.emit(f"Index ready — {len(entries):,} files to search")
        patterns = self._patterns
        overlap  = max(pat_len for _, pat_len, _ in patterns) - 1
        total    = len(entries)
        hits     = 0
        done     = 0
        n_threads = min(8, os.cpu_count() or 4)

        def _decode_ctx(data: bytes, enc: str) -> str:
            if enc == 'utf-8':
                return data.decode('utf-8', errors='replace')
            if len(data) % 2:
                data = data[1:]
            return data.decode(enc, errors='replace')

        def search_entry(_name, data_offset, file_size):
            if self._stop.is_set():
                return []
            results = []
            chunk   = self._CHUNK
            pos     = 0
            try:
                with open(self.zip_path, 'rb') as fh:
                    fh.seek(data_offset)
                    buf_start = 0
                    leftover  = b''
                    while pos < file_size and not self._stop.is_set():
                        read_len    = min(chunk, file_size - pos)
                        raw         = fh.read(read_len)
                        if not raw:
                            break
                        block       = leftover + raw
                        block_lower = block.lower()
                        block_base  = buf_start
                        for pat, pat_len, enc in patterns:
                            idx = 0
                            while True:
                                idx = block_lower.find(pat, idx)
                                if idx == -1:
                                    break
                                file_offset = block_base + idx
                                ctx_start   = max(0, idx - self._CTX_BYTES)
                                ctx_end     = min(len(block), idx + pat_len + self._CTX_BYTES)
                                ctx_bytes   = block[ctx_start:ctx_end]
                                before      = ctx_bytes[:idx - ctx_start]
                                after       = ctx_bytes[idx - ctx_start + pat_len:]
                                hit_text    = block[idx:idx + pat_len].decode(enc, errors='replace')
                                context     = (
                                    _decode_ctx(before, enc)
                                    + f'[{hit_text}]'
                                    + _decode_ctx(after, enc)
                                )
                                results.append((file_offset, context))
                                idx += pat_len
                        leftover  = block[-overlap:] if overlap > 0 else b''
                        buf_start = block_base + len(block) - len(leftover)
                        pos      += read_len
            except OSError:
                pass
            return results

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = {
                pool.submit(search_entry, name, off, size): name
                for name, off, size in entries
            }
            for fut in as_completed(futures):
                if self._stop.is_set():
                    break
                name = futures[fut]
                done += 1
                self.progress.emit(done, total)
                try:
                    for file_offset, context in fut.result():
                        hits += 1
                        self.result_found.emit(name, file_offset, context)
                except Exception:
                    pass

        self.finished.emit(hits, done, total, self._stop.is_set())


# ── NestedArchiveSearchWorker ─────────────────────────────────────────────────

class NestedArchiveSearchWorker(QThread):
    """Search entries inside repacked nested archive ZIPs for a keyword.

    result_found carries five arguments so the click handler can reopen
    the exact entry from the stored ZIP, not from the FFS zip.
    """

    result_found = Signal(str, int, str, str, str)  # (virtual_ui_path, offset, context, stored_path, entry_path)
    progress     = Signal(int, int)                  # (done, total)
    finished     = Signal(int, int, int)             # (hits, files_done, files_total)

    _CTX_BYTES = 40

    def __init__(self, nested_archive_map: dict, keyword: str, parent=None):
        super().__init__(parent)
        self._map      = nested_archive_map
        self._stop     = threading.Event()
        self._patterns: list = []
        for _enc in ('utf-8', 'utf-16-le', 'utf-16-be'):
            _pat = keyword.encode(_enc, errors='replace')
            self._patterns.append((_pat.lower(), len(_pat), _enc))

    def stop(self):
        self._stop.set()

    @staticmethod
    def _read_entry(stored_path: str, entry_path: str) -> bytes | None:
        """Read one entry's bytes from stored_path.

        stored_path is either a repackaged ZIP (for ZIP sources) or a raw
        decompressed blob (for gzip sources).  Try ZIP first; if the file
        is not a ZIP at all fall back to reading the whole blob directly —
        matching the logic in _load_nested_entry_preview.
        """
        try:
            with zipfile.ZipFile(stored_path, 'r') as zf:
                return zf.read(entry_path)
        except zipfile.BadZipFile:
            pass
        except Exception:
            return None
        try:
            with open(stored_path, 'rb') as f:
                return f.read()
        except Exception:
            return None

    def run(self):
        # Build the work list from the already-loaded entries dict (avoids
        # reopening every stored ZIP just to enumerate filenames).
        all_entries: list[tuple[str, str, str]] = []
        for archive_ui_path, arch in self._map.items():
            for e in arch.get('entries', []):
                ep = e.get('entry_path', '') if isinstance(e, dict) else str(e)
                if ep and not ep.endswith('/'):
                    all_entries.append((archive_ui_path, arch['stored_path'], ep))

        total = len(all_entries)
        self.progress.emit(0, total)
        hits = done = 0

        for archive_ui_path, stored_path, entry_path in all_entries:
            if self._stop.is_set():
                break
            data = self._read_entry(stored_path, entry_path)
            if data is None:
                done += 1
                self.progress.emit(done, total)
                continue

            data_lower = data.lower()
            vpath = f"{archive_ui_path}/{entry_path}"
            for pat, pat_len, enc in self._patterns:
                idx = 0
                while True:
                    idx = data_lower.find(pat, idx)
                    if idx == -1:
                        break
                    ctx_start = max(0, idx - self._CTX_BYTES)
                    ctx_end   = min(len(data), idx + pat_len + self._CTX_BYTES)
                    ctx_bytes = data[ctx_start:ctx_end]
                    before    = ctx_bytes[:idx - ctx_start]
                    after     = ctx_bytes[idx - ctx_start + pat_len:]
                    try:
                        hit_text = data[idx:idx + pat_len].decode(enc, errors='replace')
                    except Exception:
                        hit_text = ''
                    context = (
                        before.decode('utf-8', errors='replace')
                        + f'[{hit_text}]'
                        + after.decode('utf-8', errors='replace')
                    )
                    hits += 1
                    self.result_found.emit(vpath, idx, context, stored_path, entry_path)
                    idx += pat_len
            done += 1
            self.progress.emit(done, total)

        self.finished.emit(hits, done, total)


# ── DbSearchLoader ────────────────────────────────────────────────────────────

class DbSearchLoader(QThread):
    """Fetch cached search results from the DB on a background thread.

    Emits rows_ready in batches of BATCH_SIZE so the UI can insert them
    incrementally without blocking the main thread."""

    rows_ready = Signal(list)   # list of (filename, offset, context)
    finished   = Signal(int)    # total rows fetched

    BATCH_SIZE = 200

    def __init__(self, case_dir: str, term: str, parent=None):
        super().__init__(parent)
        self._case_dir = case_dir
        self._term     = term

    def run(self):
        try:
            db = _open_results_db(self._case_dir)
            try:
                rows = db.execute(
                    'SELECT r.filename, r.offset, r.context '
                    'FROM search_results r '
                    'JOIN search_index i ON r.term_id = i.id '
                    'WHERE i.keyword=? '
                    'ORDER BY r.rowid',
                    (self._term,)
                ).fetchall()
            finally:
                db.close()
        except Exception:
            self.finished.emit(0)
            return

        for i in range(0, len(rows), self.BATCH_SIZE):
            self.rows_ready.emit(rows[i:i + self.BATCH_SIZE])

        self.finished.emit(len(rows))


# ── DbRecentLoader ────────────────────────────────────────────────────────────

class DbRecentLoader(QThread):
    """Load recent search terms for a case dir on a background thread."""

    loaded = Signal(list)   # list[str] of terms

    def __init__(self, case_dir: str, parent=None):
        super().__init__(parent)
        self._case_dir = case_dir

    def run(self):
        try:
            db = _open_results_db(self._case_dir)
            try:
                rows = db.execute(
                    'SELECT keyword FROM search_index ORDER BY used_at DESC LIMIT 20'
                ).fetchall()
                terms = [r[0] for r in rows]
            finally:
                db.close()
        except Exception:
            terms = []
        self.loaded.emit(terms)


# ── SearchContextDelegate ─────────────────────────────────────────────────────

class SearchContextDelegate(QStyledItemDelegate):
    """Renders the Context column (col 2) with the search term highlighted."""

    CONTEXT_COL = 2
    _HL_BG = "#ffeb3b"
    _HL_FG = "#000000"

    def __init__(self, get_term, parent=None):
        super().__init__(parent)
        self._get_term = get_term

    def paint(self, painter, option, index):
        if index.column() != self.CONTEXT_COL:
            super().paint(painter, option, index)
            return

        text = index.data(Qt.ItemDataRole.DisplayRole) or ''
        term = self._get_term()
        if not term or not text:
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)

        style = option.widget.style() if option.widget else QApplication.style()
        option.text = ''
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        escaped = html.escape(text)
        pattern = re.compile(re.escape(html.escape(term)), re.IGNORECASE)
        highlighted = pattern.sub(
            lambda m: (f'<span style="background:{self._HL_BG};color:{self._HL_FG};">'
                       f'{m.group()}</span>'),
            escaped
        )

        if option.state & QStyle.StateFlag.State_Selected:
            fg = option.palette.highlightedText().color().name()
        else:
            fg = option.palette.text().color().name()

        doc = QTextDocument()
        doc.setDefaultStyleSheet(f'body {{ color: {fg}; }}')
        doc.setHtml(f'<body>{highlighted}</body>')
        doc.setTextWidth(option.rect.width())

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, option, option.widget)

        painter.save()
        painter.translate(text_rect.topLeft())
        painter.setClipRect(text_rect.translated(-text_rect.topLeft()))
        doc.drawContents(painter)
        painter.restore()


# ── SearchProgressDialog ──────────────────────────────────────────────────────

class SearchProgressDialog(QDialog):
    """Modal progress dialog shown during a keyword search."""

    cancelled = Signal()

    def __init__(self, term: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyword Search")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setMinimumHeight(260)
        self._interrupted = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(QLabel(f"<b>Searching for:</b> {term}"))

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFixedHeight(100)
        self._log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self._log)

        self._progress_label = QLabel("Starting…")
        layout.addWidget(self._progress_label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)
        layout.addWidget(self._bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

    def append_status(self, text: str):
        self._log.appendPlainText(text)
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    def update_progress(self, done: int, total: int, hits: int):
        if self._bar.maximum() != total:
            self._bar.setRange(0, total)
        self._bar.setValue(done)
        self._progress_label.setText(
            f"Checked {done:,} / {total:,} files  |  "
            f"hits in {hits:,} file{'s' if hits != 1 else ''} so far")

    def mark_finished(self, n_files: int, total_hits: int):
        self._bar.setValue(self._bar.maximum())
        self._progress_label.setText(
            f"Complete — {total_hits:,} hit{'s' if total_hits != 1 else ''} "
            f"across {n_files:,} file{'s' if n_files != 1 else ''}")
        self._cancel_btn.setText("Close")

    def mark_interrupted(self, n_files: int):
        self._interrupted = True
        self._progress_label.setText(
            f"Partial search — interrupted  "
            f"({n_files:,} file{'s' if n_files != 1 else ''} with hits)")
        self._cancel_btn.setText("Close")

    @property
    def was_interrupted(self) -> bool:
        return self._interrupted

    def _on_cancel(self):
        if self._cancel_btn.text() == "Close":
            self.accept()
        else:
            self.cancelled.emit()

    def closeEvent(self, event):
        if self._cancel_btn.text() not in ("Close",):
            self.cancelled.emit()
            event.ignore()
        else:
            super().closeEvent(event)


# ── Mixin ─────────────────────────────────────────────────────────────────────

class KeywordSearchMixin:
    """Methods and setup for the keyword-search tab.

    Designed to be mixed into FastZipBrowser (QMainWindow).
    Accesses instance attributes set by FastZipBrowser.__init__ and _setup_search_tab.
    """

    def _setup_search_tab(self) -> QWidget:
        """Build the keyword-search tab widget and initialise all search instance state.
        Returns the tab QWidget to be added to center_tabs."""
        self._search_worker: KeywordSearchWorker | None = None
        self._nested_search_worker: NestedArchiveSearchWorker | None = None
        self._search_index_worker: SearchIndexWorker | None = None
        self._db_loader: DbSearchLoader | None = None
        self._db_loader_term: str = ""
        self._db_loader_db_key: str = ""
        self._current_search_db_key: str = ""
        self._recent_loader: DbRecentLoader | None = None
        self._search_progress_dlg: SearchProgressDialog | None = None
        self._pending_db_hits: list[tuple] = []
        self._search_entries:     list | None = None
        self._search_incomplete:  bool = False
        self._search_incomplete_files: tuple[int,int] = (0, 0)  # (done, total)
        self._search_folder_items: dict[str, QStandardItem] = {}
        self._search_file_items:   dict[str, QStandardItem] = {}
        self._recent_searches: list = []

        search_tab = QWidget()
        search_tab_layout = QVBoxLayout(search_tab)
        search_tab_layout.setContentsMargins(4, 4, 4, 4)
        search_tab_layout.setSpacing(4)

        search_ctrl = QHBoxLayout()
        self.search_recent_combo = QComboBox()
        self.search_recent_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search_recent_combo.setMinimumContentsLength(20)
        self.search_recent_combo.setMaximumWidth(260)
        self.search_recent_combo.setToolTip("Recent searches")
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Enter keyword…")
        self.search_field.returnPressed.connect(self._start_keyword_search)
        self.search_btn = QPushButton("Search")
        self.search_btn.setFixedWidth(80)
        self.search_btn.clicked.connect(self._start_keyword_search)
        self.search_stop_btn = QPushButton("Stop")
        self.search_stop_btn.setFixedWidth(60)
        self.search_stop_btn.setEnabled(False)
        self.search_stop_btn.clicked.connect(self._stop_keyword_search)
        self.search_status = QLabel("No search running")
        self.search_scope_combo = QComboBox()
        self.search_scope_combo.setMinimumWidth(160)
        self._refresh_search_scope_combo()
        search_ctrl.addWidget(QLabel("Recent:"))
        search_ctrl.addWidget(self.search_recent_combo)
        search_ctrl.addSpacing(8)
        search_ctrl.addWidget(QLabel("Search:"))
        search_ctrl.addWidget(self.search_field, 1)
        search_ctrl.addWidget(self.search_scope_combo)
        search_ctrl.addWidget(self.search_btn)
        search_ctrl.addWidget(self.search_stop_btn)
        self._incomplete_banner = QLabel()
        self._incomplete_banner.setWordWrap(True)
        self._incomplete_banner.setStyleSheet(
            "background:#fff3cd; color:#856404; border:1px solid #ffc107;"
            "border-radius:4px; padding:4px 8px;")
        self._incomplete_banner.setVisible(False)

        search_tab_layout.addLayout(search_ctrl)
        search_tab_layout.addWidget(self.search_status)
        search_tab_layout.addWidget(self._incomplete_banner)

        self.search_results_model = QStandardItemModel()
        self.search_results_model.setHorizontalHeaderLabels(
            ["Name", "Hits", "Context", "Offset"])
        self.search_results_view = QTreeView()
        self.search_results_view.setModel(self.search_results_model)
        self.search_results_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.search_results_view.setSelectionBehavior(QTreeView.SelectionBehavior.SelectRows)
        self.search_results_view.setAlternatingRowColors(True)
        self.search_results_view.setUniformRowHeights(True)
        _row_h = self.search_results_view.fontMetrics().height() + 8
        self.search_results_view.setStyleSheet(
            f"QTreeView::item {{ height: {_row_h}px; }}")
        hdr = self.search_results_view.header()
        hdr.setStretchLastSection(False)
        hdr.resizeSection(0, 220)
        hdr.resizeSection(1, 50)
        hdr.resizeSection(3, 90)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(2, 300)
        self.search_results_view.selectionModel().selectionChanged.connect(
            self._on_search_row_selected)
        self._search_context_delegate = SearchContextDelegate(
            lambda: self.search_field.text().strip())
        self.search_results_view.setItemDelegate(self._search_context_delegate)
        self.search_results_view.expanded.connect(self._on_search_tree_expanded)
        self.search_results_view.setExpandsOnDoubleClick(False)
        self.search_results_view.doubleClicked.connect(self._on_search_results_double_clicked)
        self.search_results_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_results_view.customContextMenuRequested.connect(
            self._on_search_results_context_menu)
        search_tab_layout.addWidget(self.search_results_view, stretch=1)

        self._refresh_search_recent_combo()
        self.search_recent_combo.activated.connect(self._on_search_recent_selected)
        return search_tab

    # ── Scope combo ──────────────────────────────────────────────────────────

    def _refresh_search_scope_combo(self):
        """Rebuild the scope dropdown: fixed options + current bookmark groups."""
        prev = self.search_scope_combo.currentData()
        self.search_scope_combo.blockSignals(True)
        self.search_scope_combo.clear()
        self.search_scope_combo.addItem("All Files",      userData="all")
        self.search_scope_combo.addItem("App Data",       userData="app_data")
        self.search_scope_combo.addItem("Selected Files", userData="selected")
        self.search_scope_combo.setToolTip(
            "All Files      — search every stored file in the archive\n"
            "App Data       — search only files under mobile/Containers\n"
            "Selected Files — search only currently selected files/folders\n"
            "BM: <group>    — search only files in that bookmark group"
        )
        groups = []
        if getattr(self, '_case_dir', None):
            try:
                db = _open_results_db(self._case_dir)
                groups = load_bookmark_groups(db)
                db.close()
            except Exception:
                pass
        if groups:
            self.search_scope_combo.insertSeparator(self.search_scope_combo.count())
            for g in groups:
                lbl = f"BM: {g['name']}"
                if g['count']:
                    lbl += f"  ({g['count']:,})"
                self.search_scope_combo.addItem(
                    lbl,
                    userData={'type': 'bookmark', 'group_id': g['id'], 'name': g['name']},
                )
        # Restore previous selection (match by group_id for bookmark entries)
        restored = False
        for i in range(self.search_scope_combo.count()):
            d = self.search_scope_combo.itemData(i)
            if d == prev:
                self.search_scope_combo.setCurrentIndex(i)
                restored = True
                break
            if (isinstance(d, dict) and isinstance(prev, dict)
                    and d.get('group_id') == prev.get('group_id')):
                self.search_scope_combo.setCurrentIndex(i)
                restored = True
                break
        if not restored:
            self.search_scope_combo.setCurrentIndex(0)
        self.search_scope_combo.blockSignals(False)

    def _filter_entries_by_ui_paths(self, ui_paths: list, nested_map: dict) -> tuple:
        """Return (filtered_zip_entries, filtered_nested_map) for the given ui_paths.

        Regular files are resolved to physical zip-entry names and matched against
        self._search_entries.  Extracted archives (in nested_map) are routed to the
        NestedArchiveSearchWorker — their content lives outside the FFS zip.
        """
        physical_names: set = set()
        scoped_nested: dict = {}

        for ui_path in ui_paths:
            # File is a previously-extracted nested archive — search its stored copy.
            if ui_path in nested_map:
                scoped_nested[ui_path] = nested_map[ui_path]
                continue
            # Virtual path *inside* an extracted archive (e.g. archive.zip/entry.txt).
            nested_found = False
            for archive_path in nested_map:
                if ui_path.startswith(archive_path + '/'):
                    if archive_path not in scoped_nested:
                        scoped_nested[archive_path] = nested_map[archive_path]
                    nested_found = True
                    break
            if nested_found:
                continue
            # Regular file — resolve ui_path to physical zip-entry name.
            try:
                physical = self._adapter.resolve(ui_path)
                physical_names.add(physical.lstrip('/'))
            except Exception:
                pass

        if not physical_names or self._search_entries is None:
            return [], scoped_nested

        filtered = [e for e in self._search_entries
                    if e[0].lstrip('/') in physical_names]
        return filtered, scoped_nested

    def _resolve_search_scope(self, scope) -> tuple:
        """Return (scoped_entries, scoped_nested_map, scope_label).

        scoped_entries=None means pass all entries to the worker (it handles filtering).
        """
        nested_map = getattr(self, '_nested_archive_map', {})

        if isinstance(scope, dict) and scope.get('type') == 'bookmark':
            group_id   = scope['group_id']
            group_name = scope.get('name', 'Bookmarks')
            ui_paths   = self._get_ui_paths_for_search_scope(group_id)
            entries, nm = self._filter_entries_by_ui_paths(ui_paths, nested_map)
            return entries, nm, f"BM: {group_name}"

        if scope == 'selected':
            get_bm   = getattr(self, '_get_paths_for_bookmark', None)
            pairs    = get_bm() if get_bm else []
            ui_paths = [p for p, _ in pairs]
            entries, nm = self._filter_entries_by_ui_paths(ui_paths, nested_map)
            return entries, nm, f"selected files ({len(ui_paths):,})"

        if scope == 'app_data':
            entries = (
                [e for e in self._search_entries if 'mobile/Containers' in e[0]]
                if self._search_entries is not None else None
            )
            return entries, nested_map, "App Data"

        # "all" — exclude Cellebrite internal metadata entries
        _cellebrite_excludes = ('metadata1/', 'metadata2/')
        exclude = (
            _cellebrite_excludes
            if self._adapter.format == FfsAdapter.FORMAT_CELLEBRITE
            else ()
        )
        if self._search_entries is not None and exclude:
            entries = [e for e in self._search_entries
                       if not any(e[0].lstrip('/').startswith(p) for p in exclude)]
        elif self._search_entries is not None:
            entries = self._search_entries
        else:
            entries = None
        return entries, nested_map, "all files"

    def _get_ui_paths_for_search_scope(self, group_id: int) -> list:
        """Return ui_paths for all entries in a bookmark group."""
        if not getattr(self, '_case_dir', None):
            return []
        try:
            db = _open_results_db(self._case_dir)
            entries = load_bookmark_entries(db, group_id)
            db.close()
            return [e['ui_path'] for e in entries]
        except Exception:
            return []

    # ── Recent combo ─────────────────────────────────────────────────────────

    def _set_incomplete_banner(self, files_done: int = 0, total_files: int = 0):
        """Show the incomplete-search warning banner, or hide it if called with no args."""
        if files_done or total_files:
            self._incomplete_banner.setText(
                f"⚠️  Incomplete search — stopped after {files_done:,} of "
                f"{total_files:,} files. Results may be missing.")
            self._incomplete_banner.setVisible(True)
        else:
            self._incomplete_banner.setVisible(False)

    def _refresh_search_recent_combo(self):
        self.search_recent_combo.blockSignals(True)
        self.search_recent_combo.clear()
        self.search_recent_combo.addItem("Recent searches…")
        model = self.search_recent_combo.model()
        item  = model.item(0)
        item.setFlags(item.flags() & ~(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled))
        for db_key in self._recent_searches:
            term, scope_label = _decode_search_key(db_key)
            display = f"{term}  [{scope_label}]" if scope_label != 'all files' else term
            self.search_recent_combo.addItem(display, userData=db_key)
        self.search_recent_combo.blockSignals(False)

    def _on_search_recent_selected(self, index):
        if index == 0:
            return
        db_key = self.search_recent_combo.itemData(index)
        if not db_key:
            db_key = self.search_recent_combo.itemText(index)  # fallback for old entries
        term, scope_label = _decode_search_key(db_key)
        self.search_field.setText(term)
        self._set_search_scope_by_label(scope_label)
        if self._load_search_from_db(db_key):
            self._update_search_status_bar()
            return
        self._start_keyword_search()

    def _set_search_scope_by_label(self, scope_label: str):
        """Set the scope combo to the entry matching scope_label."""
        for i in range(self.search_scope_combo.count()):
            d = self.search_scope_combo.itemData(i)
            if d == 'all' and scope_label == 'all files':
                self.search_scope_combo.setCurrentIndex(i)
                return
            if d == 'app_data' and scope_label == 'App Data':
                self.search_scope_combo.setCurrentIndex(i)
                return
            if d == 'selected' and scope_label.startswith('selected files'):
                self.search_scope_combo.setCurrentIndex(i)
                return
            if (isinstance(d, dict) and d.get('type') == 'bookmark'
                    and f"BM: {d.get('name','')}" == scope_label):
                self.search_scope_combo.setCurrentIndex(i)
                return
        self.search_scope_combo.setCurrentIndex(0)  # default to All Files

    # ── Row selection ─────────────────────────────────────────────────────────

    def _on_search_row_selected(self):
        indexes = self.search_results_view.selectionModel().selectedRows(0)
        if not indexes:
            return
        item = self.search_results_model.itemFromIndex(indexes[0])
        if not item:
            return
        _PATH_ROLE        = Qt.ItemDataRole.UserRole
        _OFFSET_ROLE      = Qt.ItemDataRole.UserRole + 1
        _PHYS_ROLE        = Qt.ItemDataRole.UserRole + 2
        _STORED_PATH_ROLE = Qt.ItemDataRole.UserRole + 3
        _ENTRY_PATH_ROLE  = Qt.ItemDataRole.UserRole + 4

        path        = item.data(_PATH_ROLE) or ''
        offset      = item.data(_OFFSET_ROLE)
        physical    = item.data(_PHYS_ROLE)
        stored_path = item.data(_STORED_PATH_ROLE)
        entry_path  = item.data(_ENTRY_PATH_ROLE)

        keyword = self.search_field.text().strip()
        if stored_path and entry_path and offset is not None:
            self.status_bar.showMessage(f'{path}  —  offset: {offset:,}')
            self._open_nested_hex_from_search(stored_path, entry_path, path, offset, keyword)
        elif physical and offset is not None:
            self.status_bar.showMessage(f'{path}  —  offset: {offset:,}')
            self._open_hex_from_search(physical, path, offset, keyword)
        else:
            self.status_bar.showMessage(path)

    # ── Tree helpers ──────────────────────────────────────────────────────────

    def _hits_cell_for(self, item: QStandardItem) -> QStandardItem | None:
        """Return the Hits (column 1) sibling of *item*."""
        parent = item.parent()
        if parent is None:
            return self.search_results_model.item(item.row(), 1)
        return parent.child(item.row(), 1)

    def _search_add_hit(self, filename: str, offset: int, context: str,
                        stored_path: str | None = None, entry_path: str | None = None):
        """Insert one hit into the fully-nested path tree.

        stored_path / entry_path are set for nested-archive hits so the
        click handler can reopen the entry from the stored ZIP.
        """
        _PATH_ROLE = Qt.ItemDataRole.UserRole

        folder   = filename.rsplit('/', 1)[0] if '/' in filename else ''
        basename = filename.rsplit('/', 1)[-1]

        display_folder   = self._strip_archive_prefix(self._display_path(folder))
        display_basename = self._display_name(basename)
        full_file_path   = (display_folder + '/' + display_basename) if display_folder else display_basename

        segments   = display_folder.split('/') if display_folder else []
        parent     = self.search_results_model.invisibleRootItem()
        cumulative = ''
        ancestor_hits_cells: list[QStandardItem] = []

        for seg in segments:
            cumulative = (cumulative + '/' + seg) if cumulative else seg
            if cumulative not in self._search_folder_items:
                folder_item = QStandardItem(f'📁  {seg}/')
                folder_item.setEditable(False)
                folder_item.setData(cumulative + '/', _PATH_ROLE)
                hits_item = QStandardItem('0')
                hits_item.setEditable(False)
                row = [folder_item, hits_item, QStandardItem(''), QStandardItem('')]
                for cell in row:
                    cell.setEditable(False)
                parent.appendRow(row)
                self._search_folder_items[cumulative] = folder_item
            folder_item = self._search_folder_items[cumulative]
            ancestor_hits_cells.append(self._hits_cell_for(folder_item))
            parent = folder_item

        if filename not in self._search_file_items:
            file_item = QStandardItem(f'📄  {display_basename}')
            file_item.setEditable(False)
            file_item.setData(full_file_path, _PATH_ROLE)
            file_item.setData(filename, Qt.ItemDataRole.UserRole + 2)
            if stored_path:
                file_item.setData(stored_path, Qt.ItemDataRole.UserRole + 3)
                file_item.setData(entry_path,  Qt.ItemDataRole.UserRole + 4)
            file_hits = QStandardItem('0')
            file_hits.setEditable(False)
            row = [file_item, file_hits, QStandardItem(''), QStandardItem('')]
            for cell in row:
                cell.setEditable(False)
            parent.appendRow(row)
            self._search_file_items[filename] = file_item
        file_item = self._search_file_items[filename]

        hit_item = QStandardItem('')
        hit_item.setData(full_file_path, _PATH_ROLE)
        hit_item.setData(offset, Qt.ItemDataRole.UserRole + 1)
        hit_item.setData(filename, Qt.ItemDataRole.UserRole + 2)
        if stored_path:
            hit_item.setData(stored_path, Qt.ItemDataRole.UserRole + 3)
            hit_item.setData(entry_path,  Qt.ItemDataRole.UserRole + 4)
        hit_row = [hit_item, QStandardItem(''), QStandardItem(context), QStandardItem(str(offset))]
        for cell in hit_row:
            cell.setEditable(False)
        file_item.appendRow(hit_row)

        for hits_cell in [self._hits_cell_for(file_item)] + ancestor_hits_cells:
            if hits_cell:
                hits_cell.setText(str(int(hits_cell.text()) + 1))

    def _on_search_results_double_clicked(self, idx):
        col0  = idx.siblingAtColumn(0)
        model = self.search_results_model
        view  = self.search_results_view
        if model.rowCount(col0) == 0:
            return
        if view.isExpanded(col0):
            self._collapse_search_descendants(col0)
        else:
            self._expand_search_descendants(col0)

    def _expand_search_descendants(self, parent_idx):
        model = self.search_results_model
        view  = self.search_results_view
        view.setUpdatesEnabled(False)
        view.expanded.disconnect(self._on_search_tree_expanded)
        try:
            stack = [parent_idx]
            while stack:
                idx = stack.pop()
                view.expand(idx)
                for row in range(model.rowCount(idx)):
                    stack.append(model.index(row, 0, idx))
        finally:
            view.expanded.connect(self._on_search_tree_expanded)
            view.setUpdatesEnabled(True)
        self._on_search_tree_expanded()

    def _collapse_search_descendants(self, parent_idx):
        model = self.search_results_model
        view  = self.search_results_view
        view.setUpdatesEnabled(False)
        view.expanded.disconnect(self._on_search_tree_expanded)
        try:
            stack = [parent_idx]
            while stack:
                idx = stack.pop()
                for row in range(model.rowCount(idx)):
                    stack.append(model.index(row, 0, idx))
                view.collapse(idx)
        finally:
            view.expanded.connect(self._on_search_tree_expanded)
            view.setUpdatesEnabled(True)

    def _on_search_results_context_menu(self, pos):
        idx = self.search_results_view.indexAt(pos)
        if not idx.isValid():
            return
        item = self.search_results_model.itemFromIndex(idx.siblingAtColumn(0))
        if item is None:
            return
        physical  = item.data(Qt.ItemDataRole.UserRole + 2)
        full_path = item.data(Qt.ItemDataRole.UserRole)
        if not physical or not full_path or full_path.endswith('/'):
            return
        menu   = QMenu(self)
        action = menu.addAction("Open Parent Folder")
        if menu.exec(self.search_results_view.viewport().mapToGlobal(pos)) == action:
            self._open_parent_folder_from_search(full_path)

    def _open_parent_folder_from_search(self, full_file_path: str):
        """Navigate the tree to the parent folder of *full_file_path*."""
        folder_path = full_file_path.rsplit('/', 1)[0] if '/' in full_file_path else ''
        self.center_tabs.setCurrentIndex(0)
        self.navigate_tree_to_path(folder_path)
        QTimer.singleShot(0, lambda: self._select_file_in_table(full_file_path))

    def _on_search_tree_expanded(self):
        for col in range(self.search_results_model.columnCount()):
            self.search_results_view.resizeColumnToContents(col)

    def _update_search_status_bar(self):
        if self.center_tabs.currentIndex() != 2:
            return
        term = self.search_field.text().strip()
        if not term:
            self.status_bar.showMessage("Keyword Search")
            return
        n_files = len(self._search_file_items)
        if self._search_worker and self._search_worker.isRunning():
            self.status_bar.showMessage(
                f"Searching: '{term}'  |  hits in {n_files:,} file{'s' if n_files != 1 else ''} so far")
        else:
            if n_files:
                self.status_bar.showMessage(
                    f"Search: '{term}'  |  hits in {n_files:,} file{'s' if n_files != 1 else ''}")
            else:
                self.status_bar.showMessage(f"Search: '{term}'  |  No results")

    # ── Database persistence ──────────────────────────────────────────────────

    def _open_results_db_conn(self) -> sqlite3.Connection | None:
        """Open caseresults.db for the current archive, or None if unavailable."""
        if not self._case_dir:
            return None
        try:
            return _open_results_db(self._case_dir)
        except OldSchemaError:
            raise   # caller must handle
        except OSError:
            return None

    def _save_recent_search(self, db_key: str):
        db = self._open_results_db_conn()
        if db:
            try:
                db.execute(
                    "INSERT INTO search_index (keyword, used_at) VALUES (?, strftime('%s','now'))"
                    " ON CONFLICT(keyword) DO UPDATE SET used_at=strftime('%s','now')",
                    (db_key,)
                )
                db.commit()
                rows = db.execute(
                    'SELECT keyword FROM search_index ORDER BY used_at DESC LIMIT 20'
                ).fetchall()
                self._recent_searches = [r[0] for r in rows]
            finally:
                db.close()
        self._refresh_search_recent_combo()

    def _load_recent_searches_from_db(self):
        # Cancel any in-flight loader from the previous archive (race guard).
        if self._recent_loader and self._recent_loader.isRunning():
            self._recent_loader.loaded.disconnect()
            self._recent_loader.quit()
            self._recent_loader.wait()

        # Clear immediately so old terms never linger.
        self._recent_searches = []
        self._refresh_search_recent_combo()

        if not self._case_dir:
            return

        self._recent_loader = DbRecentLoader(self._case_dir)
        self._recent_loader.loaded.connect(self._apply_recent_searches)
        self._recent_loader.start()

    def _apply_recent_searches(self, terms: list):
        self._recent_searches = terms
        self._refresh_search_recent_combo()

    def _load_search_from_db(self, db_key: str) -> bool:
        """Kick off async population of the results table from the DB for *db_key*.
        Returns True immediately if the DB has cached results, False if none."""
        if not self._case_dir or not self.zip_path:
            return False

        term, scope_label = _decode_search_key(db_key)
        scope_tag = f" in {scope_label}" if scope_label != 'all files' else ''

        self._set_incomplete_banner()   # always reset before loading any result
        db = self._open_results_db_conn()
        if db is None:
            return False
        try:
            row = db.execute(
                'SELECT id, complete, files_searched, total_files '
                'FROM search_index WHERE keyword=?',
                (db_key,)
            ).fetchone()
            if row is None:
                return False  # never searched
            term_id, complete, files_searched, total_files = row
            (count,) = db.execute(
                'SELECT COUNT(*) FROM search_results WHERE term_id=?', (term_id,)
            ).fetchone()
        finally:
            db.close()

        if not complete:
            from PySide6.QtWidgets import QMessageBox
            msg = QMessageBox(self)
            msg.setWindowTitle("Incomplete Search")
            msg.setText(
                f"The previous search for '{term}'{scope_tag} was stopped after "
                f"{files_searched:,} of {total_files:,} files.\n\n"
                f"Results may be missing. Redo the search from the beginning?"
            )
            redo_btn = msg.addButton("Redo Search",             QMessageBox.ButtonRole.AcceptRole)
            msg.addButton("View Incomplete Results", QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(redo_btn)
            msg.exec()
            if msg.clickedButton() == redo_btn:
                self._start_keyword_search()
                return True

        if count == 0:
            self.search_results_model.clear()
            self.search_results_model.setHorizontalHeaderLabels(
                ["Name", "Hits", "Context", "Offset"])
            self._search_folder_items.clear()
            self._search_file_items.clear()
            if not complete:
                self._set_incomplete_banner(files_searched, total_files)
                self.search_status.setText(
                    f"'{term}'{scope_tag} — 0 hits in searched files (incomplete)")
            else:
                self.search_status.setText(f"'{term}'{scope_tag} — 0 hits (from cache)")
            return True

        self._search_incomplete_files = (files_searched, total_files) if not complete else (0, 0)

        # Stop any in-flight loader for a previous term.
        if self._db_loader and self._db_loader.isRunning():
            self._db_loader.rows_ready.disconnect()
            self._db_loader.finished.disconnect()
            self._db_loader.quit()
            self._db_loader.wait()

        self.search_results_model.clear()
        self.search_results_model.setHorizontalHeaderLabels(
            ["Name", "Hits", "Context", "Offset"])
        self._search_folder_items.clear()
        self._search_file_items.clear()
        self.search_status.setText(f"Loading '{term}'{scope_tag} from cache…")

        self._db_loader_term   = term
        self._db_loader_db_key = db_key
        self._db_loader = DbSearchLoader(self._case_dir, db_key)
        self._db_loader.rows_ready.connect(self._on_db_loader_rows)
        self._db_loader.finished.connect(self._on_db_loader_finished)
        self._db_loader.start()
        return True

    def _on_db_loader_rows(self, rows: list):
        self.search_results_view.setUpdatesEnabled(False)
        try:
            for filename, offset, context in rows:
                self._search_add_hit(filename, offset, context)
        finally:
            self.search_results_view.setUpdatesEnabled(True)

    def _on_db_loader_finished(self, total: int):
        db_key = self._db_loader_db_key or self._db_loader_term
        term, scope_label = _decode_search_key(db_key)
        scope_tag = f" in {scope_label}" if scope_label != 'all files' else ''
        done, total_files = self._search_incomplete_files
        if done or total_files:
            self._set_incomplete_banner(done, total_files)
            self.search_status.setText(
                f"'{term}'{scope_tag} — {total:,} hit{'s' if total != 1 else ''} "
                f"(incomplete — {done:,} of {total_files:,} files searched)")
        else:
            self.search_status.setText(
                f"'{term}'{scope_tag} — {total:,} hit{'s' if total != 1 else ''} (from cache)")

    # ── Search lifecycle ──────────────────────────────────────────────────────

    def _start_search_index_build(self):
        """Kick off background index build immediately after an archive is loaded."""
        if self._search_index_worker and self._search_index_worker.isRunning():
            self._search_index_worker.stop()
            self._search_index_worker.wait()
        self._search_entries = None
        self._set_incomplete_banner()   # clear any banner left from the previous archive
        self.search_results_model.clear()
        self.search_field.clear()
        self.search_status.setText("")
        worker = SearchIndexWorker(
            self.zip_path,
            streaming_index=self._streaming_index,
            case_dir=self._case_dir,
            delta=self._local_extra_delta,
        )
        worker.entries_ready.connect(self._on_search_index_ready)
        self._search_index_worker = worker
        worker.start()

    def _on_search_index_ready(self, entries: list):
        self._search_entries = entries or None

    def _start_keyword_search(self):
        from PySide6.QtWidgets import QMessageBox
        term = self.search_field.text().strip()
        if not term or not self.zip_path:
            return
        self._stop_keyword_search()
        self._set_incomplete_banner()
        self.search_results_model.clear()
        self.search_results_model.setHorizontalHeaderLabels(
            ["Name", "Hits", "Context", "Offset"])
        self._search_folder_items.clear()
        self._search_file_items.clear()
        self._pending_db_hits.clear()

        scope = self.search_scope_combo.currentData()
        is_restricted = isinstance(scope, dict) or scope == 'selected'

        # For restricted scopes (bookmark / selected), we need the index to already
        # be ready so we can resolve ui_paths to physical entry names.
        if is_restricted and self._search_entries is None:
            QMessageBox.information(
                self, "Index Building",
                "The search index is still building — please wait a moment and try again.")
            return

        scoped_entries, scoped_nested_map, scope_label = self._resolve_search_scope(scope)

        if is_restricted and not scoped_entries and not scoped_nested_map:
            what = ("No files are currently selected."
                    if scope == 'selected'
                    else "This bookmark group has no entries yet.")
            QMessageBox.information(self, "Nothing to Search", what)
            return

        db_key = _encode_search_key(term, scope_label)
        self._current_search_db_key = db_key
        self._save_recent_search(db_key)
        db = self._open_results_db_conn()
        if db:
            try:
                db.execute(
                    'DELETE FROM search_results '
                    'WHERE term_id=(SELECT id FROM search_index WHERE keyword=?)',
                    (db_key,)
                )
                db.commit()
            finally:
                db.close()

        # For the fallback case (entries=None, worker builds its own list) the
        # worker still needs scope/exclude_prefixes to do its own filtering.
        worker_scope = scope if scope in ('app_data', 'all') else 'all'
        _cellebrite_excludes = ('metadata1/', 'metadata2/')
        worker_exclude = (
            _cellebrite_excludes
            if worker_scope == 'all' and self._adapter.format == FfsAdapter.FORMAT_CELLEBRITE
            else ()
        )

        scope_tag = f" in {scope_label}" if scope_label != 'all files' else ''
        self.search_status.setText(f"Searching '{term}'{scope_tag}…")
        self.search_btn.setEnabled(False)
        self.search_stop_btn.setEnabled(True)

        self._search_progress_dlg = SearchProgressDialog(term, parent=self)
        self._search_progress_dlg.cancelled.connect(self._cancel_keyword_search)

        self._search_worker = KeywordSearchWorker(
            self.zip_path, term,
            streaming_index=self._streaming_index,
            entries=scoped_entries,
            scope=worker_scope,
            exclude_prefixes=worker_exclude)
        self._search_worker.status_update.connect(self._search_progress_dlg.append_status)
        self._search_worker.result_found.connect(self._on_search_result)
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.finished.connect(self._on_search_finished)
        self._search_worker.start()

        # Start nested archive search in parallel (scoped_nested_map is already
        # filtered for restricted scopes; for unrestricted scopes it equals
        # the full nested_archive_map).
        if scoped_nested_map:
            self._nested_search_worker = NestedArchiveSearchWorker(scoped_nested_map, term)
            self._nested_search_worker.result_found.connect(self._on_nested_search_result)
            self._nested_search_worker.start()

        self._search_progress_dlg.exec()

    def _cancel_keyword_search(self):
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.stop()
        if self._nested_search_worker and self._nested_search_worker.isRunning():
            self._nested_search_worker.stop()

    def _stop_keyword_search(self):
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.stop()
            self._search_worker.wait()
        if self._nested_search_worker and self._nested_search_worker.isRunning():
            self._nested_search_worker.stop()
            self._nested_search_worker.wait()
        self.search_btn.setEnabled(True)
        self.search_stop_btn.setEnabled(False)

    def _on_search_result(self, name: str, offset: int, context: str):
        self._search_add_hit(name, offset, context)
        self._pending_db_hits.append((name, offset, context))

    def _on_nested_search_result(self, virtual_ui_path: str, offset: int,
                                  context: str, stored_path: str, entry_path: str):
        self._search_add_hit(virtual_ui_path, offset, context,
                             stored_path=stored_path, entry_path=entry_path)

    def _on_search_progress(self, done: int, total: int):
        hits = len(self._search_file_items)
        self.search_status.setText(
            f"Searching… {done:,}/{total:,} files  |  hits in {hits:,} file{'s' if hits != 1 else ''} so far")
        self._update_search_status_bar()
        if self._search_progress_dlg:
            self._search_progress_dlg.update_progress(done, total, hits)

    def _on_search_finished(self, total_hits: int, files_done: int, files_total: int,
                            stopped: bool):
        if self._search_entries is None and self._search_worker is not None:
            self._search_entries = self._search_worker.entries or None
        self.search_btn.setEnabled(True)
        self.search_stop_btn.setEnabled(False)
        db_key  = self._current_search_db_key or self.search_field.text().strip()
        term, scope_label = _decode_search_key(db_key)
        scope_tag = f" in {scope_label}" if scope_label != 'all files' else ''
        n_files = len(self._search_file_items)
        dlg     = self._search_progress_dlg

        complete       = 0 if stopped else 1
        files_searched = files_done  if stopped else files_total
        total_files    = files_total

        db = self._open_results_db_conn()
        if db:
            try:
                db.execute(
                    'UPDATE search_index SET complete=?, files_searched=?, total_files=? '
                    'WHERE keyword=?',
                    (complete, files_searched, total_files, db_key)
                )
                if self._pending_db_hits:
                    row = db.execute(
                        'SELECT id FROM search_index WHERE keyword=?', (db_key,)
                    ).fetchone()
                    if row:
                        db.executemany(
                            'INSERT INTO search_results (term_id, filename, offset, context) '
                            'VALUES (?,?,?,?)',
                            [(row[0], f, o, c) for f, o, c in self._pending_db_hits]
                        )
                db.commit()
            finally:
                db.close()
        self._pending_db_hits.clear()

        if dlg:
            if stopped:
                dlg.mark_interrupted(n_files)
                self._set_incomplete_banner(files_searched, total_files)
                self.search_status.setText(
                    f"'{term}'{scope_tag} — partial search, interrupted  "
                    f"({n_files:,} file{'s' if n_files != 1 else ''} with hits)")
            else:
                dlg.mark_finished(n_files, total_hits)
                self._set_incomplete_banner()
                self.search_status.setText(
                    f"'{term}'{scope_tag} — hits in {n_files:,} file{'s' if n_files != 1 else ''}")
        else:
            self._set_incomplete_banner()
            self.search_status.setText(
                f"'{term}'{scope_tag} — hits in {n_files:,} file{'s' if n_files != 1 else ''}")
        self._update_search_status_bar()
        for col in range(self.search_results_model.columnCount()):
            self.search_results_view.resizeColumnToContents(col)
