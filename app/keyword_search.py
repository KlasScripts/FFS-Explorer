"""keyword_search.py — keyword search worker, dialogs, and FastZipBrowser mixin."""

import os
import sqlite3
import threading
import time
import zipfile
from contextlib import closing
from itertools import batched

import msgpack

from adapters import FfsAdapter
from db_utils import (_open_cache_db, _open_results_db, OldSchemaError, save_blob, load_blob,
                      load_bookmark_groups, load_bookmark_entries,
                      save_search_scope_files, load_search_scope_files,
                      save_evidence_page_map, load_evidence_page_map,
                      load_case_setting)
from highlight_delegate import HighlightDelegate
from zip_cd_cache import CachedZipView, load as _zcd_load, compute_data_offsets as _compute_data_offsets
from zip_entry import ZipEntry
from zip_reader import ZipReader, read_nested_entry

_SEARCH_ENTRIES_VERSION = '1'

# Cellebrite internal metadata entries — excluded from "all files" searches.
_CELLEBRITE_META_PREFIXES = ('metadata1/', 'metadata2/')


def _make_patterns(keyword: str) -> list:
    """Return [(lowercase_pattern, pattern_len, encoding), …] for a keyword."""
    patterns = []
    for enc in ('utf-8', 'utf-16-le', 'utf-16-be'):
        pat = keyword.encode(enc, errors='replace')
        patterns.append((pat.lower(), len(pat), enc))
    return patterns


def _iter_pattern_hits(data_lower: bytes, patterns: list):
    """Yield (idx, pat_len, enc) for every pattern occurrence in data_lower."""
    for pat, pat_len, enc in patterns:
        idx = 0
        while True:
            idx = data_lower.find(pat, idx)
            if idx == -1:
                break
            yield idx, pat_len, enc
            idx += pat_len

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
    QVBoxLayout, QHBoxLayout, QTreeView, QTableWidget, QTableWidgetItem,
    QDialog, QProgressBar, QMessageBox,
    QPlainTextEdit, QMenu,
    QHeaderView,
)
from PySide6.QtGui import (
    QStandardItemModel, QStandardItem, QFontDatabase, QColor,
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer

# Role holding (script_name, report_rowid) on the "Jump to this row in the
# report" child item _on_sql_hit_interpreted appends for a 'report'-kind
# result -- read back by _on_search_results_double_clicked to jump the
# Artifact Viewer straight to that exact row (see
# ArtifactViewerMixin._art_jump_to_report_row). Distinct from the
# UserRole..UserRole+4 roles a HIT row itself carries (path/offset/etc,
# defined locally in _perform_search) -- this is a different item several
# levels deeper in the tree, so there's no real collision risk either way,
# but a separate, named constant keeps the two readable independently.
_REPORT_JUMP_ROLE = Qt.ItemDataRole.UserRole + 10


# ── Shared zip-entry scanner ─────────────────────────────────────────────────

def _build_zip_entries(zip_path: str, stop,
                       case_dir: str | None = None,
                       delta: int | None = None) -> list:
    """Return list of (name, data_offset, file_size) for all STORED entries.
    *stop* is a threading.Event; set it to abort early.

    Deliberately NO raw-zipfile fallback on the main archive — per this
    project's own standing Convention and direct instruction, this never
    reads the main archive any other way than through the local .zcd
    sidecar. Returns empty (search finds nothing) rather than a network
    central-directory read when .zcd genuinely isn't available — which,
    for every real caller of this function, means the case hasn't
    finished loading yet, since .zcd creation is the very first step of
    that load; a keyword search can't run before then regardless."""
    entries = []
    if not case_dir:
        return entries
    try:
        infolist = _zcd_load(zip_path, case_dir)
    except Exception:
        infolist = None
    if infolist is None:
        return entries

    stored = [
        info for info in infolist
        if info.compress_type == zipfile.ZIP_STORED and info.file_size > 0
    ]
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

    def __init__(self, zip_path: str,
                 case_dir: str | None = None, delta: int | None = None,
                 parent=None):
        super().__init__(parent)
        self.zip_path        = zip_path
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

        entries = _build_zip_entries(self.zip_path, self._stop,
                                     case_dir=self.case_dir, delta=self.delta)
        if self._stop.is_set():
            return

        if self.case_dir:
            self._save_to_db(entries)

        self.entries_ready.emit(entries)

    def _load_from_db(self) -> list | None:
        try:
            with closing(_open_cache_db(self.case_dir)) as db:
                raw = load_blob(db, 'search_entries', _SEARCH_ENTRIES_VERSION)
            if raw is None:
                return None
            rows = msgpack.unpackb(raw, raw=False)
            return [tuple(r) for r in rows] if rows else None
        except Exception:
            return None

    def _save_to_db(self, entries: list) -> None:
        try:
            raw = msgpack.packb(entries, use_bin_type=True)
            with closing(_open_cache_db(self.case_dir)) as db:
                save_blob(db, 'search_entries', _SEARCH_ENTRIES_VERSION, raw)
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
                 entries=None, scope="all",
                 exclude_prefixes: tuple = (), case_dir: str | None = None,
                 delta: int | None = None, parent=None):
        super().__init__(parent)
        self.zip_path          = zip_path
        self.keyword           = keyword.encode('utf-8', errors='replace')
        self._stop             = threading.Event()
        self._prebuilt_entries = entries
        self._scope            = scope
        self._exclude_prefixes = exclude_prefixes
        # Only ever actually used when *entries* is None — the first
        # search of a session, before self._search_entries has been
        # cached (see _resolve_search_scope). Previously missing
        # entirely, which meant that first search always fell through to
        # _build_zip_entries' own (now removed) raw-zipfile fallback,
        # silently re-reading the WHOLE central directory over the
        # network a second time despite .zcd already existing by the
        # time any search can run at all — the exact cost the .zcd
        # sidecar exists to eliminate, on the single most common path in
        # the app. Threaded through here to fix that at the source.
        self._case_dir         = case_dir
        self._delta            = delta
        self.entries: list     = []
        self._patterns: list   = _make_patterns(keyword)

    def stop(self):
        self._stop.set()

    def _build_entries(self) -> list:
        return _build_zip_entries(self.zip_path, self._stop,
                                  case_dir=self._case_dir, delta=self._delta)

    def run(self):
        if self._prebuilt_entries is not None:
            entries = self._prebuilt_entries
        else:
            self.status_update.emit("Preparing search index…")
            full_entries = self._build_entries()
            self.entries = full_entries
            if self._scope == "app_data":
                entries = [e for e in full_entries if "mobile/Containers" in e[0] or "data/data" in e[0]]
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
        reader   = ZipReader(self.zip_path)

        def _decode_ctx(data: bytes, enc: str) -> str:
            if enc == 'utf-8':
                return data.decode('utf-8', errors='replace')
            if len(data) % 2:
                data = data[1:]
            return data.decode(enc, errors='replace')

        def search_entry(entry):
            _name, data_offset, file_size = entry
            if self._stop.is_set():
                return []
            results  = []
            buf_start = 0
            leftover  = b''
            try:
                for raw in reader.read_chunked_at(data_offset, file_size,
                                                  chunk_size=self._CHUNK,
                                                  stop_fn=self._stop.is_set):
                    block       = leftover + raw
                    block_lower = block.lower()
                    block_base  = buf_start
                    for idx, pat_len, enc in _iter_pattern_hits(block_lower, patterns):
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
                    leftover  = block[-overlap:] if overlap > 0 else b''
                    buf_start = block_base + len(block) - len(leftover)
            except OSError:
                pass
            return results

        # Throttle progress: one queued signal per file means hundreds of
        # thousands of GUI label repaints on a full-archive search — the main
        # thread ends up busier than the search itself.
        _last_prog = 0.0
        for entry, entry_results in reader.run_parallel(
                entries, search_entry, cancel_check=self._stop.is_set):
            done += 1
            now = time.monotonic()
            if now - _last_prog >= 0.1 or done == total:
                _last_prog = now
                self.progress.emit(done, total)
            try:
                name = entry[0]
                for file_offset, context in entry_results:
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
        self._patterns: list = _make_patterns(keyword)

    def stop(self):
        self._stop.set()

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
        _last_prog = 0.0

        def _report():
            nonlocal _last_prog
            now = time.monotonic()
            if now - _last_prog >= 0.1 or done == total:
                _last_prog = now
                self.progress.emit(done, total)

        for archive_ui_path, stored_path, entry_path in all_entries:
            if self._stop.is_set():
                break
            data = read_nested_entry(stored_path, entry_path)
            if data is None:
                done += 1
                _report()
                continue

            data_lower = data.lower()
            vpath = f"{archive_ui_path}/{entry_path}"
            for idx, pat_len, enc in _iter_pattern_hits(data_lower, self._patterns):
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
            done += 1
            _report()

        self.finished.emit(hits, done, total)


class LevelDbSearchWorker(QThread):
    """Search casecache.db's leveldb_search_index table (built by
    FastZipBrowser._index_leveldb_folders_batched, via
    leveldb_viewer.index_leveldb_folder_for_search) for a keyword — a
    THIRD, parallel search pass alongside KeywordSearchWorker (the real
    archive's own physical bytes) and NestedArchiveSearchWorker (extracted
    nested archives), added 2026-09-19 specifically because Keyword
    Search otherwise has zero visibility into decoded LevelDB/IndexedDB
    content — confirmed directly by reading this file before building
    anything, not assumed: nothing here ever referenced folder_map/
    _nested_virtual_paths/_leveldb_folder_map. A real deserialized
    IndexedDB value, or a Chrome DOM-Storage value re-encoded from
    UTF-16LE for display, is frequently not a literal byte substring of
    the raw archive file at all — the main archive-wide search can never
    find it no matter how thoroughly it scans.

    A pure local SQLite read — no zip/network I/O at all — so unlike
    LevelDB folder INDEXING itself (see FastZipBrowser.
    _index_leveldb_folders_batched's own docstring for why THAT
    deliberately runs frame-budgeted on the main thread instead of a
    worker), running the actual SEARCH QUERY on a background QThread is
    safe and unremarkable here: a fresh sqlite3 connection, never
    touching the GUI's own shared zip reader.

    result_found carries (folder_ui_path, record_index, offset_in_text,
    context, display_name, content_type) — enough for
    _on_leveldb_search_result to both render the hit (folder/
    display_name/context, the exact same tree/columns an ordinary hit
    already uses) and, later, resolve it back to the exact real record
    for the "Show Record in File Browser" context-menu action —
    folder_ui_path + record_index is the same stable identifier
    _leveldb_folder_map's own 'record_vpaths' list is already keyed by,
    so no second identifier scheme was needed."""

    result_found = Signal(str, int, int, str, str, str)
    # (folder_ui_path, record_index, offset_in_text, context, display_name, content_type)
    progress = Signal(int, int)
    finished = Signal(int)   # total hits

    _CTX_CHARS = 40

    def __init__(self, case_dir: str, keyword: str, path_prefix: str | None = None, parent=None):
        super().__init__(parent)
        self._case_dir   = case_dir
        self._keyword    = keyword
        self._path_prefix = path_prefix   # e.g. 'data/data/' for the App Data scope
        self._stop       = threading.Event()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _escape_like(term: str) -> str:
        """Escape SQL LIKE wildcards in a user-typed search term — a
        literal '%'/'_' the examiner actually searched for must match
        itself, never act as a wildcard (confirmed necessary: this
        project's own real Local Storage records include keys like
        'Mon, 29 Jan 2024 21:10:23 GMT'-style values where a literal
        search would otherwise silently over-match)."""
        return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    def run(self):
        if not self._case_dir or not self._keyword:
            self.finished.emit(0)
            return
        try:
            db = _open_cache_db(self._case_dir)
        except Exception:
            self.finished.emit(0)
            return
        try:
            pattern = f'%{self._escape_like(self._keyword)}%'
            if self._path_prefix:
                rows = db.execute(
                    "SELECT folder_ui_path, record_index, display_name, searchable_text, content_type "
                    "FROM leveldb_search_index "
                    "WHERE record_index >= 0 AND searchable_text LIKE ? ESCAPE '\\' "
                    "AND folder_ui_path LIKE ?",
                    (pattern, self._escape_like(self._path_prefix) + '%'),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT folder_ui_path, record_index, display_name, searchable_text, content_type "
                    "FROM leveldb_search_index "
                    "WHERE record_index >= 0 AND searchable_text LIKE ? ESCAPE '\\'",
                    (pattern,),
                ).fetchall()
        except Exception:
            rows = []
        finally:
            db.close()

        total = len(rows)
        self.progress.emit(0, total)
        term_lower = self._keyword.lower()
        term_len   = len(self._keyword)
        hits = 0
        for done, (folder_ui_path, record_index, display_name, text, content_type) in enumerate(rows, 1):
            if self._stop.is_set():
                break
            text_lower = text.lower()
            start = 0
            while True:
                idx = text_lower.find(term_lower, start)
                if idx == -1:
                    break
                ctx_start = max(0, idx - self._CTX_CHARS)
                ctx_end   = min(len(text), idx + term_len + self._CTX_CHARS)
                context = (text[ctx_start:idx] + '[' + text[idx:idx + term_len] + ']'
                           + text[idx + term_len:ctx_end])
                hits += 1
                self.result_found.emit(folder_ui_path, record_index, idx, context,
                                       display_name, content_type or '')
                start = idx + term_len
            if done % 50 == 0 or done == total:
                self.progress.emit(done, total)
        self.finished.emit(hits)


# ── Bulk SQL/WAL hit discovery ────────────────────────────────────────────────
# See TODO.md item 1 for the full three-constraint design this implements:
# after a keyword search finishes, offer to bulk-run "Interpret as SQL
# Record" over every hit whose own file is a real SQLite/WAL file — never
# eagerly for a hit the examiner hasn't asked about, and never at the cost
# of reading real bytes off every hit file just to build the offer.

# Deliberately a small, LOCAL copy of ffs-explorer.py's own DATABASE_
# EXTENSIONS/WAL-suffix check, not an import from it — app/ modules never
# import from the top-level script, per this project's own standing
# convention (see ffs-explorer.py's Conventions section).
_SQL_DB_EXTENSIONS = {'.db', '.sqlite', '.sqlite3', '.db3'}


def _looks_like_sqlite_by_name(name: str) -> bool:
    """Cheap, free (no I/O) check: does this filename's own extension
    already suggest a SQLite base file or WAL/SHM sidecar? The first,
    free layer of the three this feature's discovery pass tries in
    order — extension, then this case's own already-computed header_
    types cache, then (only if still genuinely unresolved) a real header
    byte read. Most real hit files carry a self-describing extension,
    same reasoning as app_intelligence.find_evidence_databases's own
    "Row-merge + magic-byte fallback" this mirrors."""
    lower = name.lower()
    if lower.endswith(('-wal', '-shm')):
        return True
    return os.path.splitext(lower)[1] in _SQL_DB_EXTENSIONS


class SqlHitDiscoveryWorker(QThread):
    """Background pass, run once after a keyword search finishes: for
    each DISTINCT hit file (never per-hit — many hits commonly land in
    the same file), determine whether it's a SQLite base file or WAL
    sidecar. Extension-first (free) → this case's own already-loaded
    header_types cache (free, main-archive hits only — a nested-archive
    entry has no ui_path in that cache's own space) → a real header
    byte-peek ONLY for a file still genuinely unresolved after both.

    A magic-byte check here is a classification heuristic, not an
    integrity guarantee — a deliberately altered/corrupted header could
    make a real SQLite/WAL file silently NOT count toward the offer this
    feeds; stated here, not just in TODO.md, since this is the one place
    that limitation actually matters. The reverse (a forged header
    falsely counting a non-database file) is lower-stakes — it only
    costs one interpretation attempt, which already fails cleanly as
    `not_sqlite`.

    *files* is a list of dicts: {'key', 'name', 'physical', 'stored_path',
    'entry_path'} — one per distinct hit file, 'key' matching
    FastZipBrowser._search_file_items' own dict key. Emits done({key:
    bool}) — True means "interpret this file's hits.\""""
    done = Signal(dict)

    def __init__(self, zip_path: str, case_dir: str | None,
                 header_type_overrides: dict, files: list[dict], parent=None):
        super().__init__(parent)
        self._zip_path = zip_path
        self._case_dir = case_dir
        self._header_type_overrides = header_type_overrides
        self._files = files

    def run(self):
        results: dict = {}
        # Deliberately NO raw-zipfile fallback here, per this project's
        # own standing Convention ("Never read the MAIN FFS archive via
        # raw zipfile.ZipFile(...)/.read(name) directly" — neither of
        # that rule's two narrow exceptions applies to this worker) and
        # direct instruction. A main-archive byte-peek simply doesn't run
        # (that file's own extension/cache result stands) when the local
        # .zcd isn't available — z stays None and _peek_header's own
        # `if z is None: return None` already handles that path. This
        # never actually costs real coverage in practice: a keyword
        # search — the only thing that ever constructs this worker — can
        # only run once the case has fully loaded, and .zcd creation is
        # the very first step of that load (ZipMetadataWorker.run(),
        # before metadata_ready even fires), so by the time this worker
        # exists at all the .zcd is already guaranteed to exist.
        infos = _zcd_load(self._zip_path, self._case_dir) if self._case_dir else None
        z = CachedZipView(self._zip_path, infos) if infos is not None else None
        reader = ZipReader(self._zip_path)
        for f in self._files:
            try:
                results[f['key']] = self._resolve_one(f, z, reader)
            except Exception:
                results[f['key']] = False
        self.done.emit(results)

    def _resolve_one(self, f: dict, z, reader: ZipReader) -> bool:
        if _looks_like_sqlite_by_name(f['name']):
            return True
        if not f['stored_path']:
            cached = self._header_type_overrides.get(f['physical'])
            if cached == 'Database':
                return True
            if cached is not None:
                # Confidently something else already (e.g. this case's own
                # header scan already classified it as 'Picture') — no
                # need to spend a byte read confirming a negative.
                return False
        header = self._peek_header(f, z, reader)
        if not header:
            return False
        return (header[:16] == b'SQLite format 3\x00' or
                header[:4] in (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83'))

    def _peek_header(self, f: dict, z, reader: ZipReader) -> bytes | None:
        if f['stored_path'] and f['entry_path']:
            # Nested archive: always an already-extracted local file (see
            # nested_archive.py's own Conventions entry) — no zip_cd_cache
            # offset applies here at all; read_nested_entry has no
            # partial-read option, so this reads the whole entry. Nested
            # entries are typically small local files, unlike the
            # network-hosted-main-archive case the offset path below is
            # specifically there to avoid downloading in full.
            data = read_nested_entry(f['stored_path'], f['entry_path'])
            return data[:16] if data else None
        if z is None or not f['physical']:
            return None
        try:
            info = z.getinfo(f['physical'])
            offsets = _compute_data_offsets(self._zip_path, [info])
            data_offset = offsets.get(info.filename)
            if data_offset is None:
                return None
            return reader.read_at(data_offset, 16, max_bytes=16)
        except Exception:
            return None


class BulkSqlInterpretProgressDialog(QDialog):
    """Modal progress dialog for bulk "Interpret as SQL Record" — same
    visual convention as SearchProgressDialog above (label + QProgressBar
    + Cancel), not QProgressDialog, for consistency with this file's own
    existing dialog style. Cancelling stops queuing further hits — an
    already-running SqlHitInterpretWorker isn't interrupted mid-flight
    (that worker has no cancellation support today), it's just the last
    one started."""

    cancelled = Signal()

    def __init__(self, total: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Interpreting SQL/WAL Hits")
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self._label = QLabel(f"Interpreting hit 1 of {total:,}…" if total else "")
        layout.addWidget(self._label)
        self._bar = QProgressBar()
        self._bar.setRange(0, total)
        layout.addWidget(self._bar)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.cancelled.emit)
        btn_row.addWidget(self._cancel_btn)
        layout.addLayout(btn_row)

    def update_progress(self, done: int, total: int):
        self._bar.setValue(done)
        if done < total:
            self._label.setText(f"Interpreting hit {done + 1:,} of {total:,}…")
        else:
            self._label.setText("Finishing…")

    def mark_finished(self, total: int):
        self._bar.setValue(total)
        self._label.setText(
            f"Done — interpreted {total:,} hit{'s' if total != 1 else ''}.")
        self._cancel_btn.setText("Close")


# ── SqlHitInterpretWorker ─────────────────────────────────────────────────────

class SqlHitInterpretWorker(QThread):
    """Background computation for "Interpret as SQL Record" — a Keyword
    Search hit's file may be a SQLite database, in which case the hit's
    own byte offset can be attributed to a specific table/rowid (and,
    best-effort, column) via sqlite_carve.locate_offset, then further
    resolved to either an existing artifact report's own row (the
    common, most useful case) or a live-but-unsupported row shown with
    its real PRAGMA table_info column names. See CLAUDE.md's
    `locate_offset` Conventions entry for the byte-level mechanism this
    builds on, and TODO.md item 1 for the feature this implements.

    Never touches any GUI object — reads its own archive bytes fresh
    (same "workers own their own reader, never share the GUI's cached
    zip handle" convention KeywordSearchWorker/NestedArchiveSearchWorker
    already establish in this file) and opens caseresults.db via its own
    short-lived connection. Emits exactly one `finished(dict)` — never
    raises, any failure surfaces as a `{'kind': 'error', ...}` result
    rather than crashing the worker silently.
    """

    finished = Signal(dict)

    def __init__(self, zip_path: str, case_dir: str, offset: int,
                 physical: str | None, stored_path: str | None, entry_path: str | None,
                 hit_ui_path: str, guid_to_bundle: dict, adapter, zip_names,
                 platform: str, parent=None):
        super().__init__(parent)
        self.zip_path       = zip_path
        self.case_dir       = case_dir
        self.offset         = offset
        self.physical       = physical
        self.stored_path    = stored_path
        self.entry_path     = entry_path
        self.hit_ui_path    = hit_ui_path
        self.guid_to_bundle = guid_to_bundle
        self.adapter        = adapter
        self.zip_names      = zip_names
        self.platform       = platform

    def _read_raw_bytes(self, physical_override: str | None = None) -> bytes | None:
        """Read this hit's own file, or — when *physical_override* is
        given — an arbitrary OTHER main-archive path (used to read the
        sibling BASE `.db` file's bytes for a WAL hit, see run()'s own
        WAL branch below; a WAL sidecar's own schema-less content means
        the base file has to be read too, and that's always a plain
        main-archive path, never inside a nested archive — the same
        scoping `_interpret_search_hit_as_sql`'s own `hit_ui_path`
        derivation already applies)."""
        if physical_override is not None:
            physical = physical_override
        elif self.stored_path and self.entry_path:
            return read_nested_entry(self.stored_path, self.entry_path)
        else:
            physical = self.physical
        if not physical:
            return None
        try:
            # No raw-zipfile fallback on the main archive here either
            # (see SqlHitDiscoveryWorker.run()'s own comment for the
            # full reasoning) — a search hit can only exist once the
            # case has fully loaded, and .zcd creation is the very first
            # step of that load, so it's already guaranteed present by
            # the time any hit could be interpreted at all. `infos is
            # None` (case_dir unset, e.g. no case folder at all) falls
            # through to the existing `except Exception: return None`
            # below via CachedZipView(None) failing its own getinfo,
            # same honest "couldn't read it" outcome as every other
            # failure this method already handles this way.
            infos = _zcd_load(self.zip_path, self.case_dir) if self.case_dir else None
            zf = CachedZipView(self.zip_path, infos)
            zinfo = zf.getinfo(physical)
            entry = ZipEntry(self.zip_path, physical, zinfo)
            return entry.read()
        except Exception:
            return None

    def _find_report_match(self, table: str, rowid: int) -> dict | None:
        """First loaded parser whose SQL-backed record_source declares
        *table* against the SAME file this hit is in, whose report
        already has a row citing *rowid* — or None. Deliberately only
        matches a FIXED `table` entry (never a per-row `table_field`
        one — there's no live row here to read that field from), and
        stops at the first match rather than trying to rank several —
        a documented, narrow v1 scope, not an oversight."""
        from artifact_runner import list_artifacts, resolve_module_file_ui_path
        from artifact_db import list_completed_artifacts

        try:
            with closing(_open_results_db(self.case_dir)) as case_conn:
                completed = set(list_completed_artifacts(case_conn))
                for script_name, mod in list_artifacts(self.platform):
                    if script_name not in completed:
                        continue
                    raw_rs = getattr(mod, 'record_source', None)
                    if not raw_rs:
                        continue
                    entries = raw_rs if isinstance(raw_rs, list) else [raw_rs]
                    for entry in entries:
                        if 'table' not in entry or entry.get('table') != table:
                            continue
                        file_key = entry.get('file_key')
                        if not file_key:
                            continue
                        try:
                            ui_path = resolve_module_file_ui_path(
                                mod, file_key, self.guid_to_bundle,
                                adapter=self.adapter, zip_names=self.zip_names)
                        except Exception:
                            continue
                        if ui_path != self.hit_ui_path:
                            continue
                        rowid_fields = entry.get('rowid_fields') or []
                        if not rowid_fields:
                            continue
                        table_name = f"artifact_{script_name}"
                        for field in rowid_fields:
                            try:
                                # rowid AS "_report_rowid" -- the SAME
                                # implicit SQLite rowid ArtifactTableModel's
                                # own DB mode keys its rows by (see
                                # _art_show_report's `SELECT rowid FROM
                                # ... ORDER BY rowid`), captured here so a
                                # caller can jump the Artifact Viewer
                                # straight to this exact row instead of
                                # only naming the report (see
                                # ArtifactViewerMixin._art_jump_to_report_row).
                                # Selected as its own aliased column, not
                                # mixed into `row` below, so it never shows
                                # up as a spurious extra field in the
                                # rendered result.
                                cursor = case_conn.execute(
                                    f'SELECT rowid AS "_report_rowid", * FROM "{table_name}" '
                                    f'WHERE "{field}" = ?', (str(rowid),))
                                row = cursor.fetchone()
                            except Exception:
                                row = None
                            if row is not None:
                                cols = [d[0] for d in cursor.description]
                                row_dict = dict(zip(cols, row))
                                report_rowid = row_dict.pop('_report_rowid')
                                report_name = getattr(mod, 'name', script_name)
                                return {
                                    'kind':          'report',
                                    'script_name':   script_name,
                                    'report_name':   report_name,
                                    'row':           row_dict,
                                    'report_rowid':  report_rowid,
                                }
        except Exception:
            pass
        return None

    def _get_page_map(self, raw: bytes, ui_path: str | None = None):
        """The page-ownership map locate_offset/identify_structure both
        consult (see sqlite_carve.build_page_map's own docstring) —
        loaded from casecache.db's `evidence_page_map` table when a
        previous interpretation (this session or any earlier one, same
        case) already built it for this exact file, built and saved
        fresh on a miss. *ui_path* defaults to `self.hit_ui_path` (the
        hit's own file); a WAL hit passes the SIBLING BASE file's own
        ui_path instead, so its page map is cached/shared under the
        base file's own identity -- the natural key, and one a later
        ordinary (non-WAL) hit in that same base file benefits from too.
        Skipped entirely when no ui_path applies at all (a nested-archive
        hit — see _interpret_search_hit_as_sql's own comment on why a
        nested entry has no ui_path in the main archive's own space to
        key a cache entry by); build_page_map still runs, just without
        persistence, identical to every file's very first interpretation."""
        import sqlite_carve
        key = ui_path if ui_path is not None else self.hit_ui_path
        if not key or not self.case_dir:
            return sqlite_carve.build_page_map(raw)
        try:
            with closing(_open_cache_db(self.case_dir)) as cache_conn:
                cached = load_evidence_page_map(cache_conn, key)
                if cached is not None:
                    return cached
                page_map = sqlite_carve.build_page_map(raw)
                if page_map is not None:
                    save_evidence_page_map(cache_conn, key, page_map)
                return page_map
        except Exception:
            # Caching is a pure optimization -- any failure here (a
            # locked/corrupt casecache.db, an unexpected exception) must
            # never block the interpretation itself, only its speed.
            return sqlite_carve.build_page_map(raw)

    def _run_wal(self, wal_raw: bytes):
        """WAL-file counterpart of run()'s own base-file path — see
        sqlite_carve.locate_wal_offset/identify_wal_structure's own
        docstrings and CLAUDE.md's Conventions entry for why a WAL hit
        needs the sibling BASE file's own schema/page map rather than
        being self-sufficient the way a base-file hit is. Deliberately
        does NOT attempt _find_report_match for a WAL-sourced row (see
        TODO.md) — cross-referencing a row that may be historical or
        genuinely deleted against a report built from a LIVE query is a
        materially different, riskier claim than the base-file "live row
        covered by this report" case, so a WAL hit always reports as its
        own `wal_row` kind, never silently folded into `report`/`live`."""
        import os
        import struct
        import tempfile
        import sqlite_carve

        # Scoped to main-archive WAL hits only, matching hit_ui_path's
        # own scoping in _interpret_search_hit_as_sql -- a nested-archive
        # entry has no ui_path in the main archive's own space to derive
        # a sibling base file's identity from.
        if self.stored_path or not self.physical or not self.physical.endswith('-wal'):
            self.finished.emit({'kind': 'not_sqlite'})
            return

        base_physical = self.physical[:-len('-wal')]
        base_raw = self._read_raw_bytes(physical_override=base_physical)
        if base_raw is None or base_raw[:16] != b'SQLite format 3\x00':
            self.finished.emit({'kind': 'error',
                                'message': "Could not read this WAL file's sibling base database"})
            return

        try:
            wal_page_size = struct.unpack('>I', wal_raw[8:12])[0]
        except Exception:
            self.finished.emit({'kind': 'error', 'message': 'Malformed WAL header'})
            return

        try:
            base_header = sqlite_carve.parse_db_header(base_raw)
        except Exception:
            base_header = {}
        reserved_bytes = base_header.get('reserved_bytes', 0)

        base_ui_path = (self.hit_ui_path[:-len('-wal')]
                       if self.hit_ui_path and self.hit_ui_path.endswith('-wal') else None)
        base_page_map = self._get_page_map(base_raw, ui_path=base_ui_path)
        if base_page_map is None:
            self.finished.emit({'kind': 'error',
                                'message': "Could not read the base database's schema"})
            return

        fd, tmp_path = tempfile.mkstemp(suffix='.sqlite')
        base_conn = None
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(base_raw)
            base_conn = sqlite3.connect(f'file:{tmp_path}?mode=ro', uri=True, timeout=5)

            loc = sqlite_carve.locate_wal_offset(
                wal_raw, self.offset, wal_page_size, base_page_map, base_conn,
                reserved_bytes=reserved_bytes)
            if loc is None:
                structure = sqlite_carve.identify_wal_structure(
                    wal_raw, self.offset, wal_page_size, base_page_map, base_conn=base_conn)
                self.finished.emit({'kind': 'unresolved', 'structure': structure})
                return

            self.finished.emit({
                'kind':             'wal_row',
                'table':            loc['table'],
                'rowid':            loc['rowid'],
                'row':              loc.get('row_values') or {},
                'column_name':      loc.get('column_name'),
                'wal_frame_index':  loc.get('wal_frame_index'),
            })
        finally:
            if base_conn is not None:
                base_conn.close()
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def run(self):
        raw = self._read_raw_bytes()
        if raw is None:
            self.finished.emit({'kind': 'error', 'message': 'Could not read this file'})
            return
        # A WAL sidecar's own magic bytes are NOT "SQLite format 3\x00" —
        # checked first, as its own real case, rather than falling
        # through to the generic not_sqlite negative below (see _run_wal
        # and CLAUDE.md's Conventions entry for why this needed its own
        # separate handling: no schema of its own to resolve against).
        if raw[:4] in (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83'):
            self._run_wal(raw)
            return
        if raw[:16] != b'SQLite format 3\x00':
            self.finished.emit({'kind': 'not_sqlite'})
            return

        import sqlite_carve
        page_map = self._get_page_map(raw)
        loc = sqlite_carve.locate_offset(raw, self.offset, page_map=page_map)
        if loc is None:
            # Not a live table row -- but that doesn't mean "somewhere in
            # the db" is the best this can say. identify_structure names
            # the actual on-disk structure (an index, the schema table
            # itself, the freelist, the file header, ...) whenever it can
            # tell — see its own docstring and CLAUDE.md's Conventions
            # entry for the real ServiceLogin/urls_url_index case this
            # was built to stop flattening into a generic negative.
            structure = sqlite_carve.identify_structure(raw, self.offset, page_map=page_map)
            self.finished.emit({'kind': 'unresolved', 'structure': structure})
            return

        table, rowid = loc['table'], loc['rowid']

        report_match = self._find_report_match(table, rowid)
        if report_match is not None:
            self.finished.emit(report_match)
            return

        live = sqlite_carve.read_live_row(raw, table, rowid)
        if live is None:
            # locate_offset already confirmed this rowid is live -- a
            # failure here means something narrower went wrong (e.g. a
            # column value sqlite3 itself can't decode), not that the
            # row doesn't exist -- report it honestly as such rather
            # than silently falling back to "unresolved".
            self.finished.emit({'kind': 'error', 'message':
                                f'Found live row {table}.rowid={rowid} but could not read its values'})
            return
        columns, values = live
        self.finished.emit({
            'kind':         'live',
            'table':        table,
            'rowid':        rowid,
            'row':          dict(zip(columns, values)),
            'column_name':  loc.get('column_name'),
        })


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
            with closing(_open_results_db(self._case_dir)) as db:
                rows = db.execute(
                    'SELECT r.filename, r.offset, r.context '
                    'FROM search_results r '
                    'JOIN search_index i ON r.term_id = i.id '
                    'WHERE i.keyword=? '
                    'ORDER BY r.rowid',
                    (self._term,)
                ).fetchall()
        except Exception:
            self.finished.emit(0)
            return

        for batch in batched(rows, self.BATCH_SIZE):
            self.rows_ready.emit(list(batch))

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
            with closing(_open_results_db(self._case_dir)) as db:
                terms = [r[0] for r in db.execute(
                    'SELECT keyword FROM search_index ORDER BY used_at DESC LIMIT 20')]
        except Exception:
            terms = []
        self.loaded.emit(terms)


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
        self._last_progress = (done, total)
        if self._bar.maximum() != total:
            self._bar.setRange(0, total)
        self._bar.setValue(done)
        self._progress_label.setText(
            f"Checked {done:,} / {total:,} files  |  "
            f"hits in {hits:,} file{'s' if hits != 1 else ''} so far")

    def update_hit_count(self, hits: int):
        """Refresh just the 'hits so far' portion of the progress label —
        added 2026-09-20 for the nested-archive/LevelDB search passes,
        which have their own real progress but not in the same units as
        the main worker's file-scan count that drives the progress BAR
        (entries scanned / LevelDB rows checked vs. files scanned) — so
        this keeps whatever "Checked X / Y files" prefix update_progress
        last set (self._last_progress, tracked explicitly rather than
        parsed back out of the label's own text) and only replaces the
        hit-count suffix."""
        done_total = getattr(self, '_last_progress', None)
        if done_total is not None:
            done, total = done_total
            self._progress_label.setText(
                f"Checked {done:,} / {total:,} files  |  "
                f"hits in {hits:,} file{'s' if hits != 1 else ''} so far")
        else:
            self._progress_label.setText(
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
        self._leveldb_search_worker: LevelDbSearchWorker | None = None
        # Real bug found and fixed 2026-09-20, by direct user report: the
        # search-complete message and the "N hits" total shown at the end
        # used to come ONLY from KeywordSearchWorker's own finished signal
        # — a real LevelDB hit (often the ONLY place a match exists at
        # all, since a UTF-16LE-encoded Chrome value re-decoded to UTF-8
        # for search never matches its own raw on-disk bytes) could finish
        # arriving AFTER the dialog had already declared "0 hits" and
        # re-enabled the search button. _search_hit_count is now
        # incremented by every one of the three result handlers
        # (_on_search_result/_on_nested_search_result/
        # _on_leveldb_search_result) — a true combined total, not any one
        # worker's own count — and _search_pending_workers tracks which
        # of the (up to three) workers actually started for THIS search,
        # so the real "search complete" finalization
        # (_finalize_search_results) only runs once every one of them has
        # genuinely finished, never just the first (usually fastest) one.
        self._search_hit_count: int = 0
        self._search_pending_workers: set[str] = set()
        self._search_main_finish_stats: tuple | None = None
        self._search_index_worker: SearchIndexWorker | None = None
        self._db_loader: DbSearchLoader | None = None
        self._db_loader_term: str = ""
        self._db_loader_db_key: str = ""
        self._current_search_db_key: str = ""
        self._recent_loader: DbRecentLoader | None = None
        self._search_progress_dlg: SearchProgressDialog | None = None
        self._pending_db_hits: list[tuple] = []
        self._live_hit_buffer: list[tuple] = []
        self._live_hit_flush_scheduled = False
        self._search_entries:     list | None = None
        self._search_incomplete:  bool = False
        self._search_incomplete_files: tuple[int,int] = (0, 0)  # (done, total)
        self._search_folder_items: dict[str, QStandardItem] = {}
        self._search_file_items:   dict[str, QStandardItem] = {}
        self._recent_searches: list = []
        self._current_scope_ui_paths: list | None = None

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

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.search_status, stretch=1)
        self._search_scope_files_btn = QPushButton()
        self._search_scope_files_btn.setFlat(True)
        self._search_scope_files_btn.setVisible(False)
        self._search_scope_files_btn.clicked.connect(self._show_search_scope_files_dialog)
        status_row.addWidget(self._search_scope_files_btn)
        search_tab_layout.addLayout(status_row)

        search_tab_layout.addWidget(self._incomplete_banner)

        self.search_results_model = QStandardItemModel()
        self.search_results_model.setHorizontalHeaderLabels(
            ["Name", "Hits", "Context", "Offset"])
        # Bumped every time the results model is cleared (a new search, a
        # recent-search reload, ...) -- guards SqlHitInterpretWorker's
        # completion handler against mutating a QStandardItem whose
        # underlying C++ object the model has since destroyed (a real
        # PySide6 crash risk, not just a cosmetic stale-update concern),
        # since that worker can finish well after the tree it was
        # started against is gone.
        self._search_generation = 0
        self._sql_interpret_workers = {}  # keep QThread refs alive while running
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
        self._search_context_delegate = HighlightDelegate(
            lambda: self.search_field.text().strip(), column=2)
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

    def _refresh_search_scope_combo(self, groups=None):
        """Rebuild the scope dropdown: fixed options + current bookmark groups.

        Pass *groups* when the caller already loaded them (the bookmark panel
        does) — opening the results DB here would block the main thread."""
        prev = self.search_scope_combo.currentData()
        self.search_scope_combo.blockSignals(True)
        self.search_scope_combo.clear()
        self.search_scope_combo.addItem("All Files",      userData="all")
        self.search_scope_combo.addItem("App Data",       userData="app_data")
        self.search_scope_combo.addItem("Selected Files", userData="selected")
        self.search_scope_combo.setToolTip(
            "All Files      — search every stored file in the archive\n"
            "App Data       — search only files under mobile/Containers (iOS) or data/data (Android)\n"
            "Selected Files — search only currently selected files/folders\n"
            "BM: <group>    — search only files in that bookmark group"
        )
        if groups is None:
            groups = []
            if getattr(self, '_case_dir', None):
                try:
                    with closing(_open_results_db(self._case_dir)) as db:
                        groups = load_bookmark_groups(db)
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
        """Return (scoped_entries, scoped_nested_map, scope_label, scope_ui_paths).

        scoped_entries=None means pass all entries to the worker (it handles filtering).
        scope_ui_paths is the list of ui_paths for BM/selected scopes, None otherwise.
        """
        nested_map = getattr(self, '_nested_archive_map', {})

        if isinstance(scope, dict) and scope.get('type') == 'bookmark':
            group_id   = scope['group_id']
            group_name = scope.get('name', 'Bookmarks')
            ui_paths   = self._get_ui_paths_for_search_scope(group_id)
            entries, nm = self._filter_entries_by_ui_paths(ui_paths, nested_map)
            return entries, nm, f"BM: {group_name}", ui_paths

        if scope == 'selected':
            checked    = getattr(self, '_checked_folders', set())
            folder_map = getattr(self, 'folder_map', {})
            seen: set  = set()
            ui_paths: list = []
            for folder in checked:
                for child in folder_map.get(folder, []):
                    if child not in seen:
                        seen.add(child)
                        ui_paths.append(child)
            entries, nm = self._filter_entries_by_ui_paths(ui_paths, nested_map)
            return entries, nm, f"selected files ({len(ui_paths):,})", ui_paths

        if scope == 'app_data':
            entries = (
                [e for e in self._search_entries if 'mobile/Containers' in e[0] or 'data/data' in e[0]]
                if self._search_entries is not None else None
            )
            return entries, nested_map, "App Data", None

        # "all" — exclude Cellebrite internal metadata entries
        exclude = (
            _CELLEBRITE_META_PREFIXES
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
        return entries, nested_map, "all files", None

    def _get_ui_paths_for_search_scope(self, group_id: int) -> list:
        """Return ui_paths for all entries in a bookmark group."""
        if not getattr(self, '_case_dir', None):
            return []
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                entries = load_bookmark_entries(db, group_id)
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

    def _show_search_scope_files_dialog(self):
        """Show a two-column list of files in scope with their sizes."""
        ui_paths = self._current_scope_ui_paths or []
        full_meta = getattr(self, 'full_metadata', {})
        _zero_colour = QColor(160, 160, 160)

        def _fmt_size(sz):
            if sz is None or sz < 0:
                return "—"
            if sz == 0:
                return "0 B"
            for unit in ('B', 'KB', 'MB', 'GB'):
                if sz < 1024:
                    return f"{sz:,.0f} {unit}" if unit == 'B' else f"{sz:,.1f} {unit}"
                sz /= 1024
            return f"{sz:,.1f} TB"

        dlg = QDialog(self)
        n = len(ui_paths)
        dlg.setWindowTitle(f"Files in Search Scope ({n:,})")
        dlg.setMinimumWidth(760)
        dlg.setMinimumHeight(420)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(6)
        layout.addWidget(QLabel(f"{n:,} file{'s' if n != 1 else ''} were in scope for this search:"))

        table = QTableWidget(n, 2)
        table.setHorizontalHeaderLabels(["File", "Size"])
        table.verticalHeader().setVisible(False)
        table.setWordWrap(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().resizeSection(1, 65)
        table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        for row, path in enumerate(ui_paths):
            sz = (full_meta.get(path) or {}).get('size', None)
            path_item = QTableWidgetItem(path)
            size_item = QTableWidgetItem(_fmt_size(sz))
            size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if sz == 0:
                path_item.setForeground(_zero_colour)
                size_item.setForeground(_zero_colour)
            table.setItem(row, 0, path_item)
            table.setItem(row, 1, size_item)

        layout.addWidget(table, stretch=1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
        dlg.exec()

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
        self._search_scope_files_btn.setVisible(False)
        self._current_scope_ui_paths = None
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
            # Nothing selected — including a fresh switch into this tab
            # when Search has never been used yet (see "Per-tab state on
            # switching" in CLAUDE.md). Clear the shared hex panel rather
            # than leaving another tab's content showing under this one.
            self._clear_hex_preview()
            return
        item = self.search_results_model.itemFromIndex(indexes[0])
        if not item:
            self._clear_hex_preview()
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
                        stored_path: str | None = None, entry_path: str | None = None,
                        leveldb_folder: str | None = None, leveldb_record_index: int | None = None,
                        offset_label: str | None = None):
        """Insert one hit into the fully-nested path tree.

        stored_path / entry_path are set for nested-archive hits so the
        click handler can reopen the entry from the stored ZIP.

        leveldb_folder / leveldb_record_index (added 2026-09-19) are set
        for a LevelDB/IndexedDB-sourced hit (see LevelDbSearchWorker) —
        the real folder ui_path and the record's own stable decode-order
        index, resolved back to the exact record later by
        _show_leveldb_hit_in_browser the same way _leveldb_folder_map's
        own 'record_vpaths' list is already keyed. offset_label, when
        given, replaces the plain str(offset) shown in the Offset
        column — used for exactly this same case, since a LevelDB hit's
        own "offset" is a position within DECODED/RENDERED text, not a
        real byte offset into the archive, and showing it unlabeled
        would risk being mistaken for one.
        """
        _PATH_ROLE = Qt.ItemDataRole.UserRole
        _LEVELDB_FOLDER_ROLE = Qt.ItemDataRole.UserRole + 5
        _LEVELDB_RECORD_ROLE = Qt.ItemDataRole.UserRole + 6

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
        if leveldb_folder is not None:
            hit_item.setData(leveldb_folder,       _LEVELDB_FOLDER_ROLE)
            hit_item.setData(leveldb_record_index, _LEVELDB_RECORD_ROLE)
        hit_row = [hit_item, QStandardItem(''), QStandardItem(context),
                  QStandardItem(offset_label if offset_label is not None else str(offset))]
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

        jump_item = model.itemFromIndex(col0)
        jump_data = jump_item.data(_REPORT_JUMP_ROLE) if jump_item is not None else None
        if jump_data is not None:
            script_name, report_rowid = jump_data
            self._art_jump_to_report_row(script_name, report_rowid)
            return

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
        offset = item.data(Qt.ItemDataRole.UserRole + 1)   # only set on a real hit row, never a file/folder row
        leveldb_folder = item.data(Qt.ItemDataRole.UserRole + 5)   # set only for a LevelDB/IndexedDB-sourced hit

        menu   = QMenu(self)
        action = menu.addAction("Open Parent Folder")
        sql_action = None
        leveldb_action = None
        if leveldb_folder is not None:
            # "Interpret as SQL Record" doesn't apply here — this hit's
            # own "offset" is a position within already-decoded/rendered
            # text, not a real byte offset into a database file, so
            # there's no SQL row to resolve it against. Added 2026-09-19,
            # per direct user request ("can we allow the user to right
            # click result to find the value in the browser?"), mirroring
            # this same menu's existing "Interpret as SQL Record" ->
            # "Jump to this row in the report" precedent.
            leveldb_action = menu.addAction("Show Record in File Browser")
        elif offset is not None:
            sql_action = menu.addAction("Interpret as SQL Record")
        chosen = menu.exec(self.search_results_view.viewport().mapToGlobal(pos))
        if chosen == action:
            self._open_parent_folder_from_search(full_path)
        elif sql_action is not None and chosen == sql_action:
            self._interpret_search_hit_as_sql(item)
        elif leveldb_action is not None and chosen == leveldb_action:
            self._show_leveldb_hit_in_browser(item)

    def _interpret_search_hit_as_sql(self, item: QStandardItem, on_done=None):
        """Kick off a background SqlHitInterpretWorker for the hit *item*
        (lazy -- only ever runs for a hit the examiner explicitly asked
        about, never eagerly for every hit) and show a "Computing…"
        placeholder child under it until the result is ready — the
        explicit usability requirement behind this feature (never a
        silently-frozen wait): see CLAUDE.md's `locate_offset` Conventions
        entry and TODO.md item 1.

        *on_done*, if given, is called once this hit's own interpretation
        is fully processed — used ONLY by the bulk runner
        (_advance_bulk_sql_interpret) to know when to move on to the next
        queued hit; the ordinary right-click path never passes it. Called
        on every exit path, including the early-return below, so a
        malformed hit item can never silently stall the bulk queue."""
        _PATH_ROLE        = Qt.ItemDataRole.UserRole
        _OFFSET_ROLE      = Qt.ItemDataRole.UserRole + 1
        _PHYS_ROLE        = Qt.ItemDataRole.UserRole + 2
        _STORED_PATH_ROLE = Qt.ItemDataRole.UserRole + 3
        _ENTRY_PATH_ROLE  = Qt.ItemDataRole.UserRole + 4

        offset      = item.data(_OFFSET_ROLE)
        physical    = item.data(_PHYS_ROLE)
        stored_path = item.data(_STORED_PATH_ROLE)
        entry_path  = item.data(_ENTRY_PATH_ROLE)
        if offset is None or not self._case_dir:
            if on_done is not None:
                on_done()
            return
        # NOT item.data(_PATH_ROLE) -- that's the DISPLAY path, which for
        # an iOS third-party app has its GUID segment substituted with the
        # bundle id (_display_path, for readability). record_source's own
        # resolve_module_file_ui_path always resolves to a ui_path with
        # the RAW GUID still in it (adapters/ffs.py's resolve()/
        # strip_display_prefix() never do that substitution -- only
        # _display_path does, a separate, later step) -- comparing the
        # display path against it would silently never match any iOS
        # app's report. _strip_archive_prefix on the RAW physical/archive
        # path (no GUID substitution applied to it at all) gives the
        # right ui_path space instead. Only meaningful for a main-archive
        # hit -- a nested-archive hit's entry_path lives inside an
        # extracted sidecar file, not the main archive's own ui_path
        # space, so it correctly never matches any record_source entry.
        hit_ui_path = self._strip_archive_prefix(physical) if physical and not stored_path else None

        # Replace any previous interpretation (re-invoked on the same hit)
        # rather than stacking a second result underneath it.
        item.removeRows(0, item.rowCount())
        placeholder = QStandardItem("⏳  Computing…")
        placeholder.setEditable(False)
        item.appendRow([placeholder, QStandardItem(''), QStandardItem(''), QStandardItem('')])
        self.search_results_view.expand(item.index())

        platform = 'android' if self._is_android_archive() else 'ios'
        generation = self._search_generation
        worker = SqlHitInterpretWorker(
            self.zip_path, self._case_dir, offset, physical, stored_path, entry_path,
            hit_ui_path, self.guid_to_bundle, self._adapter, self.zip_names, platform)
        worker.finished.connect(
            lambda result, it=item, gen=generation, w=worker:
                self._on_sql_hit_interpreted(it, gen, result, w))
        if on_done is not None:
            worker.finished.connect(lambda *_args: on_done())
        self._sql_interpret_workers[id(worker)] = worker
        worker.start()

    def _render_unresolved_structure(self, structure: dict | None) -> list[tuple[str, str]]:
        """Turn sqlite_carve.identify_structure's result into the actual
        (label, value) rows shown for an "unresolved" (not a live table
        row) hit — naming the real on-disk structure whenever
        identify_structure could tell, instead of a single flat
        "somewhere in the db" negative. See CLAUDE.md's own Conventions
        entry for the real ServiceLogin/urls_url_index case this replaces
        a plain negative for, and identify_structure's own docstring for
        the full priority order/reasoning behind each case below.

        Returns (label, value) pairs — label goes in the tree's Name
        column, value in its existing Context column — rather than one
        long combined string, per direct feedback that cramming both
        into Name forced constant manual column-resizing to read."""
        if structure is None:
            return [("Result", "Not attributable to any current database "
                               "structure (could not be determined)")]
        kind = structure.get('kind')
        if kind == 'file_header':
            return [("Result", "Inside the database file's own header — "
                               "not row content"),
                   ("Detail", "page 1's own structural fields (page size, "
                             "reserved bytes, freelist count, ...)")]
        if kind == 'freelist':
            return [("Result", "On a freelist page"),
                   ("Detail", "reclaimed, currently-unused database space "
                             "— not a specific row")]
        if kind in ('table', 'index'):
            name = structure['name']
            owner = structure['table']
            role = "leaf" if structure.get('is_leaf') else "interior (internal navigation)"
            if kind == 'index':
                cols = structure.get('columns') or []
                indexed = f"{owner}.{', '.join(cols)}" if cols else owner
                return [("Result", "Index entry — not a table row itself"),
                       ("Index", name),
                       ("Indexes", indexed),
                       ("Page type", role)]
            # sqlite_master itself is a real 'table' match (its rootpage
            # is page 1, per identify_structure's own docstring) but
            # deserves its own clearer wording rather than the generic
            # "table" phrasing below -- it's schema/structural content
            # (CREATE TABLE/INDEX/TRIGGER text), never application data.
            if name == 'sqlite_master':
                return [("Result", "Schema table (sqlite_master)"),
                       ("Detail", "structural/schema content — e.g. CREATE "
                                 "TABLE/INDEX/TRIGGER text, not application "
                                 "row data")]
            # Any other 'table' match here (locate_offset already returned
            # None) means the offset sits on a live leaf/interior page
            # belonging to a real table, but NOT inside any of that page's
            # own currently-used cells -- e.g. unused/freed slack within an
            # otherwise-live page, or (for an interior page) the table's
            # own navigation structure rather than a row.
            if role == "leaf":
                return [("Result", "On a live table page, not inside a used row"),
                       ("Table", name),
                       ("Detail", "likely unused/freed space within this page")]
            return [("Result", "Table's own internal navigation structure"),
                   ("Table", name),
                   ("Detail", "an interior b-tree page, not row content")]
        if kind == 'unattached_btree_page':
            return [("Result", "Unlinked page"),
                   ("Detail", "has the shape of a real database page but isn't "
                             "linked to any current table or index — may be a "
                             "live overflow page for some record's own long "
                             "field, or a remnant of a dropped/renamed structure")]
        return [("Result", "Not attributable to any current database "
                           "structure (may be unallocated space)")]

    def _on_sql_hit_interpreted(self, item: QStandardItem, generation: int,
                                result: dict, worker: 'SqlHitInterpretWorker'):
        self._sql_interpret_workers.pop(id(worker), None)
        if generation != self._search_generation:
            # A new search (or a recent-search reload) cleared the tree
            # while this was running -- `item` may already be a dangling
            # reference to a QStandardItem the model has destroyed
            # (clear() releases the whole hierarchy), so nothing about it
            # is safe to touch, not even a row-count check.
            return
        try:
            item.removeRows(0, item.rowCount())
        except RuntimeError:
            return   # underlying C++ item already deleted -- nothing to update

        kind = result.get('kind')
        if kind == 'not_sqlite':
            rows = [("Result", "Not a SQLite database")]
        elif kind == 'error':
            rows = [("Error", result.get('message', 'unknown error'))]
        elif kind == 'unresolved':
            rows = self._render_unresolved_structure(result.get('structure'))
        elif kind == 'report':
            rows = [("Covered by report", result['report_name'])]
            rows += list(result['row'].items())
        elif kind == 'live':
            rows = [("Table", result['table']), ("Rowid", str(result['rowid']))]
            col = result.get('column_name')
            if col:
                rows.append(("Column", col))
            rows.append(("Status", "not covered by any existing report"))
            rows += list(result['row'].items())
        elif kind == 'wal_row':
            rows = [("Table", result['table']), ("Rowid", str(result['rowid']))]
            col = result.get('column_name')
            if col:
                rows.append(("Column", col))
            rows.append(("Source", "WAL sidecar — not (necessarily) in the live database"))
            rows += list(result['row'].items())
            rows.append(("Note", "this may be current, superseded, or genuinely "
                                 "deleted content — check the live database "
                                 "separately to tell which"))
        else:
            rows = [("Result", f"Unexpected result: {result!r}")]

        # label -> Name column, value -> the tree's existing Context column
        # (index 2) rather than both crammed into one long Name string —
        # per direct feedback that the combined form forced constant
        # manual column-resizing to read.
        for name, context in rows:
            name_item = QStandardItem(str(name))
            name_item.setEditable(False)
            context_item = QStandardItem(str(context))
            context_item.setEditable(False)
            item.appendRow([name_item, QStandardItem(''), context_item, QStandardItem('')])

        # A 'report' result names the report and shows its row's values
        # inline, but doesn't yet select that exact row in the Artifact
        # Viewer's own Report table -- this child row is the jump. Only
        # possible when _find_report_match actually resolved a real
        # underlying rowid (report_rowid); every current caller of that
        # method does, so this is unconditional on kind=='report' rather
        # than a defensive .get() check masking a real gap.
        if kind == 'report':
            jump_item = QStandardItem("→ Jump to this row in the report")
            jump_item.setEditable(False)
            jump_item.setData(
                (result['script_name'], result['report_rowid']), _REPORT_JUMP_ROLE)
            item.appendRow([jump_item, QStandardItem(''), QStandardItem(''), QStandardItem('')])

        self.search_results_view.expand(item.index())
        self.search_results_view.resizeColumnToContents(0)

    # ── Bulk SQL/WAL hit discovery + interpretation ─────────────────────────

    def _collect_hit_files_for_sql_discovery(self) -> list[dict]:
        """One entry per DISTINCT hit file currently in the results tree
        (self._search_file_items) — never per-hit, matching this
        feature's own bounded-cost design (TODO.md item 1)."""
        _PHYS_ROLE        = Qt.ItemDataRole.UserRole + 2
        _STORED_PATH_ROLE = Qt.ItemDataRole.UserRole + 3
        _ENTRY_PATH_ROLE  = Qt.ItemDataRole.UserRole + 4
        files = []
        for key, file_item in self._search_file_items.items():
            stored_path = file_item.data(_STORED_PATH_ROLE)
            entry_path  = file_item.data(_ENTRY_PATH_ROLE)
            files.append({
                'key':         key,
                'name':        key.rsplit('/', 1)[-1],
                'physical':    None if stored_path else key,
                'stored_path': stored_path,
                'entry_path':  entry_path,
            })
        return files

    def _start_sql_hit_discovery(self):
        """Kicked off once, right after a keyword search finishes (see
        _on_search_finished) — never eagerly for every search, only when
        there's at least one hit file to check at all. See TODO.md item 1
        for the full design; SqlHitDiscoveryWorker for the actual
        extension → cache → byte-peek resolution."""
        files = self._collect_hit_files_for_sql_discovery()
        if not files:
            return
        self._sql_discovery_worker = SqlHitDiscoveryWorker(
            self.zip_path, self._case_dir,
            dict(self._header_type_overrides), files, parent=self)
        self._sql_discovery_worker.done.connect(self._on_sql_discovery_done)
        self._sql_discovery_worker.start()

    def _on_sql_discovery_done(self, results: dict):
        qualifying_keys = {k for k, v in results.items() if v}
        if not qualifying_keys:
            return
        items = []
        for key in qualifying_keys:
            file_item = self._search_file_items.get(key)
            if file_item is None:
                continue
            for row in range(file_item.rowCount()):
                items.append(file_item.child(row, 0))
        if not items:
            return
        total_hits = sum(fi.rowCount() for fi in self._search_file_items.values())
        ans = QMessageBox.question(
            self, "Interpret SQL/WAL Hits?",
            f"{len(items):,} of these {total_hits:,} hits are inside SQLite/"
            f"WAL files (checked by real header bytes, not just file "
            f"extension). Interpret them all now?\n\n"
            "This runs the same background \"Interpret as SQL Record\" "
            "step already available per hit via right-click — just "
            "sequentially, for all of them.\n\n"
            "Note: a header-byte check is a classification heuristic, not "
            "an integrity guarantee — a deliberately altered file header "
            "could make a real SQLite/WAL file not count toward this "
            "number.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._start_bulk_sql_interpret(items)

    def _start_bulk_sql_interpret(self, items: list[QStandardItem]):
        self._bulk_sql_items = items
        self._bulk_sql_index = 0
        self._bulk_sql_cancelled = False
        self._bulk_sql_progress = BulkSqlInterpretProgressDialog(len(items), parent=self)
        self._bulk_sql_progress.cancelled.connect(self._on_bulk_sql_cancel)
        self._bulk_sql_progress.show()
        self._advance_bulk_sql_interpret()

    def _on_bulk_sql_cancel(self):
        self._bulk_sql_cancelled = True

    def _advance_bulk_sql_interpret(self):
        total = len(self._bulk_sql_items)
        if self._bulk_sql_cancelled or self._bulk_sql_index >= total:
            self._bulk_sql_progress.mark_finished(self._bulk_sql_index)
            return
        self._bulk_sql_progress.update_progress(self._bulk_sql_index, total)
        item = self._bulk_sql_items[self._bulk_sql_index]
        self._bulk_sql_index += 1
        self._interpret_search_hit_as_sql(item, on_done=self._advance_bulk_sql_interpret)

    def _open_parent_folder_from_search(self, full_file_path: str):
        """Navigate the tree to the parent folder of *full_file_path*."""
        folder_path = full_file_path.rsplit('/', 1)[0] if '/' in full_file_path else ''
        self.center_tabs.setCurrentIndex(0)
        self.navigate_tree_to_path(folder_path)
        QTimer.singleShot(0, lambda: self._select_file_in_table(full_file_path))

    def _show_leveldb_hit_in_browser(self, item: QStandardItem):
        """Right-click "Show Record in File Browser" for a LevelDB/
        IndexedDB-sourced search hit — added 2026-09-19, per direct user
        request ("can we allow the user to right click result to find the
        value in the browser?"), mirroring this same menu's existing
        "Interpret as SQL Record" -> jump-to-report-row precedent.

        Ensures the record's own real folder is decoded
        (self._decode_leveldb_folder — idempotent, a no-op if it's
        already decoded from a prior browse-in or a prior click on
        another hit from the same folder; fast even the first time,
        since the search-indexing pass already extracted the folder's
        real files to local disk), then resolves the hit's own stable
        record_index back to its real vpath via _leveldb_folder_map's
        own 'record_vpaths' list — the SAME stable identifier
        leveldb_viewer.index_leveldb_folder_for_search built the search
        index from, so this can never resolve to the wrong record.
        Finally reuses the identical navigate+select dance
        _open_parent_folder_from_search already uses for an ordinary
        hit."""
        _LEVELDB_FOLDER_ROLE = Qt.ItemDataRole.UserRole + 5
        _LEVELDB_RECORD_ROLE = Qt.ItemDataRole.UserRole + 6
        folder_ui_path = item.data(_LEVELDB_FOLDER_ROLE)
        record_index   = item.data(_LEVELDB_RECORD_ROLE)
        if folder_ui_path is None or record_index is None:
            return
        if folder_ui_path not in self._leveldb_folder_map:
            if not self._decode_leveldb_folder(folder_ui_path):
                QMessageBox.warning(
                    self, "Could Not Open Record",
                    "Could not re-decode this LevelDB/IndexedDB folder:\n\n"
                    f"{folder_ui_path}")
                return
        entry = self._leveldb_folder_map.get(folder_ui_path)
        record_vpaths = entry.get('record_vpaths', []) if entry else []
        if not entry or record_index >= len(record_vpaths):
            QMessageBox.warning(
                self, "Record Not Found",
                "This record could no longer be found in its own folder — "
                "it may have changed since being indexed for search. Try "
                "re-indexing (Search Coverage) and searching again.")
            return
        vpath = record_vpaths[record_index]
        self.center_tabs.setCurrentIndex(0)
        self.navigate_tree_to_path(folder_ui_path)
        QTimer.singleShot(0, lambda: self._select_file_in_table(vpath))

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
            with closing(db):
                db.execute(
                    "INSERT INTO search_index (keyword, used_at) VALUES (?, strftime('%s','now'))"
                    " ON CONFLICT(keyword) DO UPDATE SET used_at=strftime('%s','now')",
                    (db_key,)
                )
                db.commit()
                self._recent_searches = [r[0] for r in db.execute(
                    'SELECT keyword FROM search_index ORDER BY used_at DESC LIMIT 20')]
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
        with closing(db):
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
            scope_files = load_search_scope_files(db, term_id)
        self._current_scope_ui_paths = scope_files if scope_files else None

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
            self._search_generation += 1
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
            if self._current_scope_ui_paths:
                n = len(self._current_scope_ui_paths)
                self._search_scope_files_btn.setText(f"Files searched ({n:,})")
                self._search_scope_files_btn.setVisible(True)
            else:
                self._search_scope_files_btn.setVisible(False)
            return True

        self._search_incomplete_files = (files_searched, total_files) if not complete else (0, 0)

        # Stop any in-flight loader for a previous term.
        if self._db_loader and self._db_loader.isRunning():
            self._db_loader.rows_ready.disconnect()
            self._db_loader.finished.disconnect()
            self._db_loader.quit()
            self._db_loader.wait()

        self.search_results_model.clear()
        self._search_generation += 1
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
        if self._current_scope_ui_paths:
            n = len(self._current_scope_ui_paths)
            self._search_scope_files_btn.setText(f"Files searched ({n:,})")
            self._search_scope_files_btn.setVisible(True)
        else:
            self._search_scope_files_btn.setVisible(False)

    # ── Search lifecycle ──────────────────────────────────────────────────────

    def _start_search_index_build(self):
        """Kick off background index build immediately after an archive is loaded."""
        if self._search_index_worker and self._search_index_worker.isRunning():
            self._search_index_worker.stop()
            self._search_index_worker.wait()
        self._search_entries = None
        self._current_scope_ui_paths = None
        self._set_incomplete_banner()   # clear any banner left from the previous archive
        self.search_results_model.clear()
        self._search_generation += 1
        self.search_field.clear()
        self.search_status.setText("")
        self._search_scope_files_btn.setVisible(False)
        worker = SearchIndexWorker(
            self.zip_path,
            case_dir=self._case_dir,
            delta=self._local_extra_delta,
        )
        worker.entries_ready.connect(self._on_search_index_ready)
        self._search_index_worker = worker
        worker.start()

    def _on_search_index_ready(self, entries: list):
        self._search_entries = entries or None

    def _current_header_scan_tier(self) -> tuple[int, int]:
        """(complete_tier, requested_tier) for the current case.

        A LOCAL read of the same two case_settings keys
        FastZipBrowser._refresh_header_scan_indicator (ffs-explorer.py)
        already reads, rather than importing that method's own dict —
        app/ modules never import from ffs-explorer.py (see e.g. the
        DATABASE_ constant a few lines up in this same file, deliberately
        kept as its own local copy for the identical reason). The tier
        LABEL text itself still comes from `self._HEADER_SCAN_TIER_TEXT`
        (accessed as a plain instance attribute at call time, since this
        mixin ends up part of the same FastZipBrowser class that defines
        it) so the wording can never drift from what the blue banner
        already shows — only this (complete_tier, requested_tier) pair
        is re-read here, never a second copy of the wording."""
        if not self._case_dir:
            return 0, 0
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                complete_tier = int(load_case_setting(db, 'header_scan_complete_tier', '0'))
                requested_tier = int(load_case_setting(db, 'header_scan_tier', '0'))
        except Exception:
            return 0, 0
        return complete_tier, requested_tier

    def _start_keyword_search(self):
        from PySide6.QtWidgets import QMessageBox, QCheckBox
        term = self.search_field.text().strip()
        if not term or not self.zip_path:
            return
        skip_once = getattr(self, '_skip_search_reminder_once', False)
        self._skip_search_reminder_once = False
        unextracted_archives = self._unextracted_archive_count()
        complete_tier, requested_tier = self._current_header_scan_tier()
        # LevelDB/IndexedDB indexing is DELIBERATELY NOT asked about here
        # at all — see _ensure_leveldb_indexed_then_run's own docstring.
        # This dialog used to also offer an "Index N LevelDB Folder(s)
        # Now" button alongside the archive one, per direct user
        # feedback ("if I want to do both it is not clear how... can we
        # just remove it and just process them without asking? it does
        # not take long") — two independent "process X first" choices
        # crammed into one dialog had no clear way to do BOTH in one
        # pass (picking one button returned immediately without
        # searching, and picking the other meant the first choice's own
        # gap was never addressed at all). Archives still get an actual
        # choice here because decompressing one can be slow and large;
        # LevelDB indexing measured at ~3-9s of CPU for a real 234-folder
        # archive doesn't need one.
        #
        # The header-scan TIER is a genuinely separate, third gap, added
        # 2026-09-20 per direct user feedback ("during search there is
        # now 3 reason to check more: header sql records, leveldb and
        # archive... without fully typing all the file we cannot pre
        # process the db or find the archive"): a low tier means an
        # extensionless/mistyped SQLite database or embedded archive can
        # be completely UNDISCOVERED, not merely left compressed once
        # found — _unextracted_archive_count() itself only ever counts
        # archives the header scan's own overrides already surfaced (see
        # that method's own `overrides = self._header_type_overrides`
        # dependency), so a low tier can silently make the archive count
        # above read as "nothing to do" when there's really more to find.
        # Shown whenever the tier hasn't reached the exhaustive Tier 3,
        # not only when a known archive is already waiting — the whole
        # point is that a low tier can hide the very existence of more to
        # find, not just leave a known one uncompressed. The default
        # button now goes to review/upgrade the tier (and select
        # archives) rather than searching immediately, since a Tier 3
        # scan can be slow and this is the one moment an examiner is
        # actively thinking about search completeness.
        #
        # Wording reworked 2026-09-22, collaboratively with the user
        # ("I don't like the text in the dialog... it should indicate
        # the current header tier and what has not been checked, and
        # then what that means"): the old text just restated the tier's
        # own single "Covers: ..." sentence; this version explicitly
        # states what IS and ISN'T checked at the current tier (from the
        # shared `self._HEADER_SCAN_TIER_COVERAGE` dict — see
        # ffs-explorer.py's own module-level definition and comment for
        # why it's a class attribute rather than a bare import), then a
        # separate "what this means" consequence line — only shown when
        # something is actually left unchecked (complete_tier < 3).
        # LevelDB was directly discussed and confirmed NOT tier-dependent
        # (its own folder is recognized by literal filenames — CURRENT,
        # *.ldb, *.log — already present in folder_map from the archive's
        # own file listing, never by magic-byte content scanning), so it
        # gets its own flat "always found" line rather than being folded
        # into the checked/not-checked framing above it.
        if (not skip_once
                and not getattr(self, '_search_coverage_reminder_muted', False)
                and (unextracted_archives > 0 or complete_tier < 3)):
            box = QMessageBox(self)
            box.setWindowTitle("Search Coverage")
            box.setIcon(QMessageBox.Icon.Information)
            tier_name = {0: "Off", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}.get(
                complete_tier, "Off")
            coverage = self._HEADER_SCAN_TIER_COVERAGE.get(
                complete_tier, self._HEADER_SCAN_TIER_COVERAGE[0])
            tier_note = ""
            if requested_tier > complete_tier:
                tier_note = f"  (a Tier {requested_tier} scan is currently running)"
            lines = [
                f"Header scan: {tier_name} (current){tier_note}",
                f"Checked: {coverage['checked']}",
                f"Not checked: {coverage['not_checked']}",
            ]
            if complete_tier < 3:
                lines += [
                    "",
                    "What this means: a database or compressed archive "
                    "that falls into the \"Not checked\" area above — for "
                    "example one with a misleading or missing file "
                    "extension — won't be discovered at all. It won't "
                    "appear in search, and won't be offered for "
                    "decompression.",
                ]
            lines += [
                "",
                "LevelDB/IndexedDB — always found, regardless of tier "
                "(its own filenames are unambiguous).",
                "",
                "Compressed archives — "
                + (f"{unextracted_archives:,} not yet decompressed."
                   if unextracted_archives
                   else "none currently known to be undecompressed."),
                "",
                "Review the header scan tier and select archives to "
                "decompress before searching?",
            ]
            box.setText("\n".join(lines))
            review_btn = box.addButton("Review Header Scan Tier and Archives…",
                                       QMessageBox.ButtonRole.ActionRole)
            search_btn = box.addButton("Search Now",
                                       QMessageBox.ButtonRole.AcceptRole)
            box.setDefaultButton(review_btn)
            mute_chk = QCheckBox("Don't remind me again this session")
            box.setCheckBox(mute_chk)
            box.exec()
            if mute_chk.isChecked():
                self._search_coverage_reminder_muted = True
            if box.clickedButton() is review_btn:
                self._open_process_dialog(preselect_nested=True,
                                          resume_search=True,
                                          auto_archive_selection=True)
                return
        self._ensure_leveldb_indexed_then_run(term)

    def _ensure_leveldb_indexed_then_run(self, term: str):
        """Silently indexes any not-yet-indexed LevelDB/IndexedDB folders
        (no dialog, no examiner choice — just a brief status-bar message)
        before actually launching the search, then runs it.

        Deliberately NOT a prompt, unlike the archive-decompression
        choice above — per direct user feedback, 2026-09-20: LevelDB
        indexing is fast enough (measured ~3-9s of CPU for a real
        234-folder/257,804-record archive) that asking first only adds
        friction, and the two-question dialog this replaced (archives
        AND LevelDB, each with their own "process now" button) had no
        clear way to do both in one pass — picking either button
        returned immediately without running the search at all, leaving
        the OTHER gap unaddressed until the examiner searched again.
        Every unindexed folder still gets covered — just automatically,
        every time, rather than needing a deliberate choice."""
        _, unindexed_leveldb = self._leveldb_search_coverage()
        if unindexed_leveldb:
            self.search_status.setText(
                f"Indexing {len(unindexed_leveldb):,} LevelDB/IndexedDB "
                "folder(s) for search…")
            self._index_leveldb_folders_batched(
                unindexed_leveldb,
                on_done=lambda: self._start_keyword_search_run(term))
        else:
            self._start_keyword_search_run(term)

    def _start_keyword_search_run(self, term: str):
        """The actual search launch — split out of _start_keyword_search
        2026-09-19 so the Search Coverage reminder's own "Index LevelDB
        Folder(s) Now" button can defer this until
        _index_leveldb_folders_batched's on_done fires, rather than
        duplicating everything below it."""
        from PySide6.QtWidgets import QMessageBox
        self._stop_keyword_search()
        self._set_incomplete_banner()
        self.search_results_model.clear()
        self._search_generation += 1
        self.search_results_model.setHorizontalHeaderLabels(
            ["Name", "Hits", "Context", "Offset"])
        self._search_folder_items.clear()
        self._search_file_items.clear()
        self._pending_db_hits.clear()
        self._live_hit_buffer.clear()
        self._search_hit_count = 0
        self._search_pending_workers = set()
        self._search_main_finish_stats = None

        scope = self.search_scope_combo.currentData()
        is_restricted = isinstance(scope, dict) or scope == 'selected'

        # For restricted scopes (bookmark / selected), we need the index to already
        # be ready so we can resolve ui_paths to physical entry names.
        if is_restricted and self._search_entries is None:
            QMessageBox.information(
                self, "Index Building",
                "The search index is still building — please wait a moment and try again.")
            return

        scoped_entries, scoped_nested_map, scope_label, scope_ui_paths = self._resolve_search_scope(scope)
        self._current_scope_ui_paths = scope_ui_paths
        self._search_scope_files_btn.setVisible(False)

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
            with closing(db):
                db.execute(
                    'DELETE FROM search_results '
                    'WHERE term_id=(SELECT id FROM search_index WHERE keyword=?)',
                    (db_key,)
                )
                db.commit()

        # For the fallback case (entries=None, worker builds its own list) the
        # worker still needs scope/exclude_prefixes to do its own filtering.
        worker_scope = scope if scope in ('app_data', 'all') else 'all'
        worker_exclude = (
            _CELLEBRITE_META_PREFIXES
            if worker_scope == 'all' and self._adapter.format == FfsAdapter.FORMAT_CELLEBRITE
            else ()
        )

        scope_tag = f" in {scope_label}" if scope_label != 'all files' else ''
        self.search_status.setText(f"Searching '{term}'{scope_tag}…")
        self.search_btn.setEnabled(False)
        self.search_stop_btn.setEnabled(True)

        self._search_progress_dlg = SearchProgressDialog(term, parent=self)
        self._search_progress_dlg.cancelled.connect(self._cancel_keyword_search)

        # Registered BEFORE any worker starts, so a worker that happens to
        # finish (and fire its own .finished signal) before the next one
        # even gets constructed can't be mistaken for "everything's done"
        # — see _mark_search_subworker_done's own docstring for the real
        # bug this fixes.
        self._search_pending_workers = {'main'}

        self._search_worker = KeywordSearchWorker(
            self.zip_path, term,
            entries=scoped_entries,
            scope=worker_scope,
            exclude_prefixes=worker_exclude,
            case_dir=self._case_dir,
            delta=getattr(self, '_local_extra_delta', None))
        self._search_worker.status_update.connect(self._search_progress_dlg.append_status)
        self._search_worker.result_found.connect(self._on_search_result)
        self._search_worker.progress.connect(self._on_search_progress)
        self._search_worker.finished.connect(self._on_main_search_finished)
        self._search_worker.start()

        # Start nested archive search in parallel (scoped_nested_map is already
        # filtered for restricted scopes; for unrestricted scopes it equals
        # the full nested_archive_map).
        if scoped_nested_map:
            self._search_pending_workers.add('nested')
            self._nested_search_worker = NestedArchiveSearchWorker(scoped_nested_map, term)
            self._nested_search_worker.result_found.connect(self._on_nested_search_result)
            self._nested_search_worker.progress.connect(self._on_search_hits_updated)
            self._nested_search_worker.finished.connect(self._on_nested_search_finished)
            self._nested_search_worker.start()

        # Start the LevelDB/IndexedDB search index in parallel too — added
        # 2026-09-19. Scoped to 'all'/'app_data' only for this first version
        # (a real, disclosed limitation, not silently pretended away):
        # 'selected'/bookmark scopes are about specific FILES, and mapping
        # those onto which decoded LevelDB RECORDS fall "inside" them isn't
        # implemented yet. Since the real archive's own LevelDB stores are
        # overwhelmingly under data/data/ anyway (measured: 228 of 234 real
        # folders in this project's own test archive), 'app_data' is scoped
        # with the same 'data/data/' prefix the main worker's own app_data
        # filter already uses, via LevelDbSearchWorker's path_prefix.
        if worker_scope in ('all', 'app_data'):
            self._search_pending_workers.add('leveldb')
            path_prefix = 'data/data/' if worker_scope == 'app_data' else None
            self._leveldb_search_worker = LevelDbSearchWorker(self._case_dir, term, path_prefix)
            self._leveldb_search_worker.result_found.connect(self._on_leveldb_search_result)
            self._leveldb_search_worker.progress.connect(self._on_search_hits_updated)
            self._leveldb_search_worker.finished.connect(self._on_leveldb_search_finished)
            self._leveldb_search_worker.start()

        self._search_progress_dlg.exec()

    def _cancel_keyword_search(self):
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.stop()
        if self._nested_search_worker and self._nested_search_worker.isRunning():
            self._nested_search_worker.stop()
        if self._leveldb_search_worker and self._leveldb_search_worker.isRunning():
            self._leveldb_search_worker.stop()

    def _stop_keyword_search(self):
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.stop()
            self._search_worker.wait()
        if self._nested_search_worker and self._nested_search_worker.isRunning():
            self._nested_search_worker.stop()
            self._nested_search_worker.wait()
        if self._leveldb_search_worker and self._leveldb_search_worker.isRunning():
            self._leveldb_search_worker.stop()
            self._leveldb_search_worker.wait()
        self.search_btn.setEnabled(True)
        self.search_stop_btn.setEnabled(False)

    def _on_search_result(self, name: str, offset: int, context: str):
        # Buffer hits and insert them into the tree in batches — one tree
        # insert per signal freezes the GUI on terms with many thousands of hits.
        self._search_hit_count += 1
        self._pending_db_hits.append((name, offset, context))
        self._live_hit_buffer.append((name, offset, context, None, None, None, None, None))
        self._schedule_live_hit_flush()

    def _on_nested_search_result(self, virtual_ui_path: str, offset: int,
                                  context: str, stored_path: str, entry_path: str):
        self._search_hit_count += 1
        self._live_hit_buffer.append(
            (virtual_ui_path, offset, context, stored_path, entry_path, None, None, None))
        self._schedule_live_hit_flush()

    def _on_leveldb_search_result(self, folder_ui_path: str, record_index: int,
                                   offset: int, context: str, display_name: str,
                                   content_type: str):
        """LevelDbSearchWorker's own result_found handler — builds the same
        (folder -> file -> hit) tree shape every other hit already uses,
        with the record's own real display name (identical to what
        browsing that folder would show) as the "file," and an explicit
        "in decoded text" offset label (added 2026-09-19, direct design
        decision: this offset is a position within RENDERED/decoded
        text, never a real byte offset into the archive — labeling it
        plainly avoids it ever being mistaken for one, the same
        forensic-honesty bar this project holds every other citation to).

        display_name is sanitized for the '/' -> '∕' (U+2215 DIVISION
        SLASH) substitution before being combined into the tree's own
        "filename" path string — a real, confirmed-necessary fix: a
        genuine record display name routinely contains a literal URL
        ("https://mlb.com - PubMatic_USP"), and _search_add_hit's own
        folder/basename split (`filename.rsplit('/', 1)`) would otherwise
        silently misparse the URL's own slashes as extra, bogus tree
        nesting levels rather than as part of one file's own name."""
        safe_name = display_name.replace('/', '∕')
        filename  = f"{folder_ui_path}/{safe_name}"
        offset_label = f"{offset:,} (in decoded text)"
        self._search_hit_count += 1
        self._live_hit_buffer.append(
            (filename, offset, context, None, None, folder_ui_path, record_index, offset_label))
        self._schedule_live_hit_flush()

    def _on_search_hits_updated(self, *_args):
        """Lightweight progress refresh for the nested-archive/LevelDB
        search passes — added 2026-09-20 alongside the "0 hits shown at
        completion" fix. These workers have their own real progress, but
        not in the same units as the main worker's file-scan count that
        drives the dialog's progress BAR (entries scanned vs. LevelDB
        rows checked vs. files scanned) — so this only refreshes the
        'hits so far' text, via the dialog's own update_hit_count, rather
        than trying to force an apples-to-oranges done/total into the
        bar. *_args absorbs whatever (done, total) shape the calling
        worker's own progress signal happens to carry — this handler
        only cares that A tick happened, not its specific numbers."""
        hits = len(self._search_file_items)
        self._update_search_status_bar()
        if self._search_progress_dlg:
            self._search_progress_dlg.update_hit_count(hits)

    def _schedule_live_hit_flush(self):
        if not self._live_hit_flush_scheduled:
            self._live_hit_flush_scheduled = True
            QTimer.singleShot(100, self._flush_live_hits)

    def _flush_live_hits(self):
        self._live_hit_flush_scheduled = False
        if not self._live_hit_buffer:
            return
        buf, self._live_hit_buffer = self._live_hit_buffer, []
        self.search_results_view.setUpdatesEnabled(False)
        try:
            for (name, offset, context, stored_path, entry_path,
                 leveldb_folder, leveldb_record_index, offset_label) in buf:
                self._search_add_hit(name, offset, context,
                                     stored_path=stored_path, entry_path=entry_path,
                                     leveldb_folder=leveldb_folder,
                                     leveldb_record_index=leveldb_record_index,
                                     offset_label=offset_label)
        finally:
            self.search_results_view.setUpdatesEnabled(True)

    def _on_search_progress(self, done: int, total: int):
        hits = len(self._search_file_items)
        self.search_status.setText(
            f"Searching… {done:,}/{total:,} files  |  hits in {hits:,} file{'s' if hits != 1 else ''} so far")
        self._update_search_status_bar()
        if self._search_progress_dlg:
            self._search_progress_dlg.update_progress(done, total, hits)

    def _mark_search_subworker_done(self, name: str):
        """One of the (up to three) parallel search workers for the
        current search has finished. Only once ALL of them have (main,
        and whichever of nested/leveldb actually started for this
        search — see _search_pending_workers' own registration in
        _start_keyword_search_run) does the search genuinely count as
        complete.

        Real bug fixed 2026-09-20, by direct user report: the dialog
        used to declare "0 hits" and re-enable the search button the
        MOMENT the main archive worker finished — regardless of whether
        LevelDbSearchWorker/NestedArchiveSearchWorker were still running.
        Since a real LevelDB hit is very often the ONLY place a match
        exists at all (a UTF-16LE-encoded Chrome value re-decoded to
        UTF-8 for search never matches its own raw on-disk bytes), the
        examiner would see the real hit appear in the results tree a
        moment AFTER being told the search was already complete with
        nothing found — confirmed directly against the user's own real
        report, not assumed."""
        self._search_pending_workers.discard(name)
        if not self._search_pending_workers and self._search_main_finish_stats is not None:
            total_hits, files_done, files_total, stopped = self._search_main_finish_stats
            self._finalize_search_results(total_hits, files_done, files_total, stopped)

    def _on_main_search_finished(self, total_hits: int, files_done: int, files_total: int,
                                 stopped: bool):
        self._search_main_finish_stats = (total_hits, files_done, files_total, stopped)
        self._mark_search_subworker_done('main')

    def _on_nested_search_finished(self, hits: int, files_done: int, files_total: int):
        self._mark_search_subworker_done('nested')

    def _on_leveldb_search_finished(self, total_hits: int):
        self._mark_search_subworker_done('leveldb')

    def _finalize_search_results(self, total_hits: int, files_done: int, files_total: int,
                                 stopped: bool):
        """The REAL "declare the search complete" step — runs once every
        worker started for this search has genuinely finished (see
        _mark_search_subworker_done), never just the first/fastest one.
        total_hits/files_done/files_total/stopped are the MAIN worker's
        own numbers (the only one that tracks "files scanned" and "was
        this interrupted" at all — nested/leveldb have no equivalent
        concept of "total files in the archive"); the hit COUNT/FILE
        COUNT actually shown to the examiner
        (self._search_hit_count / len(self._search_file_items)) is the
        TRUE combined total across all three sources, incremented by
        each one's own result handler as hits arrive — never any single
        worker's own count alone."""
        self._flush_live_hits()   # drain any buffered hits before counting
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
            with closing(db):
                db.execute(
                    'UPDATE search_index SET complete=?, files_searched=?, total_files=? '
                    'WHERE keyword=?',
                    (complete, files_searched, total_files, db_key)
                )
                term_row = db.execute(
                    'SELECT id FROM search_index WHERE keyword=?', (db_key,)
                ).fetchone()
                if term_row:
                    term_id = term_row[0]
                    if self._pending_db_hits:
                        db.executemany(
                            'INSERT INTO search_results (term_id, filename, offset, context) '
                            'VALUES (?,?,?,?)',
                            [(term_id, f, o, c) for f, o, c in self._pending_db_hits]
                        )
                    if self._current_scope_ui_paths is not None:
                        save_search_scope_files(db, term_id, self._current_scope_ui_paths)
                db.commit()
        self._pending_db_hits.clear()
        if self._current_scope_ui_paths is not None:
            n = len(self._current_scope_ui_paths)
            self._search_scope_files_btn.setText(f"Files searched ({n:,})")
            self._search_scope_files_btn.setVisible(True)

        if dlg:
            if stopped:
                dlg.mark_interrupted(n_files)
                self._set_incomplete_banner(files_searched, total_files)
                self.search_status.setText(
                    f"'{term}'{scope_tag} — partial search, interrupted  "
                    f"({n_files:,} file{'s' if n_files != 1 else ''} with hits)")
            else:
                dlg.mark_finished(n_files, self._search_hit_count)
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

        # Last step of the search, per direct design instruction — never
        # interleaved with the search itself, and never for a search with
        # no hits at all. See TODO.md item 1 / _start_sql_hit_discovery.
        self._start_sql_hit_discovery()
