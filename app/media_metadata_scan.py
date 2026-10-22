"""media_metadata_scan.py — EXIF (images) / container metadata (video)
extraction for the File Browser's "(EXIF)" columns (added 2026-10-11,
direct request: "i should just do a media pass after the header check
via type i.e. pic[t]ure, png and video[,] get all the exif and add it
as ad[d]itional columns in the browser... it is super use[ful] where it
is present").

Qt-free (same convention as header_scan.py/embedded_media_scan.py — this
module must stay usable from a plain background QThread.run(), never
touch Qt objects itself).

**"a smaller check before the decode"** (direct question, confirmed
real and worth doing): neither extraction here ever decodes a pixel or
a video frame — `Image.open()` is lazy (EXIF is a header-level read) and
PyAV's container open only needs the moov/metadata atoms, never frame
data, for duration/tags/stream dimensions. The real cost that DOES need
addressing is I/O, not CPU: the obvious implementation reads a whole
candidate's bytes into memory before handing them to PyAV/Pillow, which
is wasteful for a large video (one real test file: 102MB, `moov` atom
near the front) when only the metadata actually gets touched.
`_LazyZipSlice` (a read/seek/tell file-like view doing direct seeks on
one held file handle — no `zipfile`, no decompression, FFS
archives are always stored uncompressed, confirmed and reused rather
than re-derived) lets PyAV/Pillow pull only the bytes they actually
need. Confirmed directly against that same real 102MB video: full-read
took 25.2ms and touched 100% of the file; the lazy reader took 6.2ms
and touched 0.22% of it (225KB) — the same real `ZipEntry.read_at`
primitive the hex viewer's own scroll-paging already uses, just handed
to a different consumer.

Also confirmed directly (a different real worry: "does a file WITHOUT
EXIF cost the same as one WITH it, or could a cheap pre-check skip
further work"): no — 151 real app-bundle PNGs (9 had EXIF) averaged
0.162ms/file parse; 139 real DCIM HEIC photos (139 had EXIF) averaged
0.146ms/file. No measurable difference, because the "cost" is parsing
the file's own container structure, which has to happen either way
just to confirm it's a valid, openable file at all — checking for EXIF
afterward is then a free dict/pointer lookup. There is no cheaper
signal below that; the open+header-parse already IS the cheapest real
check.
"""

import io
import os
import re

from zip_reader import ZipReader, thread_handle


class _LazyZipSlice:
    """A read/seek/tell file-like VIEW of one zip entry's bytes
    (data_offset..data_offset+size within *zip_path*) — reads only what the
    caller (PyAV/Pillow) actually asks for, never materializes the whole
    entry up front. See this module's own docstring for the real, confirmed
    cost difference.

    Holds ONE file handle, opened on first read, for the slice's lifetime:
    PyAV/Pillow make many small reads per file, and opening the archive
    afresh for each one is a real cost on a network share. Use as a context
    manager (or call close()) so the handle is released promptly.
    """

    def __init__(self, zip_path: str, data_offset: int, size: int):
        self._zip_path = zip_path
        self._fh = None
        self._base = data_offset
        self._size = size
        self._pos = 0

    def read(self, n: int | None = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        n = max(0, min(n, self._size - self._pos))
        if n == 0:
            return b''
        # A worker thread's shared handle (zip_reader.thread_handle) — one
        # open per thread instead of one per media file, which is what made
        # this stage slow on Windows. Seeks every read, so sharing is safe.
        fh = thread_handle(self._zip_path)
        if fh is None:
            if self._fh is None:
                self._fh = open(self._zip_path, 'rb')
            fh = self._fh
        fh.seek(self._base + self._pos)
        data = fh.read(n)
        self._pos += len(data)
        return data

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True


# EXIF tag ids (PIL.ExifTags has names for these, but the raw ints are
# used directly here so this module has no Pillow-version-specific
# import surface beyond Image/ExifTags.GPS itself).
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME          = 0x0132
_TAG_MAKE              = 0x010F
_TAG_MODEL             = 0x0110
_TAG_SOFTWARE          = 0x0131
_TAG_ORIENTATION       = 0x0112
_TAG_GPS_IFD           = 0x8825

_GPS_LAT     = 2   # GPSLatitude  (rational triple: deg, min, sec)
_GPS_LAT_REF = 1   # 'N' | 'S'
_GPS_LON     = 4   # GPSLongitude
_GPS_LON_REF = 3   # 'E' | 'W'

# Real, confirmed bug caught during this feature's own first test run:
# without this, EVERY real HEIC file (iOS's default photo format since
# iOS 11 — 139/139 in one real DCIM folder) raised UnidentifiedImageError
# and silently returned {} — no width/height, no EXIF, nothing — because
# Pillow has no built-in HEIC decoder; it needs pillow_heif's opener
# registered first. Same one-time-registration flag/pattern media_viewer.
# py's _load_qimage already uses for the exact same reason, duplicated
# (not imported) since that module pulls in PySide6, which this Qt-free
# one must not.
_heif_opener_registered = False


def _ensure_heif_opener() -> None:
    global _heif_opener_registered
    if not _heif_opener_registered:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _heif_opener_registered = True


def _dms_to_decimal(dms, ref: str) -> float | None:
    try:
        deg, minute, sec = (float(v) for v in dms)
    except (TypeError, ValueError):
        return None
    value = deg + minute / 60.0 + sec / 3600.0
    if ref in ('S', 'W'):
        value = -value
    return value


def _format_exif_datetime(raw: str) -> str | None:
    """EXIF DateTime strings are 'YYYY:MM:DD HH:MM:SS' — normalized to
    this project's own 'YYYY-MM-DD HH:MM:SS' display convention."""
    if not raw or len(raw) < 19:
        return None
    try:
        date_part, time_part = raw[:10], raw[11:19]
        return f"{date_part.replace(':', '-')} {time_part}"
    except Exception:
        return None


def extract_image_exif(file_like) -> dict:
    """{'taken', 'camera', 'gps', 'width', 'height', 'software',
    'orientation'} — only the keys actually found (a key present with a
    falsy/blank value is never stored; absence IS the "not present"
    signal consumed by the File Browser column builder). *file_like*
    must support read/seek/tell (a plain local file object, or
    _LazyZipSlice above) — never raises; returns {} on any decode
    failure (a byte-for-byte unreadable/corrupt file is simply "nothing
    found", same as every other best-effort scan in this project)."""
    from PIL import Image
    result: dict = {}
    try:
        _ensure_heif_opener()
        img = Image.open(file_like)
        w, h = img.size
        if w and h:
            result['width'], result['height'] = w, h
        exif = img.getexif()
        if not exif:
            return result
        taken_raw = exif.get(_TAG_DATETIME_ORIGINAL) or exif.get(_TAG_DATETIME)
        taken = _format_exif_datetime(taken_raw) if taken_raw else None
        if taken:
            result['taken'] = taken
        make  = (exif.get(_TAG_MAKE) or '').strip()
        model = (exif.get(_TAG_MODEL) or '').strip()
        camera = f"{make} {model}".strip()
        if camera:
            result['camera'] = camera
        software = (exif.get(_TAG_SOFTWARE) or '').strip()
        if software:
            result['software'] = software
        orientation = exif.get(_TAG_ORIENTATION)
        if orientation:
            result['orientation'] = int(orientation)
        try:
            gps_ifd = exif.get_ifd(_TAG_GPS_IFD)
        except Exception:
            gps_ifd = None
        if gps_ifd:
            lat = _dms_to_decimal(gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF, 'N'))
            lon = _dms_to_decimal(gps_ifd.get(_GPS_LON), gps_ifd.get(_GPS_LON_REF, 'E'))
            if lat is not None and lon is not None:
                result['gps'] = f"{lat:.6f},{lon:.6f}"
    except Exception:
        pass
    return result


_ISO6709_RE = re.compile(r'^([+-]\d+\.?\d*)([+-]\d+\.?\d*)')


def _parse_iso6709(raw: str) -> str | None:
    m = _ISO6709_RE.match(raw or '')
    if not m:
        return None
    try:
        lat, lon = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    return f"{lat:.6f},{lon:.6f}"


def extract_video_metadata(file_like) -> dict:
    """{'taken', 'camera', 'gps', 'duration_s', 'width', 'height',
    'software'} — only the keys actually found. *file_like* as
    extract_image_exif above. Opens the CONTAINER only — av.open()
    reads just the demuxer's own index/metadata atoms, never decodes a
    video frame (never calls .decode()/iterates frames anywhere in this
    function)."""
    import av
    result: dict = {}
    try:
        container = av.open(file_like)
        try:
            if container.duration:
                result['duration_s'] = container.duration / 1_000_000.0
            tags = container.metadata or {}
            vstream = next((s for s in container.streams if s.type == 'video'), None)
            if vstream and vstream.width and vstream.height:
                result['width'], result['height'] = vstream.width, vstream.height

            creation_time = tags.get('creation_time')
            if creation_time:
                # Real container tag is ISO8601 UTC, e.g.
                # '2023-07-02T01:07:10.000000Z' — normalized the same
                # 'YYYY-MM-DD HH:MM:SS' way extract_image_exif's EXIF
                # strings are (display consistency across both EXIF
                # columns, even though this one source IS unambiguously
                # UTC and EXIF's own DateTimeOriginal is not — the
                # column name alone doesn't carry that distinction, a
                # real, disclosed narrow gap rather than overbuilding a
                # second timezone-aware column pair nobody asked for).
                taken = creation_time.replace('T', ' ')[:19].replace('T', ' ')
                if len(taken) >= 10:
                    result['taken'] = taken

            make  = tags.get('com.apple.quicktime.make', '')
            model = tags.get('com.apple.quicktime.model', '')
            camera = f"{make} {model}".strip()
            if camera:
                result['camera'] = camera

            software = tags.get('com.apple.quicktime.software', '')
            if software:
                result['software'] = software

            location = (tags.get('com.apple.quicktime.location.ISO6709')
                       or tags.get('location'))
            if location:
                gps = _parse_iso6709(location)
                if gps:
                    result['gps'] = gps
        finally:
            container.close()
    except Exception:
        pass
    return result


# Dispatch set — kept here (not imported from header_scan.GRAPHIC_EXTENSIONS
# etc. at call sites) so this module's own public entrypoint is self-
# contained; the worker that builds candidates is still the one place
# that decides WHICH files qualify as Picture/Graphic/Video in the first
# place (ffs-explorer.py's own _get_file_type/header_type_overrides).
from header_scan import VIDEO_THUMB_EXTENSIONS


def extract_one(file_like, ext: str) -> dict:
    """Dispatch to extract_image_exif/extract_video_metadata by *ext*
    (lower-cased, with the leading dot)."""
    if ext in VIDEO_THUMB_EXTENSIONS:
        return extract_video_metadata(file_like)
    return extract_image_exif(file_like)


def scan_zip_candidates(
    zip_path: str,
    candidates: list,   # [(ui_path, data_offset, file_size, ext), ...]
    progress_cb=None,
    cancel_check=None,
) -> dict:
    """{ui_path: {field: value, ...}} for every candidate that yielded
    at least one real field — candidates straight from the main FFS
    zip, read lazily via _LazyZipSlice (never a whole-file read,
    never zipfile/decompression — see this module's own docstring).
    Runs on up to 8 threads via zip_reader.ZipReader.run_parallel, the
    same real concurrency primitive header_scan.scan_entries already
    uses."""
    reader = ZipReader(zip_path)

    def _scan_one(item):
        ui_path, data_offset, file_size, ext = item
        with _LazyZipSlice(zip_path, data_offset, file_size) as slice_:
            return extract_one(slice_, ext)

    results: dict = {}
    _pcb = (lambda d, t: progress_cb(t - d)) if progress_cb else None
    for (ui_path, _, __, ___), fields in reader.run_parallel(
            candidates, _scan_one, progress_cb=_pcb,
            cancel_check=cancel_check):
        if fields:
            results[ui_path] = fields
    return results


def scan_local_candidates(
    candidates: list,   # [(ui_path, local_path_or_bytes, ext), ...]
    progress_cb=None,
    cancel_check=None,
) -> dict:
    """{ui_path: {field: value, ...}} for candidates whose real bytes
    don't live in the main FFS zip — nested-archive-extracted files and
    embedded-media-recovered hits ("for the zips and embe[d]ded it
    should be done as part of the process," direct request). The
    second tuple element is either:
      - a local file path (str) — an embedded-media hit, already
        extracted to its own content-addressed file on disk
        (embedded_media_scan.extract_media_bytes); opened here as a
        plain local file, itself already a true lazy/seekable OS-level
        stream, same "only read what's needed" reasoning as
        _LazyZipSlice, just a different, already-cheap source.
      - raw bytes (bytes) — a nested-archive entry, which lives inside
        a REPACKAGED zip (zip_reader.read_nested_entry's own target,
        not a plain extracted file) so the caller (ffs-explorer.py,
        which already knows that repackaging layout) reads it first and
        hands over the bytes directly rather than this Qt-free module
        needing to know about nested_archives' own on-disk shape too."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: dict = {}
    total = len(candidates)
    if total == 0:
        return results

    def _scan_one(item):
        ui_path, source, ext = item
        try:
            if isinstance(source, bytes):
                return ui_path, extract_one(io.BytesIO(source), ext)
            with open(source, 'rb') as f:
                return ui_path, extract_one(f, ext)
        except OSError:
            return ui_path, {}

    done = 0
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
        futures = [pool.submit(_scan_one, item) for item in candidates]
        for fut in as_completed(futures):
            if cancel_check and cancel_check():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            ui_path, fields = fut.result()
            if fields:
                results[ui_path] = fields
            done += 1
            if progress_cb:
                progress_cb(total - done)
    return results


def format_duration(seconds: float) -> str:
    """12.5 -> '0:12', 125.0 -> '2:05', 3700.0 -> '1:01:40'."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
