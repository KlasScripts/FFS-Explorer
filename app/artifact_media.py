"""artifact_media.py — thumbnail delegate + full-view dialog for artifact
report media columns (see the `media_fields` convention on a parser module,
e.g. artifacts/ios/whatsapp.py's `attachment_path`).

Reuses media_viewer.py's ThumbnailWorker for decoding (same on-disk cache
DB, same ffmpeg video-frame extraction the Media tab already uses) so an
attachment thumbnail costs nothing extra to generate if the Media tab has
already cached that same file, and vice versa.

Viewers must be read-only towards the archive (see CLAUDE.md conventions):
images are decoded straight from in-memory bytes (no filesystem write at
all); video is written to a locked-down read-only temp copy before handing
it to QMediaPlayer, which needs a seekable local file — the same pattern
mcp_server.py's Tier-3 SQLite extraction uses, cleaned up when the dialog
closes.
"""

import os
import shutil
import sqlite3
import tempfile

from PySide6.QtCore import QBuffer, QIODevice, Qt, QObject, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
    QStyle, QStyledItemDelegate, QTextEdit, QVBoxLayout,
)

from db_utils import _open_cache_db
from dialog_helpers import note_label
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


class MediaFullViewDialog(QDialog):
    """Full-size image display, or video playback with basic transport
    controls, opened on double-click of a media-column cell.

    Non-modal (shown via .show(), not .exec()) so the underlying report
    table stays interactive while this is open — see load_content, which
    lets artifact_viewer.py's row-selection handler swap this SAME open
    dialog's content to whatever row is now selected, rather than the
    examiner having to close and re-double-click for every row."""

    def __init__(self, ui_path: str, data: bytes, parent=None):
        super().__init__(parent)
        self._tmpdir = None
        self._player = None
        self._webview = None
        QVBoxLayout(self)
        self.load_content(ui_path, data)
        self.resize(760, 680)

    def load_content(self, ui_path: str, data: bytes) -> None:
        """(Re)build this dialog's content for *ui_path*/*data* — used both
        by __init__ (first open) and by artifact_viewer.py to follow row
        selection into an already-open dialog. Tears down whatever the
        PREVIOUS content needed (a running QMediaPlayer, a temp-file
        copy) before rebuilding; closeEvent below still handles final
        cleanup when the dialog itself closes."""
        if self._player is not None:
            self._player.stop()
            self._player = None
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
        self._webview = None   # old QWebEngineView (if any) is deleted below

        self.setWindowTitle(ui_path.rsplit('/', 1)[-1] or ui_path)
        layout = self.layout()
        self._clear_layout(layout)

        ext = os.path.splitext(ui_path)[1].lower()
        # Sniff (not just extension) for the same reason media_viewer's
        # ThumbnailWorker does: a generic filename (e.g. Google Messages'
        # MMS cache files, always named "..._part_N_.bin" regardless of
        # real content) carries no usable extension.
        kind = sniff_media_kind(ext, data)
        if kind == 'video':
            self._build_video(layout, ui_path, data)
        elif kind == 'webpage':
            self._build_webpage(layout, ui_path, data)
        elif kind == 'text':
            self._build_text(layout, data)
        elif kind == 'image':
            self._build_image(layout, data)
        else:
            # 'pdf', or a byte-for-byte unrecognized attachment — no
            # in-app renderer for either (see 2026-08-21 decision: adding
            # PDF rendering means a new dependency in a forensic tool's
            # chain of custody, not taken lightly). Honest "not supported"
            # panel instead of pretending an image decode was attempted.
            self._build_unsupported(layout, ui_path, data, kind)

    @staticmethod
    def _clear_layout(layout) -> None:
        """Recursively tear down every widget/nested-layout *layout*
        currently holds — plain QVBoxLayout.count()/takeAt(0) is only
        one level deep, and _build_video's own transport-controls row
        (layout.addLayout(controls)) needs the recursive case too."""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
                continue
            child_layout = item.layout()
            if child_layout is not None:
                MediaFullViewDialog._clear_layout(child_layout)
                child_layout.deleteLater()

    def _build_image(self, layout, data: bytes) -> None:
        img = QImage()
        if not img.loadFromData(data):
            layout.addWidget(QLabel(
                "Could not decode this file as an image — it may be an "
                "unsupported format, or not actually image data."))
            return
        label = QLabel()
        label.setPixmap(QPixmap.fromImage(img))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidget(label)
        scroll.setWidgetResizable(img.width() < 760 and img.height() < 680)
        layout.addWidget(scroll)

    def _build_text(self, layout, data: bytes) -> None:
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        view.setPlainText(data.decode('utf-8', errors='replace'))
        layout.addWidget(view)

    def _build_webpage(self, layout, ui_path: str, data: bytes) -> None:
        """Render a Chrome Offline Pages .mhtml/.mht archive as an actual
        page — QWebEngineView understands MHTML's multipart/related
        structure (inline images/CSS as separate MIME parts) natively,
        which the plain-text view (_build_text, still used for every
        other TEXT_ATTACHMENT_EXTENSIONS kind) can't render at all, just
        show as raw MIME source.

        Needs a real local file, same reason _build_video does: WebEngine
        parses the archive by loading a file:// URL, not from an in-memory
        buffer — setHtml() only understands plain HTML, not this
        multipart format. Same read-only scratch-copy pattern (the
        archive itself is never written to; removed in closeEvent()).

        JavaScript and remote/network access are explicitly OFF — this is
        a forensic snapshot, not a live page: nothing here should execute
        embedded script from evidence, and an MHTML archive is by
        definition self-contained (every real resource is already inline
        in the file), so disabling remote fetches costs no legitimate
        rendering — it only stops something in the archive silently
        reaching out to the network (a beacon/tracking pixel, or simply
        an unwanted signal that this specific evidence is being reviewed
        right now) the moment an examiner opens it."""
        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._tmpdir = tempfile.mkdtemp(prefix='ffs_media_')
        tmp_path = os.path.join(
            self._tmpdir, os.path.basename(ui_path) or 'page.mhtml')
        with open(tmp_path, 'wb') as f:
            f.write(data)
        os.chmod(tmp_path, 0o444)

        view = QWebEngineView()
        settings = view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        view.setUrl(QUrl.fromLocalFile(tmp_path))
        layout.addWidget(view, 1)
        self._webview = view   # keep a reference so it isn't GC'd mid-render

    def _build_unsupported(self, layout, ui_path: str, data: bytes,
                           kind: str | None) -> None:
        """PDF, or anything sniff_media_kind couldn't classify at all —
        no in-app renderer for either, so say so plainly rather than
        showing a false 'could not decode as image' error."""
        label = 'PDF document' if kind == 'pdf' else 'Unrecognized file type'
        layout.addWidget(note_label(
            f"{label} — {len(data):,} bytes\n\n"
            "No in-app preview for this attachment yet. Use File ▸ Export, "
            "or the Hex tab, to inspect it."))
        layout.addStretch(1)

    def _build_video(self, layout, ui_path: str, data: bytes) -> None:
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        # QMediaPlayer needs a seekable local file; the archive itself is
        # never written to — this is a scratch copy in a locked-down temp
        # dir, chmod'd read-only, removed in closeEvent().
        self._tmpdir = tempfile.mkdtemp(prefix='ffs_media_')
        tmp_path = os.path.join(self._tmpdir, os.path.basename(ui_path) or 'video')
        with open(tmp_path, 'wb') as f:
            f.write(data)
        os.chmod(tmp_path, 0o444)

        video_widget = QVideoWidget()
        layout.addWidget(video_widget, 1)

        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setVideoOutput(video_widget)
        self._player.setSource(QUrl.fromLocalFile(tmp_path))

        controls = QHBoxLayout()
        play_btn = QPushButton("Pause")

        def _toggle():
            if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._player.pause()
            else:
                self._player.play()
        play_btn.clicked.connect(_toggle)

        def _sync_btn(state):
            play_btn.setText(
                "Pause" if state == QMediaPlayer.PlaybackState.PlayingState else "Play")
        self._player.playbackStateChanged.connect(_sync_btn)

        slider = QSlider(Qt.Orientation.Horizontal)
        self._player.durationChanged.connect(lambda d: slider.setRange(0, d))

        def _sync_slider(pos):
            if not slider.isSliderDown():
                slider.setValue(pos)
        self._player.positionChanged.connect(_sync_slider)
        slider.sliderMoved.connect(self._player.setPosition)

        controls.addWidget(play_btn)
        controls.addWidget(slider, 1)
        layout.addLayout(controls)

        self._player.play()

    def closeEvent(self, event) -> None:
        if self._player is not None:
            self._player.stop()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().closeEvent(event)
