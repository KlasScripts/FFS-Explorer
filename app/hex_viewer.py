"""hex_viewer.py — hex-viewer constants, worker, and FastZipBrowser mixin."""

import warnings
import zipfile

from zip_entry import ZipEntry
from zip_reader import read_nested_entry
from PySide6.QtWidgets import (
    QWidget, QLabel, QProgressBar, QVBoxLayout, QFrame,
    QPlainTextEdit, QTextEdit, QTabWidget,
)
from PySide6.QtGui import (
    QFont, QFontMetricsF, QTextCursor, QTextCharFormat, QColor,
)
from PySide6.QtCore import QThread, Signal, QTimer, QEvent

# ── Layout constants ──────────────────────────────────────────────────────────
# Format: "XXXXXXXX  [GRP0]  [GRP1]  …  [GRP7]  ASCII…"
# Each group: "XX XX XX XX" = 11 chars; groups separated by 2 spaces.
_HEX_OFFSET_COLS   = 10    # width of "XXXXXXXX  "
_HEX_GROUP_STRIDE  = 13    # 11 chars/group + 2-char separator
_HEX_GROUPS        = 8
_HEX_BYTES_PER_ROW = 32
_HEX_ASCII_START   = _HEX_OFFSET_COLS + _HEX_GROUPS * _HEX_GROUP_STRIDE - 2 + 2

INITIAL_HEX_BYTES       = 16384   # bytes shown immediately on open
HEX_PAGE_BYTES          = 32768   # bytes loaded per scroll page
MAX_HEX_HIGHLIGHT_BYTES = 512     # cap on simultaneous byte highlights
HIT_WINDOW_BEFORE       = 8192    # bytes before a search hit to load initially
HIT_WINDOW_AFTER        = 8192    # bytes after a search hit to load initially

# Precomputed table: maps each byte value to its printable ASCII char or '.'
_ASCII_XLAT = bytes(b if 32 <= b < 127 else ord('.') for b in range(256))


# ── Helper functions ──────────────────────────────────────────────────────────

def _hex_col_to_byte(col: int) -> int | None:
    """Map a column index within a hex line to its byte index (0–31), or None."""
    if col < _HEX_OFFSET_COLS:
        return None
    rel    = col - _HEX_OFFSET_COLS
    group  = rel // _HEX_GROUP_STRIDE
    within = rel % _HEX_GROUP_STRIDE
    if group >= _HEX_GROUPS or within >= 11:
        return None
    b_in_grp = within // 3
    return (group * 4 + b_in_grp) if b_in_grp < 4 else None


def _ascii_col_to_byte(col: int) -> int | None:
    """Map a column index in the ASCII section of a hex line to its byte index, or None."""
    if col < _HEX_ASCII_START:
        return None
    b = col - _HEX_ASCII_START
    return b if b < _HEX_BYTES_PER_ROW else None


# ── Worker ────────────────────────────────────────────────────────────────────

class HexLoadWorker(QThread):
    """Fallback worker for compressed zip entries — reads via zipfile decompression."""
    progress      = Signal(int, int)   # bytes_read, total_bytes
    load_complete = Signal(bytes)
    error         = Signal(str)

    CHUNK = 8192
    LIMIT = 65536

    def __init__(self, entry: ZipEntry):
        super().__init__()
        self.entry       = entry
        self.total_bytes = min(entry.file_size, self.LIMIT) if entry.file_size > 0 else self.LIMIT

    def run(self):
        try:
            data = bytearray()
            with zipfile.ZipFile(self.entry.zip_path, 'r') as z:
                with z.open(self.entry.physical_path) as f:
                    while len(data) < self.LIMIT:
                        if self.isInterruptionRequested():
                            return
                        chunk = f.read(self.CHUNK)
                        if not chunk:
                            break
                        data.extend(chunk)
                        self.progress.emit(len(data), self.total_bytes)
            self.load_complete.emit(bytes(data[:self.LIMIT]))
        except Exception as e:
            self.error.emit(str(e))


# ── Mixin ─────────────────────────────────────────────────────────────────────

class HexViewerMixin:
    """Methods and setup for the hex-viewer panel.

    Designed to be mixed into FastZipBrowser (QMainWindow).
    Accesses instance attributes set by FastZipBrowser.__init__ and _setup_hex_panel.
    """

    _HEX_REF_SIZE = 15.0   # reference point size; scale up/down to taste

    # Full 32-byte hex line at worst-case values — used for font measurement.
    _HEX_SAMPLE_LINE = (
        "ffffffff  "
        "ff ff ff ff  ff ff ff ff  ff ff ff ff  ff ff ff ff  "
        "ff ff ff ff  ff ff ff ff  ff ff ff ff  ff ff ff ff  "
        "................................"
    )

    def _setup_hex_panel(self, section_style: str, status_style: str) -> QWidget:
        """Build the file-preview panel (Hex + Text tabs) and initialise hex state.
        Returns the panel QWidget to be added to the outer splitter."""
        self._fitting_hex_font  = False
        self._hex_loading_more  = False
        self._hex_entry: ZipEntry | None = None
        self._hex_file_size:    int = 0
        self._hex_bytes_loaded: int = 0
        self._hex_view_start:   int = 0
        self._hex_ui_path:      str = ""
        self._pending_hex_jump: tuple | None = None

        # ── Hex tab ───────────────────────────────────────────────────────────
        self.hex_label = QLabel("No file selected")
        self.hex_label.setStyleSheet(status_style)

        self.hex_view = QPlainTextEdit()
        self.hex_view.setReadOnly(True)
        _hex_font = QFont("Menlo", 14)
        _hex_font.setStyleHint(QFont.StyleHint.Monospace)
        self.hex_view.setFont(_hex_font)
        self.hex_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.hex_view.setStyleSheet("")
        self.hex_view.document().setDocumentMargin(10)
        self.hex_view.setPlaceholderText(
            "Double-click a file to preview it here, or right-click and choose "
            "'Preview in Hex Viewer'.")
        self.hex_view.selectionChanged.connect(self._on_hex_selection_changed)
        self.hex_view.viewport().installEventFilter(self)
        self.hex_view.verticalScrollBar().valueChanged.connect(self._on_hex_scroll)

        self.hex_progress_bar = QProgressBar()
        self.hex_progress_bar.hide()

        hex_tab = QWidget()
        hex_tab_layout = QVBoxLayout(hex_tab)
        hex_tab_layout.setContentsMargins(0, 0, 0, 0)
        hex_tab_layout.setSpacing(2)
        hex_tab_layout.addWidget(self.hex_label)
        hex_tab_layout.addWidget(self.hex_progress_bar)
        hex_tab_layout.addWidget(self.hex_view, stretch=1)

        # ── Text tab ──────────────────────────────────────────────────────────
        self.text_label = QLabel("No file selected")
        self.text_label.setStyleSheet(status_style)

        self.text_view = QPlainTextEdit()
        self.text_view.setReadOnly(True)
        _text_font = QFont("Menlo", 13)
        _text_font.setStyleHint(QFont.StyleHint.Monospace)
        self.text_view.setFont(_text_font)
        self.text_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text_view.document().setDocumentMargin(10)
        self.text_view.setPlaceholderText(
            "Decoded text content of the selected file appears here.")

        text_tab = QWidget()
        text_tab_layout = QVBoxLayout(text_tab)
        text_tab_layout.setContentsMargins(0, 0, 0, 0)
        text_tab_layout.setSpacing(2)
        text_tab_layout.addWidget(self.text_label)
        text_tab_layout.addWidget(self.text_view, stretch=1)

        # ── Tab widget ────────────────────────────────────────────────────────
        self.preview_tabs = QTabWidget()
        self.preview_tabs.addTab(hex_tab,  "Hex")    # 0
        self.preview_tabs.addTab(text_tab, "Text")   # 1

        # ── Outer panel ───────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)

        section_label = QLabel("File Preview")
        section_label.setStyleSheet(section_style)

        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        outer_layout.addWidget(sep)
        outer_layout.addWidget(section_label)
        outer_layout.addWidget(self.preview_tabs, stretch=1)
        return outer

    # ── Event filter ─────────────────────────────────────────────────────────

    def eventFilter(self, obj: object, event: object) -> bool:
        if obj is self.hex_view.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._fit_hex_font)
        return super().eventFilter(obj, event)

    # ── Loading ───────────────────────────────────────────────────────────────

    def _stop_hex_worker(self):
        """Stop any in-flight hex-load worker before starting a new one.

        Uses cooperative interruption instead of QThread.terminate() (an
        unsafe forced-kill that can leave the worker mid-syscall in an
        undefined state), and disconnects its signals first so an
        already-queued load_complete/progress from the old worker can never
        land against the file that replaces it.
        """
        worker = self._hex_worker
        if worker is None or not worker.isRunning():
            return
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            for name in ("progress", "load_complete", "error"):
                try:
                    getattr(worker, name).disconnect()
                except (RuntimeError, TypeError):
                    pass
        worker.requestInterruption()
        worker.wait()

    def _load_hex_preview(self, ui_path):
        self.preview_tabs.setCurrentIndex(0)

        self._stop_hex_worker()

        self._hex_entry        = None
        self._hex_file_size    = 0
        self._hex_bytes_loaded = 0
        self._hex_view_start   = 0
        self._hex_ui_path      = ui_path

        physical_path = self._adapter.resolve(ui_path)
        self.hex_view.clear()
        self.hex_progress_bar.hide()

        try:
            zinfo = self._get_zip_handle().getinfo(physical_path)
            entry = ZipEntry(self.zip_path, physical_path, zinfo)
        except Exception as e:
            self._on_hex_error(str(e))
            return

        self._hex_file_size = entry.file_size or self.full_metadata.get(ui_path, {}).get('size', 0)

        if entry.is_stored:
            self._hex_entry = entry
            try:
                chunk = entry.read(min(INITIAL_HEX_BYTES, self._hex_file_size or INITIAL_HEX_BYTES))
            except Exception as e:
                self._on_hex_error(str(e))
                return
            self._hex_bytes_loaded = len(chunk)
            self.hex_view.setPlainText(self._render_hex(chunk))
            self._fit_hex_font()
            self._update_hex_label()
        else:
            self.hex_label.setText(f"Loading: {ui_path}")
            self.hex_progress_bar.setRange(0, max(self._hex_file_size, 1))
            self.hex_progress_bar.setValue(0)
            self.hex_progress_bar.show()
            self._hex_worker = HexLoadWorker(entry)
            self._hex_worker.progress.connect(self._on_hex_progress)
            self._hex_worker.load_complete.connect(self._on_hex_ready)
            self._hex_worker.error.connect(self._on_hex_error)
            self._hex_worker.start()

    def _load_hex_preview_from_bytes(self, data: bytes, label: str) -> None:
        """Populate the Hex tab from raw bytes without reading the FFS zip."""
        self.preview_tabs.setCurrentIndex(0)
        self._stop_hex_worker()
        self._hex_entry        = None
        self._hex_file_size    = len(data)
        self._hex_bytes_loaded = len(data)
        self._hex_view_start   = 0
        self._hex_ui_path      = label
        self.hex_view.clear()
        self.hex_progress_bar.hide()
        chunk = data[:INITIAL_HEX_BYTES]
        self.hex_view.setPlainText(self._render_hex(chunk))
        self._fit_hex_font()
        self._update_hex_label()

    def _open_hex_from_search(self, physical_path: str, display_label: str,
                               jump_to: int | None, keyword: str):
        """Load *physical_path* into the hex viewer positioned at *jump_to* offset."""
        self._stop_hex_worker()

        self._hex_entry        = None
        self._hex_file_size    = 0
        self._hex_bytes_loaded = 0
        self._hex_view_start   = 0
        self._hex_ui_path      = display_label
        self._pending_hex_jump = (jump_to, keyword) if jump_to is not None else None

        self.hex_view.clear()
        self.hex_progress_bar.hide()

        try:
            zinfo = self._get_zip_handle().getinfo(physical_path)
            entry = ZipEntry(self.zip_path, physical_path, zinfo)
        except Exception as e:
            self._on_hex_error(str(e))
            return

        self._hex_file_size = entry.file_size

        if entry.is_stored:
            self._hex_entry = entry
            if jump_to is not None:
                kw_len    = len(keyword.encode('utf-8', errors='replace')) if keyword else 0
                win_start = max(0, ((jump_to - 10) // 32) * 32)
                win_end   = ((jump_to + kw_len + HIT_WINDOW_AFTER + 31) // 32) * 32
                if self._hex_file_size > 0:
                    win_end = min(win_end, self._hex_file_size)
                try:
                    chunk = entry.read_at(win_start, win_end - win_start)
                except Exception as e:
                    self._on_hex_error(str(e))
                    return
                self._hex_view_start = win_start
            else:
                try:
                    chunk = entry.read(min(INITIAL_HEX_BYTES, self._hex_file_size or INITIAL_HEX_BYTES))
                except Exception as e:
                    self._on_hex_error(str(e))
                    return
                self._hex_view_start = 0
            self._hex_bytes_loaded = len(chunk)
            self.hex_view.setPlainText(self._render_hex(chunk, self._hex_view_start))
            self._fit_hex_font()
            self._update_hex_label()
            if jump_to is not None:
                QTimer.singleShot(0, lambda jt=jump_to, kw=keyword: self._jump_to_hex_offset(jt, kw))
        else:
            self.hex_label.setText(f"Loading: {display_label}")
            self.hex_progress_bar.setRange(0, max(self._hex_file_size, 1))
            self.hex_progress_bar.setValue(0)
            self.hex_progress_bar.show()
            self._hex_worker = HexLoadWorker(entry)
            self._hex_worker.progress.connect(self._on_hex_progress)
            self._hex_worker.load_complete.connect(self._on_hex_ready)
            self._hex_worker.error.connect(self._on_hex_error)
            self._hex_worker.start()

    def _open_nested_hex_from_search(self, stored_path: str, entry_path: str,
                                      display_label: str, jump_to: int | None,
                                      keyword: str) -> None:
        """Load an entry from a repacked nested archive ZIP into the hex viewer.

        Positions the view around *jump_to* and highlights the keyword,
        mirroring the behaviour of _open_hex_from_search for FFS zip entries.
        """
        self._stop_hex_worker()

        self._hex_entry        = None
        self._hex_ui_path      = display_label
        self._pending_hex_jump = None

        self.hex_view.clear()
        self.hex_progress_bar.hide()

        data = read_nested_entry(stored_path, entry_path)
        if data is None:
            self._on_hex_error(f"Cannot read nested entry: {entry_path}")
            return

        self._hex_file_size  = len(data)
        self._hex_bytes_buf  = data   # store full buffer so scrolling can page further

        if jump_to is not None:
            kw_len    = len(keyword.encode('utf-8', errors='replace')) if keyword else 0
            win_start = max(0, ((jump_to - 10) // 32) * 32)
            win_end   = min(len(data),
                            ((jump_to + kw_len + HIT_WINDOW_AFTER + 31) // 32) * 32)
            chunk = data[win_start:win_end]
            self._hex_view_start = win_start
        else:
            chunk = data[:INITIAL_HEX_BYTES]
            self._hex_view_start = 0

        self._hex_bytes_loaded = len(chunk)
        self.hex_view.setPlainText(self._render_hex(chunk, self._hex_view_start))
        self.preview_tabs.setCurrentIndex(0)
        self._fit_hex_font()
        self._update_hex_label()
        if jump_to is not None:
            QTimer.singleShot(0, lambda jt=jump_to, kw=keyword: self._jump_to_hex_offset(jt, kw))

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _update_hex_label(self):
        view_end = self._hex_view_start + self._hex_bytes_loaded
        total    = self._hex_file_size
        if self._hex_view_start > 0:
            label = (f"{self._hex_ui_path}  —  "
                     f"bytes {self._hex_view_start:,}–{view_end:,} of {total:,}")
        else:
            label = f"{self._hex_ui_path}  —  {view_end:,} / {total:,} bytes shown"
        hints = []
        if self._hex_view_start > 0:
            hints.append("scroll up for earlier")
        if total == 0 or view_end < total:
            hints.append("scroll down for more")
        if hints:
            label += f"  ({', '.join(hints)})"
        self.hex_label.setText(label)

    def _on_hex_scroll(self, value):
        if self._hex_entry is None or self._hex_loading_more:
            return
        scrollbar = self.hex_view.verticalScrollBar()
        view_end  = self._hex_view_start + self._hex_bytes_loaded

        if value <= 5 and self._hex_view_start > 0:
            self._hex_loading_more = True
            back_start = max(0, ((self._hex_view_start - HEX_PAGE_BYTES) // 32) * 32)
            back_len   = self._hex_view_start - back_start
            try:
                chunk = self._hex_entry.read_at(back_start, back_len)
            except Exception as e:
                self._log(f"Hex scroll load error: {e}")
                self._hex_loading_more = False
                return
            if chunk:
                new_text  = self._render_hex(chunk, back_start)
                old_max   = scrollbar.maximum()
                old_val   = scrollbar.value()
                cursor    = QTextCursor(self.hex_view.document())
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                cursor.insertText(new_text + "\n")
                self._hex_view_start   = back_start
                self._hex_bytes_loaded += len(chunk)
                scrollbar.setValue(old_val + (scrollbar.maximum() - old_max))
                self._update_hex_label()
            self._hex_loading_more = False
            return

        if value < scrollbar.maximum() - 5:
            return
        if self._hex_file_size > 0 and view_end >= self._hex_file_size:
            return
        self._hex_loading_more = True
        remaining = (self._hex_file_size - view_end) if self._hex_file_size > 0 else HEX_PAGE_BYTES
        try:
            chunk = self._hex_entry.read_at(view_end, min(HEX_PAGE_BYTES, remaining))
        except Exception as e:
            self._log(f"Hex scroll load error: {e}")
            self._hex_loading_more = False
            return
        if not chunk:
            self._hex_loading_more = False
            return
        new_text = self._render_hex(chunk, view_end)
        self._hex_bytes_loaded += len(chunk)
        cursor = self.hex_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n" + new_text)
        self._update_hex_label()
        self._hex_loading_more = False

    def _on_hex_progress(self, done, total):
        self.hex_progress_bar.setRange(0, max(total, 1))
        self.hex_progress_bar.setValue(done)

    def _on_hex_ready(self, data):
        self.hex_progress_bar.hide()
        self._hex_bytes_loaded = len(data)
        truncated = len(data) == HexLoadWorker.LIMIT
        self.hex_label.setText(
            f"{self._hex_ui_path}  —  {len(data):,} bytes shown"
            + ("  (truncated to 64 KB)" if truncated else "")
        )
        self.hex_view.setPlainText(self._render_hex(data))
        self._fit_hex_font()
        self._log(f"Hex preview: {self._hex_ui_path}")
        if self._pending_hex_jump:
            jump_to, keyword = self._pending_hex_jump
            QTimer.singleShot(0, lambda: self._jump_to_hex_offset(jump_to, keyword))

    def _on_hex_error(self, msg):
        self.hex_progress_bar.hide()
        self.hex_label.setText(f"Cannot preview: {msg}")
        self.hex_view.clear()

    # ── Rendering ─────────────────────────────────────────────────────────────

    @staticmethod
    def _render_hex(data: bytes, base_offset: int = 0) -> str:
        ascii_str = data.translate(_ASCII_XLAT).decode('latin-1')
        h = data.hex()
        rows = []
        n = len(data)
        for i in range(0, n, 32):
            o = i * 2
            if n - i >= 32:
                s = h[o:o + 64]
                hex_part = (
                    f"{s[0:2]} {s[2:4]} {s[4:6]} {s[6:8]}"
                    f"  {s[8:10]} {s[10:12]} {s[12:14]} {s[14:16]}"
                    f"  {s[16:18]} {s[18:20]} {s[20:22]} {s[22:24]}"
                    f"  {s[24:26]} {s[26:28]} {s[28:30]} {s[30:32]}"
                    f"  {s[32:34]} {s[34:36]} {s[36:38]} {s[38:40]}"
                    f"  {s[40:42]} {s[42:44]} {s[44:46]} {s[46:48]}"
                    f"  {s[48:50]} {s[50:52]} {s[52:54]} {s[54:56]}"
                    f"  {s[56:58]} {s[58:60]} {s[60:62]} {s[62:64]}"
                )
            else:
                row_len = n - i
                s = h[o:o + row_len * 2]
                grps = []
                for g in range(8):
                    gs = g * 8
                    if gs >= len(s):
                        grps.append('')
                        continue
                    b = s[gs:min(gs + 8, len(s))]
                    grps.append(' '.join(b[j:j + 2] for j in range(0, len(b), 2)))
                hex_part = '  '.join(f'{g:<11}' for g in grps)
            rows.append(f"{base_offset + i:08x}  {hex_part}  {ascii_str[i:i + 32]}")
        return '\n'.join(rows)

    # ── Font fitting ──────────────────────────────────────────────────────────

    def _fit_hex_font(self):
        """Defer one event-loop tick so the viewport has settled before measuring."""
        QTimer.singleShot(0, self._do_fit_hex_font)

    def _do_fit_hex_font(self):
        if self._fitting_hex_font:
            return
        vp_width = self.hex_view.viewport().width()
        if vp_width <= 0:
            return
        self._fitting_hex_font = True
        try:
            ref_font = QFont("Menlo", self._HEX_REF_SIZE)
            ref_font.setStyleHint(QFont.StyleHint.Monospace)
            fm = QFontMetricsF(ref_font)
            text_width = fm.horizontalAdvance(self._HEX_SAMPLE_LINE)
            if text_width <= 0:
                return
            doc_margin    = self.hex_view.document().documentMargin()
            content_width = text_width + 2 * doc_margin
            new_size      = self._HEX_REF_SIZE * (vp_width / content_width)
            new_size      = max(6.0, min(new_size, 32.0))
            ref_font.setPointSizeF(new_size)
            self.hex_view.setFont(ref_font)
        finally:
            self._fitting_hex_font = False

    # ── Selection and highlighting ────────────────────────────────────────────

    def _on_hex_selection_changed(self):
        cursor = self.hex_view.textCursor()
        if not cursor.hasSelection():
            self.hex_view.setExtraSelections([])
            return

        doc       = self.hex_view.document()
        sel_start = min(cursor.position(), cursor.anchor())
        sel_end   = max(cursor.position(), cursor.anchor())

        hl_fmt = QTextCharFormat()
        hl_fmt.setBackground(QColor(255, 190, 0, 140))

        extra_sels = []
        start_block = doc.findBlock(sel_start)
        end_block   = doc.findBlock(max(sel_end - 1, sel_start))
        total_bytes = 0

        block = start_block
        while block.isValid():
            bpos  = block.position()
            btext = block.text()
            blen  = len(btext)

            cs = max(0, sel_start - bpos)
            ce = min(blen, sel_end - bpos)

            selected = set()
            for col in range(cs, ce):
                b = _hex_col_to_byte(col)
                if b is None:
                    b = _ascii_col_to_byte(col)
                if b is not None:
                    selected.add(b)

            total_bytes += len(selected)
            if total_bytes > MAX_HEX_HIGHLIGHT_BYTES:
                break

            for b in selected:
                hex_col   = _HEX_OFFSET_COLS + (b // 4) * _HEX_GROUP_STRIDE + (b % 4) * 3
                ascii_col = _HEX_ASCII_START + b
                for col, width in ((hex_col, 2), (ascii_col, 1)):
                    if col + width > blen:
                        continue
                    es = QTextEdit.ExtraSelection()
                    es.format = hl_fmt
                    tc = QTextCursor(doc)
                    tc.setPosition(bpos + col)
                    tc.setPosition(bpos + col + width, QTextCursor.MoveMode.KeepAnchor)
                    es.cursor = tc
                    extra_sels.append(es)

            if block == end_block:
                break
            block = block.next()

        self.hex_view.setExtraSelections(extra_sels)

    def _jump_to_hex_offset(self, offset: int, keyword: str):
        """Scroll the hex view so *offset* is visible, then highlight the keyword."""
        self._pending_hex_jump = None
        line  = (offset - self._hex_view_start) // _HEX_BYTES_PER_ROW
        doc   = self.hex_view.document()
        block = doc.findBlockByLineNumber(line)
        if block.isValid():
            cursor = QTextCursor(block)
            self.hex_view.setTextCursor(cursor)
            self.hex_view.ensureCursorVisible()
            sb      = self.hex_view.verticalScrollBar()
            visible = self.hex_view.viewport().height() // max(
                1, self.hex_view.fontMetrics().lineSpacing())
            sb.setValue(max(0, sb.value() - visible // 3))
        if keyword:
            kw_bytes = keyword.encode('utf-8', errors='replace')
            self._highlight_hex_range(offset, len(kw_bytes))

    def _highlight_hex_range(self, start_offset: int, length: int):
        """Highlight *length* bytes at *start_offset* in both the hex and ASCII columns."""
        doc    = self.hex_view.document()
        hl_fmt = QTextCharFormat()
        hl_fmt.setBackground(QColor(255, 235, 0, 210))

        view_end   = self._hex_view_start + self._hex_bytes_loaded
        extra_sels = []
        for i in range(min(length, MAX_HEX_HIGHLIGHT_BYTES)):
            byte_pos = start_offset + i
            if byte_pos < self._hex_view_start or byte_pos >= view_end:
                break
            line  = (byte_pos - self._hex_view_start) // _HEX_BYTES_PER_ROW
            b     = byte_pos % _HEX_BYTES_PER_ROW
            block = doc.findBlockByLineNumber(line)
            if not block.isValid():
                break
            bpos  = block.position()
            blen  = len(block.text())
            hex_col   = _HEX_OFFSET_COLS + (b // 4) * _HEX_GROUP_STRIDE + (b % 4) * 3
            ascii_col = _HEX_ASCII_START + b
            for col, width in ((hex_col, 2), (ascii_col, 1)):
                if col + width > blen:
                    continue
                es = QTextEdit.ExtraSelection()
                es.format = hl_fmt
                tc = QTextCursor(doc)
                tc.setPosition(bpos + col)
                tc.setPosition(bpos + col + width, QTextCursor.MoveMode.KeepAnchor)
                es.cursor = tc
                extra_sels.append(es)

        self.hex_view.setExtraSelections(extra_sels)

    # ── Text viewer ───────────────────────────────────────────────────────────

    def _clear_text_preview(self) -> None:
        """Wipe the Text tab without switching to it."""
        self.text_label.setText('')
        self.text_view.clear()

    def _load_text_preview(self, text: str, label: str) -> None:
        """Display *text* in the Text tab with *label* shown above it."""
        self.text_label.setText(label)
        self.text_view.setPlainText(text)
        self.preview_tabs.setCurrentIndex(1)

    # ── Raw byte reader ───────────────────────────────────────────────────────

    def _read_zip_bytes(self, ui_path: str, max_bytes: int = -1) -> bytes | None:
        """Read raw (stored/decompressed) bytes for *ui_path* from the FFS zip.

        max_bytes=-1 reads the entire entry.  Returns None on any error.
        """
        physical = self._adapter.resolve(ui_path)
        try:
            zinfo = self._get_zip_handle().getinfo(physical)
            entry = ZipEntry(self.zip_path, physical, zinfo)
            n = entry.file_size if max_bytes < 0 else min(max_bytes, entry.file_size)
            return entry.read(n)
        except Exception:
            return None
