"""leveldb_viewer.py — LevelDbViewerMixin: browse a LevelDB directory's own
real key/value records (Chrome's own Local Storage, IndexedDB, and Session
Storage all use this format — see app/ccl_leveldb.py, already used by
artifacts/android/chrome_local_storage.py and chrome_indexeddb_origins.py)
directly from the file browser, without needing an artifact parser first.

This is the raw-browser counterpart to the bplist/ABX work in
ffs-explorer.py's own _render_as_text — but triggered from a FOLDER, not a
file double-click: LevelDB is a real on-disk DIRECTORY of files (CURRENT,
MANIFEST-*, NNNNNN.ldb/.log), never one file with a magic-byte header the
way SQLite/SEGB/bplist/ABX all are. See show_tree_context_menu's own
"Open as LevelDB" action (ffs-explorer.py) for the entry point —
_looks_like_leveldb_dir below decides when to offer it, checked against
the folder's own real children via folder_map, not guessed from a
directory-name convention (which varies: "...leveldb", ".../Storage/
leveldb", no fixed suffix at all for a non-Chrome app).

Reuses ArtifactTableModel in its existing "list mode" (load_rows) — already
built for exactly this shape (a small in-memory dataset, no live DB
connection needed) — rather than a new table model.

Double-clicking a record's Key or Value cell shows its FULLY decoded
content via FastZipBrowser._render_as_text — the SAME sniff-and-decode
logic (JSON/XML/bplist/ABX/plain text) the main raw file browser already
uses for a double-clicked file, called directly rather than duplicated,
since this mixin is composed into the same FastZipBrowser class (see
CLAUDE.md's own Conventions entry for the direct reason this exists: a
LevelDB value can be a binary plist — an app isn't always "purely SQL" or
"purely LevelDB", and a plist embedded inside a LevelDB record's raw bytes
is exactly the shape decode_plist_blob already exists to catch, generalized
here to ANY LevelDB value's bytes rather than one parser that already knew
in advance a given column held one)."""

import os
import re

from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QTableView, QDialog, QPlainTextEdit,
    QDialogButtonBox,
)
from PySide6.QtCore import Qt

from artifact_viewer import ArtifactTableModel

# See ccl_leveldb.RawLevelDb.__init__/DATA_FILE_PATTERN and
# ManifestFile.MANIFEST_FILENAME_PATTERN — the real on-disk shape a
# genuine LevelDB directory has, checked directly against that class
# rather than duplicated by guesswork.
_LDB_DATA_PATTERN = re.compile(r'^[0-9]{6}\.(ldb|log|sst)$')
_LDB_MANIFEST_PATTERN = re.compile(r'^MANIFEST-[0-9A-Fa-f]{6}$')


def _looks_like_leveldb_dir(folder_map: dict, folder_ui_path: str) -> bool:
    """True if folder_ui_path's own direct children (via folder_map)
    include a real LevelDB CURRENT file and at least one MANIFEST-*/
    NNNNNN.{ldb,log,sst} data file. folder_map[path] holds ALL direct
    children (files and subfolders alike — confirmed by reading
    _get_all_children's own use of it, which distinguishes a subfolder
    from a file by checking membership in folder_map itself, not by any
    separate files-only list) — a bare basename check against each
    child is enough, no separate file-listing lookup needed."""
    children = folder_map.get(folder_ui_path)
    if not children:
        return False
    has_current = False
    has_data = False
    for child in children:
        name = child.rsplit('/', 1)[-1]
        if name == 'CURRENT':
            has_current = True
        elif _LDB_DATA_PATTERN.match(name) or _LDB_MANIFEST_PATTERN.match(name):
            has_data = True
        if has_current and has_data:
            return True
    return False


def _preview_bytes(raw: bytes, max_len: int = 300) -> str:
    """Best-effort short text for a table cell — never raises, never
    silently shows nothing for a key/value that just happens not to be
    valid UTF-8 (common: Chrome's own DOM Storage value-type tag byte,
    binary plist magic, protobuf). Truncated for the CELL only — the
    double-click detail view (_on_leveldb_cell_double_clicked) shows the
    real, full, correctly-decoded content, this is just a list-row
    glance."""
    if not raw:
        return ''
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.hex()
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text if len(text) <= max_len else text[:max_len] + '…'


class LevelDbViewerMixin:

    # ── Setup ──────────────────────────────────────────────────────────────

    def _setup_leveldb_tab(self) -> QWidget:
        """Build the LevelDB preview tab. Returns the QWidget to be added
        to self.preview_tabs — same registration shape as
        SqliteViewerMixin._setup_sqlite_tab/SegbViewerMixin._setup_segb_tab."""
        self._ldb_model:   ArtifactTableModel = ArtifactTableModel()
        self._ldb_records: list = []   # raw ccl_leveldb Record objects, index-parallel to the model's rows
        self._ldb_dir_path: str | None = None

        self._ldb_status_label = QLabel("No LevelDB directory open")

        self._ldb_table_view = QTableView()
        self._ldb_table_view.setModel(self._ldb_model)
        self._ldb_table_view.setAlternatingRowColors(True)
        self._ldb_table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._ldb_table_view.horizontalHeader().setStretchLastSection(True)
        self._ldb_table_view.verticalHeader().setVisible(False)
        self._ldb_table_view.doubleClicked.connect(self._on_leveldb_cell_double_clicked)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self._ldb_status_label)
        layout.addWidget(self._ldb_table_view, stretch=1)
        return tab

    # ── Opening a directory ───────────────────────────────────────────────

    def _open_leveldb_folder(self, folder_ui_path: str) -> None:
        """Extract folder_ui_path's own real files to a local scratch dir
        under this case's own artifact_parser_files-style area, open via
        ccl_leveldb.RawLevelDb, and populate the LevelDB preview tab.

        A small, PARALLEL extraction routine, not a reuse of
        artifact_runner.open_leveldb — that helper is shaped for a
        RUNNING PARSER's own paths dict (_read_zip_bytes there takes a
        PHYSICAL zip entry name; _app_base_ui_path/_parser_files_dir are
        parser-run-scoped). This is triggered from the live GUI instead,
        whose own self._read_zip_bytes takes a UI_PATH and resolves it
        internally — a real, already-documented distinction elsewhere in
        this project (CLAUDE.md's own hex_viewer._read_zip_bytes note).
        Small enough (about a dozen lines) that unifying the two shapes
        into one function wasn't worth the added indirection for either
        caller."""
        import ccl_leveldb

        children = self.folder_map.get(folder_ui_path) or []
        if not children:
            self.status_bar.showMessage(f"{folder_ui_path}: empty folder")
            return

        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', folder_ui_path)
        extract_dir = os.path.join(self._case_dir, 'leveldb_browse', safe_name)
        os.makedirs(extract_dir, exist_ok=True)
        for child_ui_path in children:
            if child_ui_path in self.folder_map:
                continue   # a subfolder — a real LevelDB directory is flat
            name = child_ui_path.rsplit('/', 1)[-1]
            dest = os.path.join(extract_dir, name)
            if os.path.exists(dest):
                continue   # already extracted from a prior open — same
                           # skip-if-present convention artifact_runner.
                           # open_leveldb uses, and this project's own
                           # extraction convention generally
            data = self._read_zip_bytes(child_ui_path)
            if data is None:
                continue
            with open(dest, 'wb') as f:
                f.write(data)

        try:
            db = ccl_leveldb.RawLevelDb(extract_dir)
        except Exception as exc:
            self.status_bar.showMessage(f"Could not open as LevelDB: {exc}")
            return

        records = []
        try:
            for rec in db.iterate_records_raw():
                records.append(rec)
        finally:
            db.close()

        self._ldb_dir_path = folder_ui_path
        self._ldb_records = records

        columns = ["Key", "Value", "State", "Seq", "Source File", "Offset"]
        rows = []
        for rec in records:
            rows.append((
                _preview_bytes(rec.user_key),
                _preview_bytes(rec.value),
                rec.state.name,
                rec.seq,
                os.path.basename(str(rec.origin_file)),
                rec.offset,
            ))
        self._ldb_model.load_rows(columns, rows)
        self._ldb_status_label.setText(
            f"{folder_ui_path}  —  {len(records)} record(s)"
            + ("" if records else "  (no records found — the directory "
                                   "looked like LevelDB but ccl_leveldb "
                                   "found nothing in it)"))
        self.preview_tabs.setCurrentIndex(self._ldb_tab_index)
        self.status_bar.showMessage(
            f"Opened as LevelDB: {folder_ui_path} ({len(records)} records)")

    # ── Record detail ────────────────────────────────────────────────────

    def _on_leveldb_cell_double_clicked(self, index) -> None:
        """Show the double-clicked cell's own FULL, correctly-decoded
        content — reusing FastZipBrowser._render_as_text (self, since
        this mixin is composed into that same class) rather than a
        second sniff-and-decode implementation. A Value cell that
        happens to be a binary plist, JSON, ABX, or plain text all
        render correctly here, the same as they would double-clicking a
        real file in the main browser — the whole reason this reuses
        that method instead of just showing raw bytes."""
        if not index.isValid():
            return
        row = index.row()
        if row >= len(self._ldb_records):
            return
        rec = self._ldb_records[row]
        col = index.column()
        # Column 0 = Key, column 1 = Value (see the columns list in
        # _open_leveldb_folder) — anything else has no raw bytes of its
        # own to decode further.
        if col == 0:
            raw, label = rec.user_key, "Key"
        elif col == 1:
            raw, label = rec.value, "Value"
        else:
            return

        # Try _render_as_text FIRST — unchanged, no special-casing needed
        # here: its own bplist/ABX checks are unconditional on the magic
        # bytes, and its JSON/XML checks content-sniff regardless of the
        # ext argument, so a real plist/JSON/XML/ABX value is caught
        # here exactly as it would be for a real file, ext="bin" or not.
        text = self._render_as_text(raw, f"{label.lower()}.bin")
        if text is not None:
            kind = "decoded"
        elif raw:
            # _render_as_text returned None: not bplist/ABX/JSON/XML. A
            # LevelDB value has no real file extension of its own to
            # justify _render_as_text's own lenient (errors='replace')
            # text fallback the way a genuine ".txt" FILE would — tried
            # directly here instead, STRICT ONLY, so genuinely-binary
            # content (a protobuf blob, raw bytes) falls through to hex
            # rather than rendering as replacement-character garbage.
            # Confirmed against 60 real Chrome Local Storage values:
            # a real UTF-8-tagged value (Chrome's own DOM Storage type-
            # tag byte prefix, 0x01) decodes cleanly this way — with
            # that tag byte visibly present as a literal leading
            # control character, since this generic LevelDB viewer
            # deliberately doesn't know Chrome's own app-specific
            # value-encoding convention (that's chrome_local_storage.py's
            # own job) — while a genuinely-binary protobuf-shaped value
            # correctly stays hex, not garbled text.
            try:
                text = raw.decode("utf-8", errors="strict")
                kind = "decoded (plain UTF-8 text)"
            except UnicodeDecodeError:
                text = None

        if text is None:
            text = raw.hex() if raw else "(empty)"
            kind = "raw bytes (hex — not decodable as text/JSON/XML/plist/ABX)"

        dlg = QDialog(self)
        dlg.setWindowTitle(f"LevelDB record — {label} ({kind})")
        dlg.resize(700, 500)
        layout = QVBoxLayout(dlg)
        info = QLabel(
            f"State: {rec.state.name}    Seq: {rec.seq}    "
            f"Offset: {rec.offset}    Source: "
            f"{os.path.basename(str(rec.origin_file))}")
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(editor, stretch=1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)
        dlg.exec()

    # ── Cleanup ───────────────────────────────────────────────────────────

    def _clear_leveldb_preview(self) -> None:
        self._ldb_records = []
        self._ldb_dir_path = None
        self._ldb_model.clear()
        self._ldb_status_label.setText("No LevelDB directory open")
