"""chrome_cache.py — Chrome's HTTP disk cache (Simple Cache format), and
synthesizing a self-contained .mhtml document from a cached page plus
whatever of its own sub-resources also survive in the same cache.

Format reverse-engineered directly against real cache entries from this
project's own Android 14 JoshHickman casework (data/data/com.android.chrome/
cache/Cache/Cache_Data/*) — NOT the app_chrome/Default database directory
every other Chrome parser in this project reads from; this is a genuinely
different location. Verified end-to-end on two real, independently-known
cached pages (Chrome History already documented both titles): both
decompress to real HTML whose own <title> matches Chrome History's title
for that URL exactly.

On-disk entry layout, per file (each file is ONE Simple Cache entry,
version 5 seen on real data; the trailing "_0" in Chrome's own filenames is
this project's OWN convention only in that a "_s" suffix marks a sparse/
range-request entry, handled identically here — both are read whole):

    [SimpleFileHeader, 24 bytes: magic(Q) version(I) key_length(I)
     key_hash(I) reserved(I)]
    [key, key_length bytes -- the cache KEY, not just a URL, see
     _parse_cache_key]
    [STREAM 1 -- the response BODY, compressed however Content-Encoding
     says]
    [EOF record for stream 1]
    [STREAM 0 -- raw HTTP response headers (NUL-separated, exactly
     Chromium's own HttpResponseHeaders::raw_headers() serialization,
     status line first) followed by SSL certificate / connection metadata
     as a separate pickle blob -- not parsed here, not needed]
    [EOF record for stream 0]

The 24-byte header is confirmed one uint32 longer than the header this
project could find independently documented elsewhere (magic/version/
key_length/key_hash, 20 bytes) -- verified directly by finding the actual
key text was consistently truncated by exactly 4 bytes at the classic
20-byte offset, and exactly correct at 24, on two independent real
entries. Never trust a byte-offset claim about this format without
checking against real data the way this was checked.

EOF-record boundaries are located by searching for Simple Cache's own EOF
magic number, not by computing an expected offset — deliberately: it is a
reliable anchor regardless of any (real, observed) padding/reserved-field
quirks the header itself might have, the same reasoning the 24-vs-20-byte
header discovery above already demonstrated once.

Nothing here ever writes to the archive; every function takes bytes
already in memory and returns None (never raises) on anything that
doesn't parse as expected — a cache is exactly the kind of structure
where a single corrupt/partially-overwritten entry among thousands must
never take down the whole parser run.
"""

import re
import struct
import zlib
from urllib.parse import urljoin, urlparse

_HEADER_MAGIC = 0xfcfb6d1ba7725c30
_EOF_MAGIC = struct.pack('<Q', 0xf4fa6f45970d41d8)
_HEADER_STRUCT = struct.Struct('<QIIII')   # magic, version, key_len, key_hash, reserved
_HEADER_SIZE = _HEADER_STRUCT.size          # 24 -- see module docstring


def _parse_cache_key(key: str) -> tuple[str | None, str]:
    """Chrome's HTTP Cache Partitioning (Network State Partitioning) key
    format, confirmed on real data: "<flags>/_dk_<top-level-site>
    <top-level-site> <resource-url>" for a partitioned entry (the
    top-level site appears twice — real cache-key behavior, not a
    parsing artifact, confirmed identically on every partitioned entry
    checked), or just a bare URL for an unpartitioned one (older/simpler
    entries — never assumed absent, only reported when genuinely not
    found). Returns (top_level_site_or_None, resource_url) — resource_url
    is always the LAST whitespace-separated token, which is what's
    actually cacheable/fetchable regardless of which key shape this is."""
    key = key.lstrip('\x00').strip()
    if '_dk_' in key:
        after = key.split('_dk_', 1)[1]
        parts = after.split(' ')
        if len(parts) >= 3:
            return parts[0], parts[-1]
        if len(parts) == 2:
            return parts[0], parts[-1]
    parts = key.split(' ')
    return None, parts[-1] if parts else key


def _parse_raw_headers(blob: bytes) -> tuple[str, dict[str, str]]:
    """blob is Chromium's own HttpResponseHeaders::raw_headers() format:
    status line, then NUL-separated "Name: Value" lines, no trailing
    terminator required here (caller already sliced up to the double-NUL
    that ends the block). Duplicate header names (real HTTP allows
    repeats, e.g. multiple Set-Cookie) are joined with '; ' rather than
    the last one silently winning — never drop a real header value."""
    lines = [l.decode('utf-8', errors='replace') for l in blob.split(b'\x00') if l]
    status_line = lines[0] if lines else ''
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ':' not in line:
            continue
        name, _, value = line.partition(':')
        name = name.strip().lower()
        value = value.strip()
        headers[name] = f'{headers[name]}; {value}' if name in headers else value
    return status_line, headers


def _decompress_body(body: bytes, content_encoding: str) -> tuple[bytes | None, str | None]:
    """(decompressed_bytes_or_None, error_note_or_None). Never raises —
    a body that fails to decompress is reported as such, not silently
    dropped or (worse) shown as if it decompressed to something. gzip and
    deflate are always available (stdlib zlib); brotli ('br') and zstd
    need the optional `brotli`/`zstandard` packages — both real, common
    encodings on real casework, not hypothetical: on this project's own
    Android 14 JoshHickman cache, 496 of 3,041 entries (16%) were 'br'
    and 56 were 'zstd' before these packages were added as real
    dependencies (see requirements.txt) — same lazy-optional-dependency
    convention as this project's mcp/uvicorn either way, so a build
    missing one still reports a real, honest gap per-row rather than
    crashing or silently dropping the row."""
    enc = (content_encoding or '').strip().lower()
    if not enc or enc == 'identity':
        return body, None
    try:
        if enc == 'gzip':
            return zlib.decompress(body, zlib.MAX_WBITS | 16), None
        if enc == 'deflate':
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS), None
            except zlib.error:
                return zlib.decompress(body), None
        if enc == 'br':
            try:
                import brotli
            except ImportError:
                return None, "brotli-compressed body -- 'brotli' package not installed"
            return brotli.decompress(body), None
        if enc == 'zstd':
            try:
                import zstandard
            except ImportError:
                return None, "zstd-compressed body -- 'zstandard' package not installed"
            return zstandard.ZstdDecompressor().decompress(
                body, max_output_size=200 * 1024 * 1024), None
        return None, f"unrecognized content-encoding '{content_encoding}'"
    except Exception as exc:
        return None, f'{enc} decompression failed: {exc}'


def parse_simple_cache_entry(raw: bytes) -> dict | None:
    """Parse one Simple Cache entry file's raw bytes. Returns None if it
    doesn't structurally look like one at all (wrong magic, truncated) —
    every OTHER kind of problem (unresolvable headers, failed
    decompression) is reported as a field on the returned dict instead,
    since a partial read of a real cache entry is still real evidence,
    the same "partial beats none, but state it" principle sqlite_carve.py
    already applies to a truncated carved record.

    Returns {'url', 'top_level_site', 'status_line', 'headers' (dict,
    lowercased names), 'content_type', 'content_encoding', 'body' (bytes
    or None), 'body_error' (str or None), 'raw_body_length',
    'header_parse_error' (str or None, when stream 0 couldn't be located
    at all)}."""
    try:
        if len(raw) < _HEADER_SIZE:
            return None
        magic, version, key_len, key_hash, _reserved = _HEADER_STRUCT.unpack_from(raw, 0)
        if magic != _HEADER_MAGIC:
            return None
        key_start = _HEADER_SIZE
        key = raw[key_start:key_start + key_len].decode('utf-8', errors='replace')
        top_level_site, url = _parse_cache_key(key)

        body_start = key_start + key_len
        eof1 = raw.find(_EOF_MAGIC, body_start)
        if eof1 == -1:
            # No stream boundary found at all -- can't even isolate the
            # body from whatever follows. Real, not hypothetical: happens
            # for a genuinely truncated/partially-overwritten entry.
            return {
                'url': url, 'top_level_site': top_level_site,
                'status_line': '', 'headers': {}, 'content_type': '',
                'content_encoding': '', 'body': None,
                'body_error': None, 'raw_body_length': 0,
                'header_parse_error': 'no stream boundary found (truncated entry)',
            }
        body = raw[body_start:eof1]

        status_line, headers, header_err = '', {}, None
        status_idx = raw.find(b'HTTP/1.', eof1)
        if status_idx == -1:
            header_err = 'HTTP status line not found after body stream'
        else:
            hdr_end = raw.find(b'\x00\x00', status_idx)
            if hdr_end == -1:
                header_err = 'header block had no terminator'
            else:
                status_line, headers = _parse_raw_headers(raw[status_idx:hdr_end])

        content_type = headers.get('content-type', '')
        content_encoding = headers.get('content-encoding', '')
        decoded_body, body_error = _decompress_body(body, content_encoding)

        return {
            'url': url,
            'top_level_site': top_level_site,
            'status_line': status_line,
            'headers': headers,
            'content_type': content_type,
            'content_encoding': content_encoding,
            'body': decoded_body,
            'body_error': body_error,
            'raw_body_length': len(body),
            'header_parse_error': header_err,
        }
    except Exception as exc:
        return {
            'url': None, 'top_level_site': None, 'status_line': '',
            'headers': {}, 'content_type': '', 'content_encoding': '',
            'body': None, 'body_error': None, 'raw_body_length': 0,
            'header_parse_error': f'unexpected error: {exc}',
        }


# ── HTML reference extraction + MHTML synthesis ─────────────────────────────

# Deliberately a lightweight regex scan, not a real HTML/CSS parser — this
# project has no HTML-parsing dependency anywhere else and adding one just
# for this would be disproportionate. Covers the attributes that actually
# carry a fetchable sub-resource URL in ordinary markup; a page relying on
# JS to construct resource URLs at runtime (common on real, especially
# heavily-scripted, sites) is a known, expected gap — this synthesizes what
# a static reference scan can find, not a full browser fetch, which is
# exactly the "data may be missing" limitation inherent to reconstructing
# from a cache rather than replaying a real page load.
_REF_RE = re.compile(
    rb'''(?:src|href)\s*=\s*["']([^"'#][^"']*)["']''', re.IGNORECASE)
_CSS_URL_RE = re.compile(rb'''url\(\s*["']?([^"')]+)["']?\s*\)''', re.IGNORECASE)


def find_referenced_urls(html: bytes, base_url: str) -> list[str]:
    """Every local resource URL a page's own markup references (img/
    script/link src|href, plus CSS url()), resolved to absolute against
    *base_url*. De-duplicated, order-preserving. Data URIs and fragment-
    only references are skipped (data: has nothing to look up in the
    cache; a bare "#anchor" resolves to base_url itself, never a real
    sub-resource)."""
    seen, out = set(), []
    for pattern in (_REF_RE, _CSS_URL_RE):
        for m in pattern.finditer(html):
            raw_ref = m.group(1).decode('utf-8', errors='replace').strip()
            if not raw_ref or raw_ref.startswith('data:'):
                continue
            absolute = urljoin(base_url, raw_ref)
            if absolute not in seen:
                seen.add(absolute)
                out.append(absolute)
    return out


def _mime_boundary() -> str:
    import uuid
    return f'----MultipartBoundary--{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}----'


def build_synthetic_mhtml(main_url: str, main_html: bytes,
                          resources: dict[str, tuple[bytes, str]],
                          title: str = '') -> bytes:
    """Assemble a real multipart/related .mhtml document from a
    reconstructed page (*main_html*) plus whatever of its referenced
    sub-resources were actually found elsewhere in the SAME cache
    (*resources*: {absolute_url: (raw_bytes, content_type)}). Uses
    Content-Location per MIME part to identify each resource by its
    OWN original URL — exactly how a real Chrome-generated .mhtml
    archive already links its own parts (confirmed directly against a
    real Chrome Offline Pages archive earlier in this project — see
    artifact_media.py's _build_webpage) — rather than rewriting the
    HTML's own attribute values to point at Content-IDs, which risks
    corrupting anything JS-driven in the markup. QWebEngineView resolves
    references by matching against Content-Location, so this needs no
    HTML rewriting at all: the reconstructed page's markup is embedded
    completely unmodified, byte for byte.

    Marked plainly as a RECONSTRUCTION, not a real Chrome-generated
    snapshot — the top-level headers say so explicitly, and a resource
    the cache no longer has (evicted, never cached, blocked by an
    extension, etc.) is simply absent, the same "absence isn't evidence
    it never existed" caveat this project's whole chrome_offline_pages.py
    description already carries for Chrome's OWN snapshot mechanism."""
    boundary = _mime_boundary()
    parts = []

    main_part = (
        f'Content-Type: text/html; charset=utf-8\r\n'
        f'Content-Transfer-Encoding: binary\r\n'
        f'Content-Location: {main_url}\r\n\r\n'
    ).encode('ascii') + main_html + b'\r\n'
    parts.append(main_part)

    import base64
    for url, (raw_bytes, content_type) in resources.items():
        b64 = base64.b64encode(raw_bytes)
        # MIME requires base64 payload wrapped at 76 chars/line.
        wrapped = b'\r\n'.join(b64[i:i + 76] for i in range(0, len(b64), 76))
        part = (
            f'Content-Type: {content_type or "application/octet-stream"}\r\n'
            f'Content-Transfer-Encoding: base64\r\n'
            f'Content-Location: {url}\r\n\r\n'
        ).encode('ascii') + wrapped + b'\r\n'
        parts.append(part)

    body = (f'\r\n--{boundary}\r\n').encode('ascii').join([b''] + parts) + \
           f'\r\n--{boundary}--\r\n'.encode('ascii')

    header = (
        f'From: <Saved by Claude (ios-ffs-browser reconstruction, NOT a real '
        f'Chrome snapshot -- assembled from separately cached resources; '
        f'anything the cache no longer holds is simply absent, not confirmed '
        f'never loaded)>\r\n'
        f'Snapshot-Content-Location: {main_url}\r\n'
        f'Subject: {title}\r\n'
        f'MIME-Version: 1.0\r\n'
        f'Content-Type: multipart/related;\r\n'
        f'\ttype="text/html";\r\n'
        f'\tboundary="{boundary}"\r\n\r\n'
    ).encode('utf-8')

    return header + body


# ── Full directory pass, shared by both split reports ───────────────────────
#
# artifacts/android/chrome_cache_media.py ("Chrome Cache - Media") and
# chrome_cache_pages.py ("Chrome Cache - Pages") each call parse_all_entries
# and filter/project the SAME full parse down to their own rows, rather than
# each re-implementing the entry-decode/reconstruction loop -- two
# independent parser RUNS (each pays the ~3,000-entry decode cost once, see
# CLAUDE.md for the measured ~1s/run), never two copies of the actual
# decode/reconstruct LOGIC. Originally one merged parser with a custom
# nested-tree UI, then a custom flat-table UI with three filtered views under
# one node; split into two genuinely separate top-level reports per direct
# instruction -- "changing chrome cache to two report rather than nested ...
# chrome cache - media and chrome cache - pages."

def _extract_title(html: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return ""
    return m.group(1).decode("utf-8", errors="replace").strip()


_HEAD_RE = re.compile(rb'<head\b[^>]*>.*?</head>', re.I | re.S)
_SCRIPT_RE = re.compile(rb'<script\b[^>]*>.*?</script>', re.I | re.S)
_STYLE_RE = re.compile(rb'<style\b[^>]*>.*?</style>', re.I | re.S)
_TAG_RE = re.compile(rb'<[^>]+>')


def _visible_static_text_length(html: bytes) -> int:
    """Best-effort estimate of how much text a JS-DISABLED render of this
    page would actually show -- <head> (title/meta, never rendered body
    text), <script>, and <style> content are stripped before counting,
    the same three things a real renderer never displays as page text
    either. Exists specifically to flag which reconstructed pages are
    worth opening at all: prompted directly after a report that "most of
    the mhtml do not work" -- investigated by actually rendering every
    one of this case's own 141 real reconstructions in a real headless
    QWebEngineView with the SAME JavaScript-disabled lockdown the real
    viewer uses (MediaFullViewDialog._build_webpage), not guessed at.
    Result: NOT a reconstruction bug -- of 141, exactly 23 pages have
    any real static body content, and all 23 render with real matching
    text; the other 118 are genuinely empty outside a <script> tag (ad-
    auction payloads, tracking-sync JS, React/SPA app shells like
    Discord/Calendly whose entire body is one empty <div id="root">) --
    correctly blank once JS is off, the same known "JS-driven content is
    a gap" limitation this module's own find_referenced_urls already
    documents, just now surfaced per-row instead of discovered by
    opening each one. This exact 40-char threshold, applied to this
    exact heuristic, was cross-checked against the real render of all
    141 real pages in this project's own Android 14 JoshHickman case and
    matched EVERY ONE (23 flagged "has content" / 23 actually rendered
    text, 118 / 118 blank) -- not assumed accurate."""
    stripped = _HEAD_RE.sub(b'', html)
    stripped = _SCRIPT_RE.sub(b'', stripped)
    stripped = _STYLE_RE.sub(b'', stripped)
    text = _TAG_RE.sub(b' ', stripped)
    return len(b' '.join(text.split()))


def _safe_filename(url: str) -> str:
    import hashlib
    return hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()


def _parse_http_date(date_header):
    """RFC 2822 HTTP Date header -> Unix epoch seconds (int), or None if
    absent/unparseable. This is the response's own Date header (when Chrome
    fetched/revalidated it), the only real timestamp a Simple Cache entry's
    own headers carry -- not a claim about when the examiner's device first
    cached it (no such field survives in what this format exposes)."""
    if not date_header:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_header)
        return int(dt.timestamp())
    except Exception:
        return None


def _guess_media_ext(content_type: str) -> str:
    import mimetypes
    bare = (content_type or '').split(';', 1)[0].strip().lower()
    ext = mimetypes.guess_extension(bare) if bare else None
    return ext or '.bin'


def parse_all_entries(paths: dict) -> list[dict]:
    """Full pass over Cache_Data: parses every Simple Cache entry, builds
    the page->asset AND asset->page(s) cross-reference (the latter is
    chrome_cache_media.py's own `referenced_by_pages` column -- "so the
    examiner can search the pages table for it", per direct request),
    writes each successfully-decoded image/video body out as a real local
    file (`decoded_media_path`, needed because the RAW cache entry is still
    wrapped in Simple Cache's own container + whatever Content-Encoding
    applied, not directly decodable as an image), and each successfully-
    reconstructed HTML page out as a real .mhtml (`reconstructed_mhtml_path`,
    same reasoning as chrome_offline_pages.py's own real snapshots — see
    build_synthetic_mhtml above). Returns ONE row per cache entry with every
    field either split report needs; each just filters this down to its own
    content-type, rather than re-deriving any of it.

    Expects the SAME reserved `paths` keys artifact_runner.py's own
    run_artifact() sets up for a directory-enumerating parser (see
    artifact_runner.py's own comment on _zip_names/_read_zip_bytes/
    _app_base_ui_path/_parser_files_dir/_adapter) -- returns [] if any are
    missing, the same "nothing to do" contract chrome_cache_media.py/
    chrome_cache_pages.py's own run() already expects from the OLD single
    merged parser this replaces."""
    import os

    zip_names = paths.get("_zip_names") or []
    read_bytes = paths.get("_read_zip_bytes")
    app_base = paths.get("_app_base_ui_path", "")
    parser_dir = paths.get("_parser_files_dir")
    adapter = paths.get("_adapter")
    if not zip_names or read_bytes is None or not app_base or adapter is None:
        return []

    # _zip_names is the archive's own PHYSICAL namelist (see
    # artifact_runner.py's own comment on that reserved key) -- a bare
    # ui_path-style prefix like f"{app_base}/cache/..." never matches it
    # directly on a real archive (confirmed: this exact mistake was made
    # and caught here before shipping -- a zip_extras-format archive's
    # physical entries carry a "Dump/" root the ui_path convention
    # doesn't). Resolve the SAME prefix through the adapter first, the
    # same conversion every other extraction path in this project already
    # goes through, and filter the physical names against THAT instead.
    cache_ui_prefix = f"{app_base}/cache/Cache/Cache_Data/"
    cache_physical_prefix = adapter.resolve(cache_ui_prefix.rstrip("/")) + "/"
    # "_0" = an ordinary entry (headers+body, possibly merged into one
    # file -- see this module's own docstring); "_s" = a sparse/range-
    # request entry (e.g. byte-range video caching), read the identical
    # way. index/index-dir files are Simple Cache's own lookup index, not
    # individual resource entries -- skipped, not missed: they carry no
    # single resource's own headers/body to show.
    entry_names = [
        n for n in zip_names
        if n.startswith(cache_physical_prefix) and (n.endswith("_0") or n.endswith("_s"))
    ]

    # First pass: parse every entry and index by its own resource URL, so
    # a later page's own reference scan can look siblings up by URL
    # regardless of which order entries happen to be enumerated in.
    parsed = []
    url_index = {}
    for physical_path in entry_names:
        data = read_bytes(physical_path)
        if data is None:
            continue
        entry = parse_simple_cache_entry(data)
        if entry is None or not entry.get("url"):
            continue
        # Stored in true ui_path form (app_base's own convention + this
        # entry's bare filename), NOT the physical_path just used to read
        # it -- this is what later gets persisted as raw_ui_path and read
        # again by the GUI's OWN _read_zip_bytes (hex_viewer.py), which
        # expects a ui_path and resolves it through the adapter itself;
        # handing it an already-physical path would double-resolve to
        # something that doesn't exist.
        basename = physical_path.rsplit("/", 1)[-1]
        entry["_ui_path"] = cache_ui_prefix + basename
        parsed.append(entry)
        url_index[entry["url"]] = entry

    # Second pass, page-only: every image/video URL that ANY html page's
    # own reference scan resolved to a sibling in this same cache -- both
    # directions built together (page->assets AND asset->pages) so
    # there's exactly one definition of "linked to a page" that both
    # reports agree with, not two separately-derived ones that could
    # drift apart.
    page_asset_urls: dict[str, list[str]] = {}     # page url -> [asset urls]
    asset_referenced_by: dict[str, list[str]] = {} # asset url -> [page urls]
    page_title_by_url: dict[str, str] = {}         # page url -> its own <title>
    for entry in parsed:
        content_type = entry.get("content_type") or ""
        if "text/html" not in content_type.lower() or not entry.get("body"):
            continue
        page_title_by_url[entry["url"]] = _extract_title(entry["body"])
        refs = find_referenced_urls(entry["body"], entry["url"])
        assets = []
        for ref_url in refs:
            sibling = url_index.get(ref_url)
            if not sibling or not sibling.get("body"):
                continue
            sib_type = (sibling.get("content_type") or "").lower()
            if sib_type.startswith("image/") or sib_type.startswith("video/"):
                assets.append(ref_url)
                asset_referenced_by.setdefault(ref_url, []).append(entry["url"])
        page_asset_urls[entry["url"]] = assets

    out = []
    for entry in parsed:
        content_type = entry.get("content_type") or ""
        is_html = "text/html" in content_type.lower()
        is_media = content_type.lower().startswith(("image/", "video/"))
        reconstructed_path = ""
        decoded_media_path = ""
        refs_found = refs_resolved = None
        render_note = ""

        if is_html and entry.get("body") and parser_dir:
            refs = find_referenced_urls(entry["body"], entry["url"])
            resources = {}
            for ref_url in refs:
                sibling = url_index.get(ref_url)
                if sibling and sibling.get("body"):
                    resources[ref_url] = (sibling["body"], sibling.get("content_type") or "")
            refs_found = len(refs)
            refs_resolved = len(resources)
            title = _extract_title(entry["body"])
            if _visible_static_text_length(entry["body"]) < 40:
                render_note = (
                    "No visible static content -- renders blank with "
                    "JavaScript disabled (script/ad-tech shell)")
            elif not any((ct or "").lower().startswith("text/css")
                        for _, ct in resources.values()):
                # A real, separate failure mode from the one above: real
                # text content exists, but NO stylesheet resolved for this
                # page -- confirmed directly against a real GTD ground-
                # truth pair (chrome-bbc-google-001): BBC's own site
                # ships zero <link rel="stylesheet"> in its static markup
                # at all (its actual styling is CSS-in-JS, injected by
                # the same JavaScript this viewer deliberately disables)
                # -- rendered SOLID BLACK top to bottom despite the DOM
                # text matching the real page exactly (confirmed via
                # toPlainText()). A page whose CSS ships as an ordinary
                # external stylesheet (confirmed on a real MLB article,
                # ONE resolved text/css part) renders correctly styled by
                # contrast -- so "zero resolved text/css" is a real,
                # verified signal, not a guess, though a page styled
                # ENTIRELY via inline <style> with no external stylesheet
                # reference at all would false-positive here (rare on
                # real sites, not observed in this verification).
                render_note = (
                    "No stylesheet resolved -- has real text content but "
                    "may render unstyled/plain (styling likely delivered "
                    "via JavaScript on this site, or its stylesheet "
                    "simply wasn't found in this same cache)")
            try:
                mhtml = build_synthetic_mhtml(
                    entry["url"], entry["body"], resources, title=title)
                dest = os.path.join(parser_dir, _safe_filename(entry["url"]) + ".mhtml")
                with open(dest, "wb") as f:
                    f.write(mhtml)
                reconstructed_path = dest
            except OSError:
                reconstructed_path = ""
        elif is_media and entry.get("body") and parser_dir:
            # Same idea as the .mhtml reconstruction above, for a raw
            # image/video body -- write the already-decompressed bytes out
            # as their own real local file so media_viewer.ThumbnailWorker
            # (which now reads a parser-generated LOCAL file directly, see
            # its own os.path.isabs() branch) can actually thumbnail/open
            # it; the RAW cache entry (raw_ui_path) is not itself
            # image/video-decodable, still wrapped in Simple Cache's own
            # container plus whatever Content-Encoding applied.
            ext = _guess_media_ext(content_type)
            dest = os.path.join(parser_dir, _safe_filename(entry["url"]) + ext)
            try:
                with open(dest, "wb") as f:
                    f.write(entry["body"])
                decoded_media_path = dest
            except OSError:
                decoded_media_path = ""

        page_title = _extract_title(entry["body"]) if is_html and entry.get("body") else ""
        # Delimited, not JSON -- artifact_db.py stores every column as
        # plain TEXT (see its own docstring); URLs never legitimately
        # contain a literal newline, so "\n"-joined is unambiguous.
        child_asset_urls = "\n".join(page_asset_urls.get(entry["url"], []))
        referencing_pages = asset_referenced_by.get(entry["url"], [])
        referenced_by_pages = "\n".join(referencing_pages)
        # Positionally parallel to referenced_by_pages (line N is that
        # page's own <title>, possibly empty) rather than combined into
        # one string -- title/url as separate columns is this project's
        # own established convention (see chrome_cache_pages.py's own
        # url/title columns).
        referenced_by_page_titles = "\n".join(
            page_title_by_url.get(u, "") for u in referencing_pages)

        out.append({
            "url": entry["url"],
            "title": page_title,
            "top_level_site": entry.get("top_level_site") or "",
            "status_line": entry.get("status_line") or "",
            "content_type": content_type,
            "content_encoding": entry.get("content_encoding") or "",
            "raw_body_length": entry.get("raw_body_length") or 0,
            "body_decoded_length": len(entry["body"]) if entry.get("body") else None,
            "body_error": entry.get("body_error") or "",
            "header_parse_error": entry.get("header_parse_error") or "",
            "response_date": entry.get("headers", {}).get("date") or "",
            "response_date_epoch": _parse_http_date(entry.get("headers", {}).get("date")),
            "references_found": refs_found,
            "references_resolved": refs_resolved,
            "reconstructed_mhtml_path": reconstructed_path,
            "decoded_media_path": decoded_media_path,
            "render_note": render_note,
            "child_asset_urls": child_asset_urls,
            "referenced_by_pages": referenced_by_pages,
            "referenced_by_page_titles": referenced_by_page_titles,
            "raw_ui_path": entry["_ui_path"],
        })
    return out
