"""media_viewer.py — thumbnail worker, media grid widget, and FastZipBrowser mixin."""

import io
import os
import shutil
import sqlite3
import tempfile
from itertools import batched

import av

from db_utils import _open_cache_db
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
    QWidget, QLabel, QScrollArea, QGridLayout, QVBoxLayout,
    QDialog, QHBoxLayout, QPushButton, QSlider, QTextEdit,
)
from PySide6.QtGui import QImage, QPixmap, QFontDatabase
from PySide6.QtCore import Qt, QThread, Signal, QBuffer, QIODevice, QTimer, QUrl

# ── Constants ─────────────────────────────────────────────────────────────────

THUMB_SIZE         = 160   # thumbnail box size in pixels
_THUMB_BATCH_COMMIT = 20   # inserts to accumulate before a single db.commit()


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


# ── ClickableThumb ────────────────────────────────────────────────────────────

class ClickableThumb(QWidget):
    """A thumbnail container that emits clicked(ui_path) on a single press
    and doubleClicked(ui_path) on a double-click — Qt delivers a
    mousePressEvent for both halves of a double-click, so clicked always
    fires first (the normal select-this-thumbnail behavior) followed by
    doubleClicked (open the full viewer), never the reverse."""
    clicked = Signal(str)
    doubleClicked = Signal(str)

    def __init__(self, ui_path: str, parent=None):
        super().__init__(parent)
        self._ui_path = ui_path
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._ui_path)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(self._ui_path)
        super().mouseDoubleClickEvent(event)

    def set_selected(self, selected: bool):
        if selected:
            self.setStyleSheet(
                "ClickableThumb { background-color: #1e4080; "
                "border: 2px solid #4d94ff; border-radius: 4px; }")
        else:
            self.setStyleSheet("ClickableThumb { background-color: transparent; }")


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
                 cache_dir=None):
        super().__init__()
        self.zip_path        = zip_path
        self.items           = items
        self.path_resolver   = path_resolver
        self.thumb_size      = thumb_size
        self.zip_info_map    = zip_info_map
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
                        # already uses for the identical reason.
                        is_local = os.path.isabs(ui_path)
                        if is_local:
                            try:
                                file_size = os.path.getsize(ui_path)
                            except OSError:
                                continue
                            physical = ui_path
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
                                with open(ui_path, 'rb') as lf:
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

    def _setup_media_tab(self, status_style: str) -> QWidget:
        """Build the media-browser tab widget and initialise all media instance state.
        Returns the tab QWidget to be added to center_tabs."""
        self._thumb_worker: ThumbnailWorker | None = None
        self._thumb_cols       = 1
        self._thumb_widgets:   dict = {}
        self._thumb_img_labels: dict = {}
        self._thumb_positions:  dict = {}
        # Bumped on every _start_thumbnail_load call; a batched placeholder
        # pass (_place_thumb_placeholders_batched) checks this before each
        # chunk and quietly stops if it's gone stale (a new folder loaded
        # while it was still running) — same guard pattern as the folder
        # tree's own _tree_gen (see _reset_tree_model).
        self._thumb_gen = 0
        self._selected_media_path: str | None = None
        self._pending_media_selection: str | None = None
        self._media_full_dialog: MediaFullViewDialog | None = None
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
        Every file gets its blank-square-plus-filename placeholder container
        up front (via _place_thumb_placeholders_batched, in small batches
        so the main thread is never blocked building hundreds/thousands of
        widgets in one shot) — already clickable, selectable, and hex-
        previewable before its thumbnail decodes, and permanently so if it
        never does (an unsupported format, or a video frame extraction that
        fails — previously such a file got no widget at all, ever).
        _on_thumbnail_ready fills in the actual pixmap for whichever
        placeholders succeed; unfilled ones just stay blank squares."""
        # Retire (don't wait) — the worker may be stuck inside a long video
        # decode call and wait() would freeze the GUI until it returns.
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
        self._thumb_gen += 1

        if not media_paths or not self.zip_path:
            self._media_status.setText(
                "No media files" if self.zip_path else "Select a folder to view media")
            self._clear_hex_preview()
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

        self._place_thumb_placeholders_batched(media_paths, 0, self._thumb_gen)

        zip_info_map = {
            self._adapter.resolve(p): self.full_metadata.get(p, {}).get('size', 0)
            for p in media_paths
        }

        self._thumb_worker = ThumbnailWorker(
            self.zip_path, media_paths, self._adapter.resolve, THUMB_SIZE, zip_info_map,
            cache_dir=self._case_dir)
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
        container.doubleClicked.connect(self._on_thumb_double_clicked)

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

    _THUMB_PLACEHOLDER_BATCH = 60

    def _place_thumb_placeholders_batched(self, media_paths: list, start: int, gen: int) -> None:
        """Create every file's blank-square-plus-filename container a
        chunk at a time via QTimer.singleShot(0, …) instead of all in one
        pass — same reasoning as the folder tree's own
        _populate_tree_children_batched: hundreds/thousands of widgets
        built synchronously would freeze the GUI for the duration. *gen*
        is this call's _thumb_gen snapshot; if a newer _start_thumbnail_load
        has since bumped it (a different folder loaded while this batch
        was still running), stop quietly rather than placing widgets into
        a grid that's already been cleared and repurposed for different
        paths/positions."""
        if gen != self._thumb_gen:
            return
        end = min(start + self._THUMB_PLACEHOLDER_BATCH, len(media_paths))
        for ui_path in media_paths[start:end]:
            if ui_path not in self._thumb_widgets:
                self._place_thumb_container(ui_path)
        if end < len(media_paths):
            QTimer.singleShot(
                0, lambda: self._place_thumb_placeholders_batched(media_paths, end, gen))

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
        self._load_hex_preview(ui_path)
        self._media_sync_open_dialog(ui_path)

    def _on_thumb_double_clicked(self, ui_path: str) -> None:
        """Open the full-size image/video viewer for *ui_path* — non-modal
        (.show(), not .exec()) and tracked on self so a plain single-click
        selecting a DIFFERENT thumbnail can keep swapping this same open
        dialog's content instead of it blocking the grid (see
        _media_sync_open_dialog, called from _on_thumb_clicked above), same
        "open viewer follows selection" convention artifact_viewer.py's
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
        shouldn't pop it open, only a double-click (_on_thumb_double_clicked)
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
        CLAUDE.md). Needed as its own call, separate from _on_thumb_clicked
        above: the existing thumbnail-grid reload logic in
        _on_center_tab_changed only re-fires _on_thumb_clicked when a
        FILE BROWSER selection is pending sync into Media Browser — a
        plain "switch back to Media Browser, nothing changed, no pending
        File Browser selection" pass touches neither, which would
        otherwise leave whatever another tab last put in the shared panel
        showing here instead."""
        if self._selected_media_path and self._selected_media_path in self._thumb_widgets:
            self._load_hex_preview(self._selected_media_path)
        else:
            self._clear_hex_preview()

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
