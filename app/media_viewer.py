"""media_viewer.py — thumbnail worker, media grid widget, and FastZipBrowser mixin."""

import os
import subprocess
import sqlite3
import zipfile
from itertools import batched

from db_utils import _open_cache_db
from PySide6.QtWidgets import (
    QWidget, QLabel, QScrollArea, QGridLayout, QVBoxLayout,
)
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtCore import Qt, QThread, Signal, QBuffer, QIODevice

# ── Constants ─────────────────────────────────────────────────────────────────

MEDIA_EXTENSIONS = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.heif',
    '.mov', '.mp4', '.m4v', '.3gp', '.avi',
})
VIDEO_THUMB_EXTENSIONS = frozenset({'.mov', '.mp4', '.m4v', '.3gp', '.avi'})
THUMB_SIZE         = 160   # thumbnail box size in pixels
_THUMB_BATCH_COMMIT = 20   # inserts to accumulate before a single db.commit()


# ── Helper functions ──────────────────────────────────────────────────────────

def _find_ffmpeg() -> str | None:
    """Return the absolute path to ffmpeg, or None if not found."""
    if not hasattr(_find_ffmpeg, '_result'):
        import shutil
        candidate = shutil.which('ffmpeg')
        if candidate is None:
            for p in ('/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'):
                if os.path.isfile(p):
                    candidate = p
                    break
        if candidate:
            try:
                subprocess.run([candidate, '-version'],
                               capture_output=True, timeout=3, check=True)
            except Exception:
                candidate = None
        _find_ffmpeg._result = candidate
    return _find_ffmpeg._result


def _video_frame_bytes(video_data: bytes) -> bytes | None:
    """Extract a frame from video bytes via ffmpeg, returning PNG bytes or None."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    for seek in ('00:00:01', '00:00:00'):
        try:
            result = subprocess.run(
                [ffmpeg, '-hide_banner', '-loglevel', 'error',
                 '-probesize', '100M',
                 '-analyzeduration', '100M',
                 '-ss', seek,
                 '-i', 'pipe:0',
                 '-vframes', '1',
                 '-f', 'image2',
                 '-vcodec', 'png',
                 'pipe:1'],
                input=video_data,
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
    return None


# ── ClickableThumb ────────────────────────────────────────────────────────────

class ClickableThumb(QWidget):
    """A thumbnail container that emits clicked(ui_path) when pressed."""
    clicked = Signal(str)

    def __init__(self, ui_path: str, parent=None):
        super().__init__(parent)
        self._ui_path = ui_path
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._ui_path)
        super().mousePressEvent(event)

    def set_selected(self, selected: bool):
        if selected:
            self.setStyleSheet(
                "ClickableThumb { background-color: #1e4080; "
                "border: 2px solid #4d94ff; border-radius: 4px; }")
        else:
            self.setStyleSheet("ClickableThumb { background-color: transparent; }")


# ── ThumbnailWorker ───────────────────────────────────────────────────────────

class ThumbnailWorker(QThread):
    """Loads thumbnails from the cache DB or the ZIP, emits (ui_path, QImage).

    Single-threaded by design: QImage.loadFromData() and img.scaled() are Qt
    Python-binding calls that hold the GIL, so a ThreadPoolExecutor would only
    serialise them anyway while adding contention overhead."""
    thumbnail_ready = Signal(str, object)   # ui_path, QImage
    finished_all    = Signal()

    def __init__(self, zip_path, items, path_resolver, thumb_size, zip_info_map,
                 streaming_index=None, cache_dir=None):
        super().__init__()
        self.zip_path        = zip_path
        self.items           = items
        self.path_resolver   = path_resolver
        self.thumb_size      = thumb_size
        self.zip_info_map    = zip_info_map
        self.streaming_index = streaming_index
        self.cache_dir       = cache_dir
        self._stop           = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _encode_jpeg(img):
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, 'JPEG', 85)
        data = bytes(buf.data())
        buf.close()
        return data

    def run(self):
        use_cache = bool(self.cache_dir)
        db = _open_cache_db(self.cache_dir) if use_cache else None
        _DB_BATCH = 50   # items per DB query — small so first thumbnails emit fast

        try:
            pending = []
            items_list = list(self.items)

            _zf = None if self.streaming_index is not None else zipfile.ZipFile(self.zip_path, 'r')
            try:
                for batch in batched(items_list, _DB_BATCH):
                    # Query DB for just this batch
                    cached = {}
                    if db is not None:
                        placeholders = ','.join('?' * len(batch))
                        try:
                            for r in db.execute(
                                f'SELECT ui_path, file_size, data FROM thumbnails '
                                f'WHERE thumb_size=? AND ui_path IN ({placeholders})',
                                (self.thumb_size, *batch)
                            ):
                                cached[(r[0], r[1])] = r[2]
                        except sqlite3.Error:
                            pass

                    for ui_path in batch:
                        if self._stop:
                            return

                        physical  = self.path_resolver(ui_path)
                        file_size = self.zip_info_map.get(physical, 0)
                        ext       = os.path.splitext(physical)[1].lower()
                        blob      = cached.get((ui_path, file_size))

                        if blob:
                            stale = (ext in VIDEO_THUMB_EXTENSIONS and len(blob) < 10_000)
                            if not stale:
                                img = QImage()
                                if img.loadFromData(blob):
                                    self.thumbnail_ready.emit(ui_path, img)
                                    continue
                            if db is not None:
                                try:
                                    db.execute(
                                        'DELETE FROM thumbnails WHERE '
                                        'ui_path=? AND file_size=? AND thumb_size=?',
                                        (ui_path, file_size, self.thumb_size))
                                    db.commit()
                                except sqlite3.Error:
                                    pass

                        try:
                            if self.streaming_index is not None:
                                data = self.streaming_index.get_entry(physical).read()
                            else:
                                data = _zf.read(physical)
                        except Exception:
                            continue

                        if ext in VIDEO_THUMB_EXTENSIONS:
                            data = _video_frame_bytes(data)
                            if not data:
                                continue

                        img = QImage()
                        if not img.loadFromData(data):
                            continue
                        img = img.scaled(self.thumb_size, self.thumb_size,
                                         Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
                        self.thumbnail_ready.emit(ui_path, img)

                        if db is not None:
                            jpeg = self._encode_jpeg(img)
                            if jpeg:
                                pending.append((ui_path, file_size, self.thumb_size, jpeg))
                                if len(pending) >= _THUMB_BATCH_COMMIT:
                                    try:
                                        db.executemany(
                                            'INSERT OR REPLACE INTO thumbnails '
                                            '(ui_path,file_size,thumb_size,data) '
                                            'VALUES (?,?,?,?)', pending)
                                        db.commit()
                                    except sqlite3.Error:
                                        pass
                                    pending.clear()

            finally:
                if _zf is not None:
                    _zf.close()

            if db is not None and pending:
                try:
                    db.executemany(
                        'INSERT OR REPLACE INTO thumbnails '
                        '(ui_path,file_size,thumb_size,data) '
                        'VALUES (?,?,?,?)', pending)
                    db.commit()
                except sqlite3.Error:
                    pass

        finally:
            if db is not None:
                db.close()
        self.finished_all.emit()


# ── Mixin ─────────────────────────────────────────────────────────────────────

class MediaViewerMixin:
    """Methods and setup for the media-browser tab.

    Designed to be mixed into FastZipBrowser (QMainWindow).
    Accesses instance attributes set by FastZipBrowser.__init__ and _setup_media_tab.
    """

    def _setup_media_tab(self, status_style: str) -> QWidget:
        """Build the media-browser tab widget and initialise all media instance state.
        Returns the tab QWidget to be added to center_tabs."""
        self._thumb_worker: ThumbnailWorker | None = None
        self._thumb_cols       = 1
        self._thumb_widgets:   dict = {}
        self._thumb_img_labels: dict = {}
        self._thumb_positions:  dict = {}
        self._selected_media_path: str | None = None
        self._pending_media_selection: str | None = None
        self._media_context    = None
        self._media_total_files: int | None = None
        self._media_sort_desc: str = ""

        self._media_status = QLabel("Select a folder to view media")
        self._media_status.setStyleSheet(status_style)

        self._media_grid_widget = QWidget()
        self._media_grid = QGridLayout(self._media_grid_widget)
        self._media_grid.setSpacing(8)
        self._media_grid.setContentsMargins(8, 8, 8, 8)

        _media_container = QWidget()
        _media_container_layout = QVBoxLayout(_media_container)
        _media_container_layout.setContentsMargins(0, 0, 0, 0)
        _media_container_layout.setSpacing(0)
        _media_container_layout.addWidget(self._media_grid_widget)
        _media_container_layout.addStretch()

        self._media_scroll = QScrollArea()
        self._media_scroll.setWidgetResizable(True)
        self._media_scroll.setWidget(_media_container)

        media_tab = QWidget()
        media_tab_layout = QVBoxLayout(media_tab)
        media_tab_layout.setContentsMargins(0, 4, 0, 0)
        media_tab_layout.setSpacing(2)
        media_tab_layout.addWidget(self._media_status)
        media_tab_layout.addWidget(self._media_scroll, stretch=1)
        return media_tab

    def _load_media_from_file_model(self):
        """Load the media tab using exactly the current visible file model rows."""
        model = self.file_model

        total_files = sum(1 for r in model._rows if r[1] not in self.folder_map)
        media_paths = [
            r[1] for r in model._rows
            if r[1] not in self.folder_map
            and os.path.splitext(r[1])[1].lower() in MEDIA_EXTENSIONS
        ]

        self._media_context = tuple(media_paths)

        if 0 <= model._sort_col < len(model._headers):
            arrow = "↑" if model._sort_order == Qt.SortOrder.AscendingOrder else "↓"
            sort_desc = f", sorted by {model._headers[model._sort_col]} {arrow}"
        else:
            sort_desc = ""

        self._start_thumbnail_load(media_paths, total_files, sort_desc)

    def _start_thumbnail_load(self, media_paths, total_files=None, sort_desc=""):
        """Stop any running thumb worker, clear the grid, then start the worker.
        Containers are created lazily in _on_thumbnail_ready so the main thread
        is never blocked pre-building hundreds of widgets up front."""
        # Retire (don't wait) — the worker may be stuck inside a long ffmpeg
        # call and wait() would freeze the GUI until it returns.
        self._retire_worker(self._thumb_worker)
        self._thumb_worker = None

        while self._media_grid.count():
            item = self._media_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._thumb_widgets      = {}
        self._thumb_img_labels   = {}
        self._thumb_positions    = {}
        self._selected_media_path = None

        if not media_paths or not self.zip_path:
            self._media_status.setText(
                "No media files" if self.zip_path else "Select a folder to view media")
            return

        self._media_total_files = total_files
        self._media_sort_desc   = sort_desc
        of_total = f" of {total_files:,} file(s)" if total_files is not None else ""
        self._media_status.setText(f"Loading {len(media_paths):,} media file(s){of_total}…")
        n_cols = max(1, self._media_grid_widget.width() // (THUMB_SIZE + 16))
        self._thumb_cols = n_cols

        # Pre-compute grid positions (pure integer arithmetic — no widget creation)
        for i, ui_path in enumerate(media_paths):
            self._thumb_positions[ui_path] = divmod(i, n_cols)

        zip_info_map = {
            self._adapter.resolve(p): self.full_metadata.get(p, {}).get('size', 0)
            for p in media_paths
        }

        self._thumb_worker = ThumbnailWorker(
            self.zip_path, media_paths, self._adapter.resolve, THUMB_SIZE, zip_info_map,
            streaming_index=self._streaming_index, cache_dir=self._case_dir)
        self._thumb_worker.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._thumb_worker.finished_all.connect(self._on_thumbnails_done)
        self._thumb_worker.start()

    def _place_thumb_container(self, ui_path: str):
        """Create and insert the container widget for *ui_path* into the grid."""
        name = ui_path.split('/')[-1]
        row, col = self._thumb_positions[ui_path]

        container = ClickableThumb(ui_path)
        container.setFixedSize(THUMB_SIZE + 8, THUMB_SIZE + 28)
        container.clicked.connect(self._on_thumb_clicked)

        v = QVBoxLayout(container)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(2)

        img_label = QLabel()
        img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        img_label.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        img_label.setToolTip(ui_path)

        name_label = QLabel()
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_label.setFixedWidth(THUMB_SIZE + 4)
        name_label.setWordWrap(False)
        name_label.setStyleSheet("font-size: 10px;")
        fm = name_label.fontMetrics()
        name_label.setText(
            fm.elidedText(name, Qt.TextElideMode.ElideMiddle, THUMB_SIZE + 4))
        name_label.setToolTip(ui_path)

        v.addWidget(img_label)
        v.addWidget(name_label)

        self._thumb_widgets[ui_path]    = container
        self._thumb_img_labels[ui_path] = img_label
        self._media_grid.addWidget(container, row, col)

    def _on_thumbnail_ready(self, ui_path, img):
        if ui_path not in self._thumb_img_labels:
            if ui_path not in self._thumb_positions:
                return
            self._place_thumb_container(ui_path)
        self._thumb_img_labels[ui_path].setPixmap(QPixmap.fromImage(img))

    def _on_thumb_clicked(self, ui_path):
        if self._selected_media_path and self._selected_media_path in self._thumb_widgets:
            self._thumb_widgets[self._selected_media_path].set_selected(False)
        self._selected_media_path = ui_path
        if ui_path in self._thumb_widgets:
            self._thumb_widgets[ui_path].set_selected(True)
        self.status_bar.showMessage(ui_path)
        self._select_file_in_table(ui_path)

    def _on_thumbnails_done(self):
        count    = self._media_grid.count()
        of_total = (f" of {self._media_total_files:,} file(s)"
                    if self._media_total_files is not None else "")
        self._media_status.setText(
            f"{count:,} media file(s){of_total}{self._media_sort_desc}")
        if self._pending_media_selection and \
                self._pending_media_selection in self._thumb_widgets:
            self._on_thumb_clicked(self._pending_media_selection)
            self._media_scroll.ensureWidgetVisible(
                self._thumb_widgets[self._pending_media_selection])
        self._pending_media_selection = None
