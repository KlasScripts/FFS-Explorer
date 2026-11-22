"""File types by name: the extension sets, the one extension -> type
table and _get_file_type. Moved out of ffs-explorer.py (2026-11-22) so the
first-open parse process can type files too (header_counts). Qt-free."""
import os
import re

import header_scan


# .png/.gif are deliberately NOT here — see header_scan.GRAPHIC_EXTENSIONS
# and _EXT_TO_TYPE below for why they're their own 'Graphic' Type instead
# (added 2026-10-11, direct request: "i want you to seperate out png and
# gif into graphic type both via the ext and the header scan[,] so that
# the filter only need to use the type without filltering further and
# missing some images").
PICTURE_EXTENSIONS = {
    '.jpg', '.jpeg', '.heic', '.heif',
    '.bmp', '.tiff', '.tif', '.webp', '.raw', '.cr2', '.nef', '.dng',
}
VIDEO_EXTENSIONS = {
    '.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv',
    '.flv', '.webm', '.3gp', '.mts', '.m2ts', '.mpg', '.mpeg',
}
DATABASE_EXTENSIONS = {
    '.db', '.sqlite', '.sqlite3', '.db3',
    '.db-wal', '.db-shm', '.db-journal',
    '.sqlite-wal', '.sqlite-shm', '.sqlite-journal',
}
LOG_EXTENSIONS = {
    '.log', '.ips', '.crash', '.clslog', '.logarchive',
}
DOCUMENT_EXTENSIONS = {
    '.pdf', '.txt', '.rtf', '.csv',
    '.doc', '.docx', '.pages', '.numbers', '.key',
    '.xls', '.xlsx', '.ppt', '.pptx',
}
AUDIO_EXTENSIONS = {
    '.mp3', '.m4a', '.aac', '.wav', '.flac', '.aiff', '.aif',
    '.caf', '.opus', '.ogg', '.amr', '.wma',
}
ARCHIVE_EXTENSIONS = {
    '.zip', '.gz', '.tar', '.bz2', '.xz',
    '.tgz', '.tbz', '.zst', '.aar',
}
APPLICATION_EXTENSIONS = {'.ipa', '.apk', '.apks', '.jar'}
JSON_EXTENSIONS = {'.json', '.jsonl', '.ndjson'}
XML_EXTENSIONS  = {'.xml', '.xsl', '.xslt', '.xsd', '.gpx', '.kml', '.rss', '.svg'}
WEB_DATA_EXTENSIONS = {
    '.html', '.htm', '.js', '.css', '.ts',
    '.yaml', '.yml', '.toml',
}
CERTIFICATE_EXTENSIONS = {
    '.pem', '.cer', '.crt', '.der', '.p12', '.p7s', '.p8', '.pfx',
}


# Single extension → type lookup table.  One O(1) dict hit replaces a cascade
# of set-membership tests — _get_file_type runs once per entry in several
# full-archive loops.  The extension sets are mutually disjoint.
_EXT_TO_TYPE: dict = {
    **{e: 'Picture'       for e in PICTURE_EXTENSIONS},
    **{e: 'Graphic'       for e in header_scan.GRAPHIC_EXTENSIONS},
    **{e: 'Video'         for e in VIDEO_EXTENSIONS},
    **{e: 'Database'      for e in DATABASE_EXTENSIONS},
    '.plist': 'Property List',
    **{e: 'Log'           for e in LOG_EXTENSIONS},
    **{e: 'Document'      for e in DOCUMENT_EXTENSIONS},
    **{e: 'Audio'         for e in AUDIO_EXTENSIONS},
    **{e: 'Archive'       for e in ARCHIVE_EXTENSIONS},
    **{e: 'Application'   for e in APPLICATION_EXTENSIONS},
    **{e: 'JSON'          for e in JSON_EXTENSIONS},
    **{e: 'XML'           for e in XML_EXTENSIONS},
    **{e: 'Web / Data'    for e in WEB_DATA_EXTENSIONS},
    **{e: 'Certificate'   for e in CERTIFICATE_EXTENSIONS},
}


_WEB_CACHE_NAME = re.compile(r'^[0-9a-f]{16}_[01s]$')


def _ext(name: str) -> str:
    """os.path.splitext(name)[1], without its cost for a plain name (no
    path separator or drive): the part from the last '.', unless only dots
    come before it ('.bashrc' has none). _get_file_type runs once per entry
    in several whole-archive loops; splitext was ~40% of it (2026-11-22)."""
    if '/' in name or '\\' in name or ':' in name:
        return os.path.splitext(name)[1]
    i = name.rfind('.')
    if i <= 0 or not name[:i].lstrip('.'):
        return ''
    return name[i:]


def _get_file_type(name):
    # Dotted sidecar extensions (.db-wal, .sqlite-journal, …) are all in the
    # table; only bare dotless companions ('foo-wal') need the endswith fallback.
    ft = _EXT_TO_TYPE.get(_ext(name).lower())
    if ft is not None:
        return ft
    if name.lower().endswith(('-wal', '-shm')):
        return 'Database'
    # Chromium Simple Cache files ('<16 hex>_0'/'_1'/'_s'), used by Chrome and
    # every app with a Chromium WebView (ASSUMPTIONS.md A-75). By name here;
    # header_scan also recognises the format's own magic for any other name.
    if _WEB_CACHE_NAME.match(name):
        return 'Web Cache'
    return 'Other'
