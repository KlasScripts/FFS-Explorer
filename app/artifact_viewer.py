"""artifact_viewer.py — ArtifactRunnerWorker, ArtifactRunnerDialog, and ArtifactViewerMixin."""

import html
import os
import pathlib
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import partial

from PySide6.QtWidgets import (
    QWidget, QLabel, QLineEdit, QComboBox, QHBoxLayout, QVBoxLayout,
    QTableView, QTreeView, QPlainTextEdit, QStackedWidget, QSplitter,
    QScrollArea, QCheckBox, QPushButton, QDialog, QMessageBox,
    QProgressDialog, QMenu, QDateEdit, QSpinBox, QDoubleSpinBox, QGroupBox,
    QTextBrowser, QListWidget, QListWidgetItem, QAbstractItemView,
)
from PySide6.QtCore import Qt, QThread, Signal, QAbstractTableModel, QModelIndex, QDate
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont

from db_utils import _open_results_db, start_run_log, complete_run_log, load_last_run
from highlight_delegate import HighlightDelegate
from artifact_media import (
    MediaThumbnailDelegate, MediaFullViewDialog, WebpageThumbnailRenderer, THUMB_CELL_SIZE,
)
from dialog_helpers import note_label, error_label, ERROR_STYLE, WARNING_COLOR, ACTIVE_BUTTON_STYLE
import validation_store
import parser_validation
import parser_versions


def _local_date(iso_str: str) -> str:
    """'YYYY-MM-DD' local-date for a stored UTC run_log ISO timestamp.

    Tool-provenance only (when the examiner ran this parser), never
    evidence — see ffs-explorer.py's _format_tool_ts_local for the fuller
    version (with UTC offset) used elsewhere; this one only needs the date.
    """
    if not iso_str:
        return ''
    try:
        dt_utc = datetime.strptime(iso_str, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return iso_str[:10]
    return dt_utc.astimezone().strftime('%Y-%m-%d')


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.?!])\s+(?=[A-Z0-9(])')
_BACKTICK_SPLIT_RE = re.compile(r'`([^`]+)`')


def _notes_html_paragraphs(text: str, sentences_per_para: int = 2) -> str:
    """Turn one of this project's hand-written `description`/`warning`
    strings — always a single sentence-dense paragraph in the source, easy
    to read there but a hard-to-scan wall of text in the UI — into
    readable HTML: paragraph breaks every couple of sentences, and
    `backtick`-quoted identifiers (already used throughout these strings
    for table/column names) rendered as monospace spans."""
    if not text:
        return ''
    sentences = _SENTENCE_SPLIT_RE.split(text.strip())
    paras = [' '.join(sentences[i:i + sentences_per_para])
             for i in range(0, len(sentences), sentences_per_para)]
    out = []
    for para in paras:
        # Alternating [plain, code, plain, code, ..., plain] — escape each
        # piece on its own so a literal '<'/'&' inside a `backtick span`
        # (e.g. a SQL comparison) can't break the surrounding markup.
        pieces = _BACKTICK_SPLIT_RE.split(para)
        rendered = []
        for i, piece in enumerate(pieces):
            escaped = html.escape(piece)
            if i % 2 == 1:  # odd indices are the backtick-captured groups
                escaped = (f'<code style="background:rgba(127,127,127,0.15); '
                          f'padding:1px 4px; border-radius:3px;">{escaped}</code>')
            rendered.append(escaped)
        out.append(f'<p style="margin:0 0 10px 0; line-height:145%;">{"".join(rendered)}</p>')
    return ''.join(out)


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
        # Timestamp display: {column_name: unit_code} from the parser
        # module's own timestamp_fields declaration, plus a formatter
        # callback — see set_timestamp_formatting(). Empty/None means no
        # timestamp columns in this report, or nothing to reformat (the
        # ordinary case for most tables).
        self._ts_units:     dict = {}
        self._ts_formatter        = None
        # Column names (by real column name, same space as _ts_units —
        # NOT visible-index space) whose raw value is a byte count (int)
        # to be MB-formatted at display time — see set_byte_columns().
        # Same raw-value/format-at-display split as timestamps above, and
        # for the identical reason: a value baked into a display string
        # before storage (e.g. '51.80 MB') can't be sorted as a number —
        # sort() would compare it lexicographically ('10.00 MB' sorting
        # before '9.00 MB'). Added 2026-08-25 for the Apps table's Total
        # Size column, after fixing list-mode sort() the same day made
        # this exact failure mode reachable/visible for the first time.
        self._byte_cols:    set  = set()
        # Column indices holding an archive ui_path to an attachment/media
        # file — from the parser module's own media_fields declaration
        # (names), resolved to indices here. See set_media_columns().
        self._media_cols:   set  = set()
        # Real (self._columns-space) indices of columns actually shown in
        # the Report table — everything by default. A parser's own
        # hidden_fields declaration (see set_hidden_columns()) narrows
        # this, e.g. a joined table's raw rowid kept only for the Hex
        # panel's Record mode to look up, never meant for display. Every
        # QAbstractTableModel method below works in VISIBLE-index space
        # (what the QTableView actually sees); row_dict() is the one
        # exception, deliberately returning ALL columns since callers like
        # ArtifactViewerMixin._art_load_record_hex need the hidden fields.
        self._visible_idx:  list = []

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
        self._ts_units, self._ts_formatter = {}, None
        self._byte_cols = set()
        self._media_cols = set()
        self._visible_idx = list(range(len(self._columns)))
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
        self._ts_units, self._ts_formatter = {}, None
        self._byte_cols = set()
        self._media_cols = set()
        self._visible_idx = list(range(len(self._columns)))
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
        self._ts_units, self._ts_formatter = {}, None
        self._byte_cols = set()
        self._media_cols = set()
        self._visible_idx = []
        self.endResetModel()

    def set_timestamp_formatting(self, units: dict, formatter) -> None:
        """units: {column_name: unit_code}, from the parser module's own
        timestamp_fields declaration. formatter(raw_value, unit_code) -> str,
        or None to disable (no timestamp columns in this report). The
        active mode itself is shown once, in the shared banner above every
        tab (see FastZipBrowser._refresh_timestamp_mode_indicator) — not
        repeated per column header here. Call once, right after
        load_from_db()/load_rows() — resets on every fresh load, so a
        report with no declaration never inherits a previous report's
        formatting."""
        self._ts_units     = dict(units) if units else {}
        self._ts_formatter = formatter if units else None
        # layoutChanged rather than hand-built dataChanged/headerDataChanged
        # ranges — simpler, and safe on an empty table (no row/column-bound
        # index construction to get right).
        self.layoutChanged.emit()

    def set_byte_columns(self, col_names) -> None:
        """col_names: iterable of column-name strings whose raw stored
        value is a byte count (int) — displayed as fixed-unit MB
        ('51.80 MB') rather than baked into the stored value, so the
        underlying int stays numerically sortable (see sort()'s list-mode
        branch). Call once, right after load_from_db()/load_rows() — same
        reset-on-every-load reasoning as set_timestamp_formatting above."""
        self._byte_cols = set(col_names) if col_names else set()
        self.layoutChanged.emit()

    def set_media_columns(self, col_names) -> None:
        """col_names: iterable of column-name strings from the parser
        module's own media_fields declaration — cells there hold an archive
        ui_path to an attachment/media file, and are painted as thumbnails
        by MediaThumbnailDelegate (see ArtifactViewerMixin._art_show_report)
        rather than as plain path text. Indices are in VISIBLE-column space
        (what setItemDelegateForColumn etc. actually address) — call after
        set_hidden_columns() so self._visible_idx already reflects any
        hidden fields. Call once, right after load_from_db()/load_rows() —
        resets on every fresh load, same reasoning as
        set_timestamp_formatting() above."""
        names = set(col_names) if col_names else set()
        self._media_cols = {vis_i for vis_i, real_i in enumerate(self._visible_idx)
                            if self._columns[real_i] in names}
        self.layoutChanged.emit()

    def media_columns(self) -> set:
        return set(self._media_cols)

    def set_hidden_columns(self, names) -> None:
        """names: iterable of column-name strings from the parser module's
        own hidden_fields declaration — internal plumbing (e.g. a joined
        table's raw rowid, kept only for the Hex panel's Record mode to
        look up — see record_source) that should never appear as a Report
        table column. Changes columnCount(), so it's a structural reset,
        not just layoutChanged. Call once, right after load_from_db()/
        load_rows(), and BEFORE set_media_columns() (which indexes in the
        resulting visible-column space this method establishes)."""
        self.beginResetModel()
        hidden = set(names) if names else set()
        self._visible_idx = [i for i, c in enumerate(self._columns) if c not in hidden]
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
        return 0 if parent.isValid() else len(self._visible_idx)

    def _real_col(self, visible_col: int) -> int | None:
        """Translate a QTableView column index (visible-space, the only
        space the view/header/sort ever deal in) to its real index into
        self._columns/a row tuple. None if out of range."""
        return (self._visible_idx[visible_col]
                if 0 <= visible_col < len(self._visible_idx) else None)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._get_row(index.row())
        if row is None:
            return ''
        real_col = self._real_col(index.column())
        if real_col is None:
            return ''
        val = row[real_col] if real_col < len(row) else None
        if val is None or val == '':
            return ''
        col_name = self._columns[real_col]
        unit_code = self._ts_units.get(col_name)
        if unit_code and self._ts_formatter:
            return self._ts_formatter(val, unit_code)
        if col_name in self._byte_cols:
            try:
                return f"{float(val) / 1_048_576:,.2f} MB"
            except (TypeError, ValueError):
                return str(val)
        return str(val)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            real_col = self._real_col(section)
            return self._columns[real_col] if real_col is not None else None
        return str(section + 1)

    def row_dict(self, model_row: int) -> dict:
        """Raw (unformatted) column-name -> value mapping for one row —
        used by the "jump to record in hex" feature, which needs the
        actual rowid/source-table values a parser wrote (ints, not
        display text), unlike data() which stringifies everything and
        reformats timestamp columns for on-screen display."""
        row = self._get_row(model_row)
        if row is None:
            return {}
        return {col: (row[i] if i < len(row) else None)
                for i, col in enumerate(self._columns)}

    def sort(self, col: int, order: Qt.SortOrder) -> None:
        """DB mode: re-query rowids from SQLite using ORDER BY — fast
        C-level sort. List mode: sort the in-memory rows directly in
        Python (added 2026-08-25 — list mode never implemented sorting at
        all before this; the header click reached here via
        setSortingEnabled(True) same as DB mode, but this method's own
        `if not self._conn: return` silently no-op'd every time, which is
        why sorting looked broken specifically for a list-mode table like
        the Apps view or Exported Files, never for an ordinary DB-backed
        Report). *col* arrives from the QTableView header in visible-space,
        same as every other QAbstractTableModel method here — see
        _real_col()."""
        real_col = self._real_col(col)
        if real_col is None:
            return
        if self._conn is None:
            if not self._rows:
                return
            reverse = order == Qt.SortOrder.DescendingOrder

            def _key(i):
                row = self._rows[i]
                v = row[real_col] if real_col < len(row) else None
                # Blank/None sorts as the smallest value regardless of the
                # column's real type (int, str, ...) — comparing (0, '')
                # against (1, v) never touches '' vs v directly, since
                # tuple comparison short-circuits on the leading 0/1, so
                # this is safe even when v isn't otherwise comparable to
                # a bare ''.
                return (0, '') if v is None or v == '' else (1, v)
            try:
                new_order = sorted(self._rowids, key=_key, reverse=reverse)
            except TypeError:
                # Same column holding genuinely mixed, mutually-incomparable
                # types (shouldn't happen by construction — each parser/
                # _flatten_app_intelligence_row column is one type — but
                # degrade to a string sort rather than leaving the click
                # silently doing nothing, same spirit as the old no-op).
                new_order = sorted(self._rowids, key=lambda i: str(_key(i)), reverse=reverse)
            self.beginResetModel()
            self._rowids = new_order
            self._cache.clear()
            self._lru.clear()
            self.endResetModel()
            return
        direction = 'DESC' if order == Qt.SortOrder.DescendingOrder else 'ASC'
        col_name  = self._columns[real_col]
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
                 term: str, col_idx: int, exclude_low_confidence: bool = False,
                 parent=None):
        super().__init__(parent)
        self._db_path = db_path
        self._table   = table
        self._columns = columns
        self._pattern = f'%{term}%'
        self._term    = term
        self._col_idx = col_idx
        # See _apply_art_filter's "Hide likely false positives" checkbox —
        # only ever True for a report whose parser declares
        # recoverable_tables. ANDed onto the term match below (both can
        # be active at once — an examiner filtering by keyword still
        # doesn't want a garbage carved match cluttering the results);
        # the caller already guarantees at least one of term/this is
        # truthy before ever constructing this worker (see the
        # `not term and not hide_lowconf` early-return).
        self._exclude_low_confidence = exclude_low_confidence

    def run(self) -> None:
        p = self._pattern
        conditions, args = [], []
        if self._term:
            if self._col_idx < 0:
                conditions.append('(' + ' OR '.join(f'"{c}" LIKE ?' for c in self._columns) + ')')
                args.extend([p] * len(self._columns))
            else:
                conditions.append(f'"{self._columns[self._col_idx]}" LIKE ?')
                args.append(p)
        if self._exclude_low_confidence and 'source' in self._columns:
            # NULL-safe: LIKE against a genuinely NULL "source" evaluates
            # to NULL (neither true nor false in SQL's three-valued logic),
            # which a bare `NOT "source" LIKE ?` would silently drop —
            # this table's own "source" is never NULL in practice (always
            # at least '', see artifact_db.py's TEXT-column convention),
            # but the explicit OR IS NULL keeps this correct even if that
            # ever changes.
            conditions.append('("source" IS NULL OR "source" NOT LIKE ?)')
            args.append('%(likely false positive%')
        where = ' AND '.join(conditions) if conditions else '1'
        try:
            conn   = sqlite3.connect(self._db_path, timeout=5)
            rowids = [r[0] for r in conn.execute(
                f'SELECT rowid FROM "{self._table}" WHERE {where}', args)]
            conn.close()
        except Exception:
            rowids = []
        self.done.emit(rowids, where, args)


class AppIntelligenceWorker(QThread):
    """Runs app_intelligence.scan_apps() on a background thread — a first
    scan of a large case can take ~1 minute (documented in CLAUDE.md's
    mcp_server.py entry), so this must never run on the GUI thread. Takes
    the pre-built CaseContext in __init__ (mirrors ArtifactRunnerWorker's
    pattern of snapshotting plain data at construction time rather than
    reading `self.xxx` live off the calling widget from a different
    thread) — ctx's own lambdas already close over the FastZipBrowser's
    plain dict/list attributes, the same cross-thread-safe shape the MCP
    server's own daemon thread already uses for the identical CaseContext.
    Also persists the result to casecache.db (save_app_intelligence +
    save_blob) exactly like list_apps' own miss-path, so this scan and a
    later (or earlier) MCP list_apps call share one cache instead of each
    re-walking the archive."""

    done = Signal(list, str)   # (rows, error) — error is '' on success

    def __init__(self, ctx, cache_key: str, parent=None):
        super().__init__(parent)
        self._ctx = ctx
        self._cache_key = cache_key

    def run(self) -> None:
        import app_intelligence
        from db_utils import _open_cache_db, save_app_intelligence, save_blob
        try:
            rows = app_intelligence.scan_apps(self._ctx)
            with closing(_open_cache_db(self._ctx.case_dir)) as cache_db:
                save_app_intelligence(cache_db, rows)
                save_blob(cache_db, 'app_intelligence_scan_key', '1',
                         self._cache_key.encode())
        except Exception as exc:
            self.done.emit([], str(exc))
            return
        self.done.emit(rows, '')


# ── Tree node role sentinels ──────────────────────────────────────────────────
_ART_APPS   = "__art_apps__"     # singleton — no per-script suffix
_ART_AI_SUMMARY = "__art_ai_summary__"  # singleton — opens AISummaryDialog, no page of its own
_ART_APP_NOTES = "__art_app_notes__"  # singleton — Apps node's own notes, not per-parser
_ART_GROUP  = "__art_group__:"   # app-name node — clicking it shows the Report
_ART_APP_GROUP = "__art_app_group__:"  # multi-report app parent (see app_group_label
                                        # below) — clicking it shows one designated
                                        # member report (the suffix names which script)
_ART_NOTES  = "__art_notes__:"
_ART_SCRIPT = "__art_script__:"
_ART_SOURCE = "__art_source__:"
_ART_FILES  = "__art_files__:"
_ART_VALIDATION = "__art_validation__:"


class ArtifactRunnerWorker(QThread):
    log  = Signal(str)
    done = Signal()

    def __init__(self, selected, zip_path, adapter, case_dir, platform: str,
                guid_to_bundle: dict | None = None):
        super().__init__()
        self._selected        = selected
        self._zip_path        = zip_path
        self._adapter         = adapter
        self._case_dir        = case_dir
        self._platform        = platform
        self._guid_to_bundle  = guid_to_bundle or {}

    def run(self):
        from artifact_runner import run_artifact
        from artifact_db import write_artifact_results

        try:
            case_conn = _open_results_db(self._case_dir)
        except Exception as exc:
            self.log.emit(f"Could not open results database: {exc}")
            self.done.emit()
            return

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
                    import parser_versions
                    version = parser_versions.get_current_version(self._platform, script_name)
                    run_id = start_run_log(case_conn, f'artifact_{script_name}',
                                           parser_version=version)
                except Exception:
                    pass
                rows, error = run_artifact(
                    script_name, module,
                    self._zip_path, self._adapter,
                    case_dir=self._case_dir,
                    zip_obj=zip_obj,
                    guid_to_bundle=self._guid_to_bundle,
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
                    self._precache_media_thumbnails(module, rows, zip_obj)
        except Exception as exc:
            self.log.emit(f"\nUnexpected error: {exc}")
        finally:
            case_conn.close()
            if zip_obj:
                zip_obj.close()

        self.log.emit("\nAll selected parsers finished.")
        self.done.emit()

    def _precache_media_thumbnails(self, module, rows: list[dict], zip_obj) -> None:
        """Warm the on-disk thumbnail cache for a `media_fields` parser right
        after its rows are written, so the Report tab's thumbnails are
        already cached by the time anyone opens it — the on-open decode in
        artifact_viewer's _start_art_media_thumbnails was fine for a
        handful of attachments, but visibly slow to fill in the first time
        a report with hundreds/thousands of media rows (e.g. WhatsApp) was
        opened. Reuses media_viewer.ThumbnailWorker's own QThread class as
        a plain synchronous call (.run(), not .start()) — safe because
        we're already running on this worker's own background QThread, not
        the GUI thread, and ThumbnailWorker touches no GUI/widget state,
        only QImage decode + its own zip handle + its own cache DB
        connection. Same cache (keyed ui_path+file_size+thumb_size) the
        Report tab reads from, so a hit there is instant regardless of
        whether it was filled here or on an earlier report open. Best
        effort: never allowed to fail the parser run itself."""
        media_fields = getattr(module, 'media_fields', None)
        if not media_fields or not rows:
            return
        paths = {row[f] for f in media_fields for row in rows if row.get(f)}
        if not paths:
            return
        try:
            zip_info_map = {}
            for p in paths:
                physical = self._adapter.resolve(p)
                try:
                    zip_info_map[physical] = zip_obj.getinfo(physical).file_size
                except KeyError:
                    pass
            self.log.emit(f"  Pre-caching thumbnails for {len(paths)} attachment(s)…")
            from media_viewer import ThumbnailWorker
            thumb_worker = ThumbnailWorker(
                self._zip_path, list(paths), self._adapter.resolve,
                THUMB_CELL_SIZE, zip_info_map, cache_dir=self._case_dir)
            thumb_worker.run()
        except Exception as exc:
            self.log.emit(f"  Thumbnail pre-cache skipped: {exc}")


class ArtifactRunnerDialog(QDialog):
    parsers_completed = Signal()

    def __init__(self, zip_path, zip_names, adapter, case_dir,
                 is_android, parent=None, guid_to_bundle: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Run Artifact Parsers")
        self.setMinimumSize(480, 540)
        self._worker = None
        self._guid_to_bundle = guid_to_bundle or {}

        platform = 'android' if is_android else 'ios'
        self._platform = platform

        from artifact_runner import load_artifacts, _resolve_app_group_base
        all_artifacts, load_errors = load_artifacts(platform)

        def _exists(candidates):
            return any(c in zip_names for c in candidates)

        def _mod_matches(mod):
            if hasattr(mod, 'app_path') and hasattr(mod, 'files'):
                app_base = mod.app_path.strip('/')
                # A parser that enumerates a whole directory itself rather
                # than declaring fixed files (e.g. chrome_cache.py, via
                # the _zip_names/_read_zip_bytes reserved paths keys —
                # see artifact_runner.py) has genuinely no files entries
                # to check existence against — `any()` over an empty
                # files.values() is always False, which would silently
                # hide it from this list regardless of whether its real
                # target actually exists in the archive. Confirmed a real
                # gap, not hypothetical: chrome_cache.py was invisible
                # here on real casework where its own Cache_Data
                # directory genuinely exists. existence_check_paths is
                # the same subpath-relative-to-app_base convention as
                # files.values(), for exactly this case — only consulted
                # when files itself has nothing to check.
                check_paths = (list(mod.files.values())
                              or list(getattr(mod, 'existence_check_paths', ())))
                if not check_paths:
                    return False
                return any(
                    _exists(adapter.user_candidates(f"{app_base}/{sub.lstrip('/')}"))
                    for sub in check_paths
                )
            if hasattr(mod, 'app_group') and hasattr(mod, 'files'):
                app_base = _resolve_app_group_base(mod.app_group, self._guid_to_bundle)
                if app_base is None:
                    return False
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

        # Surface parser scripts that failed to import (e.g. a dependency that
        # wasn't bundled into the frozen build).  Without this they'd silently
        # disappear from the list, making it look like nothing matched.
        if load_errors:
            err_lines = "\n".join(f"  • {fn}: {msg}" for fn, msg in load_errors)
            warn = note_label(
                "⚠ Some parser scripts could not be loaded and are unavailable:\n"
                f"{err_lines}", style=ERROR_STYLE)
            layout.addWidget(warn)

        if not available:
            layout.addWidget(QLabel(
                "No parsers are available — see the errors above."
                if load_errors else
                "No artifact parsers matched files in the loaded archive."))
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
            last = run_history.get(script_name)
            cb = QCheckBox(getattr(mod, 'name', script_name))
            # Default to unchecked for parsers already run to completion in this
            # case — they don't need re-running.  Never-run and incomplete
            # parsers stay ticked.
            cb.setChecked(not (last and last['complete']))
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(cb)
            if last:
                rows   = last['output_rows'] or 0
                run_at = _local_date(last['run_at'])
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
            self._case_dir, self._platform,
            guid_to_bundle=self._guid_to_bundle,
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


class AISummaryWorker(QThread):
    """Runs ai_summary.run_summary() on a background thread. A real run is
    several sequential local-LLM calls (one per time-gap chunk, plus a
    hierarchical reduce over the mini-summaries — see ai_summary.py's own
    module docstring) — confirmed by direct testing to take anywhere from
    well under a minute (a handful of chunks) to several minutes (a
    fragmented, multi-month dataset), so this must never run on the GUI
    thread."""

    done = Signal(dict)      # the full result dict from run_summary (has 'error' on failure)
    progress = Signal(str)   # one short status line per chunk/combine call — see ai_summary.run_summary's own progress param

    def __init__(self, case_dir: str, script_name: str, platform: str, parent=None):
        super().__init__(parent)
        self._case_dir = case_dir
        self._script_name = script_name
        self._platform = platform

    def run(self) -> None:
        import ai_summary
        try:
            result = ai_summary.run_summary(self._case_dir, self._script_name,
                                            platform=self._platform,
                                            progress=self.progress.emit)
        except Exception as exc:
            result = {"error": str(exc)}
        self.done.emit(result)


class ModelListWorker(QThread):
    """GET .../models against the configured local LLM server — a plain
    network call (local_llm.list_models), but still off the GUI thread:
    the server may be unreachable and hang up to its own timeout rather
    than fail instantly."""

    done = Signal(dict)  # {'models': [...]} or {'error': ...}

    def __init__(self, endpoint_base: str, api_key: str, parent=None):
        super().__init__(parent)
        self._endpoint_base = endpoint_base
        self._api_key = api_key

    def run(self) -> None:
        import local_llm
        self.done.emit(local_llm.list_models(self._endpoint_base, self._api_key))


class AISummaryDialog(QDialog):
    """The AI Summary feature's configuration + run dialog — reachable from
    the Artifact tree's own "AI Summary" top-level node (_ART_AI_SUMMARY /
    ArtifactViewerMixin._open_ai_summary_dialog below). Lets an examiner
    pick which already-completed report to summarize, which of its columns
    get sent to the local LLM, the chunk-size/time-gap thresholds that
    drive ai_summary.py's map-reduce splitting, and the prompt template
    itself — then run it and see the result.

    Deliberately reads/writes settings through ai_summary.py /
    ai_summary_store.py, the SAME module the MCP tools
    (get_ai_summary_settings/set_ai_summary_settings/run_ai_summary in
    mcp_server.py) already use — a setting saved here and one saved via an
    AI client editing it through MCP are the same setting, never two
    independent copies, and a run triggered from either surface sends
    exactly the same data the same way.
    """

    def __init__(self, case_dir: str, platform: str, reports: list,
                parent=None):
        """*reports* is [(script_name, display_label), ...] for every
        report that's actually been run in this case — built by the caller
        (ArtifactViewerMixin._open_ai_summary_dialog) the same way
        _refresh_artifact_tab already enumerates completed parsers, so
        this dialog never needs its own copy of that logic."""
        super().__init__(parent)
        self.setWindowTitle("AI Summary")
        self._case_dir = case_dir
        self._platform = platform
        self._worker = None
        self._model_worker = None
        self._generating_script_name = None
        self._column_checks: list[tuple[QCheckBox, str]] = []

        outer = QVBoxLayout(self)

        if not reports:
            outer.addWidget(note_label(
                "No artifact reports have been run in this case yet — run a "
                "parser first (see the Apps node), then reopen AI Summary."))
            close_btn = QPushButton("Close")
            close_btn.clicked.connect(self.reject)
            outer.addWidget(close_btn)
            self.setMinimumSize(420, 120)
            return

        # Everything configuration-related lives in ONE scrollable section
        # (report picker, columns, chunking, prompt, connection) — found
        # necessary by direct testing: this dialog has enough content that
        # a fixed dialog height (via setMinimumSize alone, the first cut's
        # approach) squeezed rows below their natural size and made the
        # chunk-size row visually collide with the column checklist above
        # it. Wrapping the config section in a QScrollArea means it simply
        # scrolls instead, regardless of how many columns a report has or
        # how small the dialog gets resized — never another cramped-layout
        # bug of this shape. The action buttons, progress log, and result
        # area stay OUTSIDE the scroll, pinned at the bottom, since those
        # are what an examiner needs visible at a glance once a run starts.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        form = QVBoxLayout(form_widget)

        # ── Report picker ──────────────────────────────────────────────
        report_row = QHBoxLayout()
        report_row.addWidget(QLabel("Report:"))
        self._report_combo = QComboBox()
        for script_name, label in reports:
            self._report_combo.addItem(label, script_name)
        report_row.addWidget(self._report_combo, 1)
        form.addLayout(report_row)

        # ── Columns to send ────────────────────────────────────────────
        columns_group = QGroupBox("Columns to send")
        self._columns_layout = QVBoxLayout(columns_group)
        self._columns_layout.setSpacing(2)
        form.addWidget(columns_group)

        # ── Chunking ───────────────────────────────────────────────────
        chunk_group = QGroupBox("Chunking")
        chunk_group_layout = QVBoxLayout(chunk_group)
        chunk_row = QHBoxLayout()
        chunk_row.addWidget(QLabel("Rows per chunk (max):"))
        self._chunk_size_spin = QSpinBox()
        self._chunk_size_spin.setRange(1, 1000)
        chunk_row.addWidget(self._chunk_size_spin)
        chunk_row.addSpacing(20)
        chunk_row.addWidget(QLabel("New chunk after a gap of (minutes):"))
        self._max_gap_spin = QDoubleSpinBox()
        self._max_gap_spin.setRange(0.5, 100000)
        self._max_gap_spin.setDecimals(1)
        chunk_row.addWidget(self._max_gap_spin)
        chunk_row.addStretch()
        chunk_group_layout.addLayout(chunk_row)
        chunk_group_layout.addWidget(note_label(
            "Rows are grouped into chunks along natural time gaps in the "
            "data, not fixed counts — a chunk only splits mid-session if it "
            "also hits the row cap above. Each chunk gets its own LLM call; "
            "more than one chunk is then combined into a final narrative."))
        form.addWidget(chunk_group)

        # ── Prompt ─────────────────────────────────────────────────────
        prompt_group = QGroupBox("Prompt template (must contain {data})")
        prompt_group_layout = QVBoxLayout(prompt_group)
        self._prompt_edit = QPlainTextEdit()
        self._prompt_edit.setFixedHeight(140)
        prompt_group_layout.addWidget(self._prompt_edit)
        self._prompt_error = error_label()
        prompt_group_layout.addWidget(self._prompt_error)
        form.addWidget(prompt_group)

        # ── Connection ─────────────────────────────────────────────────
        conn_group = QGroupBox("Local LLM connection")
        conn_group_layout = QVBoxLayout(conn_group)
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Endpoint:"))
        self._endpoint_edit = QLineEdit()
        conn_row.addWidget(self._endpoint_edit, 1)
        conn_group_layout.addLayout(conn_row)

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API key:"))
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self._api_key_edit, 1)
        conn_group_layout.addLayout(key_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("Model:"))
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        model_row.addWidget(self._model_combo, 1)
        self._refresh_models_btn = QPushButton("Refresh Models")
        self._refresh_models_btn.clicked.connect(self._on_refresh_models)
        model_row.addWidget(self._refresh_models_btn)
        conn_group_layout.addLayout(model_row)

        self._connection_status = note_label("")
        conn_group_layout.addWidget(self._connection_status)
        form.addWidget(conn_group)

        form.addStretch()
        scroll.setWidget(form_widget)
        outer.addWidget(scroll, 1)

        # ── Action buttons (pinned, not scrolled) ─────────────────────
        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.clicked.connect(self._on_save_settings)
        self._generate_btn = QPushButton("Generate Summary")
        self._generate_btn.clicked.connect(self._on_generate)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._save_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._generate_btn)
        btn_row.addWidget(self._close_btn)
        outer.addLayout(btn_row)

        # ── Log (pinned) — a lightweight sense of what's being sent/
        # returned call-by-call, fed by AISummaryWorker's progress signal
        # (itself fed by ai_summary.run_summary's own progress callback).
        # Deliberately just short status lines, never the actual chunk
        # text/prompt content — see the "not too much data" ask this was
        # built for. ──────────────────────────────────────────────────
        log_group = QGroupBox("Log")
        log_group_layout = QVBoxLayout(log_group)
        self._log_edit = QPlainTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setFixedHeight(90)
        self._log_edit.setFont(QFont("Courier", 10))
        log_group_layout.addWidget(self._log_edit)
        outer.addWidget(log_group)

        # ── Result (pinned) ────────────────────────────────────────────
        result_group = QGroupBox("Result")
        result_group_layout = QVBoxLayout(result_group)
        self._status_label = note_label("Ready.")
        result_group_layout.addWidget(self._status_label)
        self._output_edit = QPlainTextEdit()
        self._output_edit.setReadOnly(True)
        result_group_layout.addWidget(self._output_edit, 1)

        self._show_chunks_check = QCheckBox("Show per-chunk detail")
        self._show_chunks_check.toggled.connect(self._on_toggle_chunk_detail)
        result_group_layout.addWidget(self._show_chunks_check)
        self._chunk_detail_edit = QPlainTextEdit()
        self._chunk_detail_edit.setReadOnly(True)
        self._chunk_detail_edit.setFixedHeight(140)
        self._chunk_detail_edit.setVisible(False)
        result_group_layout.addWidget(self._chunk_detail_edit)
        outer.addWidget(result_group, 1)

        # A true floor (the dialog can be resized smaller and the scroll
        # area absorbs it) plus an explicit initial size that actually
        # fits the content — NOT relying on Qt's sizeHint-on-first-show,
        # since setMinimumSize below would itself pre-empt that (the exact
        # bug this whole restructure fixes; see the scroll-area comment
        # above). resize() after setMinimumSize wins since it's above the
        # floor either way.
        self.setMinimumSize(560, 480)
        self.resize(720, 860)

        self._report_combo.currentIndexChanged.connect(self._on_report_changed)
        self._on_report_changed()

    # ── Helpers ────────────────────────────────────────────────────────

    def _current_script_name(self):
        return self._report_combo.currentData()

    def _rebuild_column_checks(self, all_columns: list, selected: set) -> None:
        while self._columns_layout.count():
            item = self._columns_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._column_checks = []
        for col in all_columns:
            cb = QCheckBox(col)
            cb.setChecked(col in selected)
            self._columns_layout.addWidget(cb)
            self._column_checks.append((cb, col))
        self._columns_layout.addStretch()

    def _collect_selected_columns(self) -> list:
        return [col for cb, col in self._column_checks if cb.isChecked()]

    def _validate_prompt(self) -> bool:
        if "{data}" not in self._prompt_edit.toPlainText():
            self._prompt_error.setText(
                "Prompt must contain a {data} placeholder — nothing would be sent otherwise.")
            return False
        self._prompt_error.setText("")
        return True

    # ── Slots ──────────────────────────────────────────────────────────

    def _on_report_changed(self, *_args) -> None:
        import ai_summary
        import ai_summary_store
        script_name = self._current_script_name()
        if not script_name:
            return
        settings = ai_summary.get_settings(self._case_dir, script_name)
        self._rebuild_column_checks(settings["all_columns"], set(settings["columns"]))
        self._chunk_size_spin.setValue(settings["chunk_size"])
        self._max_gap_spin.setValue(settings["max_gap_minutes"])
        self._prompt_edit.setPlainText(settings["prompt"])
        self._prompt_error.setText("")

        conn = ai_summary_store.get_connection()
        self._endpoint_edit.setText(conn["endpoint"])
        self._api_key_edit.setText(conn["api_key"])
        self._model_combo.clear()
        if conn["model"]:
            self._model_combo.addItem(conn["model"])
        self._model_combo.setCurrentText(conn["model"])

        self._output_edit.clear()
        self._chunk_detail_edit.clear()
        self._log_edit.clear()
        self._status_label.setText(
            "Ready." if settings["all_columns"] else
            "This report has no columns yet — has it been run in this case?")

    def _on_save_settings(self) -> bool:
        import ai_summary_store
        if not self._validate_prompt():
            return False
        columns = self._collect_selected_columns()
        if not columns:
            self._prompt_error.setText("Select at least one column to send.")
            return False
        ai_summary_store.set_report_settings(
            self._current_script_name(),
            columns=columns,
            chunk_size=self._chunk_size_spin.value(),
            max_gap_minutes=self._max_gap_spin.value(),
            prompt=self._prompt_edit.toPlainText(),
        )
        ai_summary_store.set_connection(
            endpoint=self._endpoint_edit.text().strip(),
            api_key=self._api_key_edit.text(),
            model=self._model_combo.currentText().strip(),
        )
        self._status_label.setText("Settings saved.")
        return True

    def _on_refresh_models(self) -> None:
        endpoint = self._endpoint_edit.text().strip()
        if not endpoint:
            self._connection_status.setText("Enter an endpoint first.")
            return
        # *endpoint* is the full chat-completions URL; the models list
        # lives at the API root instead (see local_llm.list_models).
        base = endpoint
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        self._refresh_models_btn.setEnabled(False)
        self._connection_status.setText("Checking…")
        self._model_worker = ModelListWorker(base, self._api_key_edit.text())
        self._model_worker.done.connect(self._on_models_listed)
        self._model_worker.start()

    def _on_models_listed(self, result: dict) -> None:
        self._refresh_models_btn.setEnabled(True)
        if "error" in result:
            self._connection_status.setText(f"Could not reach server: {result['error']}")
            return
        models = result.get("models", [])
        current = self._model_combo.currentText()
        self._model_combo.clear()
        self._model_combo.addItems(models)
        if current and current not in models:
            self._model_combo.addItem(current)
        self._model_combo.setCurrentText(current)
        self._connection_status.setText(
            f"{len(models)} model(s) found." if models else
            "Connected, but no models are currently loaded.")

    def _on_generate(self) -> None:
        if not self._on_save_settings():
            return
        script_name = self._current_script_name()
        self._generating_script_name = script_name
        for w in (self._generate_btn, self._save_btn, self._report_combo, self._close_btn):
            w.setEnabled(False)
        self._output_edit.clear()
        self._chunk_detail_edit.clear()
        self._log_edit.clear()
        self._status_label.setText(
            "Running — one LLM call per time-gap chunk, then a combine pass. "
            "Can take from under a minute to several minutes depending on how "
            "fragmented the data is.")

        self._worker = AISummaryWorker(self._case_dir, script_name, self._platform)
        self._worker.progress.connect(self._log_edit.appendPlainText)
        self._worker.done.connect(self._on_generate_done)
        self._worker.start()

    def _on_generate_done(self, result: dict) -> None:
        for w in (self._generate_btn, self._save_btn, self._report_combo, self._close_btn):
            w.setEnabled(True)

        if "error" in result:
            self._status_label.setText(f"Error: {result['error']}")
            completed = result.get("completed_chunks")
            if completed:
                self._output_edit.setPlainText(
                    "(The final combine step failed — showing the per-chunk "
                    "mini-summaries that did complete, so nothing already "
                    "produced is lost.)\n\n" + self._render_chunks(completed))
            return

        self._output_edit.setPlainText(result["text"])
        self._status_label.setText(
            f"Done — {result['chunk_count']} chunk(s), {result['total_rows']} row(s).")
        self._chunk_detail_edit.setPlainText(self._render_chunks(result.get("chunks", [])))

        import ai_summary
        ai_summary.save_summary(self._case_dir, self._generating_script_name, result)

    @staticmethod
    def _render_chunks(chunks: list) -> str:
        return "\n\n".join(
            f"[{c['rows']} rows"
            + (f", {c['time_range']}" if c.get("time_range") else "")
            + f"]\n{c['text']}"
            for c in chunks
        )

    def _on_toggle_chunk_detail(self, checked: bool) -> None:
        self._chunk_detail_edit.setVisible(checked)

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

        # Top row: row-count label far LEFT, Columns controls far RIGHT —
        # same left/right split as ffs-explorer.py's own File Browser
        # sel_bar (table_status_label ... stretch ... columns_btn), its
        # own row ABOVE the filter row (not beside it). "All"/"Core" sit
        # as their own quick-access buttons next to "Columns…" so the
        # examiner can jump straight to either without opening the
        # dialog — "Columns…" itself still opens the full dialog (see
        # _show_art_columns_dialog) for individual toggles/reordering/
        # "None". Not Apps-only — meaningful for every real per-parser
        # report — so all three start hidden and _setup_report_filter_ui
        # shows/hides them per call depending on whether a real
        # script_name was given (Exported Files/Apps pass none and get
        # them hidden, same pattern Category/Date use, just inverted). A
        # parser opts "Core" in (both here and in the dialog) via a
        # module-level core_fields list (see WRITING_ARTIFACT_PARSERS.md)
        # — absent for a report that declares none, and it's also this
        # project's chosen DEFAULT view for a report the examiner has
        # never customized (see _setup_report_filter_ui) — a report
        # showing every field a parser happens to produce by default is
        # exactly the overwhelm this whole feature exists to avoid.
        self._art_columns_script_name: str | None = None
        self._art_columns_all: list = []
        self._art_columns_core: list = []
        self._art_row_label = QLabel()
        report_columns_row = QHBoxLayout()
        report_columns_row.addWidget(self._art_row_label)
        report_columns_row.addStretch()
        self._art_columns_all_btn = QPushButton("All")
        self._art_columns_all_btn.clicked.connect(
            lambda: self._set_art_visible_columns(self._art_columns_all))
        self._art_columns_all_btn.setVisible(False)
        report_columns_row.addWidget(self._art_columns_all_btn)
        self._art_columns_core_btn = QPushButton("Core")
        self._art_columns_core_btn.clicked.connect(
            lambda: self._set_art_visible_columns(self._art_columns_core))
        self._art_columns_core_btn.setVisible(False)
        report_columns_row.addWidget(self._art_columns_core_btn)
        self._art_columns_filter_btn = QPushButton("Columns…")
        self._art_columns_filter_btn.setToolTip(
            "Show, hide, or reorder this report's columns.")
        self._art_columns_filter_btn.clicked.connect(self._show_art_columns_dialog)
        self._art_columns_filter_btn.setVisible(False)
        report_columns_row.addWidget(self._art_columns_filter_btn)
        report_layout.addLayout(report_columns_row)

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
        report_filter_row.addWidget(QLabel("Filter:"))
        report_filter_row.addWidget(self._art_filter_input, 1)
        report_filter_row.addWidget(self._art_filter_col)
        report_filter_row.addWidget(self._art_filter_btn)
        # Only meaningful for a report declaring recoverable_tables (see
        # _setup_report_filter_ui, which shows/hides and resets this per
        # load) -- filters OUT rows whose own source label carries the
        # confidence-gate's "(likely false positive" suffix (see
        # sqlite_carve.py's notnull_violations/timestamp_issues). Checked
        # by default per direct instruction ("no value in having them
        # reported"), but this is a FILTER, not a delete: total_rows
        # still counts them (see _apply_art_filter/_art_row_label), so
        # unchecking it always brings them straight back -- nothing is
        # ever silently gone the way an actual row deletion would be,
        # which this project's standing rule treats as a real problem for
        # a forensic tool (see CLAUDE.md's app_intelligence.py escalation
        # precedent).
        self._art_hide_lowconf_checkbox = QCheckBox("Hide likely false positives")
        self._art_hide_lowconf_checkbox.setChecked(True)
        self._art_hide_lowconf_checkbox.toggled.connect(self._apply_art_filter)
        self._art_hide_lowconf_checkbox.setVisible(False)
        report_filter_row.addWidget(self._art_hide_lowconf_checkbox)

        # Apps-table-only filters (added 2026-08-26, same widget pattern as
        # ffs-explorer.py's own File Browser Date/Type filters — see
        # _populate_apps_table/_art_show_report for the show/hide toggle
        # that keeps these hidden for every other report). Category: a
        # checklist popup menu over the existing Category column's distinct
        # values (including a toggleable '(blank)' entry), not the free-text
        # box above, since it's a small fixed set of values, not a search
        # term — same UX shape as the File Browser's own Type filter menu.
        self._art_category_filter_selected: set | None = None   # None = all
        self._art_category_filter_btn = QPushButton("Category: All")
        self._art_category_menu = QMenu(self._art_category_filter_btn)
        self._art_category_menu.aboutToShow.connect(self._populate_art_category_menu)
        self._art_category_filter_btn.setMenu(self._art_category_menu)
        report_filter_row.addWidget(self._art_category_filter_btn)

        # Date: pick which of the Apps table's own timestamp columns, then
        # a From/To range — identical shape to ffs-explorer.py's
        # filter_date_enable/_combo/_from/_to, just a separate instance
        # since the Apps table is a different QTableView/model entirely.
        self._art_date_filter_enable = QCheckBox("Date")
        self._art_date_filter_enable.toggled.connect(self._on_art_date_filter_toggled)
        report_filter_row.addWidget(self._art_date_filter_enable)
        self._art_date_filter_combo = QComboBox()
        self._art_date_filter_combo.addItems(self._APPS_DATE_COLUMNS)
        self._art_date_filter_combo.currentIndexChanged.connect(
            lambda _=None: self._apply_art_filter_if_date_enabled())
        report_filter_row.addWidget(self._art_date_filter_combo)
        self._art_date_filter_from = QDateEdit(QDate(2000, 1, 1))
        self._art_date_filter_to   = QDateEdit(QDate.currentDate())
        for de in (self._art_date_filter_from, self._art_date_filter_to):
            de.setDisplayFormat("yyyy-MM-dd")
            de.setCalendarPopup(True)
            de.dateChanged.connect(lambda _=None: self._apply_art_filter_if_date_enabled())
        report_filter_row.addWidget(self._art_date_filter_from)
        report_filter_row.addWidget(QLabel("–"))
        report_filter_row.addWidget(self._art_date_filter_to)
        self._set_art_date_filter_enabled(False)
        for w in (self._art_category_filter_btn, self._art_date_filter_enable,
                 self._art_date_filter_combo, self._art_date_filter_from,
                 self._art_date_filter_to):
            w.setVisible(False)   # shown only in _populate_apps_table

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
        self._art_media_delegate = MediaThumbnailDelegate(self._art_report_view)
        self._art_media_thumb_worker = None
        self._art_apps_worker = None
        self._art_showing_apps = False
        self._art_hex_active = False
        self._art_current_mod = None
        self._art_current_script: str | None = None
        self._art_current_platform: str | None = None
        self._art_current_record_sources: list = []
        # {source_value: entry_label} -- the examiner's last manually-picked
        # record_source entry for a given row `source` (e.g. "Chrome
        # History"), sticky across every OTHER row sharing that same
        # source until the report itself reloads (_art_show_report /
        # _refresh_artifact_tab reset this) -- see _art_load_record_hex.
        self._art_record_source_sticky: dict = {}
        self._art_update_worker = None
        self._art_report_view.doubleClicked.connect(self._on_art_report_double_clicked)
        self._art_report_view.selectionModel().currentRowChanged.connect(
            self._on_art_report_row_selected)

        # Parser-version banner (see parser_versions.py / Conventions):
        # non-blocking notice, shown only when the current report was
        # produced by an older version of its parser than what's on disk
        # now. Hidden by default — _art_show_report toggles it per report.
        self._art_version_banner = QWidget()
        self._art_version_banner.setStyleSheet(
            f"background: #fdf3d9; border: 1px solid {WARNING_COLOR}; "
            "border-radius: 4px;")
        _vb_layout = QHBoxLayout(self._art_version_banner)
        _vb_layout.setContentsMargins(10, 6, 10, 6)
        self._art_version_banner_label = QLabel()
        self._art_version_banner_label.setWordWrap(True)
        self._art_version_banner_label.setStyleSheet(f"color: {WARNING_COLOR};")
        self._art_version_update_btn = QPushButton("Update")
        self._art_version_update_btn.setFixedWidth(80)
        self._art_version_update_btn.clicked.connect(self._on_art_update_parser_version)
        _vb_layout.addWidget(self._art_version_banner_label, 1)
        _vb_layout.addWidget(self._art_version_update_btn)
        self._art_version_banner.setVisible(False)
        report_layout.addWidget(self._art_version_banner)

        report_layout.addWidget(self._art_report_view)

        # Script page — read-only monospace text editor
        self._art_script_view = QPlainTextEdit()
        self._art_script_view.setReadOnly(True)
        self._art_script_view.setFont(QFont("Courier", 11))

        # Notes/Warning page — a script's own `description` (where this data
        # comes from, and any known reliability caveats), moved out of the
        # report view (it was pushing the table down, sometimes off-screen,
        # for scripts with long descriptions) into its own tree entry.
        # `warning` (optional, module-level, separate from `description`) is
        # styled red when a module sets one — reserved for something the
        # examiner must read before trusting the report, not routine notes.
        self._art_notes_label = QLabel()
        self._art_notes_label.setWordWrap(True)
        self._art_notes_label.setTextFormat(Qt.TextFormat.RichText)
        self._art_notes_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._art_notes_label.setStyleSheet(
            "background: palette(base); border: 1px solid palette(mid); "
            "border-radius: 4px; padding: 10px 12px;")
        _notes_scroll = QScrollArea()
        _notes_scroll.setWidgetResizable(True)
        _notes_scroll.setWidget(self._art_notes_label)
        _notes_scroll.setStyleSheet("QScrollArea { border: none; }")

        # Validation page — diffs this parser's current schema/folder
        # structure against a recorded GTD baseline (see
        # validation_store.py / parser_validation.py), or offers to record
        # one. Plain monospace text (like the Script page) rather than rich
        # text: this is a structured diff report, not prose.
        validation_page = QWidget()
        validation_layout = QVBoxLayout(validation_page)
        validation_layout.setContentsMargins(4, 4, 4, 4)
        self._art_validation_record_btn = QPushButton("Record This Case as Validation Baseline")
        self._art_validation_record_btn.clicked.connect(self._on_art_record_validation_baseline)
        validation_layout.addWidget(self._art_validation_record_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._art_validation_view = QPlainTextEdit()
        self._art_validation_view.setReadOnly(True)
        self._art_validation_view.setFont(QFont("Courier", 11))
        validation_layout.addWidget(self._art_validation_view)
        self._art_validation_script_name = None

        # AI Summary group-root page — an app_group's designated overview
        # member can opt in to showing its persisted AI Summary here
        # (rendered as markdown/rich text) instead of a raw stats report;
        # see artifacts/android/chrome_web_history.py's group_overview_mode
        # and _art_show_ai_summary_panel below. Read-only display only —
        # nothing on this page ever calls the LLM itself, that only
        # happens through the AI Summary dialog (_open_ai_summary_dialog)
        # or the MCP run_ai_summary tool, both of which persist their
        # result the same way (ai_summary.save_summary) so either surface
        # generating a summary makes it show up here.
        ai_summary_page = QWidget()
        ai_summary_page_layout = QVBoxLayout(ai_summary_page)
        ai_summary_page_layout.setContentsMargins(4, 4, 4, 4)
        self._art_ai_summary_label = note_label("")
        ai_summary_page_layout.addWidget(self._art_ai_summary_label)
        self._art_ai_summary_view = QTextBrowser()
        self._art_ai_summary_view.setOpenExternalLinks(False)
        ai_summary_page_layout.addWidget(self._art_ai_summary_view)

        self._art_stack = QStackedWidget()
        self._art_stack.addWidget(self._art_placeholder)  # 0
        self._art_stack.addWidget(report_page)             # 1
        self._art_stack.addWidget(self._art_script_view)  # 2
        self._art_stack.addWidget(_notes_scroll)           # 3
        self._art_stack.addWidget(validation_page)          # 4
        self._art_stack.addWidget(ai_summary_page)          # 5

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._art_tree_view)
        splitter.addWidget(self._art_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        tab = QWidget()
        QHBoxLayout(tab).addWidget(splitter)
        tab.layout().setContentsMargins(4, 4, 4, 4)
        return tab

    def _finish_artifact_hex_wiring(self) -> None:
        """Connect the shared hex panel's Record/Attachment toggle and the
        joined-record source selector (HexViewerMixin._setup_hex_panel) to
        the Artifact Viewer. Must run AFTER _setup_hex_panel exists —
        ffs-explorer.py builds the Artifact Viewer tab before the hex
        panel, so this can't be done inline in _setup_artifact_tab."""
        self._hex_source_btn_group.buttonClicked.connect(self._on_art_hex_source_toggled)
        self._hex_record_source_combo.currentIndexChanged.connect(self._on_art_hex_source_toggled)

    def _on_art_hex_source_toggled(self, *_args) -> None:
        """Re-load hex for whatever row is currently selected under the
        newly-chosen mode/source — so flipping Record/Attachment, or
        picking a different joined-record source, updates the panel
        immediately instead of waiting for the next row click. Takes
        *_args so it can be connected to either buttonClicked (passes the
        button) or currentIndexChanged (passes an int) without a wrapper
        per signal."""
        idx = self._art_report_view.currentIndex()
        if idx.isValid():
            self._on_art_report_row_selected(idx, idx)
        else:
            # No row selected — including a fresh switch into this tab
            # when no report has ever been opened here yet (see "Per-tab
            # state on switching" in CLAUDE.md). Clear directly (not via
            # _clear_art_hex, whose _art_hex_active guard would wrongly
            # no-op here if the panel is currently showing some OTHER
            # tab's content) rather than leaving that content showing
            # under this one.
            self._clear_hex_preview()
            self._art_hex_active = False

    def _refresh_artifact_tab(self):
        """Rebuild the artifact tree from completed parsers in the case DB."""
        if not hasattr(self, '_art_tree_model'):
            return
        from artifact_db import list_completed_artifacts
        from artifact_runner import list_artifacts

        self._cancel_art_filter_worker()
        self._retire_art_media_worker()
        self._retire_art_webpage_worker()
        self._retire_art_apps_worker()
        dialog = getattr(self, '_art_media_dialog', None)
        if dialog is not None:
            dialog.close()   # _on_art_media_dialog_closed clears the reference
        self._clear_art_hex()
        self._art_current_mod = None
        self._art_current_record_sources = []
        self._art_record_source_sticky = {}
        self._hex_record_source_combo.setVisible(False)
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

        platform = 'android' if self._is_android_archive() else 'ios'
        modules  = {sn: mod for sn, mod in list_artifacts(platform)}

        def _item(text, role_val=None):
            it = QStandardItem(text)
            it.setEditable(False)
            it.setCheckable(False)
            if role_val is not None:
                it.setData(role_val, Qt.ItemDataRole.UserRole)
            return it

        apps_item = _item("Apps", _ART_APPS)
        apps_item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
        # The Apps table's own notes — distinct from a per-parser group's
        # "Report Notes/Warning" child below, since the Apps table isn't
        # backed by any one parser's module/description. First child, not
        # appended after the per-app groups, so it reads before the list of
        # apps rather than after (see _art_show_app_notes for content).
        apps_item.appendRow(_item("Application Report Notes", _ART_APP_NOTES))

        if not completed:
            # The Apps node's own table (app_intelligence) doesn't depend
            # on any parser having run — still worth showing/clicking —
            # this is just a note that no per-app report groups exist yet.
            no_parsers = _item("No parsers run yet")
            no_parsers.setEnabled(False)
            apps_item.appendRow(no_parsers)

        def _build_report_item(script_name):
            mod   = modules.get(script_name)
            label = getattr(mod, 'name', script_name) if mod else script_name
            group = _item(label, _ART_GROUP + script_name)
            group.setFont(QFont("Arial", weight=QFont.Weight.Bold))
            # No separate "Report" child — clicking the app-name group item
            # itself shows the report now (see _on_art_tree_clicked). The
            # rest stay as children, collapsed by default (see expand()
            # call below) rather than always sprawled open.
            group.appendRow(_item("Report Notes/Warning", _ART_NOTES + script_name))
            group.appendRow(_item("Script",         _ART_SCRIPT + script_name))
            group.appendRow(_item("Source in ZIP",  _ART_SOURCE + script_name))
            group.appendRow(_item("Exported Files", _ART_FILES  + script_name))
            group.appendRow(_item("Validation",     _ART_VALIDATION + script_name))
            return group

        # A parser opts into a shared parent tree node (instead of sitting
        # flat under Apps like every other report) by declaring a module-
        # level `app_group_label` string — reports sharing the same label
        # nest under one bold node named after it. Optional per-member
        # `is_group_overview = True` picks which single report the PARENT
        # node itself shows when clicked (see _ART_APP_GROUP dispatch);
        # optional `group_sort_key` (int, default 100) orders members
        # within the group — the overview member always sorts first
        # regardless, since it's the one thing worth seeing before anything
        # else in that family. A label with no member run yet never
        # appears — this only groups reports that actually completed.
        app_groups: dict[str, list[str]] = {}
        standalone: list[str] = []
        for script_name in completed:
            mod = modules.get(script_name)
            label = getattr(mod, 'app_group_label', None) if mod else None
            if label:
                app_groups.setdefault(label, []).append(script_name)
            else:
                standalone.append(script_name)

        for script_name in standalone:
            apps_item.appendRow(_build_report_item(script_name))

        for app_label in sorted(app_groups):
            members = app_groups[app_label]
            overview_script = next(
                (sn for sn in members if getattr(modules.get(sn), 'is_group_overview', False)),
                None
            )
            members.sort(key=lambda sn: (
                0 if sn == overview_script else 1,
                getattr(modules.get(sn), 'group_sort_key', 100),
            ))
            app_item = _item(app_label, _ART_APP_GROUP + (overview_script or members[0]))
            app_item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
            for script_name in members:
                app_item.appendRow(_build_report_item(script_name))
            apps_item.appendRow(app_item)

        self._art_tree_model.invisibleRootItem().appendRow(apps_item)

        # A second, always-present top-level sibling — clicking it opens
        # AISummaryDialog directly (see _on_art_tree_clicked) rather than
        # showing a page of its own in _art_stack, so it needs no report-
        # notes/script/source/etc. children the way a per-parser group
        # does. Always shown, even with nothing parsed yet, same as Apps —
        # the dialog itself explains when there's nothing to summarize yet.
        ai_summary_item = _item("AI Summary", _ART_AI_SUMMARY)
        ai_summary_item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
        self._art_tree_model.invisibleRootItem().appendRow(ai_summary_item)

        # Only the Apps node itself starts expanded — its per-app children
        # (Notes/Warning, Script, Source, Exported Files, Validation) stay
        # collapsed until the examiner deliberately opens one, so the tree
        # reads as "Apps -> the ones with a report" at a glance.
        self._art_tree_view.expand(self._art_tree_model.indexFromItem(apps_item))
        # Deliberately NOT auto-selecting/showing anything here, even
        # though this method also runs after a parser finishes running
        # (parsers_completed -> _refresh_artifact_tab) — forcing a switch
        # on every rebuild would discard whatever report the examiner had
        # open mid-session, exactly the bug this project already fixed
        # once (see the Artifact Viewer per-tab-state note in CLAUDE.md's
        # Conventions section: _on_center_tab_changed used to call this
        # unconditionally and it "discarded whichever report was open").
        # "If nothing is selected, default to Apps" only has a real
        # instance at first case-load, before anything has ever been
        # picked — that specific moment is handled by the CALLER
        # (ffs-explorer.py's archive-open completion handler), which
        # explicitly selects/shows Apps right after this returns, the same
        # pattern _art_update_parser_version already uses to restore ITS
        # own specific report afterward rather than this method guessing.

    def _art_select_and_show_apps(self) -> None:
        """Select and show the Apps node — the "nothing selected yet"
        default for a genuinely fresh tree (first case load). Call right
        after _refresh_artifact_tab() has just rebuilt the tree; a no-op
        if the tree ended up empty (no case dir, DB error — see that
        method). Apps is always the FIRST top-level item when present —
        AI Summary is a second sibling added right after it, but child(0)
        is always Apps regardless — so no role-value search is needed to
        find it."""
        root = self._art_tree_model.invisibleRootItem()
        if root.rowCount() == 0:
            return
        apps_item = root.child(0)
        self._art_tree_view.setCurrentIndex(self._art_tree_model.indexFromItem(apps_item))
        self._art_show_apps()

    def _clear_art_hex(self) -> None:
        """Wipe the shared bottom Hex panel and mark it as no longer showing
        Artifact-viewer content — called on any navigation away from the
        row/report that populated it (a different report, a different tree
        node, or leaving the Artifact Viewer tab entirely) so stale
        attachment bytes never linger past the selection that loaded them."""
        if not self._art_hex_active:
            return
        self._clear_hex_preview()
        self._art_hex_active = False

    def _show_art_hex_message(self, message: str) -> None:
        """Explanatory placeholder in the Hex panel (e.g. "Record location
        not available for this parser yet") instead of stale bytes or the
        generic default text — still counts as Artifact-viewer-owned
        content for _clear_art_hex's purposes, so leaving the tab/report
        resets it the same as real hex content would."""
        self._clear_hex_preview(message)
        self._art_hex_active = True

    def _on_art_report_row_selected(self, current, previous):
        """Single-click / keyboard row selection: loads hex for the row
        per the panel's Record/Attachment toggle (HexViewerMixin), and
        keeps doing so as the user moves between rows — the toggle is the
        sticky state, not the row, so scrolling through rows in either
        mode keeps showing that same mode for each new row.

        `_on_art_hex_source_toggled` re-invokes this same method with
        current==previous (same index passed twice) when the examiner
        flips Record/Attachment or changes the joined-record combo for the
        ALREADY-selected row — as opposed to a genuine new-row selection,
        where previous is either invalid (nothing selected yet) or a
        different row. `_art_load_record_hex` needs that distinction: a
        new row always resets to ITS OWN main table (record_source's
        first entry for that row's own query), while a same-row re-trigger
        must leave whatever the examiner just picked in the combo alone —
        see reset_source_combo there."""
        if not current.isValid():
            self._clear_art_hex()
            return
        row = self._art_table_model.row_dict(current.row())
        if self._hex_source_is_record():
            is_new_row = not previous.isValid() or previous.row() != current.row()
            self._art_load_record_hex(row, reset_source_combo=is_new_row)
        else:
            self._art_load_attachment_hex(row)
        self._art_sync_open_media_dialog(row)

    def _art_resolve_row_attachment(self, row: dict):
        """(ui_path, data) for *row*'s first non-empty media_fields value,
        or ('', None) if the row has none / it couldn't be read. Shared by
        _art_load_attachment_hex (hex-panel Attachment mode) and
        _art_sync_open_media_dialog (open-viewer-follows-selection) so
        there's exactly one definition of "this row's own attachment"."""
        media_fields = getattr(self._art_current_mod, 'media_fields', ()) \
            if self._art_current_mod else ()
        ui_path = ''
        for field in media_fields:
            val = row.get(field)
            if val:
                ui_path = val
                break
        if not ui_path:
            return '', None
        return ui_path, self._read_zip_bytes(ui_path)

    def _art_load_attachment_hex(self, row: dict) -> None:
        """Attachment hex mode: the row's first non-empty media_fields
        value, same resolution _on_art_report_double_clicked uses for the
        specific cell clicked."""
        ui_path, data = self._art_resolve_row_attachment(row)
        if not ui_path:
            self._show_art_hex_message("No attachment on this row")
            return
        if data is None:
            self._show_art_hex_message(f"Attachment not found in archive: {ui_path}")
            return
        self._load_hex_preview_from_bytes(data, ui_path)
        self._art_hex_active = True
        self.status_bar.showMessage(ui_path)

    def _art_sync_open_media_dialog(self, row: dict) -> None:
        """If a MediaFullViewDialog is currently open (non-modal — see
        _on_art_report_double_clicked), keep it following row selection:
        swap its content to the newly-selected row's own attachment
        instead of leaving it showing the previous row's. Silently does
        nothing if no dialog is open, or this row has no attachment to
        show — an examiner paging through rows that mix "has an
        attachment" and "doesn't" shouldn't have the open viewer close
        or error out on the gaps, just stay on its last real content."""
        dialog = getattr(self, '_art_media_dialog', None)
        if dialog is None or not dialog.isVisible():
            return
        ui_path, data = self._art_resolve_row_attachment(row)
        if not ui_path or data is None:
            return
        dialog.load_content(ui_path, data)

    def _art_record_sources_for_row(self, row: dict) -> list:
        """Narrow the parser's full record_source declaration down to just
        the entries relevant to the QUERY that actually produced this row
        — for a report that merges more than one live query into one
        table (e.g. chrome_web_history's Chrome History rows vs.
        Segmentation Platform rows), each query has its own main table and
        its own join(s), and offering every parser-wide entry regardless
        of which query built the selected row would let an examiner pick
        a join that structurally can't apply to it (a Segmentation
        Platform row has no `visits` counterpart to jump to at all).

        An entry with no `source_match` applies to every row — the
        original, still-common single-query-parser case, unchanged.  An
        entry WITH `source_match` (a list of `source` column values)
        applies only when this row's own `source` field is one of them.
        Order is preserved from the module's own declaration, so the
        parser controls which entry is "the row's own main table" (first)
        versus a joined table reachable via the dropdown (after it) —
        see chrome_web_history.py for the worked two-query example.

        A second, independent narrowing: an entry whose presence check is
        empty/None on this row is dropped too — a LEFT JOIN that found
        nothing for this particular row (WhatsApp's Location entry on a
        plain text message, say, or Chat Mapped JID on a chat with no
        LID-privacy identity) has nothing to jump to, so listing it would
        only offer a dead end. This is genuinely per-ROW, not just
        per-query: two rows from the exact same query/source can still
        differ here depending on which of that query's own LEFT JOINs
        actually matched for each one. The check itself is
        `presence_fields` (a list) if the entry declares one, else
        `rowid_fields` — a SEPARATE declaration is needed for an entry
        whose rowid is a value BORROWED from a different, always-present
        column rather than one specific to its own join (WhatsApp's Media/
        Location: their own rowid is provably identical to the row's own
        `message_id`, reused rather than re-selected — see
        artifacts/android/whatsapp.py — so `message_id` alone can't say
        whether THEIR join matched, since it's populated whether or not
        it did; `presence_fields: ["media_path"]` / `["latitude"]` check
        the actual joined value instead)."""
        entries = self._art_current_record_sources
        row_source = row.get('source')
        scoped = [e for e in entries
                 if 'source_match' not in e or row_source in e['source_match']]
        def _resolved(e):
            fields = e.get('presence_fields') or e.get('rowid_fields', ())
            return any(row.get(f) not in (None, '') for f in fields)
        return [e for e in scoped if _resolved(e)]

    def _art_load_record_hex(self, row: dict, reset_source_combo: bool = True) -> None:
        """Record hex mode: jump to this row's own on-disk database cell
        in its source db file — see the parser module's `record_source`
        declaration (a list of entries, one per DB row a joined report's
        own rows are actually built from) and sqlite_carve.locate_live_row.
        Never guesses: a missing declaration, a row with no rowid/table
        data, an unresolvable source file, or a rowid not found in the
        CURRENT live b-tree (may be WAL-only and not yet checkpointed, or
        genuinely deleted — recover_deleted_rows' job, not this one's)
        shows a specific explanatory message rather than silently doing
        nothing or showing the wrong bytes.

        *reset_source_combo* is False only when this is the SAME row as
        last time and the examiner just changed the toggle/combo
        themselves (see _on_art_report_row_selected) — a genuine new-row
        selection (the default) always repopulates the combo to that
        row's own query-scoped entries and resets to the first one (the
        row's own main table), per direct design instruction: select a
        row, see its own row by default; deliberately pick a join from
        the dropdown, rather than staying on whichever join a previous,
        possibly differently-sourced row happened to have selected."""
        mod     = self._art_current_mod
        entries = self._art_current_record_sources
        if not entries:
            self._hex_record_source_combo.setVisible(False)
            self._show_art_hex_message("Record location not available for this parser yet")
            return

        from artifact_runner import resolve_module_file_ui_path

        # A recovered/carved row already has its exact on-disk location
        # from the carving pass itself (sqlite_carve.recover_deleted_rows
        # — raw_offset/raw_length/raw_file) — no live b-tree search needed,
        # and none would find it anyway: a carved row is by definition not
        # in the live b-tree locate_live_row walks. This is also, per
        # direct instruction, deliberately NOT part of the per-row
        # query-scoping below — a carved row's own location never depends
        # on which joined table the examiner has picked, only on where the
        # carving pass actually found it.
        if row.get('recovered') and row.get('raw_offset') is not None:
            if reset_source_combo:
                # A carved row has no join to pick — its own location
                # never depends on the combo (see above) — but the combo
                # widget itself still needs to say something CURRENT for
                # this row rather than silently keep showing whichever
                # live row's entries were last populated here, which is
                # stale content masquerading as still-current state.
                self._hex_record_source_combo.blockSignals(True)
                self._hex_record_source_combo.clear()
                # Bare label, no "N of M" prefix -- same convention a
                # single-entry LIVE row (e.g. a UKM row) already follows:
                # the prefix exists to signal a choice needs making, and a
                # carved row has none, same as a single-entry live one.
                self._hex_record_source_combo.addItem("Carved Record", None)
                self._hex_record_source_combo.setCurrentIndex(0)
                self._hex_record_source_combo.blockSignals(False)
                self._hex_record_source_combo.setVisible(True)
            source_table = row.get('source_table')
            # More than one declared entry can legitimately share a bare
            # table name (chrome_web_history's own "urls" vs ukm_db's
            # "urls" — the exact collision recoverable_tables' own
            # file_key pinning already exists to resolve at CARVE time;
            # source_file_key carries that same disambiguator through to
            # here, since matching by table name alone would silently
            # pick whichever same-named entry happens to be declared
            # first). Narrow by table name first, then by file_key only if
            # that left more than one candidate and there's a file_key on
            # the row to disambiguate with — a single match never needs
            # narrowing, so this changes nothing for the common case where
            # no other record_source entry shares that table name.
            table_matches = [e for e in entries if
                             (e['table'] if 'table' in e else row.get(e.get('table_field', ''))) == source_table]
            source_file_key = row.get('source_file_key')
            if len(table_matches) > 1 and source_file_key:
                table_matches = [e for e in table_matches
                                 if e.get('file_key') == source_file_key] or table_matches
            rs = table_matches[0] if table_matches else entries[0]
            is_wal = row.get('raw_file') == 'wal'
            file_key = f"{rs['file_key']}_wal" if is_wal else rs['file_key']
            ui_path = resolve_module_file_ui_path(
                mod, file_key, self.guid_to_bundle,
                adapter=self._adapter, zip_names=self.zip_names)
            if not ui_path:
                where = "WAL sidecar" if is_wal else "source database"
                self._show_art_hex_message(f"Could not resolve the {where} path for this parser")
                return
            data = self._read_zip_bytes(ui_path)
            if data is None:
                self._show_art_hex_message(f"Not found in archive: {ui_path}")
                return
            # row came from ArtifactTableModel.row_dict(), which for DB-mode
            # (the normal case — a report read back from caseresults.db
            # after the parser ran) hands back every column as a plain str:
            # artifact_db.write_artifact_results stores the whole table as
            # TEXT (see its own docstring). raw_offset/raw_length are ints
            # at the moment sqlite_carve produces them, but by the time a
            # click reaches here they're "683", not 683 — passed unconverted
            # into _load_hex_preview_from_bytes_at's own offset arithmetic
            # (`offset - 10`, `max(length, 0)`), this raised an uncaught
            # TypeError on every recovered row, carved or WAL alike (both
            # go through this exact branch) — silently, since nothing here
            # catches it: the row selection signal handler just stopped
            # partway through with the hex panel never updating. Confirmed
            # directly, not assumed: `("683" - 10)` reproduces the same
            # TypeError this code path was hitting.
            try:
                offset = int(row['raw_offset'])
            except (TypeError, ValueError):
                self._show_art_hex_message("No record-location data on this row")
                return
            try:
                length = int(row.get('raw_length') or 0)
            except (TypeError, ValueError):
                length = 0
            self._load_hex_preview_from_bytes_at(data, ui_path, offset, length)
            self._art_hex_active = True
            self.status_bar.showMessage(
                f"{ui_path}  —  offset: {offset:,}  (recovered: {row.get('recovery_method', '?')})")
            return

        # Live row: only the entries relevant to the query that actually
        # built THIS row (see _art_record_sources_for_row) — never the
        # parser's full, unscoped list, which may include join(s) that
        # structurally belong to a DIFFERENT query this row never went
        # through at all.
        scoped = self._art_record_sources_for_row(row)
        if not scoped:
            self._show_art_hex_message("Record location not available for this row's source")
            return

        if reset_source_combo:
            self._hex_record_source_combo.blockSignals(True)
            self._hex_record_source_combo.clear()
            # "N of M - label" only when there's actually more than one
            # entry to navigate — a bare label the rest of the time (an
            # always-"1 of 1" prefix would be noise, not clarity). Short
            # and consistent on purpose, per direct instruction: the
            # number alone is what tells the examiner a second table
            # exists at all and needs picking to see the full record —
            # the label after it just says which one this entry is.
            multi = len(scoped) > 1
            for i, entry in enumerate(scoped):
                # "label" was only ever REQUIRED with >1 entry (see
                # artifact_runner.py's docstring) -- back when a single-
                # entry combo stayed hidden, an omitted label was
                # invisible and harmless. Now that the combo is always
                # shown (see above), every OTHER existing parser's
                # single, label-less entry would otherwise surface as a
                # bare "?" -- fall back to the entry's own table name
                # (same resolution the actual jump uses further down:
                # entry['table'] if it never varies, else the row's own
                # value for table_field) instead, which is always at
                # least as informative as the declared label would be.
                label = entry.get('label') or (
                    entry['table'] if 'table' in entry
                    else row.get(entry.get('table_field', ''))) or '?'
                if multi:
                    label = f"{i + 1} of {len(scoped)} - {label}"
                self._hex_record_source_combo.addItem(label, entry)
            # A NEW row still starts on this source's own remembered pick —
            # not always scoped[0] — if the examiner already chose a
            # different entry for an EARLIER row of this same source
            # (row.get('source')) since the report was last (re)loaded.
            # _art_record_source_sticky is cleared on report reload only
            # (_art_show_report/_refresh_artifact_tab), never per-row, so a
            # deliberate join choice survives scrolling through the rest of
            # that source's rows instead of resetting on every single one.
            # Matched against the entry's own bare label (rs.get('label')),
            # never the "N of M - " display text above, which is purely
            # cosmetic and would break this match if compared directly.
            sticky_label = self._art_record_source_sticky.get(row.get('source'))
            default_idx = next(
                (i for i, e in enumerate(scoped) if e.get('label') == sticky_label), 0)
            self._hex_record_source_combo.setCurrentIndex(default_idx)
            self._hex_record_source_combo.blockSignals(False)
            # Always visible once there's at least one entry to show (even
            # just one) -- per direct instruction, so the panel's layout
            # doesn't reflow between a 1-entry row and a 2-entry row as the
            # examiner scrolls through a mixed-source report like this one.
            self._hex_record_source_combo.setVisible(True)

        if len(scoped) == 1:
            rs = scoped[0]
        else:
            rs = self._hex_record_source_combo.currentData() or scoped[0]
            # Record this as the standing choice for every OTHER row of
            # the same source too — whether it's a fresh re-application of
            # an earlier sticky pick (reset_source_combo True, harmless
            # no-op rewrite) or the examiner just changed the combo for
            # THIS row (reset_source_combo False, the actual new choice).
            self._art_record_source_sticky[row.get('source')] = rs.get('label')
        table = rs['table'] if 'table' in rs else row.get(rs.get('table_field', 'source_table'))
        rowid = None
        for field in rs.get('rowid_fields', ()):
            val = row.get(field)
            if val is not None:
                rowid = val
                break
        if not table or rowid is None:
            self._show_art_hex_message("No record-location data on this row")
            return
        try:
            rowid_int = int(rowid)
        except (TypeError, ValueError):
            self._show_art_hex_message("No record-location data on this row")
            return
        ui_path = resolve_module_file_ui_path(
            mod, rs['file_key'], self.guid_to_bundle,
            adapter=self._adapter, zip_names=self.zip_names)
        if not ui_path:
            self._show_art_hex_message("Could not resolve the source database path for this parser")
            return
        data = self._read_zip_bytes(ui_path)
        if data is None:
            self._show_art_hex_message(f"Source database not found in archive: {ui_path}")
            return
        import sqlite_carve
        loc = sqlite_carve.locate_live_row(data, str(table), rowid_int)
        if loc is None:
            self._show_art_hex_message(
                f"Record not found at its expected location in "
                f"{os.path.basename(ui_path)} (may be WAL-only, or deleted)")
            return
        self._load_hex_preview_from_bytes_at(data, ui_path, loc['abs_offset'], loc['length'])
        self._art_hex_active = True
        self.status_bar.showMessage(f"{ui_path}  —  offset: {loc['abs_offset']:,}")

    def _on_art_tree_clicked(self, index):
        self._clear_art_hex()
        role_val = index.data(Qt.ItemDataRole.UserRole)
        if not role_val:
            return
        if role_val == _ART_APPS:
            self._art_show_apps()
        elif role_val == _ART_AI_SUMMARY:
            self._open_ai_summary_dialog()
        elif role_val == _ART_APP_NOTES:
            self._art_show_app_notes()
        elif role_val.startswith(_ART_GROUP):
            self._art_show_report(role_val[len(_ART_GROUP):])
        elif role_val.startswith(_ART_APP_GROUP):
            overview_script = role_val[len(_ART_APP_GROUP):]
            from artifact_runner import list_artifacts
            platform = 'android' if self._is_android_archive() else 'ios'
            mod = {sn: m for sn, m in list_artifacts(platform)}.get(overview_script)
            if getattr(mod, 'group_overview_mode', None) == 'ai_summary':
                self._art_show_ai_summary_panel(overview_script)
            else:
                self._art_show_report(overview_script)
        elif role_val.startswith(_ART_NOTES):
            self._art_show_notes(role_val[len(_ART_NOTES):])
        elif role_val.startswith(_ART_SCRIPT):
            self._art_show_script(role_val[len(_ART_SCRIPT):])
        elif role_val.startswith(_ART_SOURCE):
            self._art_goto_source(role_val[len(_ART_SOURCE):])
        elif role_val.startswith(_ART_FILES):
            self._art_show_files(role_val[len(_ART_FILES):])
        elif role_val.startswith(_ART_VALIDATION):
            self._art_show_validation(role_val[len(_ART_VALIDATION):])

    # ── Report display ────────────────────────────────────────────────────────

    def _setup_report_filter_ui(self, columns: list, hidden: set = frozenset(),
                                script_name: str | None = None,
                                core_fields: list | None = None,
                                has_recoverable: bool = False) -> None:
        """Reset filter controls for a newly loaded report. *hidden* (a
        parser's own hidden_fields declaration) is skipped in the dropdown
        — an internal plumbing field isn't something an examiner can
        usefully filter by — but each surviving item still carries its
        REAL index into *columns* as Qt item data, since that's the index
        space ArtifactFilterWorker/filter_rows_inmem actually key off
        (unaffected by which columns are merely hidden from display).

        *script_name*/*core_fields* drive the Columns menu (see
        report_columns_store.py) — only given by _art_show_report for a
        real per-parser report; _art_show_files/_populate_apps_table call
        this with neither, which correctly hides the Columns button the
        same way Category/Date already hide themselves for a non-Apps
        report (just inverted: this one is Apps/Files-only-HIDDEN rather
        than Apps-only-shown).

        *has_recoverable* shows the "Hide likely false positives" checkbox
        (see its own construction comment) only for a report whose parser
        actually declares recoverable_tables — no other report can ever
        produce a header_signature-carved row, so the control would be
        meaningless noise everywhere else. Always reset to CHECKED here,
        same reset-per-load convention as everything else in this method —
        an examiner who unchecked it to inspect a flagged row on one
        report should not have that carry over, unnoticed, into a
        different report opened afterward."""
        self._cancel_art_filter_worker()
        self._art_active_filter = ''
        self._art_filter_input.clear()
        self._art_hide_lowconf_checkbox.setVisible(has_recoverable)
        self._art_hide_lowconf_checkbox.blockSignals(True)
        self._art_hide_lowconf_checkbox.setChecked(True)
        self._art_hide_lowconf_checkbox.blockSignals(False)
        self._art_filter_col.blockSignals(True)
        self._art_filter_col.clear()
        self._art_filter_col.addItem("All Columns", -1)
        for i, col in enumerate(columns):
            if col in hidden:
                continue
            self._art_filter_col.addItem(col, i)
        self._art_filter_col.blockSignals(False)
        self._art_filter_btn.setEnabled(True)

        # Category/Date filters are Apps-table-only (see _populate_apps_table)
        # — reset to hidden/off by default on every report load; a caller
        # showing the Apps table itself re-enables them right after this
        # returns, same show-only-for-this-tab pattern as the Hex panel's
        # Record/Attachment toggle.
        self._art_showing_apps = False
        self._art_category_filter_selected = None
        self._art_category_filter_btn.setText("Category: All")
        self._art_date_filter_enable.blockSignals(True)
        self._art_date_filter_enable.setChecked(False)
        self._art_date_filter_enable.blockSignals(False)
        self._set_art_date_filter_enabled(False)
        for w in (self._art_category_filter_btn, self._art_date_filter_enable,
                 self._art_date_filter_combo, self._art_date_filter_from,
                 self._art_date_filter_to):
            w.setVisible(False)

        # Columns state (see report_columns_store.py) — recomputed on
        # every load, same reset-per-load convention as everything else
        # here, so a previous report's column choice never leaks into a
        # newly opened one.
        self._art_columns_script_name = script_name
        self._art_columns_all = [c for c in columns if c not in hidden]
        self._art_columns_core = [c for c in (core_fields or [])
                                  if c in self._art_columns_all]
        self._art_columns_filter_btn.setVisible(script_name is not None)
        self._art_columns_all_btn.setVisible(script_name is not None)
        self._art_columns_core_btn.setVisible(
            script_name is not None and bool(self._art_columns_core))
        if script_name is not None:
            import report_columns_store
            visible = report_columns_store.get_visible_columns(
                script_name, self._art_columns_all)
            if visible is None and self._art_columns_core:
                # First time this report has ever been opened (never
                # saved either way) AND it declares core_fields: default
                # the DISPLAY to Core rather than everything — the whole
                # point of this feature is not overwhelming the examiner
                # by default. Deliberately not persisted here — the store
                # stays "never customized" until the examiner actually
                # picks something via the row buttons or the dialog, so a
                # later core_fields change on the parser still applies
                # correctly to a report nobody has touched yet.
                visible = list(self._art_columns_core)
            self._apply_art_column_visibility(visible)
            order = report_columns_store.get_column_order(
                script_name, self._art_columns_all)
            if order:
                self._apply_art_column_order(order)
        self._update_art_columns_indicator()

    def _apply_art_column_visibility(self, visible_names) -> None:
        """Show/hide QTableView columns per *visible_names* (a list —
        possibly empty — or None meaning "show every column"). Mirrors
        ffs-explorer.py's own File Browser column show/hide
        (QTableView.setColumnHidden on the view directly) rather than
        touching ArtifactTableModel — the model's own _visible_idx is
        reserved for a parser's PERMANENT hidden_fields declaration
        (never user-toggleable, never offered as a choice here); this is
        a second, independent, user-controlled layer applied at the view
        level on top of it."""
        model_visible_cols = [self._art_table_model._columns[i]
                              for i in self._art_table_model._visible_idx]
        for logical, name in enumerate(model_visible_cols):
            hide = visible_names is not None and name not in visible_names
            self._art_report_view.setColumnHidden(logical, hide)

    def _art_current_visible_column_names(self) -> set:
        model_visible_cols = [self._art_table_model._columns[i]
                              for i in self._art_table_model._visible_idx]
        return {name for logical, name in enumerate(model_visible_cols)
               if not self._art_report_view.isColumnHidden(logical)}

    def _set_art_visible_columns(self, names) -> None:
        """Apply AND persist a new visible-column set immediately — used
        by the quick "All"/"Core" buttons in the Columns row, and by the
        dialog's own All/None/Core presets. *names* is always stored as a
        concrete list, even when it happens to equal every column —
        deliberately NOT collapsed to None ("never customized"), because
        the DEFAULT for "never customized" is Core, not All (see
        _setup_report_filter_ui): once the examiner has made any real
        choice, including "show everything", it must stick as that exact
        choice rather than silently re-becoming "never touched" and
        picking up a future default change."""
        self._apply_art_column_visibility(list(names))
        self._update_art_columns_indicator()
        if self._art_columns_script_name:
            import report_columns_store
            report_columns_store.set_visible_columns(
                self._art_columns_script_name, list(names))

    def _apply_art_column_order(self, order_names: list) -> None:
        """Rearrange the Report table's header sections to match
        *order_names* (a list of visible-space column names, left to
        right) — same header.moveSection() mechanism ffs-explorer.py's
        own File Browser uses for its own column reordering
        (_apply_column_order). Any current column missing from
        *order_names* keeps its relative place at the end, same "identity
        order for the rest" fallback the File Browser uses."""
        header = self._art_report_view.horizontalHeader()
        model_visible_cols = [self._art_table_model._columns[i]
                              for i in self._art_table_model._visible_idx]
        order = [n for n in order_names if n in model_visible_cols]
        order += [n for n in model_visible_cols if n not in order]
        for visual_pos, name in enumerate(order):
            logical = model_visible_cols.index(name)
            cur = header.visualIndex(logical)
            if cur != visual_pos:
                header.moveSection(cur, visual_pos)

    def _art_current_column_order(self) -> list:
        """Current on-screen left-to-right order of visible-space column
        names — reads the header's own visual order, so it reflects any
        reordering already applied (by a previous saved order, or a drag
        just made in the dialog)."""
        header = self._art_report_view.horizontalHeader()
        model_visible_cols = [self._art_table_model._columns[i]
                              for i in self._art_table_model._visible_idx]
        return [model_visible_cols[header.logicalIndex(v)]
               for v in range(len(model_visible_cols))]

    def _update_art_columns_indicator(self) -> None:
        """Reflect the current column state on the row's own buttons —
        two independent signals, both recomputed here so every call site
        that changes visibility (the quick All/Core buttons, the dialog's
        presets/individual toggles, and a report's own initial load) stays
        in sync without each needing to know about the other:

        1. "Columns ⊘N" / tooltip-lists-hidden-names on the Columns button
           itself, same behaviour as ffs-explorer.py's own File Browser
           _update_columns_indicator — visible at a glance without
           opening the dialog.
        2. The quick All/Core buttons highlight blue (ACTIVE_BUTTON_STYLE)
           when what's CURRENTLY shown exactly matches that preset —
           neither highlights for a custom subset — a second, independent
           signal for "is anything hidden, and is it a preset or a custom
           choice" that doesn't require reading the tooltip."""
        if self._art_columns_script_name is None:
            return
        visible = self._art_current_visible_column_names()
        all_set = set(self._art_columns_all)
        core_set = set(self._art_columns_core)
        hidden = sorted(all_set - visible)
        if hidden:
            self._art_columns_filter_btn.setText(f"Columns ⊘{len(hidden)}")
            self._art_columns_filter_btn.setToolTip("Hidden: " + ", ".join(hidden))
        else:
            self._art_columns_filter_btn.setText("Columns…")
            self._art_columns_filter_btn.setToolTip(
                "Show, hide, or reorder this report's columns.")
        self._art_columns_all_btn.setStyleSheet(
            ACTIVE_BUTTON_STYLE if visible == all_set else "")
        self._art_columns_core_btn.setStyleSheet(
            ACTIVE_BUTTON_STYLE if (core_set and visible == core_set) else "")

    def _show_art_columns_dialog(self) -> None:
        """The Report table's "Columns" dialog — same shape as
        ffs-explorer.py's own File Browser _show_columns_dialog (tick to
        show/hide, drag rows or use the arrows to reorder), plus an
        "All"/"None"/"Core" preset row above the list (Core only when the
        parser declares core_fields — see WRITING_ARTIFACT_PARSERS.md)
        that a plain file listing has no equivalent need for."""
        order = self._art_current_column_order()
        currently_visible = self._art_current_visible_column_names()

        dlg = QDialog(self)
        dlg.setWindowTitle("Columns")
        dlg.setMinimumSize(320, 420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(note_label(
            "Tick to show, untick to hide. Drag rows — or use the arrows "
            "— to reorder."))

        preset_row = QHBoxLayout()
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        preset_row.addWidget(all_btn)
        preset_row.addWidget(none_btn)
        core_btn = None
        if self._art_columns_core:
            core_btn = QPushButton("Core")
            preset_row.addWidget(core_btn)
        preset_row.addStretch()
        lay.addLayout(preset_row)

        lst = QListWidget()
        lst.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        for name in order:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in currently_visible
                               else Qt.CheckState.Unchecked)
            lst.addItem(item)
        lay.addWidget(lst)

        def _apply_and_persist():
            names = [lst.item(i).text() for i in range(lst.count())]
            checked = [lst.item(i).text() for i in range(lst.count())
                      if lst.item(i).checkState() == Qt.CheckState.Checked]
            # Deliberately never collapsed to None here even when every
            # column ends up checked — see _set_art_visible_columns for
            # why "show everything" must persist as an explicit choice
            # now that "never customized" defaults to Core, not All.
            self._set_art_visible_columns(checked)
            self._apply_art_column_order(names)
            if self._art_columns_script_name:
                import report_columns_store
                report_columns_store.set_column_order(
                    self._art_columns_script_name, names)

        def _on_reordered(*_a):
            _apply_and_persist()

        def _move_current(delta: int):
            row = lst.currentRow()
            if row < 0 or not (0 <= row + delta < lst.count()):
                return
            item = lst.takeItem(row)
            lst.insertItem(row + delta, item)
            lst.setCurrentRow(row + delta)
            _on_reordered()

        def _set_all(checked_names: set):
            lst.blockSignals(True)
            for i in range(lst.count()):
                it = lst.item(i)
                it.setCheckState(Qt.CheckState.Checked if it.text() in checked_names
                                 else Qt.CheckState.Unchecked)
            lst.blockSignals(False)
            _apply_and_persist()

        lst.itemChanged.connect(lambda _item: _apply_and_persist())
        lst.model().rowsMoved.connect(_on_reordered)
        all_btn.clicked.connect(lambda: _set_all(set(self._art_columns_all)))
        none_btn.clicked.connect(lambda: _set_all(set()))
        if core_btn is not None:
            core_btn.clicked.connect(lambda: _set_all(set(self._art_columns_core)))

        move_row = QHBoxLayout()
        up_btn = QPushButton("▲ Move Up")
        down_btn = QPushButton("▼ Move Down")
        up_btn.clicked.connect(lambda: _move_current(-1))
        down_btn.clicked.connect(lambda: _move_current(1))
        move_row.addWidget(up_btn)
        move_row.addWidget(down_btn)
        lay.addLayout(move_row)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        lay.addWidget(close_btn)

        dlg.exec()

    def _art_resize_columns(self, max_width: int = 320):
        """Estimate column widths by sampling the first 200 visible rows."""
        fm          = self._art_report_view.fontMetrics()
        n_cols      = self._art_table_model.columnCount()
        n_rows      = min(200, self._art_table_model.rowCount())
        media_cols  = self._art_table_model.media_columns()
        for col in range(n_cols):
            if col in media_cols:
                # Cell shows a fixed-size thumbnail, not the raw ui_path
                # text — size from that, not from sampling path lengths.
                self._art_report_view.setColumnWidth(col, max_width)
                continue
            header = self._art_table_model.headerData(col, Qt.Orientation.Horizontal) or ''
            w = fm.horizontalAdvance(str(header)) + 24
            for row in range(n_rows):
                text = self._art_table_model.data(
                    self._art_table_model.index(row, col)) or ''
                w = max(w, fm.horizontalAdvance(text) + 12)
            self._art_report_view.setColumnWidth(col, min(w, max_width))

    def _update_art_version_banner(self, script_name: str, platform: str) -> None:
        """Show/hide the non-blocking version banner above the report
        table: visible only when the parser version that produced THIS
        report (run_log.parser_version, recorded at run time) is older
        than the parser's current on-disk version (parser_versions.py).
        Never blocks viewing the existing (older) results — the examiner
        decides whether/when to update via the banner's own button."""
        self._art_version_banner.setVisible(False)
        current_version = parser_versions.get_current_version(platform, script_name)
        if current_version is None:
            return
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                last_run = load_last_run(db, f'artifact_{script_name}')
        except Exception:
            return
        if not last_run:
            return
        used_version = last_run.get('parser_version')
        # used_version is None for a report run before this feature existed,
        # or if recording it failed at run time (start_run_log is
        # best-effort) — nothing to honestly compare against, so stay quiet
        # rather than claim an update is available when we can't say from what.
        if used_version is None or current_version <= used_version:
            return
        changelog = parser_versions.get_changelog_entry(platform, script_name, current_version)
        note = changelog or "No changelog has been recorded for this update."
        self._art_version_banner_label.setText(
            f"A newer version of this parser is available "
            f"(v{used_version} → v{current_version}). {note}")
        self._art_version_banner.setVisible(True)

    def _on_art_update_parser_version(self) -> None:
        """The version banner's Update button: re-runs just this report's
        parser against the current case, then reloads the report and
        refreshes the tree (mirroring what a normal multi-parser run
        already does via parsers_completed → _refresh_artifact_tab)."""
        script_name = self._art_current_script
        mod         = self._art_current_mod
        platform    = self._art_current_platform
        if not script_name or not mod or not platform:
            return
        self._art_version_update_btn.setEnabled(False)
        self._art_version_banner_label.setText("Updating…")
        worker = ArtifactRunnerWorker(
            [(script_name, mod)], self.zip_path, self._adapter,
            self._case_dir, platform,
            guid_to_bundle=self.guid_to_bundle,
        )

        def _done():
            self._art_version_update_btn.setEnabled(True)
            self._refresh_artifact_tab()
            self._art_show_report(script_name)

        worker.done.connect(_done)
        self._art_update_worker = worker   # keep a reference so it isn't GC'd mid-run
        worker.start()

    def _art_show_ai_summary_panel(self, script_name: str) -> None:
        """Group-root landing view for an app_group whose designated
        overview member opts in via group_overview_mode = "ai_summary"
        (see artifacts/android/chrome_web_history.py) — shows the LAST
        GENERATED AI Summary for that report (ai_summary.load_summary),
        rendered as markdown/rich text via QTextBrowser.setMarkdown,
        rather than a raw stats table. Nothing here ever calls the LLM
        itself — generating/regenerating only happens through the AI
        Summary dialog (_open_ai_summary_dialog) or the MCP run_ai_summary
        tool, both of which persist their result the same way
        (ai_summary.save_summary); this is a read-only display of
        whichever surface generated it last."""
        import ai_summary
        summary = ai_summary.load_summary(self._case_dir, script_name) if self._case_dir else None
        if not summary:
            self._art_ai_summary_label.setText(
                "No AI summary has been generated yet for this report.")
            self._art_ai_summary_view.setMarkdown(
                "Open **AI Summary** from the tree, pick this report, and click "
                "**Generate Summary** to create one. This view shows the result "
                "once one exists — nothing here calls the model itself."
            )
        else:
            generated = _local_date(summary.get("generated_at") or "")
            self._art_ai_summary_label.setText(
                f"Generated {generated or 'unknown date'} — "
                f"{summary['chunk_count']} chunk(s), {summary['total_rows']} row(s) "
                f"of real data went into this. It's an AI-generated narrative, not "
                f"raw evidence — verify anything material against the underlying "
                f"report before relying on it.")
            self._art_ai_summary_view.setMarkdown(summary["text"])
        self._art_stack.setCurrentIndex(5)

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

        from artifact_runner import list_artifacts
        platform = 'android' if self._is_android_archive() else 'ios'
        mod = {sn: m for sn, m in list_artifacts(platform)}.get(script_name)
        self._art_current_mod = mod
        self._art_current_script = script_name
        self._art_current_platform = platform

        # Parser-version banner (see parser_versions.py / Conventions):
        # list_artifacts() above already ran check_version() for every
        # script it loaded, so the store's version for `script_name` is
        # already current — just compare it against whatever version
        # produced THIS report (recorded in run_log at run time).
        self._update_art_version_banner(script_name, platform)

        # record_source (see Conventions): a list of {label, file_key,
        # table_field/table, rowid_fields, source_match} entries, one per
        # DB row this report's own rows are actually built from — a joined
        # report has more than one (its own table plus whatever it LEFT
        # JOINed in), and a report merging more than one live query (see
        # source_match / _art_record_sources_for_row) can have entries
        # that only apply to SOME rows. This is the full, unscoped
        # declaration — used as-is only by a carved row's own table-name
        # lookup in _art_load_record_hex; a live row narrows it per its
        # own `source` before the combo is ever populated, which only
        # happens once a row is actually selected (see
        # _on_art_report_row_selected/_art_load_record_hex) — nothing
        # meaningful to show in the combo before that, so it starts
        # empty/hidden here rather than pre-filled with entries that may
        # not even apply to whichever row gets selected first.
        raw_rs = getattr(mod, 'record_source', None) if mod else None
        if isinstance(raw_rs, dict):
            record_sources = [raw_rs]
        elif raw_rs:
            record_sources = list(raw_rs)
        else:
            record_sources = []
        self._art_current_record_sources = record_sources
        self._art_record_source_sticky = {}
        self._hex_record_source_combo.blockSignals(True)
        self._hex_record_source_combo.clear()
        self._hex_record_source_combo.blockSignals(False)
        self._hex_record_source_combo.setVisible(False)

        # Media columns (see media_fields on the parser module): pull every
        # distinct non-empty value now, while conn is still ours and unused
        # by anything else — load_from_db() below transfers ownership to
        # the model, and nothing may safely query it out from under that
        # afterwards.
        media_fields = list(getattr(mod, 'media_fields', ())) if mod else []
        media_paths  = set()
        for col_name in media_fields:
            if col_name not in cols:
                continue
            try:
                for (v,) in conn.execute(
                        f'SELECT DISTINCT "{col_name}" FROM "{table}" '
                        f'WHERE "{col_name}" IS NOT NULL AND "{col_name}" != \'\''):
                    media_paths.add(v)
            except Exception:
                pass

        # Transfer connection ownership to the model
        self._art_table_model.load_from_db(conn, table, cols, ids)

        # Internal plumbing fields (see hidden_fields in Conventions) — a
        # joined table's raw rowid kept only for the Hex panel's Record
        # mode to look up (record_source), never meant as report content.
        # Must run before set_media_columns(), which indexes in the
        # visible-column space this establishes.
        hidden_fields = set(getattr(mod, 'hidden_fields', ())) if mod else set()
        self._art_table_model.set_hidden_columns(hidden_fields)

        # Raw-value timestamp columns (see timestamp_fields on the parser
        # module) get formatted per the case's UTC/handset/acquisition
        # setting at display time — set_timestamp_formatting must be called
        # AFTER load_from_db, which resets it to empty/None on every load.
        units = getattr(mod, 'timestamp_fields', {}) if mod else {}
        if units:
            def _fmt(raw_value, unit_code):
                try:
                    secs = float(raw_value)
                except (TypeError, ValueError):
                    return str(raw_value)
                if unit_code == 'ms':
                    secs /= 1000
                elif unit_code == 'cocoa_s':
                    secs += 978307200
                elif unit_code == 'cocoa_ns':
                    secs = secs / 1e9 + 978307200
                elif unit_code == 'webkit_us':
                    # Chromium/WebKit timestamp: microseconds since
                    # 1601-01-01 UTC (base::Time's internal representation)
                    # — used throughout Chrome's own SQLite stores
                    # (History, Web Data, segmentation_platform's ukm_db,
                    # ...). Offset is 1601-01-01 -> 1970-01-01 in seconds.
                    secs = secs / 1e6 - 11644473600
                return self.format_ts(secs)
            self._art_table_model.set_timestamp_formatting(units, _fmt)
        else:
            self._art_table_model.set_timestamp_formatting({}, None)

        # Media columns: thumbnail delegate per-column (never touches the
        # other columns' normal HighlightDelegate), row height tall enough
        # for a thumbnail, and an async decode pass over every distinct
        # path collected above — see MediaThumbnailDelegate in
        # artifact_media.py and _start_art_media_thumbnails below.
        self._art_table_model.set_media_columns(media_fields)
        media_col_idx = self._art_table_model.media_columns()
        for col in range(self._art_table_model.columnCount()):
            self._art_report_view.setItemDelegateForColumn(
                col, self._art_media_delegate if col in media_col_idx
                     else self._art_highlight_delegate)
        self._art_report_view.verticalHeader().setDefaultSectionSize(
            THUMB_CELL_SIZE + 16 if media_col_idx else 80)
        # webpage_thumbnail_fields (optional, e.g. chrome_cache_pages.py's
        # reconstructed_mhtml_path): a still-image decode (ThumbnailWorker)
        # can't produce a meaningful thumbnail for an .mhtml -- it isn't an
        # image at all, it's a page. A module declaring this routes its
        # media_fields thumbnails through WebpageThumbnailRenderer instead
        # (an actual headless-render, see artifact_media.py) — simplifying
        # assumption: the whole media_paths set goes through ONE renderer
        # or the other, never split per-column, since no parser has needed
        # a genuine mix of the two kinds in the same report yet.
        if mod and getattr(mod, 'webpage_thumbnail_fields', None):
            self._start_art_webpage_thumbnails(list(media_paths))
        else:
            self._start_art_media_thumbnails(list(media_paths))

        core_fields = getattr(mod, 'core_fields', None) if mod else None
        has_recoverable = bool(getattr(mod, 'recoverable_tables', None)) if mod else False
        self._setup_report_filter_ui(cols, hidden_fields, script_name, core_fields, has_recoverable)
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(ids):,} rows")
        self._art_stack.setCurrentIndex(1)
        # Apply the default filter state now that the model actually holds
        # this report's rows -- covers both a checked "Hide likely false
        # positives" (the common case, see _setup_report_filter_ui) and a
        # leftover free-text term from... nothing, actually, since the
        # filter box was just cleared above, but _apply_art_filter is the
        # one place that already knows how to combine both correctly, so
        # reusing it here (rather than a parallel one-off query) keeps
        # there being exactly one implementation of "what's currently
        # filtered" instead of two that could drift apart.
        self._apply_art_filter()

    # ── Media columns (thumbnails + full view) ──────────────────────────────

    def _retire_art_media_worker(self):
        worker = getattr(self, '_art_media_thumb_worker', None)
        if worker is not None:
            self._retire_worker(worker)
            self._art_media_thumb_worker = None

    def _start_art_media_thumbnails(self, media_paths: list):
        """Decode a thumbnail for every distinct media-column path in the
        just-loaded report, off the UI thread. Reuses media_viewer's own
        ThumbnailWorker — same zip-reading logic and the
        same on-disk thumbnail cache the Media tab uses (keyed separately
        by thumb size, so this never collides with the Media tab's own
        160px thumbnails)."""
        self._retire_art_media_worker()
        self._retire_art_webpage_worker()
        self._art_media_delegate.set_cache({})
        if not media_paths or not self.zip_path:
            return
        from media_viewer import ThumbnailWorker
        zip_info_map = {
            self._adapter.resolve(p): self.full_metadata.get(p, {}).get('size', 0)
            for p in media_paths}
        worker = ThumbnailWorker(
            self.zip_path, media_paths, self._adapter.resolve, THUMB_CELL_SIZE,
            zip_info_map, cache_dir=self._case_dir)
        worker.thumbnail_ready.connect(self._on_art_media_thumbnail_ready)
        self._art_media_thumb_worker = worker
        worker.start()

    def _on_art_media_thumbnail_ready(self, ui_path, img):
        from PySide6.QtGui import QPixmap
        self._art_media_delegate.set_pixmap(ui_path, QPixmap.fromImage(img))
        self._art_report_view.viewport().update()

    def _retire_art_webpage_worker(self):
        renderer = getattr(self, '_art_webpage_thumb_renderer', None)
        if renderer is not None:
            renderer.stop()
            renderer.deleteLater()
            self._art_webpage_thumb_renderer = None

    def _start_art_webpage_thumbnails(self, media_paths: list):
        """Reconstructed Web Pages' own thumbnail pass — WebpageThumbnailRenderer
        (artifact_media.py) actually renders each .mhtml in a headless
        QWebEngineView rather than decoding bytes, so this is NOT
        ThumbnailWorker/_start_art_media_thumbnails: those paths are local
        filesystem paths (parser-generated, not archive entries), and the
        thing being previewed is a rendered page, not a still image."""
        self._retire_art_media_worker()
        self._retire_art_webpage_worker()
        self._art_media_delegate.set_cache({})
        if not media_paths:
            return
        renderer = WebpageThumbnailRenderer(
            media_paths, THUMB_CELL_SIZE, cache_dir=self._case_dir, parent=self)
        renderer.thumbnail_ready.connect(self._on_art_media_thumbnail_ready)
        self._art_webpage_thumb_renderer = renderer
        renderer.start()

    def _on_art_report_double_clicked(self, index):
        """Open the full-size image / video-player dialog for a
        media-column cell, and switch the Hex panel's toggle to
        "Attachment" (staying there for subsequent row clicks, same as any
        other toggle change — see _on_art_hex_source_toggled) so what's
        shown matches what the dialog just opened, rather than leaving a
        stale "Record" hex view underneath it. No-op for any non-media
        column.

        Non-modal (.show(), not .exec()) and tracked on self so
        _art_sync_open_media_dialog can keep swapping its content as the
        row selection changes, instead of it blocking the report table —
        a second double-click while one is already open reuses that same
        window (load_content + raise) rather than stacking another."""
        if index.column() not in self._art_table_model.media_columns():
            return
        ui_path = self._art_table_model.data(index) or ''
        if not ui_path:
            return
        data = self._read_zip_bytes(ui_path)
        if data is None:
            self.status_bar.showMessage(
                f"Attachment not found in archive: {ui_path}", 5000)
            return
        self._hex_source_attach_btn.setChecked(True)
        self._load_hex_preview_from_bytes(data, ui_path)
        self._art_hex_active = True
        self.status_bar.showMessage(ui_path)

        dialog = getattr(self, '_art_media_dialog', None)
        if dialog is not None and dialog.isVisible():
            dialog.load_content(ui_path, data)
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = MediaFullViewDialog(ui_path, data, parent=self)
        dialog.finished.connect(self._on_art_media_dialog_closed)
        self._art_media_dialog = dialog
        dialog.show()

    def _on_art_media_dialog_closed(self, _result=None) -> None:
        self._art_media_dialog = None

    def _art_show_notes(self, script_name: str):
        """Report Notes/Warning page — a script's own `description` (where
        this data comes from, and known reliability caveats), and an
        optional `warning` for something the examiner must read before
        trusting the report. Off the report page itself (see
        _art_show_report) so a long description no longer pushes the table
        down or off-screen. Rendered as rich text (paragraph breaks at
        sentence boundaries, `backtick` spans as monospace) — these
        descriptions are written as one long sentence-dense paragraph in
        the source, which reads fine there but is hard to scan as a wall
        of text in the UI."""
        from artifact_runner import list_artifacts
        platform = 'android' if self._is_android_archive() else 'ios'
        mod = {sn: m for sn, m in list_artifacts(platform)}.get(script_name)
        description = getattr(mod, 'description', '') if mod else ''
        warning     = getattr(mod, 'warning', '') if mod else ''

        if warning:
            parts = [
                '<div style="font-size:14px; font-weight:600; color:#8a1c1c; '
                'margin-bottom:8px;">⚠️&nbsp; Warning</div>',
                _notes_html_paragraphs(warning),
            ]
            if description:
                parts.append(
                    '<hr style="border:none; border-top:1px solid #e3b3b3; margin:14px 0;">'
                    '<div style="font-weight:600; margin-bottom:6px;">Notes</div>')
                parts.append(_notes_html_paragraphs(description))
            html_body = ''.join(parts)
            self._art_notes_label.setStyleSheet(
                "background:#fdecea; border:1px solid #d64545; border-radius:4px; "
                "padding:10px 12px; color:#3a1414;")
        else:
            html_body = (_notes_html_paragraphs(description)
                        if description else
                        '<span style="color:#888888;">No notes declared for this report.</span>')
            self._art_notes_label.setStyleSheet(
                "background: palette(base); border: 1px solid palette(mid); "
                "border-radius: 4px; padding: 10px 12px;")
        self._art_notes_label.setText(html_body)
        self._art_stack.setCurrentIndex(3)

    def _art_show_app_notes(self):
        """Application Report Notes page — the Apps table's own notes,
        added 2026-08-26 alongside the Data/Shared Folder Created and
        Preferences/Splash Snapshot Modified columns below, since (unlike
        a per-parser Report Notes/Warning child) this table isn't backed
        by any one script's own `description` — reuses the same
        _art_notes_label/index-3 page as _art_show_notes, just with
        hand-authored content instead of a module attribute.

        Explains why the previous Last Activity (Data Folder)/(Shared
        Folder) columns (2026-08-25) were REMOVED rather than kept
        (real casework — IOS17 JoshHickman, 2026-08-26 — confirmed at
        least 3 unrelated apps write files with a fabricated far-future
        mtime via their own third-party disk-cache libraries; a container-
        wide max() has no way to tell that from a real recent write), and
        what each replacement column actually measures and its own
        specific limitation — an examiner reading a timestamp column
        should know exactly what it can and can't tell them, not just
        trust a single opaque "last activity" number."""
        html_body = (
            '<div style="font-size:14px; font-weight:600; margin-bottom:8px;">'
            'Application Report</div>'
            + _notes_html_paragraphs(
                "This table is the case's full app inventory (app_intelligence.scan_apps), "
                "not tied to any one parser script, so these notes explain the table's own "
                "columns rather than one report's description. "
                "`Data Folder Created`/`Shared Folder Created` are each container's own "
                "creation time (its filesystem birth time, not its last-modified time) — "
                "confirmed against this case's own ground-truth documentation to be an exact, "
                "reliable proxy for when the app was first installed/set up on this device, "
                "for both its private Data container and its Shared/App-Group container. "
                "`Preferences Modified` is the mtime of the app's own "
                "`Library/Preferences/<bundle id>.plist` — a file virtually every iOS app "
                "writes to during normal use — confirmed to closely track real last use in "
                "most cases, but it can UNDERSHOOT the true last-used date if the app's final "
                "session never happened to rewrite its settings file. "
                "`Splash Snapshot Modified` is the mtime of the OS-generated app-switcher "
                "snapshot under `Library/SplashBoard/Snapshots/` — captured by iOS itself "
                "every time the app is foregrounded then backgrounded — confirmed the closest "
                "single proxy for true last use across every app checked so far, but it is "
                "still a proxy, not a certainty: an app that was never backgrounded normally "
                "(a crash, a forced kill) may not get a fresh snapshot. "
                "These replaced a single Last Activity (Data Folder)/(Shared Folder) pair "
                "(added 2026-08-25) that took the maximum mtime across every file in the "
                "whole container — removed 2026-08-26 after real casework showed multiple, "
                "unrelated apps' own third-party disk-cache libraries writing cache files "
                "with a deliberately fabricated far-future modification time (one app's cache "
                "uniformly stamped over a decade in the future), which a plain maximum has no "
                "way to distinguish from a genuine recent write. "
                "None of these four columns is a substitute for reviewing an app's own real "
                "content when a parser or manual database review is available — they exist to "
                "help triage which unparsed apps are worth that closer look."))
        self._art_notes_label.setStyleSheet(
            "background: palette(base); border: 1px solid palette(mid); "
            "border-radius: 4px; padding: 10px 12px;")
        self._art_notes_label.setText(html_body)
        self._art_stack.setCurrentIndex(3)

    # ── Validation baseline ─────────────────────────────────────────────────

    def _device_os_string(self) -> str:
        """Best-effort 'Model OS-version' string from this case's own
        device_info table — same source get_case_overview's MCP tool reads.
        Used only as context on a validation diff (see render_diff_text's
        device_os_mismatch note), never as a lookup key."""
        if not self._case_dir:
            return ''
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                rows = dict(db.execute('SELECT field_name, data FROM device_info'))
        except Exception:
            return ''
        model = rows.get('Model', '')
        for key in ('iOS Version', 'Android Version', 'OS Version'):
            if rows.get(key):
                return f"{model} {rows[key]}".strip()
        return model

    def _compute_validation_snapshot(self, script_name: str) -> dict:
        """Schema + folder-structure snapshot for script_name against the
        CURRENTLY OPEN case, reusing that parser's already-extracted files
        under case_dir/artifact_parser_files/<name>/ (no re-extraction —
        this only runs after a parser has already completed once) and this
        case's own self.folder_map (no new zip scan)."""
        from artifact_runner import list_artifacts, _resolve_app_group_base, _parser_files_dir

        platform = 'android' if self._is_android_archive() else 'ios'
        mod = {sn: m for sn, m in list_artifacts(platform)}.get(script_name)
        if mod is None:
            raise ValueError(f"parser module {script_name!r} not found")
        if not hasattr(mod, 'files'):
            raise ValueError("validation baselines only support the multi-file "
                             "(app_path/app_group + files) parser API")

        if hasattr(mod, 'app_path'):
            app_base = mod.app_path.strip('/')
        else:
            app_base = _resolve_app_group_base(mod.app_group, self.guid_to_bundle)
            if app_base is None:
                raise ValueError(f"app group {mod.app_group!r} not present on this device")

        parser_name = getattr(mod, 'name', script_name)
        dest_dir = _parser_files_dir(self._case_dir, parser_name)
        paths = {'_app_base_ui_path': app_base}
        for key, subpath in {**mod.files, **getattr(mod, 'optional_files', {})}.items():
            dest_path = os.path.join(dest_dir, os.path.basename(subpath))
            if os.path.isfile(dest_path):
                paths[key] = dest_path

        schema    = parser_validation.snapshot_schema(paths)
        structure = parser_validation.snapshot_structure(self.folder_map, app_base)
        return {'schema': schema, 'structure': structure}

    def _art_show_validation(self, script_name: str):
        self._art_validation_script_name = script_name
        platform = 'android' if self._is_android_archive() else 'ios'
        key = f'{platform}:{script_name}'
        baseline = validation_store.get(key)

        if baseline is None:
            self._art_validation_view.setPlainText(
                "No validation baseline recorded for this parser yet.\n\n"
                "Use the button above to record THIS case's schema and "
                "folder structure as the reference — only do this against "
                "a case you know is GTD-validated (a known device/app "
                "version the parser's output was actually checked "
                "against), never against real casework.")
            self._art_validation_record_btn.setText(
                "Record This Case as Validation Baseline")
        else:
            try:
                current = self._compute_validation_snapshot(script_name)
            except Exception as exc:
                self._art_validation_view.setPlainText(
                    f"Could not compute this case's current snapshot: {exc}")
                self._art_validation_record_btn.setText("Update Validation Baseline")
                self._art_stack.setCurrentIndex(4)
                return
            diff = parser_validation.diff_snapshot(baseline, current)
            text = parser_validation.render_diff_text(
                diff, baseline, self._device_os_string())
            self._art_validation_view.setPlainText(text)
            self._art_validation_record_btn.setText(
                "Update Validation Baseline (overwrites the recorded one)")
        self._art_stack.setCurrentIndex(4)

    def _on_art_record_validation_baseline(self):
        script_name = self._art_validation_script_name
        if not script_name:
            return
        reply = QMessageBox.question(
            self, "Record Validation Baseline",
            "This overwrites any existing validation baseline for this "
            "parser with a snapshot of THIS case's database schema and "
            "folder structure.\n\n"
            "Only do this against a case you know is GTD-validated — a "
            "known device/app version you trust this parser's output "
            "against — never against real casework.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            snapshot = self._compute_validation_snapshot(script_name)
        except Exception as exc:
            self.status_bar.showMessage(f"Could not compute snapshot: {exc}", 5000)
            return
        platform = 'android' if self._is_android_archive() else 'ios'
        key = f'{platform}:{script_name}'
        case_label = os.path.basename((self._case_dir or '').rstrip('/')) or '?'
        validation_store.save_baseline(
            key, snapshot, case_label, self._device_os_string())
        self.status_bar.showMessage(
            f"Validation baseline recorded for {script_name}.", 5000)
        self._art_show_validation(script_name)

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _apply_art_filter(self):
        if self._art_showing_apps:
            self._apply_apps_combined_filter()
            return

        term    = self._art_filter_input.text()
        col_idx = self._art_filter_col.currentData()   # -1 = all columns; see _setup_report_filter_ui
        if col_idx is None:
            col_idx = -1
        total   = self._art_table_model.total_rows

        self._art_active_filter = term
        # Only ever True for a real DB-mode report whose parser declares
        # recoverable_tables (see _setup_report_filter_ui/has_recoverable
        # — the checkbox stays hidden, hence never checked, otherwise), so
        # list-mode filtering below never needs to know about this at all.
        hide_lowconf = (self._art_hide_lowconf_checkbox.isVisible()
                       and self._art_hide_lowconf_checkbox.isChecked())

        if not term and not hide_lowconf:
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

        # DB mode: run SQLite LIKE (+ the low-confidence exclusion, if
        # checked) on a background thread
        self._cancel_art_filter_worker()
        self._art_filter_btn.setEnabled(False)
        self._art_row_label.setText(f"Filtering {total:,} rows…")

        db_path = os.path.join(self._case_dir, 'caseresults.db')
        worker  = ArtifactFilterWorker(
            db_path,
            self._art_table_model._table,
            self._art_table_model._columns,
            term, col_idx,
            exclude_low_confidence=hide_lowconf,
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

    # ── Apps-table-only filters (Category checklist + Date range) ─────────────
    # Added 2026-08-26, same widget pattern as ffs-explorer.py's own File
    # Browser Type/Date filters (_populate_type_menu/_filter_by_date etc.) —
    # see _apply_apps_combined_filter for why the Apps table needs its own
    # combined filter method rather than reusing filter_rows_inmem, which
    # only ever handled the single free-text dimension.

    def _all_art_category_values(self) -> set:
        rows, columns = self._art_table_model._rows, self._art_table_model._columns
        if not rows or "Category" not in columns:
            return set()
        idx = columns.index("Category")
        return {(row[idx] or '') for row in rows}

    def _populate_art_category_menu(self):
        self._art_category_menu.clear()
        all_act = self._art_category_menu.addAction("All Categories")
        all_act.triggered.connect(partial(self._set_art_category_filter, None))
        # Empty (not None) selected set — every row's category check
        # ('row[cat_idx] not in cat_selected') fails against an empty set,
        # so this starts from nothing shown; the examiner then checks just
        # the one or two categories they actually want, rather than having
        # to uncheck everything else one at a time from "All".
        none_act = self._art_category_menu.addAction("No Categories")
        none_act.triggered.connect(partial(self._set_art_category_filter, set()))
        self._art_category_menu.addSeparator()
        values = self._all_art_category_values()
        selected = self._art_category_filter_selected
        for v in sorted(values, key=lambda x: (x == '', x)):
            act = self._art_category_menu.addAction(v if v else "(blank)")
            act.setCheckable(True)
            act.setChecked(selected is None or v in selected)
            act.toggled.connect(partial(self._on_art_category_menu_toggled, v))

    def _on_art_category_menu_toggled(self, value: str, checked: bool):
        all_values = self._all_art_category_values()
        cur = (set(all_values) if self._art_category_filter_selected is None
               else set(self._art_category_filter_selected))
        cur = cur | {value} if checked else cur - {value}
        self._set_art_category_filter(None if cur >= all_values else cur)

    def _set_art_category_filter(self, selected: set | None):
        self._art_category_filter_selected = selected
        if selected is None:
            label = "Category: All"
        elif not selected:
            label = "Category: None"
        else:
            label = "Category: Filtered"
        self._art_category_filter_btn.setText(label)
        self._apply_apps_combined_filter()

    def _set_art_date_filter_enabled(self, on: bool):
        self._art_date_filter_combo.setEnabled(on)
        self._art_date_filter_from.setEnabled(on)
        self._art_date_filter_to.setEnabled(on)

    def _on_art_date_filter_toggled(self, on: bool):
        self._set_art_date_filter_enabled(on)
        self._apply_apps_combined_filter()

    def _apply_art_filter_if_date_enabled(self):
        if self._art_date_filter_enable.isChecked():
            self._apply_apps_combined_filter()

    def _apply_apps_combined_filter(self):
        """Apps table's own filter: intersects the free-text box (same
        _art_filter_input/_art_filter_col every other report uses) with the
        Category checklist and the Date range — all three narrow the SAME
        visible row set together, unlike a normal report's single free-text
        dimension. List-mode only (the Apps table is never DB-backed, see
        _populate_apps_table's load_rows call) — a plain Python pass over
        every row is cheap at this table's size (~1-2k rows, not the
        hundreds-of-thousands a DB-backed report can hold)."""
        if not self._art_showing_apps:
            return
        rows, columns = self._art_table_model._rows, self._art_table_model._columns
        total = len(rows)

        term = self._art_filter_input.text()
        self._art_active_filter = term
        t = term.casefold()
        col_idx = self._art_filter_col.currentData()
        if col_idx is None:
            col_idx = -1

        cat_idx = columns.index("Category") if "Category" in columns else -1
        cat_selected = self._art_category_filter_selected

        date_idx = -1
        date_lo = date_hi = None
        if self._art_date_filter_enable.isChecked():
            date_header = self._art_date_filter_combo.currentText()
            if date_header in columns:
                date_idx = columns.index(date_header)
                date_lo = self._art_date_filter_from.date().toString('yyyy-MM-dd')
                date_hi = self._art_date_filter_to.date().toString('yyyy-MM-dd')

        ids = []
        for i, row in enumerate(rows):
            if t:
                if col_idx < 0:
                    if not any(t in str(v).casefold() for v in row):
                        continue
                elif t not in str(row[col_idx]).casefold():
                    continue
            if cat_selected is not None and cat_idx >= 0:
                if (row[cat_idx] or '') not in cat_selected:
                    continue
            if date_idx >= 0:
                # Stored as 'YYYY-MM-DD HH:MM:SS UTC' (_format_last_activity)
                # — a plain 10-char prefix slice sorts/compares correctly
                # against the From/To 'YYYY-MM-DD' strings, same technique
                # ffs-explorer.py's own date filter already uses.
                d = (row[date_idx] or '')[:10]
                if not d or not (date_lo <= d <= date_hi):
                    continue
            ids.append(i)

        self._cancel_art_filter_worker()
        self._art_table_model.apply_rowids(ids)
        self._art_row_label.setText(f"{len(ids):,} of {total:,} app(s)")

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
                # Local time, explicit offset: this is when the tool
                # extracted the file to local disk — tool provenance, not
                # evidence content, so local (what the examiner expects
                # for "things I did") rather than UTC (reserved for
                # evidence — see CLAUDE.md's Conventions section).
                local  = datetime.fromtimestamp(st.st_mtime).astimezone()
                offset = local.utcoffset() or timedelta(0)
                sign   = '+' if offset >= timedelta(0) else '-'
                hh, mm = divmod(abs(offset).seconds // 60, 60)
                mtime  = f"{local:%Y-%m-%d %H:%M:%S} (UTC{sign}{hh:02d}:{mm:02d})"
                rows.append((entry.name, f"{st.st_size:,}", mtime, entry.path))

        self._art_table_model.load_rows(columns, rows)
        self._setup_report_filter_ui(columns)
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(rows)} file(s)")
        self._art_stack.setCurrentIndex(1)
        self.status_bar.showMessage(f"Exported files — {folder}")

    # ── Apps (app_intelligence + app_registry) ─────────────────────────────────
    #
    # app_registry — built once per case load from the device's own
    # com.apple.LaunchServices-<version>-v2.csstore (see CLAUDE.md's "iOS
    # app registry (LaunchServices)" section) — is the real STARTING POINT
    # for this table, per direct user design guidance 2026-08-25: app
    # name, bundle id, every Shared/AppGroup folder, the Data folder, and
    # every PluginKit extension all come directly from that one registry,
    # not from app_intelligence's own merged/summarized fields. score/
    # has_parser/category and total size/last-activity still come from
    # app_intelligence.scan_apps() (already correct, no reason to
    # recompute), but the identity/location columns below are grounded in
    # app_registry first, falling back to app_intelligence's own
    # `containers` list only when no app_registry row exists for that
    # app_id (Android — app_registry is iOS-only — or an unlinked App-
    # Group identity). iOS-only concept, so Android rows show blank
    # Shared/Data Folder/Plugin(s) cells rather than a guess.

    _APPS_COLUMNS = ["App", "Bundle ID", "Shared Data Folder", "Data Folder",
                     "Plugin(s)", "Total Size", "Media Files",
                     "Data Folder Created", "Shared Folder Created",
                     "Preferences Modified", "Splash Snapshot Modified",
                     "Score", "Has Parser", "Category"]
    # The four timestamp columns above, offered in the Apps table's own
    # Date filter combo (added 2026-08-26, same pattern as the File
    # Browser's filter_date_combo) — picking one applies a From/To range
    # against ITS stored 'YYYY-MM-DD ...' string (string-prefix compare,
    # same technique ffs-explorer.py's own date filter already uses).
    _APPS_DATE_COLUMNS = ["Data Folder Created", "Shared Folder Created",
                          "Preferences Modified", "Splash Snapshot Modified"]

    @staticmethod
    def _container_path_to_ui_path(raw_path: str) -> str:
        """Normalize an app_registry bundle_container_path/
        data_container_path (a literal on-device absolute path, e.g.
        '/private/var/mobile/Containers/Data/Application/<GUID>/') into
        this project's own ui_path convention. Confirmed against real
        Cellebrite casework (iOS 16.5 CTF23) that stripping a leading
        '/private/var' and any trailing slash exactly matches the SAME
        container's own path as it already appears in app_intelligence's
        `containers` field. Not independently verified against GrayKey (no
        GrayKey iOS test case was available when this was written) — this
        mirrors the same unconditional (no format branching) convention
        artifact_runner._resolve_app_group_base already uses for an
        App-Group container's own ui_path, so any GrayKey gap here is the
        same one already latent there, not something new."""
        return (raw_path or '').removeprefix('/private/var').strip('/')

    def _build_app_registry_lookup(self, app_ids: list) -> tuple[dict, dict]:
        """Returns (registry_by_bundle_id, plugins_by_bundle_id).

        registry_by_bundle_id: {bundle_id: app_registry row} — from
        app_registry (the LaunchServices csstore), main identity/location
        fields only (display name, Data/App-Group container paths).

        plugins_by_bundle_id: {host_bundle_id: [extension_bundle_id, ...]},
        built from *app_ids* — the CURRENT app_intelligence.scan_apps()
        result being displayed, NOT app_registry — confirmed by direct
        testing 2026-08-25 that app_registry does NOT reliably carry
        PluginKit extension bundle ids at all (WhatsApp's 6 real
        extensions — ServiceExtension, NotificationExtension,
        ShareExtension, TodayExtension, IntentsUI, Intents — are ABSENT
        from app_registry on real casework, since the csstore's own Bundle
        table simply doesn't always have a row for them); app_intelligence
        DOES always know about them, as their own separate rows (each
        PluginKitPlugin container resolves its own bundle id via the
        per-container metadata plist / guid_to_bundle, independent of
        the csstore — see CLAUDE.md's app_intelligence.py entry on why an
        extension is never merged into its host's own identity). Same
        dotted-suffix convention either way (e.g.
        'net.whatsapp.WhatsApp.ShareExtension' under host
        'net.whatsapp.WhatsApp'), just checked against the right source.

        registry_by_bundle_id is {} (not an error) if app_registry hasn't
        been built for this case — Android always, or an iOS case never
        (re)opened since this feature shipped."""
        registry_by_bundle: dict = {}
        if self._case_dir:
            from db_utils import _open_cache_db, load_app_registry
            try:
                with closing(_open_cache_db(self._case_dir)) as cache_db:
                    registry_by_bundle = {r['bundle_id']: r for r in load_app_registry(cache_db)}
            except Exception:
                registry_by_bundle = {}
        plugins_by_bundle: dict = {}
        for host in app_ids:
            kids = sorted(x for x in app_ids if x != host and x.startswith(host + '.'))
            if kids:
                plugins_by_bundle[host] = kids
        return registry_by_bundle, plugins_by_bundle

    def _flatten_app_intelligence_row(self, row: dict, registry_by_bundle: dict,
                                      plugins_by_bundle: dict) -> tuple:
        """One scan_apps() row -> one flat tuple for the Apps table. Every
        value here is a short string/number — deliberately never a raw
        list/dict cell: a flat table should only carry what reads sensibly
        in one cell, so a Shared Data Folder/Plugin(s) cell with more than
        one real value is comma-joined rather than truncated to the first
        (per direct user instruction — WhatsApp alone has 5 real App-Group
        folders on real casework, confirmed 2026-08-25; showing only one
        would silently hide four). Total Size is the RAW byte count
        (int), not a pre-formatted string — ArtifactTableModel.data()
        MB-formats it at display time (see set_byte_columns(), called in
        _populate_apps_table) so the underlying value stays numerically
        sortable; a value baked into '51.80 MB' before storage would sort
        lexicographically ('10.00 MB' before '9.00 MB'), the same failure
        mode timestamp columns avoid by storing a raw epoch value and
        formatting only at display time."""
        app_id = row.get('app_id', '')
        reg = registry_by_bundle.get(app_id)
        plugins = plugins_by_bundle.get(app_id, [])
        containers = row.get('containers') or []

        if reg:
            data_folder = self._container_path_to_ui_path(reg.get('data_container_path', ''))
            shared_folder = ', '.join(
                f"mobile/Containers/Shared/AppGroup/{guid}"
                for guid in sorted((reg.get('app_group_paths') or {}).values()))
        else:
            # No app_registry row for this identity (Android, an unlinked
            # App-Group, OR a PluginKit extension — app_registry doesn't
            # reliably carry extension bundle ids at all, see
            # _build_app_registry_lookup's own docstring) — fall back to
            # app_intelligence's own already-merged container list rather
            # than leaving a blank cell when the data is actually right
            # there. 'plugin' included alongside 'data' here (confirmed
            # 2026-08-26, per direct user question, that a plugin row's
            # own container path was previously shown nowhere at all —
            # neither this branch nor the app_registry one above ever
            # looked at kind=='plugin'): a PluginKitPlugin container always
            # forms its own separate row, never merged with a host app's
            # data/app_group containers (see scan_apps' identity-grouping
            # comment in app_intelligence.py), so a real app's data_folder
            # here is never accidentally joined with an unrelated plugin's
            # path — the two kinds never co-occur in the same row's
            # containers list.
            data_folder = ', '.join(c['path'] for c in containers
                                    if c.get('kind') in ('data', 'plugin'))
            shared_folder = ', '.join(c['path'] for c in containers if c.get('kind') == 'app_group')

        # Display-only category fallbacks (added 2026-08-26, per direct user
        # requests) — GUI-table-only: never written back into row['category']
        # itself, which stays exactly what scan_apps() actually found (or
        # genuinely didn't); a real declared category (from iTunesMetadata/
        # Info.plist — see app_intelligence.py's scan_ios_bundle_containers)
        # always wins over both fallbacks below, never overridden.
        #   1. 'Plug-in' — ANY vendor's PluginKit extension (WhatsApp's own
        #      ShareExtension just as much as Apple's), derived from the
        #      row's own containers' 'kind' field — a PluginKitPlugin
        #      container always forms its own separate row, never merged
        #      with a host app's data/app_group containers (see
        #      app_intelligence.py's scan_apps identity-grouping comment),
        #      so a row is unambiguously one or the other, never both. Takes
        #      priority over fallback 2 below (an extension is still an
        #      extension whichever vendor built it) — was the actual real
        #      ask this whole Category-filter feature started from: telling
        #      a real app apart from a PluginKit extension in a ~1000-row
        #      table with no other way to do it at a glance.
        #   2. 'Built-in Apple App' — a com.apple.* bundle id that's NOT a
        #      plugin (fallback 1 already claimed those) and has no real
        #      declared category — Apple's own built-in apps mostly have no
        #      iTunesMetadata.plist at all (baked into iOS, not App Store
        #      installs), so real category data is usually genuinely absent
        #      for them; this label is a display convenience, not a claim
        #      that Apple declared it.
        category = row.get('category') or ''
        if not category:
            if any(c.get('kind') == 'plugin' for c in containers):
                category = 'Plug-in'
            elif app_id.startswith('com.apple.'):
                category = 'Built-in Apple App'

        return (
            row.get('display_name') or app_id,
            app_id,
            shared_folder,
            data_folder,
            ', '.join(plugins),
            row.get('total_bytes'),
            row.get('media_file_count', 0),
            row.get('data_created_utc') or '',
            row.get('shared_created_utc') or '',
            row.get('preferences_modified_utc') or '',
            row.get('splash_snapshot_modified_utc') or '',
            row.get('score', ''),
            'Yes' if row.get('has_parser') else 'No',
            category,
        )

    def _populate_apps_table(self, rows: list) -> None:
        registry_by_bundle, plugins_by_bundle = self._build_app_registry_lookup(
            [r.get('app_id', '') for r in rows])
        flat = [self._flatten_app_intelligence_row(r, registry_by_bundle, plugins_by_bundle)
               for r in rows]
        # No parser module/record_source for this table at all — reset
        # explicitly rather than leaving whatever a previously-viewed
        # Report left behind (see _art_load_record_hex: an empty
        # _art_current_record_sources list is what makes it show "Record
        # location not available" instead of trying to resolve a stale
        # module's rowid_fields against a row shape that was never built
        # for it).
        self._art_current_mod = None
        self._art_current_script = None
        self._art_current_record_sources = []
        self._art_record_source_sticky = {}
        self._hex_record_source_combo.setVisible(False)
        self._art_table_model.load_rows(self._APPS_COLUMNS, flat)
        self._art_table_model.set_byte_columns(["Total Size"])
        for col in range(self._art_table_model.columnCount()):
            self._art_report_view.setItemDelegateForColumn(col, self._art_highlight_delegate)
        self._art_report_view.verticalHeader().setDefaultSectionSize(80)
        self._setup_report_filter_ui(self._APPS_COLUMNS)
        self._art_showing_apps = True
        for w in (self._art_category_filter_btn, self._art_date_filter_enable,
                 self._art_date_filter_combo, self._art_date_filter_from,
                 self._art_date_filter_to):
            w.setVisible(True)
        # Default view (added 2026-08-26, per direct user request): start
        # with '(blank)' and 'Plug-in' unchecked rather than "All" — this is
        # the actual fix for the original complaint that a flat ~1000-row
        # table full of empty PluginKit extensions/system daemons was
        # unusable for triage. Nothing is hidden permanently — it's just
        # this filter's OWN starting selection, exactly like checking those
        # two boxes off by hand; still one click away via the Category menu.
        self._set_art_category_filter(self._all_art_category_values() - {'', 'Plug-in'})
        self._art_resize_columns()
        self._art_stack.setCurrentIndex(1)

    def _retire_art_apps_worker(self):
        worker = getattr(self, '_art_apps_worker', None)
        if worker is not None:
            self._retire_worker(worker)
            self._art_apps_worker = None

    def _art_show_apps(self):
        """Apps tree node: the same per-app inventory the MCP list_apps
        tool returns (app_intelligence.scan_apps), flattened into the
        Report table widget — every app gathered for this case, not just
        ones with a parser. Shares casecache.db's app_intelligence table/
        staleness key with list_apps, so a scan triggered from here and a
        later (or earlier) AI-access list_apps call don't redundantly
        re-walk the archive — see AppIntelligenceWorker's own docstring."""
        self._retire_art_apps_worker()
        if not self._case_dir:
            return
        from mcp_server import CaseContext
        from db_utils import _open_cache_db, load_app_intelligence, load_blob

        ui_metadata = self.full_metadata
        # Unlike the AI-access consent toggle, there is no external-client
        # boundary here — the GUI already reads raw archive bytes freely
        # everywhere else (Hex viewer, SQLite viewer). raw_content_enabled
        # only affects the CACHE KEY (below), so a stricter, consent-gated
        # MCP scan and this always-True GUI scan never silently share a
        # result across that boundary — whichever ran last just wins under
        # its own key, and the other side's next call detects the mismatch
        # and rescans on its own terms.
        ctx = CaseContext(
            case_dir=self._case_dir,
            zip_path=self.zip_path or '',
            get_ui_metadata=lambda: self.full_metadata,
            get_folder_map=lambda: self.folder_map,
            get_folder_sizes=lambda: self._folder_sizes,
            get_guid_to_bundle=lambda: self.guid_to_bundle,
            get_header_types=lambda: self._header_type_overrides,
            adapter=getattr(self, '_adapter', None),
            raw_content_enabled=True,
            read_bytes=lambda p: self._read_zip_bytes(p),
        )
        import app_intelligence
        cache_key = f'{len(ui_metadata)}:{True}:{app_intelligence.scan_logic_version()}'

        try:
            with closing(_open_cache_db(self._case_dir)) as cache_db:
                stale = load_blob(cache_db, 'app_intelligence_scan_key', '1')
                cached_rows = load_app_intelligence(cache_db)
        except Exception:
            stale, cached_rows = None, []

        if cached_rows and stale is not None and stale.decode() == cache_key:
            self._populate_apps_table(cached_rows)
            self.status_bar.showMessage(f"Apps — {len(cached_rows):,} app(s), cached scan")
            return

        # Miss/stale: scan on a background thread — a first pass over a
        # large case can take ~1 minute (see CLAUDE.md's mcp_server.py
        # entry), must not block the GUI thread. Must ALSO switch the
        # stack to the report page here, not just on completion — found by
        # direct testing (2026-08-25): _art_row_label lives on the report
        # page (stack index 1), so leaving the stack on the index-0
        # placeholder page during a slow scan meant the "Scanning apps…"
        # text was updated but never actually visible, reading as "nothing
        # happened" for however long the scan took.
        self._art_table_model.clear()
        self._setup_report_filter_ui(self._APPS_COLUMNS)
        self._art_row_label.setText("Scanning apps…")
        self._art_stack.setCurrentIndex(1)
        self.status_bar.showMessage("Scanning apps — this can take a while on a large case…")
        worker = AppIntelligenceWorker(ctx, cache_key)
        worker.done.connect(self._on_art_apps_scan_done)
        self._art_apps_worker = worker
        worker.start()

    def _on_art_apps_scan_done(self, rows: list, error: str) -> None:
        self._art_apps_worker = None
        if error:
            self.status_bar.showMessage(f"App scan failed: {error}")
            self._art_row_label.setText("Scan failed")
            return
        self._populate_apps_table(rows)
        self.status_bar.showMessage(f"Apps — {len(rows):,} app(s)")

    def _open_artifact_runner(self):
        if not self.zip_path:
            return
        if not self._case_dir:
            QMessageBox.information(self, "No Case Folder",
                                    "A case folder is required to store results.\n"
                                    "Use File → Process Case… to set one first.")
            return
        dlg = ArtifactRunnerDialog(
            zip_path=self.zip_path,
            zip_names=self.zip_names,
            adapter=self._adapter,
            case_dir=self._case_dir,
            is_android=self._is_android_archive(),
            parent=self,
            guid_to_bundle=self.guid_to_bundle,
        )
        dlg.parsers_completed.connect(self._refresh_artifact_tab)
        dlg.parsers_completed.connect(self._on_photo_index_changed)
        dlg.exec()

    def _open_ai_summary_dialog(self) -> None:
        """Entry point for the tree's "AI Summary" node (_ART_AI_SUMMARY).
        Builds the list of already-completed reports the same way
        _refresh_artifact_tab enumerates them, then hands off to
        AISummaryDialog for everything else — column selection, chunk
        size/time-gap thresholds, prompt editing, connection settings, and
        running it, all backed by ai_summary.py/ai_summary_store.py (the
        same modules the MCP tools use)."""
        if not self._case_dir:
            QMessageBox.information(self, "No Case Folder",
                                    "A case folder is required for AI Summary.\n"
                                    "Use File → Process Case… to set one first.")
            return
        from artifact_db import list_completed_artifacts
        from artifact_runner import list_artifacts
        platform = 'android' if self._is_android_archive() else 'ios'
        try:
            with closing(_open_results_db(self._case_dir)) as case_conn:
                completed = list_completed_artifacts(case_conn)
        except Exception:
            completed = []
        modules = {sn: mod for sn, mod in list_artifacts(platform)}
        reports = sorted(
            ((sn, getattr(modules.get(sn), 'name', sn)) for sn in completed),
            key=lambda t: t[1].lower(),
        )
        dlg = AISummaryDialog(self._case_dir, platform, reports, parent=self)
        dlg.exec()

    # ── Photos.sqlite quick-process offer ─────────────────────────────────────

    @staticmethod
    def _is_ios_media_path(ui_path: str) -> bool:
        """True for the iOS media root or any subfolder, for both Cellebrite
        ('mobile/Media/…') and GrayKey ('private/var/mobile/Media/…')."""
        return ui_path.endswith('mobile/Media') or 'mobile/Media/' in ui_path

    def _photos_db_present(self) -> bool:
        cands = self._adapter.user_candidates(
            'mobile/Media/PhotoData/Photos.sqlite')
        return any(c in getattr(self, 'zip_names', ()) for c in cands)

    def _maybe_offer_photos_processing(self, folder_path: str):
        """When the user first opens an iOS Media folder, offer to run the
        Photos.sqlite parser so the photo columns become available."""
        if getattr(self, '_photos_prompt_shown', False):
            return
        if not folder_path or self._is_android_archive():
            return
        if not self._is_ios_media_path(folder_path):
            return
        if self._photo_index or not self._case_dir:
            return   # already processed, or nowhere to store results
        if not self._photos_db_present():
            return
        self._photos_prompt_shown = True
        ans = QMessageBox.question(
            self, "Process Photo Data",
            "This device's photo library (Photos.sqlite) has not been "
            "processed yet.\n\n"
            "Processing it links each photo to its album, original filename, "
            "creating app, date taken, location and detected faces — shown as "
            "extra columns in the file browser.\n\n"
            "Process the photo metadata now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._run_photos_parser()

    def _run_photos_parser(self):
        from artifact_runner import list_artifacts
        selected = [(sn, m) for sn, m in list_artifacts('ios')
                    if sn == 'photos_metadata']
        if not selected:
            return
        prog = QProgressDialog("Processing Photos.sqlite…", None, 0, 0, self)
        prog.setWindowTitle("Photo Metadata")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setCancelButton(None)
        prog.show()
        self._photos_worker = ArtifactRunnerWorker(
            selected, self.zip_path, self._adapter,
            self._case_dir, 'ios')

        def _done():
            prog.close()
            self._on_photo_index_changed()
            self._refresh_artifact_tab()
            n = len(self._photo_index)
            self.status_bar.showMessage(
                f"Photo metadata processed — {n:,} photos linked", 6000)

        self._photos_worker.done.connect(_done)
        self._photos_worker.start()
