"""artifact_media.py — thumbnail delegate for artifact report media columns
(see the `media_fields` convention on a parser module, e.g.
artifacts/ios/whatsapp.py's `attachment_path`).

Reuses media_viewer.py's ThumbnailWorker for decoding (same on-disk cache
DB, same in-process PyAV video-frame extraction the Media tab already uses)
so an attachment thumbnail costs nothing extra to generate if the Media tab
has already cached that same file, and vice versa. The full-size viewer
dialog itself (MediaFullViewDialog) moved to media_viewer.py 2026-09-14,
once the Media Browser grew its own double-click call site — import it from
there.

Viewers must be read-only towards the archive (see CLAUDE.md conventions).
"""

import os
import sqlite3

from PySide6.QtCore import QBuffer, QIODevice, Qt, QObject, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from db_utils import _open_cache_db
from media_viewer import sniff_media_kind

THUMB_CELL_SIZE = 64
_PAD = 6


class WebpageThumbnailRenderer(QObject):
    """Renders a real preview of each reconstructed .mhtml page by actually
    loading it in a headless QWebEngineView — same lockdown as
    MediaFullViewDialog._build_webpage (JS off, no remote/local-file
    access beyond the archive itself), just grabbed to a pixmap instead of
    shown. Deliberately NOT a plain "biggest embedded image" shortcut: the
    point (per direct request) is to show what the page actually looked
    like, so an examiner can tell at a glance which reconstruction is
    worth opening — a stray ad/logo image would be actively misleading
    for that.

    WebEngine cannot run off the main thread (a real Qt limitation, not a
    design choice here) — this processes one page at a time via
    signal-driven advance (loadFinished -> grab -> next), reusing a single
    QWebEngineView rather than one per page, so ordinary interaction
    stays responsive between loads even though it isn't a background
    QThread the way ThumbnailWorker's image/video decoding is.

    Disk-cached in the SAME casecache.db `thumbnails` table every other
    media thumbnail already uses (ui_path/file_size/thumb_size/data) — a
    report reopened later re-renders nothing, just reads back cached JPEG
    bytes. ui_path here is a local filesystem path (these .mhtml files
    are parser-generated, not archive entries — see chrome_cache.py's own
    run()), which the table accepts fine since it's just an opaque TEXT
    key everywhere else in this project too."""

    thumbnail_ready = Signal(str, QImage)
    finished_all = Signal()

    _LOAD_TIMEOUT_MS = 8000
    _SETTLE_MS = 150   # let the compositor deliver a frame after loadFinished
                        # before grabbing — grabbing immediately can catch a
                        # still-blank frame, a known QWebEngineView gotcha.

    def __init__(self, paths: list, thumb_size: int, cache_dir: str = '', parent=None):
        super().__init__(parent)
        self._queue = list(dict.fromkeys(paths))
        self._thumb_size = thumb_size
        self._cache_dir = cache_dir
        self._view = None
        self._timer = None
        self._current_path = None
        self._stopped = False

    def start(self) -> None:
        self._advance()

    def stop(self) -> None:
        self._stopped = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._view is not None:
            self._view.deleteLater()
            self._view = None

    def _advance(self) -> None:
        if self._stopped:
            return
        if not self._queue:
            self.finished_all.emit()
            return
        path = self._queue.pop(0)
        cached = self._read_cache(path)
        if cached is not None:
            self.thumbnail_ready.emit(path, cached)
            self._advance()
            return
        self._render(path)

    def _render(self, path: str) -> None:
        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        if self._view is None:
            view = QWebEngineView()
            settings = view.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
            settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
            view.resize(400, 300)
            view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            view.show()
            view.loadFinished.connect(self._on_load_finished)
            self._view = view

        self._current_path = path
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._on_load_finished(False))
        self._timer.start(self._LOAD_TIMEOUT_MS)
        self._view.setUrl(QUrl.fromLocalFile(path))

    def _on_load_finished(self, ok: bool) -> None:
        if self._stopped:
            return
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if not ok or self._view is None:
            self._advance()
            return
        QTimer.singleShot(self._SETTLE_MS, self._do_grab)

    def _do_grab(self) -> None:
        if self._stopped or self._view is None:
            return
        path = self._current_path
        pixmap = self._view.grab()
        img = pixmap.toImage().scaled(
            self._thumb_size, self._thumb_size,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.thumbnail_ready.emit(path, img)
        self._write_cache(path, img)
        self._advance()

    def _read_cache(self, path: str):
        if not self._cache_dir:
            return None
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        try:
            conn = _open_cache_db(self._cache_dir)
        except Exception:
            return None
        try:
            row = conn.execute(
                'SELECT data FROM thumbnails WHERE ui_path=? AND file_size=? AND thumb_size=?',
                (path, size, self._thumb_size)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if not row:
            return None
        img = QImage()
        return img if img.loadFromData(row[0]) else None

    def _write_cache(self, path: str, img: QImage) -> None:
        if not self._cache_dir:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, 'JPEG', 85)
        data = bytes(buf.data())
        buf.close()
        if not data:
            return
        try:
            conn = _open_cache_db(self._cache_dir)
            conn.execute(
                'INSERT OR REPLACE INTO thumbnails (ui_path,file_size,thumb_size,data) '
                'VALUES (?,?,?,?)', (path, size, self._thumb_size, data))
            conn.commit()
            conn.close()
        except Exception:
            pass


class MediaThumbnailDelegate(QStyledItemDelegate):
    """Paints a small thumbnail instead of raw path text for a column
    declared in a parser module's `media_fields`. Decoding happens off the
    UI thread — see ArtifactViewerMixin._start_art_media_thumbnails, which
    seeds this delegate's cache via a shared media_viewer.ThumbnailWorker at
    report-load time. A path with no cached pixmap yet (still decoding, or
    decode failed — not every message type has a resolvable attachment)
    falls back to a small placeholder box plus the filename."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmaps: dict[str, QPixmap | None] = {}

    def set_cache(self, cache: dict) -> None:
        self._pixmaps = cache

    def set_pixmap(self, ui_path: str, pixmap: QPixmap) -> None:
        self._pixmaps[ui_path] = pixmap

    def sizeHint(self, option, index):
        return QSize(THUMB_CELL_SIZE + _PAD * 2, THUMB_CELL_SIZE + _PAD * 2)

    def paint(self, painter, option, index):
        ui_path = index.data(Qt.ItemDataRole.DisplayRole) or ''
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        bg = option.palette.highlight() if selected else option.palette.base()
        painter.fillRect(option.rect, bg)
        fg = (option.palette.highlightedText().color() if selected
              else option.palette.text().color())

        x = option.rect.x() + _PAD
        pix = self._pixmaps.get(ui_path) if ui_path else None
        if pix and not pix.isNull():
            y = option.rect.y() + (option.rect.height() - pix.height()) // 2
            painter.drawPixmap(x, y, pix)
            x += pix.width() + _PAD
        elif ui_path:
            painter.setPen(fg)
            y = option.rect.y() + (option.rect.height() - THUMB_CELL_SIZE) // 2
            painter.drawRect(x, y, THUMB_CELL_SIZE, THUMB_CELL_SIZE)
            x += THUMB_CELL_SIZE + _PAD

        if ui_path:
            name = ui_path.rsplit('/', 1)[-1]
            avail = option.rect.right() - x - _PAD
            if avail > 16:
                painter.setPen(fg)
                fm = painter.fontMetrics()
                elided = fm.elidedText(name, Qt.TextElideMode.ElideMiddle, avail)
                painter.drawText(x, option.rect.y(), avail, option.rect.height(),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 elided)
        painter.restore()

