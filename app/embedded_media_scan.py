"""embedded_media_scan.py — generic, schema-agnostic sweep for image/video
content embedded inside SQLite BLOB cells and plist NSData values, across
an entire archive.

Added 2026-09-22, per direct request: "one of the main things examiners
are looking for is media files that have illegal content ... some of them
are embedded such as in bplist and plist and blobs in sql db." Designed
collaboratively across several conversations before any code was written
— see CLAUDE.md's own writeup for the full design history (candidate
scoping vs. the existing header-scan tiers, live-vs-deleted recovery via
sqlite_carve.py's already-existing carving primitives, the HTTP-response-
wrapped-body case, and the real overflow-page limitation this module's
own docstrings below state plainly rather than gloss over).

Qt-free (same reasoning as app_intelligence.py: usable from a background
thread, and from the MCP server, with no Qt dependency).

── Why this is schema-agnostic, not per-table like sqlite_carve's own
`recoverable_tables` convention ────────────────────────────────────────

Every OTHER carving/recovery feature in this project (recoverable_tables,
record_source) is built around a PARSER that already knows which table
and columns matter for one specific app. This feature's whole point is
the opposite: find media in databases and plists this project has NO
parser for at all. SQLite's own on-disk record format is genuinely
self-describing per FIELD (each field's serial type encodes its own
type and byte length — see sqlite_carve.decode_value), so a BLOB-typed
field can be identified and extracted without ever knowing which table
or column it came from. This module leans on that directly rather than
requiring a declared schema.

── Live vs. deleted recovery, and a real, disclosed v1 gap ─────────────

LIVE data is read with a genuine SQL query (`SELECT rowid, * FROM table`,
via artifact_runner.open_db_readonly) rather than any raw page-walking —
SQLite's own engine transparently follows a BLOB's overflow-page chain,
so a full-size photo/video is recovered correctly regardless of how many
pages it spans.

DELETED data (freed pages, in-page freeblocks/gaps, and WAL frame
history) reuses sqlite_carve.py's own already-existing, already-verified
carving primitives (carve_freed_pages, carve_unallocated_region,
decode_leaf_page_cells applied directly to WAL frame images) completely
unmodified. Those primitives were built for recovering ordinary rows,
where a record that doesn't fully decode on-page is correctly treated as
untrustworthy and skipped (see decode_leaf_page_cells's own docstring).
Those paths therefore only recover deleted media small enough to fit in
one page's local payload (roughly page_size - 35 bytes): thumbnails,
stickers, tiny images.

Since 2026-10-23, scan_sqlite_overflow covers the rest: a deleted record
whose payload continued on overflow pages has its chain followed
(sqlite_carve.carve_overflowing_records). A result is "complete" (the
exact original bytes) only if every chain page is still free, nothing
on the leaf page overwrote the record's own bytes or pointer, and the
chain gives exactly the declared length ending in a 0 pointer; otherwise
it is a labelled PARTIAL prefix ("-carved-partial"), or nothing. Ground
truth (real images, real SQLite, known deletes): every complete result
was byte-identical to a deleted original and every partial a true
prefix; recovered = exactly the deleted records whose bytes SQLite had
not since overwritten (4 of 15 in a plain delete test, the rest having
had part of their bytes reused by SQLite's own page reorganising).
"""

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile

import sqlite_carve as sc
from artifact_runner import decode_plist_blob, decompress_http_body, open_db_readonly
import header_scan

# Below this, a blob is essentially never a real photo/video — cuts out
# the huge volume of small BLOB-typed bookkeeping values (ids, hashes,
# short protobuf messages) every real database is full of, before ever
# running a magic-byte check against them.
MIN_MEDIA_BYTES = 256

_SQLITE_MAIN_EXTENSIONS = frozenset({'.db', '.sqlite', '.sqlite3', '.db3'})
_SQLITE_SIDECAR_SUFFIXES = ('-wal', '-shm', '-journal')
_PLIST_EXTENSION = '.plist'

_MEDIA_MAGIC_KIND = {'Picture': 'image', 'Graphic': 'image', 'Video': 'video'}

_HTTP_STATUS_RE = re.compile(rb'^HTTP/\d\.\d \d{3}')


def scan_logic_version() -> str:
    """Short content hash of this module's own source — the staleness
    key for the generic processing_registry table (db_utils.py,
    capability='embedded_media_scan'), same auto-derived technique
    leveldb_decode_logic_version()/app_intelligence.scan_logic_version()
    already use for the identical problem (a cached "already processed"
    marker silently going stale the moment the underlying scan logic
    improves, with no signal anything's wrong). Never hand-authored, so
    it can't drift from what's actually on disk — the NEXT
    classify_media_blob/scan_sqlite_deleted improvement automatically
    makes every previously-scanned file eligible for reprocessing again,
    rather than needing this bumped by hand and inevitably forgotten
    once."""
    import hashlib
    try:
        with open(__file__, 'rb') as f:
            return hashlib.blake2b(f.read(), digest_size=8).hexdigest()
    except OSError:
        return 'unknown'


# ── candidate discovery (no I/O — mirrors _count_header_candidates' own
#    "count before touching bytes" idiom in ffs-explorer.py) ───────────

def classify_scan_candidate(ui_path: str, header_type_overrides: dict) -> str | None:
    """'sqlite' | 'plist' | None for one file — by extension, or by the
    same header-scan-derived type override the file browser's own Type
    column already uses for an extensionless/mistyped file. A bare
    '-wal'/'-shm'/'-journal' sidecar is deliberately excluded here even
    though _get_file_type's own fallback types it 'Database' too — it
    isn't independently openable, it's handled as part of its own main
    db's scan (see extract_sqlite_with_sidecars).

    Real, disclosed v1 gap: an XML-format (as opposed to binary) plist
    with no '.plist' extension is not covered — header_scan's own magic-
    byte table classifies '<?xml' as 'XML', not 'Property List', and
    telling a genuine XML plist apart from an arbitrary XML file without
    reading its content would cost real I/O this enumeration step is
    meant to avoid. Virtually every real plist on both iOS and Android
    carries the '.plist' extension regardless of format, so this is a
    narrow gap, not a systematic one."""
    name = ui_path.rsplit('/', 1)[-1]
    if name.lower().endswith(_SQLITE_SIDECAR_SUFFIXES):
        return None
    ext = os.path.splitext(name)[1].lower()
    override = header_type_overrides.get(ui_path)
    if ext in _SQLITE_MAIN_EXTENSIONS or override == 'Database':
        return 'sqlite'
    if ext == _PLIST_EXTENSION or override == 'Property List':
        return 'plist'
    return None


# Chromium "Simple Cache" record files: '<16 hex>_0' (e.g. Cache_Data/
# 000e5b778df2e766_0). Chrome and every app embedding a Chromium WebView or
# browser engine use this format (on real Android archives: Chrome, Edge,
# Opera, Brave, DuckDuckGo, Instagram, Imgur, WeChat, ...). The name only
# nominates a candidate; scan_container('cache') then requires the format's
# own magic (chrome_cache.parse_simple_cache_entry) before trusting it.
# '_s' is the sparse file holding range-fetched media (video) for '_0'.
_SIMPLE_CACHE_NAME = re.compile(r'^[0-9a-f]{16}_[0s]$')

_VIDEO_TYPES = {'video/mp4': '.mp4', 'video/webm': '.webm', 'video/mp2t': '.m2ts',
                'video/quicktime': '.mov', 'video/3gpp': '.3gp', 'video/x-matroska': '.mkv'}


def _sparse_video_kind(data: bytes, content_type: str) -> str | None:
    """Extension for rebuilt sparse-cache bytes that are video, else None.
    The record's Content-Type decides first (a response's own declaration);
    otherwise the bytes: anything classify_media_blob calls video, plus WebM
    (EBML) and MPEG-TS (0x47 sync every 188 bytes), which header_scan doesn't
    know. Audio and HLS playlists are not video."""
    ct = (content_type or '').split(';')[0].strip().lower()
    if ct.startswith('video/'):
        return _VIDEO_TYPES.get(ct, '.mp4')
    if ct.startswith(('audio/', 'application/vnd.apple.mpegurl', 'application/x-mpegurl')):
        return None
    got = classify_media_blob(data)
    if got and got[0] == 'video':
        return got[1]
    if data[:4] == b'\x1a\x45\xdf\xa3':
        return '.webm'
    if len(data) > 376 and data[0] == 0x47 and data[188] == 0x47 and data[376] == 0x47:
        return '.m2ts'
    return None


def is_simple_cache_record(ui_path: str) -> bool:
    return bool(_SIMPLE_CACHE_NAME.match(ui_path.rsplit('/', 1)[-1]))


def enumerate_cache_records(full_metadata: dict, scan_folders: tuple | None = None,
                            scan_excludes: tuple = ()) -> list[str]:
    """Simple Cache record candidates, scoped exactly like enumerate_candidates."""
    out = []
    for ui_path in full_metadata:
        if scan_folders is not None and not ui_path.startswith(scan_folders):
            continue
        if scan_folders is not None and scan_excludes and ui_path.startswith(scan_excludes):
            continue
        if is_simple_cache_record(ui_path):
            out.append(ui_path)
    return out


def enumerate_candidates(full_metadata: dict, header_type_overrides: dict,
                         scan_folders: tuple | None = None,
                         scan_excludes: tuple = ()) -> tuple[list[str], list[str]]:
    """(sqlite_ui_paths, plist_ui_paths), both lists, no I/O.

    *scan_folders*, if given, restricts candidates to ui_paths starting
    with one of those prefixes — the SAME app/user-accessible tuple
    ffs-explorer.py's _header_candidate_matches already scopes header-
    scan tiers 1/2 to (FfsAdapter.scan_folders()) — so this scan's own
    "app/user-accessible areas only" choice means exactly what an
    examiner already understands that phrase to mean elsewhere in this
    app. None means everywhere (tier-3-style, unscoped). *scan_excludes*
    (FfsAdapter.scan_excludes()) carves confirmed-noise subfolders back
    out of *scan_folders* — ignored when *scan_folders* is None, since
    an unscoped sweep has no "within scope" notion to carve anything out
    of."""
    sqlite_paths, plist_paths = [], []
    for ui_path in full_metadata:
        if scan_folders is not None and not ui_path.startswith(scan_folders):
            continue
        if scan_folders is not None and scan_excludes and ui_path.startswith(scan_excludes):
            continue
        kind = classify_scan_candidate(ui_path, header_type_overrides)
        if kind == 'sqlite':
            sqlite_paths.append(ui_path)
        elif kind == 'plist':
            plist_paths.append(ui_path)
    return sqlite_paths, plist_paths


# ── media classification ─────────────────────────────────────────────

def _find_header_body_boundary(blob: bytes, max_header: int = 8192) -> int | None:
    window = blob[:max_header]
    idx = window.find(b'\r\n\r\n')
    if idx != -1:
        return idx + 4
    idx = window.find(b'\n\n')
    if idx != -1:
        return idx + 2
    return None


def unwrap_http_response(blob: bytes) -> tuple[bytes, dict[str, str]] | None:
    """If *blob* looks like a raw captured HTTP response (status line,
    then header lines, then a blank line, then the body) — some apps
    cache network responses this way rather than as a clean media file
    — returns (body_bytes, headers_dict); else None.

    Deliberately a NEW, small parser, not a reuse of chrome_cache.py's
    own header parsing: that format is Chromium's own NUL-separated
    HttpResponseHeaders::raw_headers() serialization inside a Simple
    Cache container, a genuinely different on-disk shape from a literal
    HTTP/1.x wire-format response text — only the body DEcompression
    step (Content-Encoding-driven) is actually shared logic, via
    artifact_runner.decompress_http_body."""
    if not _HTTP_STATUS_RE.match(blob[:16]):
        return None
    boundary = _find_header_body_boundary(blob)
    if boundary is None:
        return None
    header_text = blob[:boundary].decode('latin-1', errors='replace')
    lines = header_text.split('\r\n') if '\r\n' in header_text else header_text.split('\n')
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ':' not in line:
            continue
        name, _, value = line.partition(':')
        headers[name.strip().lower()] = value.strip()
    body = blob[boundary:]
    content_encoding = headers.get('content-encoding', '')
    decoded, _err = decompress_http_body(body, content_encoding)
    return (decoded if decoded is not None else body), headers


def _ext_for_media(header: bytes) -> str:
    """A concrete file extension for the extracted output file — a
    presentational detail, not a second copy of the real classification
    decision (which stays in header_scan.classify_magic/IMAGE_FTYP_BRANDS
    throughout)."""
    if header.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return '.webp'
    if header.startswith((b'II*\x00', b'MM\x00*')):
        return '.tiff'
    if header[4:8] == b'ftyp':
        if header[8:12] in header_scan.IMAGE_FTYP_BRANDS:
            return '.heic'
        return '.mp4'
    if header.startswith(b'RIFF') and header[8:12] in (b'AVI ', b'AVIX'):
        return '.avi'
    return '.bin'


def classify_media_blob(blob: bytes) -> tuple[str, str, dict | None] | None:
    """(kind, extension, http_headers_or_None) for a candidate bytes
    value that is genuinely recognizable media, or None. Tries raw magic
    bytes first (the common case — most embedded media isn't wrapped in
    anything), then the HTTP-response-unwrap fallback before giving up.

    The MIN_MEDIA_BYTES floor is applied to each CANDIDATE representation
    separately (the raw blob, and — independently — the unwrapped/
    decompressed body), never to an intermediate wrapper length: a real
    photo stored Content-Encoding: gzip could compress to far fewer bytes
    than the photo itself, so gating on the wrapped blob's own raw length
    before attempting to unwrap it would wrongly reject a real hit purely
    because compression made it small. Real, disclosed narrow gap this
    still doesn't close: every caller in this module pre-filters by
    `len(value) < min_size` on the WRAPPER bytes before ever calling this
    function at all (see scan_sqlite_live/scan_sqlite_deleted/
    scan_plist_bytes), for the ordinary and far more common reason of not
    running this check against every tiny bookkeeping blob a real
    database is full of — so a compressed-media-under-MIN_MEDIA_BYTES
    case is still missed end to end. Accepted for v1: real HTTP servers
    essentially never gzip an already-compressed image/video response in
    the first place (there's no benefit to compressing a JPEG/MP4 a
    second time), so this narrows to a genuinely rare real-world shape,
    not the common case this feature exists for."""
    cat = header_scan.classify_magic(blob[:16])
    kind = _MEDIA_MAGIC_KIND.get(cat)
    if kind and len(blob) >= MIN_MEDIA_BYTES:
        return kind, _ext_for_media(blob[:16]), None
    unwrapped = unwrap_http_response(blob)
    if unwrapped is not None:
        body, headers = unwrapped
        cat2 = header_scan.classify_magic(body[:16])
        kind2 = _MEDIA_MAGIC_KIND.get(cat2)
        if kind2 and len(body) >= MIN_MEDIA_BYTES:
            return kind2, _ext_for_media(body[:16]), headers
    return None


# ── extraction (content-addressed, dedupes identical blobs for free) ───

def extract_media_bytes(scan_dir: str, data: bytes, ext: str) -> tuple[str, str]:
    """Writes *data* to scan_dir/<sha256[:2]>/<sha256><ext> (skipping the
    write if that exact content is already there — same idempotent-
    extraction convention as nested_archive.py's own already_extracted
    check). Returns (sha256_hex, local_path). Content-addressed naming
    means the SAME image found via two different routes (e.g. present in
    both a live row and an old WAL frame) is written once, not twice."""
    digest = hashlib.sha256(data).hexdigest()
    sub = os.path.join(scan_dir, digest[:2])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, digest + ext)
    if not os.path.isfile(path):
        with open(path, 'wb') as f:
            f.write(data)
    return digest, path


_MEDIA_EXTS = frozenset(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.tif', '.tiff',
                         '.bmp', '.avif', '.mp4', '.m4v', '.mov', '.webm', '.avi', '.3gp', '.mkv', '.m2ts'))


def compute_display_name(ui_path: str, source_kind: str, location: str,
                         hit: dict) -> str:
    """The naming scheme confirmed directly with the user, 2026-09-22:
    - SQLite, live:      <dbname>-<column>-<rowid><ext>
    - SQLite, carved/recovered: <dbname>-carved<ext> — deliberately no
      column/rowid (per direct instruction "if carved then just db name
      and carved") since a recovered hit's own rowid/column identity is
      inherently less certain than a live row's.
    - Plist:             the embedded field's own key name (the plist's
      dotted key path's LAST segment — e.g. "photo" from
      "wrapper.nested_container.<nested-bplist>.photo") + <ext>.

    This computes the BASE name only — collision disambiguation across
    a container's own several hits (e.g. two "carved" hits from the same
    db) is the injection step's job (ffs-explorer.py's
    _inject_embedded_media), since only it knows every sibling name at
    once. A literal '/' in any source name (a real column name, or a
    plist key) is replaced with U+2215 (division slash) — the same
    visually-similar substitution already used elsewhere in this project
    (leveldb_viewer.py's own display-name sanitization) so a display name
    is never mistaken for a real nested path."""
    ext = hit.get('ext', '.bin')
    if source_kind == 'cache':
        # The URL's last path segment (query/fragment dropped), with the
        # real media extension appended when the URL doesn't already end in
        # one; the record's own name if the URL has no usable segment.
        from urllib.parse import urlparse, unquote
        url = hit.get('url') or ''
        seg = unquote(urlparse(url).path.rsplit('/', 1)[-1]) if url else ''
        seg = seg.strip() or ui_path.rsplit('/', 1)[-1]
        if os.path.splitext(seg)[1].lower() not in _MEDIA_EXTS:
            seg += ext
        return seg.replace('/', '∕')
    if source_kind == 'plist':
        segment = (location.rsplit('.', 1)[-1] if location else '').strip('<>[]')
        if not segment or segment.startswith('nested-bplist'):
            segment = 'embedded'
        name = f"{segment}{ext}"
    else:
        dbname = ui_path.rsplit('/', 1)[-1] or 'database'
        if hit.get('partial'):
            name = f"{dbname}-carved-partial{ext}"
        elif hit.get('recovered'):
            name = f"{dbname}-carved{ext}"
        else:
            column = hit.get('column') or 'value'
            rowid = hit.get('rowid')
            rowid = rowid if rowid is not None else '?'
            name = f"{dbname}-{column}-{rowid}{ext}"
    return name.replace('/', '∕')


# ── SQLite: live content (real SQL query — overflow handled by SQLite
#    itself, so a full-size photo/video is always recovered intact) ────

def scan_sqlite_live(db_path: str, min_size: int = MIN_MEDIA_BYTES):
    """Yields dicts for every LIVE blob-shaped column value across every
    real table in db_path that classifies as genuine media. One
    `SELECT rowid, *` per table (not one query per column) — pulls every
    column's value into Python and filters by isinstance(bytes) there,
    trading a bit of extra TEXT-column materialization for a single
    full-table pass per table instead of one per column."""
    conn = open_db_readonly(db_path)
    conn.row_factory = None
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            except sqlite3.Error:
                continue
            if not cols:
                continue
            col_list = ', '.join(f'"{c}"' for c in cols)
            try:
                cur = conn.execute(f'SELECT rowid, {col_list} FROM "{table}"')
            except sqlite3.Error:
                continue
            for row in cur:
                rowid = row[0]
                for col, value in zip(cols, row[1:]):
                    if not isinstance(value, (bytes, bytearray)) or len(value) < min_size:
                        continue
                    result = classify_media_blob(bytes(value))
                    if result is None:
                        continue
                    kind, ext, http_headers = result
                    yield {
                        'table': table, 'column': col, 'rowid': rowid,
                        'blob': bytes(value), 'kind': kind, 'ext': ext,
                        'http_headers': http_headers,
                        'recovered': False, 'recovery_source': 'live',
                    }
    finally:
        conn.close()


# ── SQLite: deleted content (freed pages, in-page freeblocks/gaps, and
#    WAL frame history — see the module docstring for the real,
#    disclosed overflow-page limitation this path has) ─────────────────

def _live_leaf_pages(raw: bytes, page_size: int) -> set[int]:
    """Every page number currently part of SOME table's live b-tree —
    needed so carve_unallocated_region knows which pages to check for
    in-page freeblocks/gaps (a partial single-row delete on a page still
    otherwise in active use, as opposed to a WHOLE freed page, which
    carve_freed_pages covers separately). A throwaway read-only temp copy
    is opened only to read sqlite_master's own rootpage column — the
    same reasoning locate_live_row already uses for the identical need."""
    leaves: set[int] = set()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        conn = sqlite3.connect(f'file:{tmp_path}?mode=ro', uri=True)
        try:
            roots = [r[0] for r in conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE type='table' "
                "AND rootpage > 0")]
        finally:
            conn.close()
        for root in roots:
            try:
                leaves.update(sc.walk_table_leaf_pages(raw, page_size, root))
            except Exception:
                continue
    except Exception:
        pass
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return leaves


def _scan_wal_frames_for_media(wal_bytes: bytes, page_size: int, min_size: int):
    """Generic (schema-agnostic) WAL-history sweep — deliberately NOT
    sqlite_carve.carve_wal_history_for_table, which is scoped to one
    already-known table's own leaf-page set; this checks every frame's
    own page-type byte directly, the same way carve_freed_pages checks a
    freed page's type byte, since decode_leaf_page_cells already works
    identically on a live page, a freed page, or a WAL frame image (see
    its own docstring)."""
    if len(wal_bytes) < 32:
        return
    for frame in sc.iter_wal_frames(wal_bytes, page_size):
        page_no = frame['page']
        image = frame['image']
        header_offset = 100 if page_no == 1 else 0
        if len(image) < header_offset + 8:
            continue
        if image[header_offset] != 0x0D:   # not a table-leaf page image
            continue
        try:
            cells = sc.decode_leaf_page_cells(image, page_no, header_offset)
        except ValueError:
            continue
        for cell in cells:
            for value in cell.get('values', []):
                if not isinstance(value, (bytes, bytearray)) or len(value) < min_size:
                    continue
                result = classify_media_blob(bytes(value))
                if result is None:
                    continue
                kind, ext, http_headers = result
                yield {
                    'rowid': cell.get('rowid'), 'blob': bytes(value),
                    'kind': kind, 'ext': ext, 'http_headers': http_headers,
                    'recovered': True, 'recovery_source': 'wal_frame',
                    'page': page_no, 'wal_frame_offset': frame['frame_offset'],
                }


def scan_sqlite_deleted(raw: bytes, wal_bytes: bytes | None = None,
                        min_size: int = MIN_MEDIA_BYTES):
    """Yields dicts for every deleted/historical blob-shaped record value
    found in freed pages, in-page freeblocks/gaps, or (if *wal_bytes* is
    given) WAL frame history, that classifies as genuine media. See the
    module docstring for the real overflow-page gap this has today."""
    try:
        header = sc.parse_db_header(raw)
    except ValueError:
        return
    page_size = header['page_size']

    candidates = []
    try:
        candidates.extend(sc.carve_freed_pages(raw, page_size, header))
    except Exception:
        pass
    live_leaves = _live_leaf_pages(raw, page_size)
    if live_leaves:
        try:
            candidates.extend(sc.carve_unallocated_region(
                raw, page_size, list(live_leaves)))
        except Exception:
            pass

    for cell in candidates:
        for value in cell.get('values', []):
            if not isinstance(value, (bytes, bytearray)) or len(value) < min_size:
                continue
            result = classify_media_blob(bytes(value))
            if result is None:
                continue
            kind, ext, http_headers = result
            yield {
                'rowid': cell.get('rowid'), 'blob': bytes(value),
                'kind': kind, 'ext': ext, 'http_headers': http_headers,
                'recovered': True,
                'recovery_source': cell.get('source', 'freed_page'),
                'page': cell.get('page'),
            }

    if wal_bytes:
        yield from _scan_wal_frames_for_media(wal_bytes, page_size, min_size)


def scan_sqlite_overflow(raw: bytes, wal_bytes: bytes | None = None,
                         min_size: int = MIN_MEDIA_BYTES, known: list | None = None):
    """Deleted media too big for one page: the record's overflow chain is
    followed (sqlite_carve.carve_overflowing_records, which checks every
    chain page is still free and nothing on the page overwrote the record).
    - complete: the exact original bytes ('recovery_source' ends
      'overflow chain');
    - partial: a true prefix, cut where the record's bytes were overwritten
      or its chain reached a reused page; flagged 'partial', named
      '-carved-partial', kept only if it still starts as media.
    Media identical to (or, for a partial, a prefix of) *known* live media
    or an earlier result is skipped: e.g. WAL history repeats live rows."""
    known = list(known or [])
    seen = {hashlib.sha256(b).digest() for b in known}
    partials = []
    for rec in sc.carve_overflowing_records(raw, wal_bytes, min_blob=min_size):
        for _col, blob, size in sc.blob_columns(rec['payload'], min_size):
            complete = rec['status'] == 'complete' and len(blob) == size
            if len(blob) < min_size:
                continue
            got = classify_media_blob(blob)
            if got is None:
                continue
            kind, ext, http_headers = got
            if complete:
                digest = hashlib.sha256(blob).digest()
                if digest in seen:
                    continue
                seen.add(digest)
                known.append(blob)
                source = f"{rec['source']}, overflow chain"
            else:
                partials.append((rec, blob, size, kind, ext, http_headers))
                continue
            yield {'rowid': rec['rowid'], 'blob': blob, 'kind': kind, 'ext': ext,
                   'http_headers': http_headers, 'recovered': True,
                   'recovery_source': source, 'page': rec['page']}
    kept: list[bytes] = []
    for rec, blob, size, kind, ext, http_headers in sorted(partials, key=lambda p: -len(p[1])):
        if any(k.startswith(blob) for k in known + kept):
            continue                  # a copy of media already found whole (or longer)
        kept.append(blob)
        yield {'rowid': rec['rowid'], 'blob': blob, 'kind': kind, 'ext': ext,
               'http_headers': http_headers, 'recovered': True, 'partial': True,
               'recovery_source': (f"{rec['source']}, overflow chain, PARTIAL "
                                   f"{len(blob):,} of {size:,} bytes: {rec['status']}"),
               'page': rec['page']}


# ── plist: recursive NSData walk, including a nested embedded bplist
#    (a real case — see leveldb_viewer.py's own confirmed example of an
#    NSData field that is itself a second, embedded bplist) ────────────

def _walk_plist_values(obj, path: tuple = (), _depth: int = 0):
    """Recursively yield (dotted_path_tuple, bytes_value) for every real
    NSData-shaped leaf in a decoded plist structure. Handles plain
    dict/list, and — since decode_plist_blob's own NSKeyedArchiver path
    (nska_deserialize) can return custom dataclass-like objects for a
    nested archived object — anything with its own __dict__ too, the
    same shape leveldb_viewer._jsonify already has to handle for the
    equivalent IndexedDB/CryptoKey case."""
    if _depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_plist_values(v, path + (str(k),), _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk_plist_values(v, path + (f'[{i}]',), _depth + 1)
    elif isinstance(obj, (bytes, bytearray)):
        b = bytes(obj)
        if b.startswith(b'bplist00'):
            nested = decode_plist_blob(b)
            if nested is not None:
                yield from _walk_plist_values(
                    nested, path + ('<nested-bplist>',), _depth + 1)
                return
        yield path, b
    elif hasattr(obj, '__dict__') and not isinstance(
            obj, (str, int, float, bool, type(None))):
        for k, v in vars(obj).items():
            yield from _walk_plist_values(v, path + (str(k),), _depth + 1)


def scan_plist_bytes(raw: bytes, min_size: int = MIN_MEDIA_BYTES):
    """Yields dicts for every NSData-shaped value in *raw* (a whole
    plist FILE's bytes, binary or XML) that classifies as genuine media."""
    content = decode_plist_blob(raw)
    if content is None:
        return
    for path, value in _walk_plist_values(content):
        if len(value) < min_size:
            continue
        result = classify_media_blob(value)
        if result is None:
            continue
        kind, ext, http_headers = result
        yield {
            'plist_path': '.'.join(path), 'blob': value,
            'kind': kind, 'ext': ext, 'http_headers': http_headers,
            'recovered': False, 'recovery_source': 'plist',
        }


# ── manual, single-file entry point ("Search for Embedded Media" ──────
#    context-menu action) plus the shared per-container orchestration
#    both that and the bulk EmbeddedMediaScanWorker use ──────────────

def classify_container_type(raw: bytes) -> str | None:
    """'sqlite' | 'plist' | None, from the file's OWN real magic bytes —
    ignoring any pre-existing extension/Type label entirely. Added
    2026-09-23 for the manual, single-file "Search for Embedded Media"
    context-menu action (ffs-explorer.py), which deliberately re-checks
    the real header even for a file already confidently typed some other
    way — "just in case wrong ext" was the direct request.

    A real, closed gap versus classify_scan_candidate's own BULK
    enumeration (see that function's own docstring for why it's scoped
    the way it is): a bulk scan deliberately avoids reading file content
    at all for cost reasons (checking thousands of candidates cheaply),
    so it only ever recognizes a binary plist via a header-scan override
    (bplist00 magic) or the '.plist' extension — genuinely missing a real
    XML-format plist with no '.plist' extension. This function reads the
    real bytes already in hand for the ONE file a manual check is run
    against, so it can also recognize a genuine XML plist directly (a
    real '<?xml' declaration followed by a real '<plist' element, not
    just any XML file) — the third of the three real container types
    this feature looks for, alongside SQLite and binary plist."""
    if raw[:16] == b'SQLite format 3\x00':
        return 'sqlite'
    if raw[:6] == b'bplist':
        return 'plist'
    head = raw[:512].lstrip()
    if head[:5] == b'<?xml' and b'<plist' in head:
        return 'plist'
    return None


def _build_hit_row(ui_path: str, source_kind: str, location: str,
                   scan_dir: str, hit: dict, scan_scope: str) -> dict:
    """One embedded_media_hits row dict, ready for db_utils.
    save_embedded_media_hits — shared by scan_container below and (until
    2026-09-23) duplicated inline in EmbeddedMediaScanWorker._build_row."""
    sha, path = extract_media_bytes(scan_dir, hit['blob'], hit['ext'])
    headers = hit.get('http_headers') or {}
    return {
        'source_ui_path': ui_path,
        'source_kind': source_kind,
        'location': location,
        'display_name': compute_display_name(ui_path, source_kind, location, hit),
        'media_kind': hit['kind'],
        'extracted_path': path,
        'sha256': sha,
        'byte_length': len(hit['blob']),
        'recovered': int(bool(hit.get('recovered'))),
        'recovery_source': hit.get('recovery_source'),
        'http_wrapped': int(bool(hit.get('http_headers'))),
        'content_type_hint': headers.get('content-type'),
        'scan_scope': scan_scope,
    }


def scan_container(kind: str, ui_path: str, raw: bytes, scan_dir: str,
                   scan_scope: str, wal_bytes: bytes | None = None,
                   partner_bytes: bytes | None = None) -> list[dict]:
    """Scan one already-classified container's raw bytes for embedded
    media and return ready-to-save embedded_media_hits row dicts. Moved
    here 2026-09-23 out of EmbeddedMediaScanWorker's own per-candidate
    loop (ffs-explorer.py) so a second real caller — the manual single-
    file "Search for Embedded Media" action — doesn't duplicate it;
    that worker now calls this too.

    *kind* is 'sqlite' or 'plist' (from either classify_scan_candidate's
    bulk, extension/override-based check, or classify_container_type's
    real-header check above — this function only cares which one was
    decided, not how). For 'sqlite', *raw* is written to a real temp
    file for the LIVE scan (SQLite's own engine needs a real path to
    follow a BLOB's overflow chain — see scan_sqlite_live's own
    docstring), then scanned again directly off *raw* (+ *wal_bytes*, if
    given) for deleted content via scan_sqlite_deleted."""
    rows: list[dict] = []
    if kind == 'cache':
        # A Chromium Simple Cache record (any app): its body is the cached
        # HTTP response. One record holds one response, so at most one hit,
        # carrying the URL for the child's "Source URL" column.
        import chrome_cache
        if chrome_cache.is_sparse(raw):
            # '<hash>_s': media fetched in byte ranges, rebuilt at its real
            # offsets. *partner_bytes* is the matching '<hash>_0' record,
            # whose headers carry the Content-Type.
            sp = chrome_cache.parse_sparse_entry(raw)
            data = sp.get('data') if sp else None
            if not data:
                return rows
            partner = chrome_cache.parse_simple_cache_entry(partner_bytes) if partner_bytes else None
            ext = _sparse_video_kind(data, (partner or {}).get('content_type', ''))
            if ext is None:
                return rows
            hit = {'blob': data, 'kind': 'video', 'ext': ext,
                   'http_headers': {'content-type': (partner or {}).get('content_type') or ''},
                   'url': sp.get('url')}
            row = _build_hit_row(ui_path, 'cache', 'sparse ranges', scan_dir, hit, scan_scope)
            row['source_url'] = sp.get('url')
            rows.append(row)
            return rows
        rec = chrome_cache.parse_simple_cache_entry(raw)
        body = rec.get('body') if rec else None
        if not body:
            return rows
        got = classify_media_blob(body)
        if got is None:
            return rows
        media_kind, ext, _wrapped = got
        headers = dict(rec.get('headers') or {})
        if rec.get('content_type'):
            headers.setdefault('content-type', rec['content_type'])
        hit = {'blob': body, 'kind': media_kind, 'ext': ext,
               'http_headers': headers, 'url': rec.get('url')}
        row = _build_hit_row(ui_path, 'cache', 'body', scan_dir, hit, scan_scope)
        row['source_url'] = rec.get('url')
        rows.append(row)
        return rows
    if kind == 'plist':
        for hit in scan_plist_bytes(raw):
            rows.append(_build_hit_row(ui_path, 'plist', hit['plist_path'],
                                       scan_dir, hit, scan_scope))
        return rows

    # kind == 'sqlite'
    seen_blobs: list[bytes] = []          # live media, to skip recovered copies of it
    tmp_dir = tempfile.mkdtemp(prefix='ffs_em_')
    tmp_path = os.path.join(tmp_dir, 'db.sqlite')
    try:
        with open(tmp_path, 'wb') as f:
            f.write(raw)
        # Live = what SQLite itself shows, which includes rows committed in
        # the -wal but not yet copied into the main file (2026-10-23: the
        # -wal used to be left out, so such media was missed). If the db
        # can't be opened with its -wal, scan it without, as before.
        live: list[dict] = []
        if wal_bytes:
            with open(tmp_path + '-wal', 'wb') as f:
                f.write(wal_bytes)
            try:
                live = list(scan_sqlite_live(tmp_path))
            except Exception:
                live = []
                for sfx in ('-wal', '-shm'):
                    try:
                        os.unlink(tmp_path + sfx)
                    except OSError:
                        pass
        if not live:
            live = list(scan_sqlite_live(tmp_path))
        for hit in live:
            location = f"{hit['table']}.{hit['column']} (rowid={hit['rowid']})"
            rows.append(_build_hit_row(ui_path, 'sqlite', location, scan_dir, hit, scan_scope))
            seen_blobs.append(hit['blob'])
    except Exception:
        pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        for hit in scan_sqlite_deleted(raw, wal_bytes=wal_bytes):
            location = (f"rowid={hit.get('rowid')} (page {hit.get('page')}, "
                       f"{hit.get('recovery_source')})")
            rows.append(_build_hit_row(ui_path, 'sqlite', location, scan_dir, hit, scan_scope))
    except Exception:
        pass
    try:
        for hit in scan_sqlite_overflow(raw, wal_bytes=wal_bytes, known=seen_blobs):
            location = (f"rowid={hit.get('rowid')} (page {hit.get('page')}, "
                        f"{hit.get('recovery_source')})")
            rows.append(_build_hit_row(ui_path, 'sqlite', location, scan_dir, hit, scan_scope))
    except Exception:
        pass
    return rows
