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
    QProgressDialog, QMenu, QDateEdit,
)
from PySide6.QtCore import Qt, QThread, Signal, QAbstractTableModel, QModelIndex, QDate
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont

from db_utils import _open_results_db, start_run_log, complete_run_log, load_last_run
from highlight_delegate import HighlightDelegate
from artifact_media import MediaThumbnailDelegate, MediaFullViewDialog, THUMB_CELL_SIZE
from dialog_helpers import note_label, ERROR_STYLE, WARNING_COLOR
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
_ART_APP_NOTES = "__art_app_notes__"  # singleton — Apps node's own notes, not per-parser
_ART_GROUP  = "__art_group__:"   # app-name node — clicking it shows the Report
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
                return any(
                    _exists(adapter.user_candidates(f"{app_base}/{sub.lstrip('/')}"))
                    for sub in mod.files.values()
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
        self._art_media_delegate = MediaThumbnailDelegate(self._art_report_view)
        self._art_media_thumb_worker = None
        self._art_apps_worker = None
        self._art_showing_apps = False
        self._art_hex_active = False
        self._art_current_mod = None
        self._art_current_script: str | None = None
        self._art_current_platform: str | None = None
        self._art_current_record_sources: list = []
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

        self._art_stack = QStackedWidget()
        self._art_stack.addWidget(self._art_placeholder)  # 0
        self._art_stack.addWidget(report_page)             # 1
        self._art_stack.addWidget(self._art_script_view)  # 2
        self._art_stack.addWidget(_notes_scroll)           # 3
        self._art_stack.addWidget(validation_page)          # 4

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
        self._retire_art_apps_worker()
        self._clear_art_hex()
        self._art_current_mod = None
        self._art_current_record_sources = []
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

        for script_name in completed:
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
            apps_item.appendRow(group)

        self._art_tree_model.invisibleRootItem().appendRow(apps_item)
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
        method). Apps is always the sole top-level item when present, so
        no role-value search is needed to find it."""
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
        mode keeps showing that same mode for each new row."""
        if not current.isValid():
            self._clear_art_hex()
            return
        row = self._art_table_model.row_dict(current.row())
        if self._hex_source_is_record():
            self._art_load_record_hex(row)
        else:
            self._art_load_attachment_hex(row)

    def _art_load_attachment_hex(self, row: dict) -> None:
        """Attachment hex mode: the row's first non-empty media_fields
        value, same resolution _on_art_report_double_clicked uses for the
        specific cell clicked."""
        media_fields = getattr(self._art_current_mod, 'media_fields', ()) \
            if self._art_current_mod else ()
        ui_path = ''
        for field in media_fields:
            val = row.get(field)
            if val:
                ui_path = val
                break
        if not ui_path:
            self._show_art_hex_message("No attachment on this row")
            return
        data = self._read_zip_bytes(ui_path)
        if data is None:
            self._show_art_hex_message(f"Attachment not found in archive: {ui_path}")
            return
        self._load_hex_preview_from_bytes(data, ui_path)
        self._art_hex_active = True
        self.status_bar.showMessage(ui_path)

    def _art_load_record_hex(self, row: dict) -> None:
        """Record hex mode: jump to this row's own on-disk database cell
        in its source db file — see the parser module's `record_source`
        declaration (a list of entries, one per DB row a joined report's
        own rows are actually built from — see the combo populated in
        _art_show_report) and sqlite_carve.locate_live_row. Never guesses:
        a missing declaration, a row with no rowid/table data, an
        unresolvable source file, or a rowid not found in the CURRENT live
        b-tree (may be WAL-only and not yet checkpointed, or genuinely
        deleted — recover_deleted_rows' job, not this one's) shows a
        specific explanatory message rather than silently doing nothing or
        showing the wrong bytes."""
        mod     = self._art_current_mod
        entries = self._art_current_record_sources
        if not entries:
            self._show_art_hex_message("Record location not available for this parser yet")
            return

        from artifact_runner import resolve_module_file_ui_path

        # A recovered/carved row already has its exact on-disk location
        # from the carving pass itself (sqlite_carve.recover_deleted_rows
        # — raw_offset/raw_length/raw_file) — no live b-tree search needed,
        # and none would find it anyway: a carved row is by definition not
        # in the live b-tree locate_live_row walks. Resolve file_key from
        # whichever record_source entry matches where this row was
        # actually recovered FROM (row['source_table']), ignoring the
        # combo selection — a carved row's location doesn't depend on
        # which joined table the examiner happens to have picked.
        if row.get('recovered') and row.get('raw_offset') is not None:
            source_table = row.get('source_table')
            rs = next(
                (e for e in entries if
                 (e['table'] if 'table' in e else row.get(e.get('table_field', ''))) == source_table),
                entries[0])
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
            offset = row['raw_offset']
            length = row.get('raw_length') or 0
            self._load_hex_preview_from_bytes_at(data, ui_path, offset, length)
            self._art_hex_active = True
            self.status_bar.showMessage(
                f"{ui_path}  —  offset: {offset:,}  (recovered: {row.get('recovery_method', '?')})")
            return

        if len(entries) == 1:
            rs = entries[0]
        else:
            rs = self._hex_record_source_combo.currentData() or entries[0]
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
        elif role_val == _ART_APP_NOTES:
            self._art_show_app_notes()
        elif role_val.startswith(_ART_GROUP):
            self._art_show_report(role_val[len(_ART_GROUP):])
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

    def _setup_report_filter_ui(self, columns: list, hidden: set = frozenset()) -> None:
        """Reset filter controls for a newly loaded report. *hidden* (a
        parser's own hidden_fields declaration) is skipped in the dropdown
        — an internal plumbing field isn't something an examiner can
        usefully filter by — but each surviving item still carries its
        REAL index into *columns* as Qt item data, since that's the index
        space ArtifactFilterWorker/filter_rows_inmem actually key off
        (unaffected by which columns are merely hidden from display)."""
        self._cancel_art_filter_worker()
        self._art_active_filter = ''
        self._art_filter_input.clear()
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
        # table_field/table, rowid_fields} entries, one per DB row this
        # report's own rows are actually built from — a joined report has
        # more than one (its own table plus whatever it LEFT JOINed in).
        # The combo is only shown/meaningful with >1 entry; a single entry
        # needs no picker, and the record_source declaration itself may
        # just be one dict rather than a list-of-one in that case.
        raw_rs = getattr(mod, 'record_source', None) if mod else None
        if isinstance(raw_rs, dict):
            record_sources = [raw_rs]
        elif raw_rs:
            record_sources = list(raw_rs)
        else:
            record_sources = []
        self._art_current_record_sources = record_sources
        self._hex_record_source_combo.blockSignals(True)
        self._hex_record_source_combo.clear()
        for entry in record_sources:
            self._hex_record_source_combo.addItem(entry.get('label', '?'), entry)
        self._hex_record_source_combo.blockSignals(False)
        self._hex_record_source_combo.setVisible(len(record_sources) > 1)

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
        self._start_art_media_thumbnails(list(media_paths))

        self._setup_report_filter_ui(cols, hidden_fields)
        self._art_resize_columns()
        self._art_row_label.setText(f"{len(ids):,} rows")
        self._art_stack.setCurrentIndex(1)

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

    def _on_art_report_double_clicked(self, index):
        """Open the full-size image / video-player dialog for a
        media-column cell, and switch the Hex panel's toggle to
        "Attachment" (staying there for subsequent row clicks, same as any
        other toggle change — see _on_art_hex_source_toggled) so what's
        shown matches what the dialog just opened, rather than leaving a
        stale "Record" hex view underneath it. No-op for any non-media
        column."""
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
        MediaFullViewDialog(ui_path, data, parent=self).exec()

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
