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
import tempfile

from PySide6.QtCore import Qt, QSize, QUrl
from PySide6.QtGui import QFontDatabase, QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
    QStyle, QStyledItemDelegate, QTextEdit, QVBoxLayout,
)

from dialog_helpers import note_label
from media_viewer import sniff_media_kind

THUMB_CELL_SIZE = 64
_PAD = 6


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
    controls, opened on double-click of a media-column cell."""

    def __init__(self, ui_path: str, data: bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle(ui_path.rsplit('/', 1)[-1] or ui_path)
        self._tmpdir = None
        self._player = None

        layout = QVBoxLayout(self)
        ext = os.path.splitext(ui_path)[1].lower()
        # Sniff (not just extension) for the same reason media_viewer's
        # ThumbnailWorker does: a generic filename (e.g. Google Messages'
        # MMS cache files, always named "..._part_N_.bin" regardless of
        # real content) carries no usable extension.
        kind = sniff_media_kind(ext, data)
        if kind == 'video':
            self._build_video(layout, ui_path, data)
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

        self.resize(760, 680)

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
