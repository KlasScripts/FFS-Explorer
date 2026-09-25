"""media_viewer.py — thumbnail worker, media grid widget, and FastZipBrowser mixin."""

import io
import os
import shutil
import sqlite3
import tempfile
from itertools import batched

import av

from contextlib import closing
from db_utils import (_open_cache_db, _open_results_db, load_embedded_media_hits,
                      mark_media_seen, unmark_media_seen, load_seen_media_paths,
                      load_all_bookmarked_paths, load_bookmark_colors)
from dialog_helpers import note_label
from zip_cd_cache import CachedZipView, load as _zcd_load
# MEDIA_EXTENSIONS/VIDEO_THUMB_EXTENSIONS/TEXT_ATTACHMENT_EXTENSIONS/
# sniff_media_kind moved to header_scan.py 2026-08-25 (re-exported here
# unchanged, so existing `from media_viewer import ...` call sites keep
# working) so app_intelligence.py can reuse the same classification
# logic for an accurate media-file count without pulling in this
# module's own PySide6 imports — app_intelligence.py must stay Qt-free
# (used by the MCP server too). See header_scan.py's own comment.
from header_scan import (classify_magic, is_text, TEXT_SIZE_LIMIT,
                         MEDIA_EXTENSIONS, VIDEO_THUMB_EXTENSIONS,
                         TEXT_ATTACHMENT_EXTENSIONS, sniff_media_kind)
from PySide6.QtWidgets import (
    QWidget, QLabel, QScrollArea, QVBoxLayout,
    QDialog, QHBoxLayout, QPushButton, QSlider, QTextEdit,
    QListView, QAbstractItemView, QStyledItemDelegate, QStyle, QMenu,
    QApplication,
)
from PySide6.QtGui import QImage, QPixmap, QFontDatabase, QColor, QPen
from PySide6.QtCore import (Qt, QThread, Signal, QBuffer, QIODevice,
                            QUrl, QAbstractListModel, QModelIndex, QSize, QRect,
                            QSettings, QItemSelectionModel)

# ── Constants ─────────────────────────────────────────────────────────────────

THUMB_SIZE         = 160   # thumbnail box size in pixels
_THUMB_BATCH_COMMIT = 20   # inserts to accumulate before a single db.commit()
_MEDIA_PAGE_SIZE_DEFAULT = 500

# Deliberately a small, LOCAL copy of ffs-explorer.py's own
# QSettings(_SETTINGS_ORG, _SETTINGS_APP) convention, not an import of it
# — app/ modules never import from ffs-explorer.py (the top-level script),
# same standing rule keyword_search.py's own equivalent copy already
# documents. QSettings itself reads straight from the OS-level store
# either way, so a second, separately-constructed instance here sees
# exactly the same persisted value Preferences ▸ Media Browser writes.
_SETTINGS_ORG = "KlasScripts"
_SETTINGS_APP = "FFS Explorer"

# Deliberately a small, LOCAL copy of ffs-explorer.py's own module-level
# _BM_GROUP_PREFIX constant (same "app/ never imports from ffs-explorer.py"
# rule as above) — used by _load_media_from_file_model to recognize a
# bookmark-group view as a "selection" worth remembering for the "Last
# Selection" button, added 2026-09-25.
_BM_GROUP_PREFIX = "__bm_group_"


def _media_page_size_pref() -> int:
    """The user's own Media Browser page-size preference (global,
    cross-case) — added 2026-09-24, direct question: "is 500 too small...
    should it be a software preference the user can change?" Read fresh
    each time a folder's media is (re)loaded (MediaViewerMixin.
    _start_thumbnail_load snapshots it into self._media_page_size once
    per folder, not re-read mid-navigation), so a change in Preferences
    takes effect the next time a folder is opened without needing a
    restart."""
    try:
        value = QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
            'media_page_size', _MEDIA_PAGE_SIZE_DEFAULT, type=int)
        return max(50, int(value))
    except Exception:
        return _MEDIA_PAGE_SIZE_DEFAULT


def _media_hide_seen_pref() -> bool:
    """Whether the Media Browser should hide a file already marked
    "seen" — added 2026-09-24, direct request: "in the setting there
    should be an option to show or hide seen files." Same LOCAL QSettings
    reasoning as _media_page_size_pref above. Off by default (show
    everything) — matches this project's own standing rule against
    silently hiding anything from review; the examiner opts in."""
    try:
        return bool(QSettings(_SETTINGS_ORG, _SETTINGS_APP).value(
            'media_hide_seen', False, type=bool))
    except Exception:
        return False


# ── Helper functions ──────────────────────────────────────────────────────────

def _video_frame_bytes(video_data: bytes) -> bytes | None:
    """Extract a frame from video bytes via PyAV (in-process libavcodec/
    libavformat bindings), returning PNG bytes or None.

    Replaced the previous subprocess-`ffmpeg` implementation 2026-09-14 —
    two independent real bugs were found and fixed by this switch, not
    just a speed/packaging win:

    1. The old implementation piped video bytes to ffmpeg via `pipe:0`
       (stdin), which is NOT seekable. Many real MOV/MP4 files (camera-
       original iPhone video, not web-optimized "faststart" files) store
       their index (`moov` atom) at the END of the file — ffmpeg reading
       from a pipe cannot jump there, and failed with "Invalid data found
       when processing input" on 13/15 real videos sampled from this
       project's own IOS17 JoshHickman test archive, including ordinary
       H.264 content, not just an edge case. Confirmed directly: feeding
       the SAME bytes to ffmpeg via a real seekable temp file fixed every
       one of those failures. `av.open(io.BytesIO(...))` gives PyAV a
       genuinely seekable in-memory stream, the same fix in effect,
       without needing a temp file at all.
    2. PyAV's own official PyPI wheel (its bundled/minimal FFmpeg build)
       cannot decode HEVC — 6/6 real HEVC test videos (Apple's default
       recording codec since iOS 11) demuxed every packet in the file
       with zero frames ever decoded, no exception raised, via the
       library's own documented high-level API. Confirmed via a real
       system FFmpeg (Homebrew, `libavcodec 63.1.101`) that the SAME
       files decode correctly — this is a real gap in PyAV's prebuilt
       wheel, not a fundamental HEVC limitation. Fixed by building PyAV
       from source, linked against a real FFmpeg with a working HEVC
       decoder, instead of installing the prebuilt wheel — see
       `requirements.txt` and `.github/workflows/build-windows-exe.yml`
       for the build-time FFmpeg linkage this now requires. Re-verified
       against all 6 previously-failing real HEVC files after the
       from-source build: exact frame counts matched (176/901/470/
       360/360/360), byte-for-byte what the container's own metadata
       declared.

    No external ffmpeg.exe to find or bundle separately anymore — the
    FFmpeg libraries this now depends on are linked into the compiled
    `av` extension module itself at build time."""
    try:
        container = av.open(io.BytesIO(video_data))
        try:
            if not container.streams.video:
                return None
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                img = frame.to_image()   # PIL Image, RGB
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                return buf.getvalue()
            return None
        finally:
            container.close()
    except Exception:
        return None


_heif_opener_registered = False


def _load_qimage(data: bytes, ext: str = '') -> QImage | None:
    """Decode raw image bytes into a QImage — the one shared entry point
    both ThumbnailWorker and MediaFullViewDialog._build_image use, added
    2026-09-14 for a real, confirmed gap: Qt's own QImage.loadFromData()
    only decodes HEIC/HEIF when the OS itself supplies a codec. macOS does
    (Apple's ImageIO framework, confirmed working via Qt's platform
    plugin) — Windows, this project's primary shipped target, has none by
    default, confirmed directly (grepped `ffs_explorer.spec`/the Windows
    CI workflow for any bundled HEIC codec: zero hits). Every other real
    format (JPEG/PNG/GIF/etc.) decodes via Qt natively on every platform
    and never reaches the fallback below at all.

    Falls back to `pillow_heif` (registers a Pillow plugin for HEIF, a
    real dependency added specifically for this — see requirements.txt)
    only for a `.heic`/`.heif` extension AND only once Qt's own decode has
    already failed — never attempted for a format Qt already handles, so
    this changes nothing for the common case on any platform.

    A real, non-obvious bug was found and fixed before this shipped, not
    assumed correct from the library's own name alone: pillow_heif reads
    a HEIC file's real EXIF orientation tag internally, but resets the
    STANDARD orientation tag (0x0112) it exposes back to 1 ("no rotation
    needed"), storing the true value separately under
    `img.info['original_orientation']` instead — so
    `PIL.ImageOps.exif_transpose()` (which only ever reads the standard
    tag) silently never rotates a real portrait photo, landing every
    portrait-orientation HEIC sideways (width/height transposed). Found
    and confirmed by cross-checking pillow_heif's raw decode against Qt's
    own (correct, orientation-applying) macOS decode of the SAME 20 real
    portrait HEIC photos from this project's own IOS17 JoshHickman
    archive: 9/20 came out transposed before this fix, 0/20 after —
    fixed by writing the real `original_orientation` value back into the
    image's own exif data before calling exif_transpose, so that
    well-tested standard library code does the actual rotation rather
    than a hand-rolled transform table."""
    img = QImage()
    if img.loadFromData(data):
        return img
    if ext.lower() not in ('.heic', '.heif'):
        return None
    try:
        import pillow_heif
        global _heif_opener_registered
        if not _heif_opener_registered:
            pillow_heif.register_heif_opener()
            _heif_opener_registered = True
        from PIL import Image, ImageOps

        pil_img = Image.open(io.BytesIO(data))
        real_orientation = pil_img.info.get('original_orientation')
        if real_orientation and real_orientation != 1:
            exif = pil_img.getexif()
            exif[0x0112] = real_orientation
            pil_img = ImageOps.exif_transpose(pil_img)
        pil_img = pil_img.convert('RGB')
        qimg = QImage(pil_img.tobytes('raw', 'RGB'), pil_img.width, pil_img.height,
                      pil_img.width * 3, QImage.Format.Format_RGB888)
        return qimg.copy()   # detach from the Python bytes object's own lifetime
    except Exception:
        return None


# ── Media grid model/delegate/view ──────────────────────────────────────────
#
# Replaced a one-real-QWidget-per-file grid (ClickableThumb container +
# QGridLayout, removed 2026-09-24) after a real, reported freeze: with
# thousands of media files in one folder, building that many QWidgets —
# even batched via QTimer.singleShot so no single frame blocked — still
# left QGridLayout holding every one of them, and a QGridLayout has to
# compute geometry for EVERY child (even ones scrolled far off-screen) to
# know the scroll area's own total size, so both the initial build and
# ongoing scrolling degraded badly well before file counts reached the
# thousands. A QListView in IconMode, backed by a plain QAbstractListModel
# and a QStyledItemDelegate that PAINTS a thumbnail rather than
# constructing a widget for it, is genuinely virtualized by Qt itself —
# only rows that actually intersect the viewport are ever queried/painted,
# so the widget-count problem disappears regardless of folder size. This
# is the same delegate-paints-a-thumbnail technique already established in
# this project for Artifact Report media columns
# (artifact_media.MediaThumbnailDelegate) — see that class for the
# original precedent, just applied to a whole grid (QListView) here
# instead of one column of a QTableView. This part of the design held up
# and is unchanged.
#
# **Loading strategy replaced with pagination, 2026-09-24, direct
# follow-up** ("the new media viewer does not really work... i want the
# viewer to be buttery smooth") — the FIRST fix's own loading half
# (continuous viewport-scroll-triggered fetch/evict, a debounce timer, a
# ThumbnailWorker restarted on every scroll tick) is what didn't hold up
# in practice: real scrolling routinely outran the 100ms debounce and the
# per-tick worker-restart overhead, showing blank cells and visibly
# lagging behind — "smooth" was never actually achieved by that part of
# the design, only the widget-count freeze was fixed. Replaced with the
# user's own proposed design instead: MediaViewerMixin now pages a large
# folder's media list into fixed-size chunks (_MEDIA_PAGE_SIZE = 500,
# matching the user's own number) — pagination only kicks in at all once
# a folder exceeds one page; a smaller folder behaves exactly as before
# (one page, shown in full, no page-nav UI). The CURRENT page's up-to-500
# thumbnails are decoded eagerly, all at once (bounded and fast — no
# viewport tracking needed at that size), while the NEXT page's own
# thumbnails are prefetched in the background the moment the current
# page's own decode finishes (_prefetch_next_page) — landing in the SAME
# MediaGridDelegate pixmap cache a Next click will look in, so paging
# forward is normally an instant, already-decoded reveal rather than a
# fresh wait. MediaGridDelegate's own cache is kept to roughly the
# current page plus its immediate neighbors (evict_except, called on
# every page load) — bounded regardless of how many thousands of files
# the folder holds or how many pages the examiner has paged through in
# one session, the same "smaller amount in memory" goal the first pass
# already established, just achieved by page boundaries instead of
# viewport tracking.


class MediaFileListModel(QAbstractListModel):
    """Backs the Media Browser's virtualized thumbnail grid — a plain list
    of ui_path strings, nothing per-item beyond that. Populating this (a
    Python list append/dict rebuild) costs nothing worth measuring even
    for tens of thousands of files, unlike the widget-per-file approach it
    replaces; the FILE COUNT was never actually the expensive part."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[str] = []
        self._row_of: dict[str, int] = {}

    def set_items(self, items: list) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._row_of = {p: i for i, p in enumerate(self._items)}
        self.endResetModel()

    def items(self) -> list:
        return self._items

    def row_of(self, ui_path: str) -> int | None:
        return self._row_of.get(ui_path)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole,
                    Qt.ItemDataRole.EditRole):
            return self._items[index.row()]
        return None


_GRID_CELL_MARGIN = 4
_GRID_NAME_HEIGHT = 18


class MediaGridDelegate(QStyledItemDelegate):
    """Paints one grid cell (thumbnail + elided filename, selection
    highlight) with no per-item widget — see the module-level note above
    for why. `_pixmaps` is a deliberately BOUNDED cache (kept to roughly
    the current page plus its immediate neighbors by
    MediaViewerMixin._load_media_page's own evict_except calls), not one
    entry per file in the folder."""

    def __init__(self, thumb_size: int, parent=None):
        super().__init__(parent)
        self._thumb_size = thumb_size
        self._pixmaps: dict[str, QPixmap] = {}
        # Which ui_paths are currently marked "seen" (2026-09-24, see the
        # Media Browser's own "Not Interested"/"Undo" feature) — paints a
        # small badge on a shown "seen" file. Only ever matters when the
        # "hide seen files" preference is OFF, since a "seen" file is
        # simply never in the model at all when it's ON — see
        # MediaViewerMixin._recompute_media_all_paths.
        self._seen_paths: set = set()
        # ui_path -> hex color string for a BOOKMARKED file (added
        # 2026-09-25, direct request: "make it clear which images are
        # bookmark[ed]... colour for each bookmark[ed group]") — draws a
        # colored outline around the thumbnail rather than a corner badge,
        # so it reads clearly alongside the (different, corner-badge)
        # "seen" indicator above without the two ever overlapping. See
        # MediaViewerMixin._recompute_media_all_paths's sibling,
        # db_utils.load_bookmark_colors, for how a multi-group file's
        # color is chosen.
        self._bookmark_colors: dict[str, str] = {}

    def cell_size(self) -> QSize:
        return QSize(self._thumb_size + _GRID_CELL_MARGIN * 2,
                     self._thumb_size + _GRID_NAME_HEIGHT + _GRID_CELL_MARGIN * 2)

    def set_pixmap(self, ui_path: str, pixmap: QPixmap) -> None:
        self._pixmaps[ui_path] = pixmap

    def set_seen_paths(self, seen: set) -> None:
        self._seen_paths = seen

    def set_bookmark_colors(self, colors: dict) -> None:
        self._bookmark_colors = colors

    def has_pixmap(self, ui_path: str) -> bool:
        return ui_path in self._pixmaps

    def cached_count(self) -> int:
        return len(self._pixmaps)

    def evict_except(self, keep: set) -> None:
        """Drop every cached pixmap NOT in *keep* (the current page plus
        its immediate neighbors — see MediaViewerMixin._load_media_page).
        Safe to call unconditionally on every page load — a still-kept
        pixmap is never touched, and an evicted one just gets
        re-requested (and re-read from the fast on-disk cache) if paged
        back to."""
        for ui_path in [p for p in self._pixmaps if p not in keep]:
            del self._pixmaps[ui_path]

    def clear(self) -> None:
        self._pixmaps.clear()

    def sizeHint(self, option, index):
        return self.cell_size()

    def paint(self, painter, option, index):
        ui_path = index.data(Qt.ItemDataRole.DisplayRole) or ''
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(option.rect, QColor('#1e4080'))
            painter.setPen(QColor('#4d94ff'))
            painter.drawRect(option.rect.adjusted(0, 0, -1, -1))

        img_rect = QRect(option.rect.x() + _GRID_CELL_MARGIN,
                         option.rect.y() + _GRID_CELL_MARGIN,
                         self._thumb_size, self._thumb_size)
        pix = self._pixmaps.get(ui_path)
        if pix and not pix.isNull():
            x = img_rect.x() + (img_rect.width() - pix.width()) // 2
            y = img_rect.y() + (img_rect.height() - pix.height()) // 2
            painter.drawPixmap(x, y, pix)

        # Bookmark outline (added 2026-09-25) — a colored border around
        # the thumbnail area, in the bookmarking group's own color (see
        # set_bookmark_colors/db_utils.load_bookmark_colors). Drawn
        # whether or not a pixmap has decoded yet, so a still-loading
        # bookmarked file is visibly distinguishable too, not just once
        # its thumbnail appears.
        bookmark_color = self._bookmark_colors.get(ui_path)
        if bookmark_color:
            pen = QPen(QColor(bookmark_color))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(img_rect.adjusted(1, 1, -2, -2))

        # "Seen" badge (added 2026-09-24) — a small translucent green
        # circle + white checkmark, top-right of the thumbnail area. Only
        # ever visible at all when "hide seen files" is OFF, since a seen
        # file is simply absent from the model entirely when it's ON —
        # see MediaViewerMixin._recompute_media_all_paths.
        if ui_path in self._seen_paths:
            badge_size = 18
            badge_rect = QRect(img_rect.right() - badge_size - 2,
                               img_rect.top() + 2, badge_size, badge_size)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(46, 160, 67, 220))
            painter.drawEllipse(badge_rect)
            painter.setPen(QColor('white'))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "✓")

        if ui_path:
            name = ui_path.rsplit('/', 1)[-1]
            name_rect = QRect(option.rect.x(), img_rect.bottom() + 2,
                              option.rect.width(), _GRID_NAME_HEIGHT)
            fm = painter.fontMetrics()
            elided = fm.elidedText(name, Qt.TextElideMode.ElideMiddle,
                                   name_rect.width() - 4)
            painter.setPen(option.palette.highlightedText().color() if selected
                          else option.palette.text().color())
            painter.drawText(name_rect, Qt.AlignmentFlag.AlignHCenter, elided)
        painter.restore()


# ── MediaFullViewDialog ────────────────────────────────────────────────────────

class MediaFullViewDialog(QDialog):
    """Full-size image display, or video playback with basic transport
    controls. Two call sites, both non-modal (shown via .show(), not
    .exec(), so the underlying grid/table stays interactive while this is
    open) and both follow the examiner's selection into an already-open
    dialog via load_content rather than making them close and reopen for
    every file:

    - Media Browser's own thumbnail grid (MediaViewerMixin, this file):
      double-click a thumbnail to open, single-clicking a DIFFERENT
      thumbnail while the dialog is open swaps its content to that file.
    - Artifact Report media-column cells (artifact_viewer.py's
      ArtifactViewerMixin, added first, 2026-09-02): double-click a
      media-column cell to open, selecting a different row swaps it.

    Originally lived in artifact_media.py (the Artifact Viewer's own
    module) since that was the first caller; moved here 2026-09-14 when
    Media Browser grew its own call site — media_viewer.py is the more
    fundamental module (artifact_media.py already imports sniff_media_kind
    from here), so this avoids a circular import rather than needing one,
    and it's arguably more at home next to the thumbnail grid it now also
    serves directly. artifact_viewer.py imports it from here unchanged."""

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
        by __init__ (first open) and by a caller's own row/thumbnail
        selection handler to follow selection into an already-open dialog.
        Tears down whatever the PREVIOUS content needed (a running
        QMediaPlayer, a temp-file copy) before rebuilding; closeEvent below
        still handles final cleanup when the dialog itself closes."""
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
        # Sniff (not just extension) for the same reason ThumbnailWorker
        # does: a generic filename (e.g. Google Messages' MMS cache files,
        # always named "..._part_N_.bin" regardless of real content)
        # carries no usable extension.
        kind = sniff_media_kind(ext, data)
        if kind == 'video':
            self._build_video(layout, ui_path, data)
        elif kind == 'webpage':
            self._build_webpage(layout, ui_path, data)
        elif kind == 'text':
            self._build_text(layout, data)
        elif kind == 'image':
            self._build_image(layout, data, ext)
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

    def _build_image(self, layout, data: bytes, ext: str = '') -> None:
        img = _load_qimage(data, ext)
        if img is None:
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


# ── ThumbnailWorker ───────────────────────────────────────────────────────────

class ThumbnailWorker(QThread):
    """Loads thumbnails from the cache DB or the ZIP, emits (ui_path, QImage).

    Single-threaded by design: QImage.loadFromData() and img.scaled() are Qt
    Python-binding calls that hold the GIL, so a ThreadPoolExecutor would only
    serialise them anyway while adding contention overhead."""
    thumbnail_ready = Signal(str, object)   # ui_path, QImage
    finished_all    = Signal()

    def __init__(self, zip_path, items, path_resolver, thumb_size, zip_info_map,
                 cache_dir=None, local_path_overrides=None):
        super().__init__()
        self.zip_path        = zip_path
        self.items           = items
        self.path_resolver   = path_resolver
        self.thumb_size      = thumb_size
        self.zip_info_map    = zip_info_map
        self.cache_dir       = cache_dir
        # {ui_path: real_local_absolute_path} for a SYNTHETIC vpath that
        # isn't itself absolute (unlike a parser-generated file's own
        # ui_path, e.g. chrome_favicons.py's, which already handles the
        # local case via a bare os.path.isabs(ui_path) check below) but
        # still has real bytes sitting on local disk rather than inside
        # the archive — added 2026-09-22 for embedded-media hits browsed
        # via their container's own File Browser folder (as opposed to
        # the standalone "Embedded Media" button, which already passes
        # real absolute paths as ui_path and needed no change here).
        self.local_path_overrides = local_path_overrides or {}
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

            # Read via the local .zcd central-directory cache only — never
            # a raw zipfile.ZipFile on the main archive (this project's
            # own standing Convention has no exception for this case,
            # even to avoid a dropped thumbnail). The main FFS archive
            # itself is never compressed, so this is a direct offset seek
            # per entry rather than a second full central-directory
            # read/decompress pass over what can be a large network-
            # hosted zip. _zf stays None when cache_dir is missing or the
            # cache isn't built yet — every real read below already goes
            # through its own `try/except: continue` (an item without a
            # loadable thumbnail is already a normal, handled case, e.g.
            # a referenced-but-uncached iCloud photo), so a None _zf
            # simply skips every item the same honest way rather than
            # needing its own separate handling.
            _view = None
            if self.cache_dir:
                infos = _zcd_load(self.zip_path, self.cache_dir)
                if infos is not None:
                    _view = CachedZipView(self.zip_path, infos)
            _zf = _view
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

                        # A parser-generated local file (e.g. chrome_favicons.py's
                        # own extracted .png, chrome_cache.py's .mhtml) is never
                        # an archive entry -- path_resolver()/the zip's own
                        # namelist have nothing to resolve it against. Same
                        # os.path.isabs() convention hex_viewer._read_zip_bytes
                        # already uses for the identical reason. A SYNTHETIC
                        # vpath (e.g. an embedded-media hit browsed via its
                        # container's own File Browser folder) isn't itself
                        # absolute, but local_path_overrides still names its
                        # real local file — checked first since it's the more
                        # specific case.
                        real_path = self.local_path_overrides.get(ui_path)
                        is_local = real_path is not None or os.path.isabs(ui_path)
                        if is_local:
                            real_path = real_path or ui_path
                            try:
                                file_size = os.path.getsize(real_path)
                            except OSError:
                                continue
                            physical = real_path
                        else:
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
                            if is_local:
                                with open(real_path, 'rb') as lf:
                                    data = lf.read()
                            else:
                                # .open(...).read(), not .read(name) -- the
                                # convenience method zipfile.ZipFile has but
                                # CachedZipView deliberately doesn't
                                # duplicate; .open() alone keeps this line
                                # identical for either backing object.
                                data = _zf.open(physical).read()
                        except Exception:
                            continue

                        kind = sniff_media_kind(ext, data)
                        if kind == 'video':
                            data = _video_frame_bytes(data)
                            if not data:
                                continue
                        elif kind != 'image':
                            continue

                        img = _load_qimage(data, ext)
                        if img is None:
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
                # CachedZipView holds no real handle of its own (each read
                # opens/closes its own file internally) — nothing to
                # close here now that the raw-zipfile fallback is gone.
                pass

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

    # How many files make up one page — pagination only kicks in at all
    # once a folder's media count exceeds this; direct request, 2026-09-24:
    # "the page[nation] would only kick if there was more th[a]n 500 media
    # files selected" (the number itself was also the user's own). Now a
    # user preference (Preferences ▸ Media Browser, _media_page_size_pref
    # above) rather than a fixed constant — see that function's own
    # docstring for the direct follow-up that prompted this. self.
    # _media_page_size (set below in _setup_media_tab, re-snapshotted per
    # folder load in _start_thumbnail_load) is what every other method in
    # this class actually reads; _MEDIA_PAGE_SIZE_DEFAULT is only the
    # fallback used before the first real folder is ever loaded.

    def _setup_media_tab(self, status_style: str) -> QWidget:
        """Build the media-browser tab widget and initialise all media instance state.
        Returns the tab QWidget to be added to center_tabs.

        Uses a virtualized QListView grid (MediaFileListModel/
        MediaGridDelegate, both above) rather than one real QWidget per
        file, PLUS pagination (_load_media_page and friends) rather than
        loading a whole huge folder's thumbnails at once — see the
        module-level comment block above for the two real, separate
        fixes this represents (2026-09-24: the initial widget-count
        freeze fix, then the follow-up "does not really work... i want
        the viewer to be buttery smooth" replacing this class's own
        first loading strategy with the user's own proposed page-based
        one)."""
        self._thumb_worker: ThumbnailWorker | None = None
        self._media_page_prefetch_worker: ThumbnailWorker | None = None
        self._selected_media_path: str | None = None
        self._pending_media_selection: str | None = None
        self._media_full_dialog: MediaFullViewDialog | None = None
        self._media_context    = None
        self._media_total_files: int | None = None
        self._media_sort_desc: str = ""
        # The FULL folder's own media list (every page), vs. _media_model
        # which only ever holds the CURRENTLY DISPLAYED page — see
        # _load_media_page.
        self._media_all_paths: list = []
        self._media_page_index: int = 0
        self._media_page_size: int = _MEDIA_PAGE_SIZE_DEFAULT
        # The full context's own resolver maps, computed once per folder
        # load (cheap — no I/O, just dict comprehensions over already-
        # in-memory metadata) and reused by every page's own
        # ThumbnailWorker (both the current page's and the next page's
        # background prefetch) afterward.
        self._media_zip_info_map: dict = {}
        self._media_local_overrides: dict = {}

        # "Not Interested"/seen-tracking state (added 2026-09-24 — see
        # CLAUDE.md's own Media Browser Conventions entry). All four are
        # (re)populated per folder load in _start_thumbnail_load, never
        # stale across folders. _media_all_paths_unfiltered is the TRUE
        # full folder list (every media file); _media_all_paths (above)
        # is the ACTIVE list after the hide-seen filter, if any, is
        # applied — see _recompute_media_all_paths.
        self._media_all_paths_unfiltered: list = []
        self._media_seen_paths: set = set()
        self._media_bookmarked_paths: set = set()
        self._media_hide_seen: bool = False
        self._media_last_seen_batch: list | None = None
        self._media_last_seen_batch_page: int = 0
        # ui_path -> hex color for a bookmarked file's grid outline
        # (added 2026-09-25) — see MediaGridDelegate.set_bookmark_colors
        # and db_utils.load_bookmark_colors.
        self._media_bookmark_colors: dict = {}

        # "Last Selection" state (added 2026-09-25, direct request: "a
        # button similar to the show selected button in the media
        # browser that allows the user to go back to the previous
        # selection"). Snapshotted by _load_media_from_file_model
        # whenever the CURRENT file-browser view is itself a "selection"
        # (the checked-folders aggregate view, or a bookmark group — see
        # that method's own docstring for the exact predicate) rather
        # than a single plain folder, so the examiner can return to it
        # later without re-selecting from scratch. _media_showing_selection
        # protects the restored view from _on_center_tab_changed's own
        # tab-switch reload logic, same convention _media_showing_embedded
        # already established for the Embedded Media button.
        self._media_last_selection_paths: list | None = None
        self._media_last_selection_label: str = ""
        self._media_showing_selection: bool = False

        self._media_model = MediaFileListModel()
        self._media_delegate = MediaGridDelegate(THUMB_SIZE)

        self._media_status = QLabel("Select a folder to view media")
        self._media_status.setStyleSheet(status_style)

        # Embedded-media sweep review button (added 2026-09-22 — see
        # app/embedded_media_scan.py and CLAUDE.md's own "Embedded-media
        # sweep" Conventions entry). Deliberately reuses this SAME grid/
        # ThumbnailWorker/MediaFullViewDialog pipeline rather than a
        # bespoke viewer — every extracted hit's `extracted_path` is a
        # real local absolute path, and this pipeline already handles
        # os.path.isabs() paths transparently (built earlier for Chrome
        # Cache Media/Favicons' own parser-generated local files), so no
        # changes were needed to the thumbnail/full-view code itself.
        self._media_showing_embedded = False
        self._embedded_media_btn = QPushButton("Embedded Media")
        self._embedded_media_btn.setVisible(False)
        self._embedded_media_btn.clicked.connect(self._show_embedded_media_hits)
        # "◀ Last Selection" (added 2026-09-25) — see this class's own
        # _media_last_selection_paths docstring above for what counts as
        # a "selection." Hidden until a selection has actually been
        # snapshotted; clicking it shows only the UNSEEN files from that
        # selection, regardless of the global "hide seen files"
        # preference — see _on_media_last_selection_clicked.
        self._media_last_selection_btn = QPushButton("◀ Last Selection")
        self._media_last_selection_btn.setVisible(False)
        self._media_last_selection_btn.setToolTip(
            "Return to the last file selection (bookmark group or "
            "checked-folders view) — showing only files not yet seen")
        self._media_last_selection_btn.clicked.connect(
            self._on_media_last_selection_clicked)
        status_row = QHBoxLayout()
        status_row.addWidget(self._media_status, 1)
        status_row.addWidget(self._media_last_selection_btn)
        status_row.addWidget(self._embedded_media_btn)
        status_row_widget = QWidget()
        status_row_widget.setLayout(status_row)

        # Page navigation row — hidden entirely for a folder with
        # <= _MEDIA_PAGE_SIZE media files (see _load_media_page), so a
        # small folder looks exactly as it always did, no new UI in the
        # way.
        self._media_page_prev_btn = QPushButton("◀ Prev")
        self._media_page_prev_btn.clicked.connect(self._on_media_prev_page)
        self._media_page_label = QLabel("")
        self._media_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._media_page_next_btn = QPushButton("Next ▶")
        self._media_page_next_btn.clicked.connect(self._on_media_next_page)
        self._media_page_next_btn.setToolTip(
            "Scroll to the bottom of this page to continue")
        # "Not Interested ▶" / "↺ Undo" — added 2026-09-24, direct
        # request: a button that marks every file on the CURRENT page
        # (except any bookmarked one — a bookmark is itself a statement
        # that the file IS of interest) as "seen" and moves on, plus an
        # Undo for the last such batch. Scoped to the page-nav row (only
        # ever visible when the folder is paginated) per the literal
        # "next to the next button" framing — a single-page folder has
        # no page to bulk-dismiss in the first place.
        self._media_not_interested_btn = QPushButton("Not Interested ▶")
        self._media_not_interested_btn.setToolTip(
            "Mark every file on this page as seen (except bookmarked "
            "ones) and move to the next page")
        self._media_not_interested_btn.clicked.connect(
            self._on_media_not_interested)
        self._media_undo_seen_btn = QPushButton("↺ Undo")
        self._media_undo_seen_btn.setToolTip(
            "Undo the last \"Not Interested\" batch")
        self._media_undo_seen_btn.setEnabled(False)
        self._media_undo_seen_btn.clicked.connect(self._on_media_undo_seen)
        page_nav_row = QHBoxLayout()
        page_nav_row.addWidget(self._media_page_prev_btn)
        page_nav_row.addWidget(self._media_page_label, 1)
        page_nav_row.addWidget(self._media_page_next_btn)
        page_nav_row.addWidget(self._media_not_interested_btn)
        page_nav_row.addWidget(self._media_undo_seen_btn)
        self._media_page_nav_widget = QWidget()
        self._media_page_nav_widget.setLayout(page_nav_row)
        self._media_page_nav_widget.setVisible(False)

        self._media_view = QListView()
        self._media_view.setModel(self._media_model)
        self._media_view.setItemDelegate(self._media_delegate)
        self._media_view.setViewMode(QListView.ViewMode.IconMode)
        self._media_view.setResizeMode(QListView.ResizeMode.Adjust)
        self._media_view.setMovement(QListView.Movement.Static)
        self._media_view.setFlow(QListView.Flow.LeftToRight)
        self._media_view.setWrapping(True)
        self._media_view.setUniformItemSizes(True)
        self._media_view.setGridSize(self._media_delegate.cell_size())
        self._media_view.setSpacing(4)
        # ExtendedSelection (not SingleSelection) — added 2026-09-24 for
        # bookmarking a multi-file selection ("keyboard shortcuts that
        # can be used to bookmark a selection of files... this should
        # work in file browser and media browser"). A plain click still
        # behaves exactly as before (selects just that one item, clearing
        # any others) — Qt's own default ExtendedSelection behavior for
        # an unmodified click; Ctrl/Shift-click add to or range-extend
        # the selection, same convention as the File Browser's own table.
        self._media_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._media_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._media_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._media_view.clicked.connect(self._on_media_item_clicked)
        self._media_view.doubleClicked.connect(self._on_media_item_double_clicked)
        self._media_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._media_view.customContextMenuRequested.connect(self._show_media_context_menu)
        # "Next" is gated on having scrolled to the bottom of the current
        # page — see _update_media_next_button_enabled's own docstring.
        # valueChanged catches an actual scroll; rangeChanged catches a
        # resize or the page's own content finishing layout, either of
        # which can change what "at the bottom" means without the user
        # having scrolled at all.
        self._media_view.verticalScrollBar().valueChanged.connect(
            self._update_media_next_button_enabled)
        self._media_view.verticalScrollBar().rangeChanged.connect(
            self._update_media_next_button_enabled)

        media_tab = QWidget()
        media_tab_layout = QVBoxLayout(media_tab)
        media_tab_layout.setContentsMargins(0, 4, 0, 0)
        media_tab_layout.setSpacing(2)
        media_tab_layout.addWidget(status_row_widget)
        media_tab_layout.addWidget(self._media_view, stretch=1)
        media_tab_layout.addWidget(self._media_page_nav_widget)
        return media_tab

    def _refresh_embedded_media_button(self):
        """Shows/labels the "Embedded Media" button with the real current
        hit count, or hides it entirely when there are none — same "never
        show a choice that doesn't apply yet" convention the ProcessDialog
        scope controls already follow. Cheap (COUNT query), safe to call
        on every case load and after a scan finishes."""
        count = 0
        if self._case_dir:
            try:
                with closing(_open_results_db(self._case_dir)) as db:
                    count = db.execute(
                        'SELECT COUNT(*) FROM embedded_media_hits').fetchone()[0]
            except Exception:
                count = 0
        self._embedded_media_btn.setVisible(count > 0)
        self._embedded_media_btn.setText(f"Embedded Media ({count:,})")

    def _show_embedded_media_hits(self):
        """Loads every recorded embedded-media hit into the SAME
        thumbnail grid a folder's media normally uses — see this
        method's own module-level Conventions entry for why no new
        thumbnail/full-view code was needed. Sets _media_showing_embedded
        so _on_center_tab_changed's own tab-switch reload logic (which
        compares against the CURRENT FOLDER's media files) leaves this
        view alone rather than silently reverting to the last-selected
        folder the next time the examiner switches tabs away and back;
        cleared by _load_media_from_file_model itself, the moment the
        examiner picks an ordinary folder again.

        Uses the SAME synthetic vpaths _inject_embedded_media already
        computed (container/display_name, with per-container name
        collisions already disambiguated) as the grid's own ui_path for
        each item — added 2026-09-23, direct follow-up: this used to pass
        each hit's raw content-hash extracted_path (e.g.
        ".../embedded_media/95/9535bf70....jpg") straight through as
        ui_path, so the status bar (_select_media_item's own
        `self.status_bar.showMessage(ui_path)`) and every tooltip showed
        a meaningless hash filename instead of the real
        "<container>/<display_name>" path an identical click coming from
        the File Browser hierarchy already shows. Re-injecting here
        (cheap — a DB read plus dict rebuilding, same cost
        _refresh_embedded_media_button's own COUNT query already pays
        every time this button is shown) rather than trusting whatever
        the last injection happened to leave in place keeps this button
        correct even if it's clicked in the same session a scan just
        finished, before any other trigger has re-run it."""
        if not self._case_dir:
            return
        self._inject_embedded_media()
        media_paths = [
            vpath
            for container in sorted(self._embedded_media_containers)
            for vpath in self.folder_map.get(container, [])
            if os.path.isfile(self.full_metadata.get(vpath, {}).get(
                '_embedded_media_source', ''))
            # Excludes a genuinely 0-byte hit — same "don't show it in the
            # viewer" rule _is_media_file applies to an ordinary folder's
            # media; a 0-byte extracted file can never decode to a real
            # thumbnail either way.
            and self.full_metadata.get(vpath, {}).get('size', 0) > 0
        ]
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                recovered = sum(1 for h in load_embedded_media_hits(db) if h.get('recovered'))
        except Exception:
            recovered = 0
        self._media_showing_embedded = True
        self._media_context = tuple(media_paths)
        # _start_thumbnail_load (below) renders the final status text
        # ("{count} media file(s) of {total} file(s){sort_desc}")
        # immediately -- model population needs no decode wait, see that
        # method's own docstring -- so this is passed through rather than
        # set directly here.
        sort_desc = (f" from the embedded-media sweep"
                    + (f" — {recovered:,} recovered from deleted content"
                       if recovered else ""))
        self._start_thumbnail_load(media_paths, len(media_paths), sort_desc)

    def _is_media_file(self, ui_path: str) -> bool:
        """True if ui_path counts as media — either by extension (the
        common, cheap case) or by its header-scan-derived type override
        (added 2026-09-23, direct report: "the media viewer only uses
        file ext to determine if the file is a media file... even though
        we have scanned the headers and labeled them as media files").
        Confirmed real before fixing: _header_type_overrides is exactly
        the same magic-byte-derived dict the File Browser's own Type
        column already reads (_classify_entry), populated by a Tier
        1/2/3 header scan for a file whose extension is missing or
        wrong — every MEDIA_EXTENSIONS check in this project used to
        skip it. The single shared predicate here (not a copy per call
        site — same "one shared predicate, never two that could drift"
        principle _header_candidate_matches already established) is
        used by _load_media_from_file_model below AND by
        ffs-explorer.py's own _on_center_tab_changed, which computes an
        equivalent "what does the Media Browser currently show" tuple to
        decide whether a tab switch needs a reload — those two
        computations silently disagreeing would either skip a needed
        reload or force an unneeded one.

        Once a path passes this gate, the actual thumbnail decode
        already handles it correctly regardless of extension —
        ThumbnailWorker calls sniff_media_kind(ext, data) with the real
        bytes already loaded, and that function's own magic-byte
        fallback needed no change.

        Also excludes a genuinely 0-byte file — direct request,
        2026-09-24: "if there are 0 byte files do not show them in the
        viewer." A 0-byte file can never decode to a real thumbnail
        (nothing for ThumbnailWorker to read), so it would only ever show
        as a permanent blank cell; checked here, in the one shared
        predicate, rather than as a separate filter in
        _load_media_from_file_model alone, so ffs-explorer.py's own
        _on_center_tab_changed keeps computing the identical "what should
        the grid show" set this docstring already requires — a size
        check added in only one of the two places would silently
        reintroduce exactly the kind of drift this predicate exists to
        prevent."""
        if os.path.splitext(ui_path)[1].lower() in MEDIA_EXTENSIONS:
            is_media = True
        else:
            is_media = self._header_type_overrides.get(ui_path) in ('Picture', 'Video')
        if not is_media:
            return False
        return self.full_metadata.get(ui_path, {}).get('size', 0) > 0

    def _load_media_from_file_model(self):
        """Load the media tab using exactly the current visible file model
        rows. Also exits the "Last Selection"/Embedded Media alternate
        views — picking an ordinary folder (or an aggregate/bookmark view,
        see below) is what "picks an ordinary folder again" means in both
        of those features' own docstrings."""
        self._media_showing_embedded = False
        self._media_showing_selection = False
        model = self.file_model

        total_files = sum(1 for r in model._rows if r[1] not in self.folder_map)
        media_paths = [
            r[1] for r in model._rows
            if r[1] not in self.folder_map
            and self._is_media_file(r[1])
        ]

        self._media_context = tuple(media_paths)

        # Snapshot this as the "Last Selection" — added 2026-09-25 —
        # whenever the CURRENT file-browser view is itself an explicit
        # multi-item SELECTION rather than one plain folder: either the
        # checked-folders aggregate view (_view_is_recursive is only ever
        # set True by _rebuild_file_view_from_checked, i.e. "Show Selected
        # Files") or a bookmark group (_view_path prefixed _BM_GROUP_PREFIX,
        # set by _show_bookmark_group). An ordinary single-folder
        # navigation never qualifies, so browsing around afterward doesn't
        # keep overwriting this with "the last folder I happened to look
        # at" — only a genuine selection counts.
        is_selection = bool(media_paths) and (
            getattr(self, '_view_is_recursive', False)
            or (getattr(self, '_view_path', '') or '').startswith(_BM_GROUP_PREFIX))
        if is_selection:
            self._media_last_selection_paths = list(media_paths)
            self._media_last_selection_label = self.status_bar.currentMessage() or "Last selection"
            self._media_last_selection_btn.setToolTip(
                f"Return to: {self._media_last_selection_label}\n"
                "(showing only files not yet seen)")
        self._media_last_selection_btn.setVisible(
            bool(self._media_last_selection_paths))

        if 0 <= model._sort_col < len(model._headers):
            arrow = "↑" if model._sort_order == Qt.SortOrder.AscendingOrder else "↓"
            sort_desc = f", sorted by {model._headers[model._sort_col]} {arrow}"
        else:
            sort_desc = ""

        self._start_thumbnail_load(media_paths, total_files, sort_desc)

    def _on_media_last_selection_clicked(self) -> None:
        """"◀ Last Selection" — added 2026-09-25, direct request: "a
        button... that allows the user to go back to the previous
        selection[;]... it should show all the files in the last
        selection that have not been viewed." Filters the snapshotted
        selection down to files NOT in _media_seen_paths — always, for
        this button specifically, regardless of the global "hide seen
        files" preference (_media_hide_seen), since the whole point here
        is "show me what I haven't looked at yet from that batch," not a
        display preference. Loads _media_seen_paths fresh first (same
        query _start_thumbnail_load always runs) so a file marked seen
        moments ago is correctly excluded even if the preference itself
        is off."""
        if not self._case_dir or not self._media_last_selection_paths:
            return
        try:
            with closing(_open_results_db(self._case_dir)) as conn:
                seen = load_seen_media_paths(conn)
        except Exception:
            seen = set()
        unseen = [p for p in self._media_last_selection_paths if p not in seen]
        self._media_showing_selection = True
        self._media_context = tuple(unseen)
        n_total = len(self._media_last_selection_paths)
        n_seen = n_total - len(unseen)
        sort_desc = f" from your last selection ({n_seen:,} already seen, hidden)" \
            if n_seen else " from your last selection"
        self._start_thumbnail_load(unseen, n_total, sort_desc)

    def _start_thumbnail_load(self, media_paths, total_files=None, sort_desc=""):
        """Records *media_paths* as the folder's own FULL media list
        (_media_all_paths — not necessarily what's shown, once paginated)
        and loads the first page (or the page containing a pending File-
        Browser-driven selection, if one's waiting). See _load_media_page
        for the actual per-page work; this method's own job is just the
        once-per-folder-load setup (resolver maps, the overall status
        text, retiring any stale workers from the previous folder)."""
        self._retire_worker(self._thumb_worker)
        self._thumb_worker = None
        self._retire_worker(self._media_page_prefetch_worker)
        self._media_page_prefetch_worker = None
        self._media_delegate.clear()
        self._selected_media_path = None
        self._media_all_paths_unfiltered = list(media_paths)
        # A "Not Interested" batch only ever applies to the folder it was
        # clicked in — a fresh folder load starts with nothing to undo.
        self._media_last_seen_batch = None
        self._media_undo_seen_btn.setEnabled(False)
        # Snapshotted once per folder load, not re-read mid-navigation —
        # see _media_page_size_pref's own docstring for why.
        self._media_page_size = _media_page_size_pref()
        self._media_hide_seen = _media_hide_seen_pref()

        if not media_paths or not self.zip_path:
            self._media_all_paths = []
            self._media_seen_paths = set()
            self._media_bookmarked_paths = set()
            self._media_bookmark_colors = {}
            self._media_delegate.set_seen_paths(set())
            self._media_delegate.set_bookmark_colors({})
            self._media_page_index = 0
            self._media_model.set_items([])
            self._media_page_nav_widget.setVisible(False)
            self._media_status.setText(
                "No media files" if self.zip_path else "Select a folder to view media")
            self._clear_hex_preview()
            return

        # Loaded fresh from caseresults.db on every folder load (not
        # cached across folders) — cheap (three small SELECTs) and means a
        # "Not Interested"/bookmark change made elsewhere in the same
        # session is always picked up correctly here.
        try:
            with closing(_open_results_db(self._case_dir)) as conn:
                self._media_seen_paths = load_seen_media_paths(conn)
                self._media_bookmarked_paths = load_all_bookmarked_paths(conn)
                self._media_bookmark_colors = load_bookmark_colors(conn)
        except Exception:
            self._media_seen_paths = set()
            self._media_bookmarked_paths = set()
            self._media_bookmark_colors = {}
        self._media_delegate.set_seen_paths(self._media_seen_paths)
        self._media_delegate.set_bookmark_colors(self._media_bookmark_colors)

        self._recompute_media_all_paths()

        self._media_total_files = total_files
        self._media_sort_desc   = sort_desc
        of_total = f" of {total_files:,} file(s)" if total_files is not None else ""
        hidden_count = len(self._media_all_paths_unfiltered) - len(self._media_all_paths)
        hidden_note = f" ({hidden_count:,} hidden as seen)" if hidden_count else ""
        self._media_status.setText(
            f"{len(media_paths):,} media file(s){of_total}{sort_desc}{hidden_note}")

        self._media_zip_info_map = {
            self._adapter.resolve(p): self.full_metadata.get(p, {}).get('size', 0)
            for p in media_paths
        }
        # Embedded-media hits browsed via their container's own File
        # Browser folder (as opposed to the standalone "Embedded Media"
        # button, which already passes real absolute paths and needs no
        # override) — see ThumbnailWorker's own local_path_overrides
        # docstring.
        self._media_local_overrides = {
            p: src for p in media_paths
            if (src := self.full_metadata.get(p, {}).get('_embedded_media_source'))
        }

        target_page = 0
        pending = self._pending_media_selection
        if pending and pending in self._media_all_paths:
            target_page = self._media_all_paths.index(pending) // self._media_page_size
        self._load_media_page(target_page)

        if pending and pending in self._media_all_paths:
            self._select_media_item(pending)
        self._pending_media_selection = None

    def _recompute_media_all_paths(self) -> None:
        """Re-derives the ACTIVE _media_all_paths from the folder's TRUE
        full list (_media_all_paths_unfiltered) plus the current seen-set
        and the "hide seen files" preference — added 2026-09-24 for the
        Media Browser's "Not Interested" feature. Called at folder load
        and again after any seen-state change (mark/undo) so the hide-seen
        filter is always LIVE within a browsing session, not just applied
        once when the folder was first opened."""
        if self._media_hide_seen:
            self._media_all_paths = [
                p for p in self._media_all_paths_unfiltered
                if p not in self._media_seen_paths]
        else:
            self._media_all_paths = list(self._media_all_paths_unfiltered)

    def _refresh_media_bookmark_badges(self) -> None:
        """Re-reads bookmark state from caseresults.db and updates the
        currently-displayed grid's own outline colors — added 2026-09-25,
        called from ffs-explorer.py after any bookmark add/delete/color
        change so the Media Browser reflects it immediately, without
        needing a full folder reload (which would also needlessly re-run
        the seen-state query and reset scroll position). A cheap no-op
        when there's no case open yet, or the tab has never loaded
        anything."""
        if not self._case_dir:
            return
        try:
            with closing(_open_results_db(self._case_dir)) as conn:
                self._media_bookmarked_paths = load_all_bookmarked_paths(conn)
                self._media_bookmark_colors = load_bookmark_colors(conn)
        except Exception:
            return
        self._media_delegate.set_bookmark_colors(self._media_bookmark_colors)
        self._media_view.viewport().update()

    def _load_media_page(self, page_index: int) -> None:
        """Loads page *page_index* (_MEDIA_PAGE_SIZE files at a time) —
        the core of the "buttery smooth" redesign, 2026-09-24, direct
        follow-up to the first (viewport-tracking) loading strategy not
        actually working well in practice: "the new media viewer does not
        really work... what do you think about [pagination]... i want the
        viewer to be buttery smooth." Eagerly decodes the WHOLE current
        page at once (bounded to _MEDIA_PAGE_SIZE, so this is always a
        small, fast, predictable batch — no viewport tracking needed at
        this size) and, once that finishes, starts prefetching the NEXT
        page's own thumbnails in the background (_prefetch_next_page) so
        a later Next click is normally an instant, already-decoded
        reveal. MediaGridDelegate's pixmap cache is trimmed to roughly
        this page plus its immediate neighbors on every call — bounded
        regardless of how many pages the examiner has paged through in
        one session, the same "smaller amount in memory" goal as before,
        just anchored to page boundaries instead of the viewport."""
        total = len(self._media_all_paths)
        if total == 0:
            self._media_page_index = 0
            self._media_model.set_items([])
            self._media_page_nav_widget.setVisible(False)
            return

        page_size = self._media_page_size
        n_pages = max(1, -(-total // page_size))   # ceil division
        page_index = max(0, min(page_index, n_pages - 1))
        self._media_page_index = page_index

        self._retire_worker(self._thumb_worker)
        self._thumb_worker = None
        self._retire_worker(self._media_page_prefetch_worker)
        self._media_page_prefetch_worker = None

        start = page_index * page_size
        end = min(total, start + page_size)
        page_items = self._media_all_paths[start:end]

        # Keep this page, the previous one (a quick Back shouldn't
        # re-decode), and the next one (already being prefetched below) —
        # evict everything else so memory stays bounded to roughly 3
        # pages regardless of how far the examiner has paged.
        keep = set(page_items)
        if start > 0:
            keep |= set(self._media_all_paths[max(0, start - page_size):start])
        if end < total:
            keep |= set(self._media_all_paths[end:min(total, end + page_size)])
        self._media_delegate.evict_except(keep)

        self._media_model.set_items(page_items)
        self._media_view.scrollToTop()

        paginated = n_pages > 1
        self._media_page_nav_widget.setVisible(paginated)
        # "Back" is always available once page_index > 0 — direct
        # request, 2026-09-24: "they can go back at any time." "Next" is
        # gated on having scrolled to the bottom of THIS page
        # (_update_media_next_button_enabled, wired to the scrollbar's
        # own valueChanged/rangeChanged in _setup_media_tab) — "only let
        # the user move to next page when they are at the bottom."
        self._media_page_prev_btn.setEnabled(page_index > 0)
        self._update_media_next_button_enabled()

        to_fetch = [p for p in page_items if not self._media_delegate.has_pixmap(p)]
        if paginated:
            if to_fetch:
                self._media_page_label.setText(
                    f"Page {page_index + 1} of {n_pages} — loading "
                    f"{len(to_fetch):,} thumbnail(s)…")
            else:
                self._media_page_label.setText(
                    f"Page {page_index + 1} of {n_pages} (showing "
                    f"{start + 1:,}–{end:,} of {total:,})")

        if to_fetch:
            worker = ThumbnailWorker(
                self.zip_path, to_fetch, self._adapter.resolve, THUMB_SIZE,
                self._media_zip_info_map, cache_dir=self._case_dir,
                local_path_overrides=self._media_local_overrides)
            worker.thumbnail_ready.connect(self._on_thumbnail_ready)
            worker.finished_all.connect(self._on_page_decode_finished)
            self._thumb_worker = worker
            worker.start()
        else:
            self._prefetch_next_page()

    def _on_page_decode_finished(self) -> None:
        """The current page's own ThumbnailWorker has decoded everything
        it was asked to (thumbnail_ready already updated the grid as each
        one finished) — update the page label to its final "showing A-B
        of N" text and start prefetching the next page. Only ever
        connected to the CURRENT page's own worker, whose signals
        _retire_worker already fully disconnects the moment a newer page
        load supersedes it, so this never fires late for a page the
        examiner has since navigated away from."""
        total = len(self._media_all_paths)
        page_size = self._media_page_size
        n_pages = max(1, -(-total // page_size))
        page_index = self._media_page_index
        start = page_index * page_size
        end = min(total, start + page_size)
        if n_pages > 1:
            self._media_page_label.setText(
                f"Page {page_index + 1} of {n_pages} (showing "
                f"{start + 1:,}–{end:,} of {total:,})")
        self._prefetch_next_page()

    def _prefetch_next_page(self) -> None:
        """Starts decoding the NEXT page's own thumbnails in the
        background while the CURRENT page is what's actually on screen —
        direct request, 2026-09-24: "for the next page it is already
        cach[e]ing them while you are viewing the first page." Results
        land in MediaGridDelegate's own pixmap cache
        (_on_prefetch_thumbnail_ready) but never touch the model (these
        items aren't the currently displayed page), so a later Next click
        (_load_media_page) finds them already decoded and reveals near-
        instantly rather than waiting on a fresh decode."""
        total = len(self._media_all_paths)
        page_size = self._media_page_size
        next_start = (self._media_page_index + 1) * page_size
        if next_start >= total:
            return   # already on the last page
        next_end = min(total, next_start + page_size)
        next_items = self._media_all_paths[next_start:next_end]
        to_fetch = [p for p in next_items if not self._media_delegate.has_pixmap(p)]
        if not to_fetch:
            return

        self._retire_worker(self._media_page_prefetch_worker)
        worker = ThumbnailWorker(
            self.zip_path, to_fetch, self._adapter.resolve, THUMB_SIZE,
            self._media_zip_info_map, cache_dir=self._case_dir,
            local_path_overrides=self._media_local_overrides)
        worker.thumbnail_ready.connect(self._on_prefetch_thumbnail_ready)
        self._media_page_prefetch_worker = worker
        worker.start()

    def _on_prefetch_thumbnail_ready(self, ui_path, img) -> None:
        self._media_delegate.set_pixmap(ui_path, QPixmap.fromImage(img))

    def _update_media_next_button_enabled(self) -> None:
        """Gates "Next" on having scrolled to the bottom of the CURRENT
        page — direct request, 2026-09-24: "only let the user move to
        next page when they are at the bottom[;] they can go back at any
        time but that mean[s] they are at the top." "Back" has no such
        gate (see _load_media_page's own unconditional `page_index > 0`
        check) — it's always available once there IS a previous page, and
        always lands at the top of it (_load_media_page's own
        scrollToTop), never mid-scroll, so arriving via Back always looks
        the same regardless of where the examiner clicked it from.

        "At the bottom" tolerates a couple of pixels of rounding (an
        exact `value() == maximum()` can be flaky depending on how Qt
        rounds the last frame's own geometry) and treats a page whose
        content fits entirely within the viewport (nothing to scroll,
        `maximum() <= 0`) as already at the bottom — the examiner has
        necessarily already seen everything on such a page, so there's no
        real "keep scrolling" gate left to apply."""
        total = len(self._media_all_paths)
        if total == 0:
            self._media_page_next_btn.setEnabled(False)
            return
        page_size = self._media_page_size
        n_pages = max(1, -(-total // page_size))
        if self._media_page_index >= n_pages - 1:
            self._media_page_next_btn.setEnabled(False)
            return
        sb = self._media_view.verticalScrollBar()
        at_bottom = sb.maximum() <= 0 or sb.value() >= sb.maximum() - 2
        self._media_page_next_btn.setEnabled(at_bottom)

    def _on_media_prev_page(self) -> None:
        if self._media_page_index > 0:
            self._load_media_page(self._media_page_index - 1)

    def _on_media_next_page(self) -> None:
        page_size = self._media_page_size
        n_pages = max(1, -(-len(self._media_all_paths) // page_size))
        if self._media_page_index < n_pages - 1:
            self._load_media_page(self._media_page_index + 1)

    def _on_media_not_interested(self) -> None:
        """"Not Interested ▶" — added 2026-09-24, direct request: marks
        every file on the CURRENT page as seen, except any bookmarked one
        (a bookmark is itself a statement that the file IS of interest —
        it should never become hidden by "hide seen files" as a side
        effect of a bulk dismissal), then moves on. "Moves on" means: if
        the hide-seen filter is ON, the newly-seen files simply disappear
        from the active list and reloading THIS SAME page index naturally
        reveals whatever now slides into that slot (a deliberate
        simplification — no special-case "advance" logic needed); if the
        filter is OFF, the marked files stay visible (now badged) and the
        view advances to the next page as a plain, literal "move to the
        next page" action.

        The whole current page, not a partial one — Undo therefore always
        reverts exactly the batch this one click just marked, matching
        the literal request as closely as its own wording allows ("undo a
        hide of the last file they hide" is read here as the last BATCH,
        since marking only ever happens in whole-page batches, never
        per-file)."""
        page_size = self._media_page_size
        start = self._media_page_index * page_size
        end = min(len(self._media_all_paths), start + page_size)
        page_items = self._media_all_paths[start:end]
        to_mark = [p for p in page_items if p not in self._media_bookmarked_paths]
        if not to_mark:
            self.status_bar.showMessage(
                "Nothing to mark — every file on this page is bookmarked")
            return

        try:
            with closing(_open_results_db(self._case_dir)) as conn:
                mark_media_seen(conn, to_mark)
        except Exception:
            self.status_bar.showMessage("Could not save — see console for details")
            return

        self._media_seen_paths.update(to_mark)
        self._media_delegate.set_seen_paths(self._media_seen_paths)
        self._media_last_seen_batch = to_mark
        # Recorded BEFORE navigating away, so Undo can return to the page
        # this batch actually came from rather than wherever "Not
        # Interested" left the view afterward (a real bug found during
        # verification: with hide-seen OFF, marking page N advances to
        # page N+1, and Undo used to just reload "the current page" —
        # i.e. N+1 — never actually showing the just-restored files).
        self._media_last_seen_batch_page = self._media_page_index
        self._media_undo_seen_btn.setEnabled(True)

        n_skipped = len(page_items) - len(to_mark)
        skipped_note = f" ({n_skipped} bookmarked file(s) left as-is)" if n_skipped else ""
        self.status_bar.showMessage(
            f"Marked {len(to_mark):,} file(s) as seen{skipped_note}")

        if self._media_hide_seen:
            self._recompute_media_all_paths()
            self._load_media_page(self._media_page_index)
        else:
            self._load_media_page(self._media_page_index + 1)

    def _on_media_undo_seen(self) -> None:
        """Reverts exactly the last "Not Interested" batch — added
        2026-09-24, direct request: "a button that allows the user to
        undo a hide... so error can be undone." Never partial, never more
        than the one most recent click's own batch."""
        batch = self._media_last_seen_batch
        if not batch:
            return

        try:
            with closing(_open_results_db(self._case_dir)) as conn:
                unmark_media_seen(conn, batch)
        except Exception:
            self.status_bar.showMessage("Could not undo — see console for details")
            return

        self._media_seen_paths.difference_update(batch)
        self._media_delegate.set_seen_paths(self._media_seen_paths)
        self._media_last_seen_batch = None
        self._media_undo_seen_btn.setEnabled(False)
        self.status_bar.showMessage(f"Undid — {len(batch):,} file(s) no longer marked seen")

        if self._media_hide_seen:
            self._recompute_media_all_paths()
        self._load_media_page(self._media_last_seen_batch_page)

    def _on_thumbnail_ready(self, ui_path, img):
        row = self._media_model.row_of(ui_path)
        if row is None:
            return   # stale -- page/folder changed since this was requested
        self._media_delegate.set_pixmap(ui_path, QPixmap.fromImage(img))
        idx = self._media_model.index(row)
        self._media_model.dataChanged.emit(idx, idx)

    def _on_media_item_clicked(self, index) -> None:
        """A plain click behaves as it always has (select just this one
        item via _select_media_item). A Ctrl/Shift-click — extending a
        multi-selection, added 2026-09-24 for bookmarking a selection of
        files — instead only syncs the shared side panels
        (_sync_media_side_panels) to whichever item was just clicked,
        WITHOUT calling _select_media_item's own setCurrentIndex.
        Confirmed directly, not assumed: setCurrentIndex collapses an
        already-multi-selected set of items back down to just the one
        passed to it, even though it's called separately from the
        selection Qt's own mouse handling has already built by the time
        this `clicked` signal fires — checking QApplication.
        keyboardModifiers() here is what actually distinguishes the two
        cases, since the `clicked` signal itself carries no modifier
        info of its own."""
        ui_path = index.data(Qt.ItemDataRole.DisplayRole)
        if not ui_path:
            return
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier |
                       Qt.KeyboardModifier.ShiftModifier):
            self._sync_media_side_panels(ui_path)
        else:
            self._select_media_item(ui_path)

    def _sync_media_side_panels(self, ui_path: str) -> None:
        """The non-selection-model side effects of picking a media item —
        status bar, File Browser sync, hex preview, following an already-
        open full-view dialog. Split out of _select_media_item 2026-09-24
        so a Ctrl/Shift-click (extending a multi-selection) can update
        these without also calling setCurrentIndex, which would otherwise
        collapse the multi-selection back down to one item — see
        _on_media_item_clicked's own docstring for how that was confirmed,
        not assumed."""
        self._selected_media_path = ui_path
        self.status_bar.showMessage(ui_path)
        self._select_file_in_table(ui_path)
        self._load_hex_preview(ui_path)
        self._media_sync_open_dialog(ui_path)

    def _select_media_item(self, ui_path: str) -> None:
        """Selects *ui_path* in the grid — updates QListView's own
        selection (so the delegate paints the highlight; no manual
        set_selected bookkeeping needed the way the old widget-per-item
        grid required) and scrolls it into view — plus the same side
        effects a click always had (_sync_media_side_panels). Used both
        by a direct (unmodified) click and by a pending File-Browser-
        driven selection landing here with no click ever having happened
        — both cases WANT a single-item selection, unlike the Ctrl/Shift-
        click case _on_media_item_clicked handles separately.

        Since pagination (2026-09-24), *ui_path* may belong to the
        folder's own full media list (_media_all_paths) without being on
        the CURRENTLY DISPLAYED page — switches to whichever page
        actually contains it first (_load_media_page) rather than
        silently no-op'ing the way a bare model lookup would."""
        row = self._media_model.row_of(ui_path)
        if row is None:
            try:
                global_index = self._media_all_paths.index(ui_path)
            except ValueError:
                return
            self._load_media_page(global_index // self._media_page_size)
            row = self._media_model.row_of(ui_path)
            if row is None:
                return
        idx = self._media_model.index(row)
        self._media_view.setCurrentIndex(idx)
        self._media_view.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)
        self._sync_media_side_panels(ui_path)

    def _on_media_item_double_clicked(self, index) -> None:
        ui_path = index.data(Qt.ItemDataRole.DisplayRole)
        if ui_path:
            self._open_media_full_view(ui_path)

    def _get_media_paths_for_bookmark(self) -> list:
        """[(ui_path, display_name)] for the Media Browser's own current
        selection — the Media Browser equivalent of ffs-explorer.py's
        `_get_paths_for_bookmark` (File Browser), added 2026-09-24, direct
        request: "make it that a user can bookmark media file[s] in the
        media browser via right click... this should work in file
        browser and media browser." Scoped to whatever's selected on the
        CURRENTLY DISPLAYED PAGE only — selection can't span pages in the
        first place, since paging (2026-09-24) replaces the grid's own
        model entirely on every page change."""
        result: list = []
        seen: set = set()
        for index in self._media_view.selectionModel().selectedIndexes():
            ui_path = index.data(Qt.ItemDataRole.DisplayRole)
            if ui_path and ui_path not in seen:
                seen.add(ui_path)
                result.append((ui_path, ui_path.rsplit('/', 1)[-1]))
        return result

    def _show_media_context_menu(self, pos) -> None:
        """Right-click "Bookmarks" menu for the Media Browser grid —
        added 2026-09-24, direct request: "make it that a user can
        bookmark media file[s] in the media browser via right click."
        Right-clicking an item that ISN'T already part of the current
        selection replaces the selection with just that one first —
        ordinary file-manager convention, so the menu always visibly acts
        on whatever's actually selected; right-clicking WITHIN an
        existing multi-selection leaves it untouched, so a multi-file
        bookmark works via right-click too, not just Ctrl+B."""
        index = self._media_view.indexAt(pos)
        if index.isValid() and not self._media_view.selectionModel().isSelected(index):
            self._media_view.setCurrentIndex(index)
            self._media_view.selectionModel().select(
                index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        paths = self._get_media_paths_for_bookmark()
        if not paths:
            return
        menu = QMenu(self._media_view)
        self._bookmark_submenu(menu, paths)
        menu.exec(self._media_view.viewport().mapToGlobal(pos))

    def _open_media_full_view(self, ui_path: str) -> None:
        """Open the full-size image/video viewer for *ui_path* — non-modal
        (.show(), not .exec()) and tracked on self so a plain single-click
        selecting a DIFFERENT thumbnail can keep swapping this same open
        dialog's content instead of it blocking the grid (see
        _media_sync_open_dialog, called from _select_media_item above),
        same "open viewer follows selection" convention artifact_viewer.py's
        Report table already established for its own media columns. A
        second double-click while one is already open reuses that same
        window (load_content + raise) rather than stacking another."""
        data = self._read_zip_bytes(ui_path)
        if data is None:
            self.status_bar.showMessage(f"File not found in archive: {ui_path}", 5000)
            return

        dialog = self._media_full_dialog
        if dialog is not None and dialog.isVisible():
            dialog.load_content(ui_path, data)
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = MediaFullViewDialog(ui_path, data, parent=self)
        dialog.finished.connect(self._on_media_full_dialog_closed)
        self._media_full_dialog = dialog
        dialog.show()

    def _on_media_full_dialog_closed(self, _result=None) -> None:
        self._media_full_dialog = None

    def _media_sync_open_dialog(self, ui_path: str) -> None:
        """If the full-size viewer is currently open, keep it following
        thumbnail selection: swap its content to whatever was just clicked
        instead of leaving it showing the previous file. Silently does
        nothing if no dialog is open — a plain click elsewhere in the app
        shouldn't pop it open, only a double-click (_open_media_full_view)
        or a further single-click while it's already showing."""
        dialog = self._media_full_dialog
        if dialog is None or not dialog.isVisible():
            return
        data = self._read_zip_bytes(ui_path)
        if data is None:
            return
        dialog.load_content(ui_path, data)

    def _resync_media_hex_preview(self) -> None:
        """Restore Media Browser's own last-selected file's hex into the
        shared bottom panel on switching back to this tab, or clear it if
        nothing has been selected here yet — same reasoning as the other
        three tabs' resyncs (see "Per-tab state on switching" in
        CLAUDE.md). Needed as its own call, separate from _select_media_item
        above: the existing thumbnail-grid reload logic in
        _on_center_tab_changed only re-selects when a FILE BROWSER
        selection is pending sync into Media Browser — a plain "switch
        back to Media Browser, nothing changed, no pending File Browser
        selection" pass touches neither, which would otherwise leave
        whatever another tab last put in the shared panel showing here
        instead. Checks _media_all_paths (the whole folder), not just the
        currently displayed page's own model — a selection on a page the
        examiner has since paged away from is still a real, restorable
        selection, not a stale one."""
        if self._selected_media_path and \
                self._selected_media_path in self._media_all_paths:
            self._load_hex_preview(self._selected_media_path)
        else:
            self._clear_hex_preview()
