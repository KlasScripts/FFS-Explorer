"""Reads CSV/XLSX hash-list spreadsheets (e.g. a ProjectVic export/diff
report, or a Griffeye case-analysis export) for the "Import Bookmarks…"
dialog, and the path/hash normalizing logic that turns one of its rows
into something that can be looked up in the FFS. Pure Python, no Qt —
see ffs-explorer.py's ImportBookmarksDialog and ImportBookmarksWorker for
the UI and the actual archive-matching run.

Added 2026-10-07, direct request: import a spreadsheet of known hashes
and filepaths (e.g. a ProjectVic CAID differences/export report), locate
each file inside the loaded FFS by path, confirm the row's hash against
the file's OWN hash computed from the archive's bytes (never trust the
spreadsheet's path-column name match alone), and bookmark every file that
verifies. XLS (legacy binary Excel) was explicitly scoped out — csv/xlsx
cover the real workflow this was built for.
"""

import csv
import hashlib
import os

# Display label -> hashlib algorithm name. Limited to what hashlib
# supports natively (no PhotoDNA/perceptual hashes — those aren't exact
# byte-for-byte verifiable the way this feature's "confirm by re-hashing
# the file in the FFS" design requires).
HASH_ALGORITHMS = {
    'MD5': 'md5',
    'SHA-1': 'sha1',
    'SHA-256': 'sha256',
    'SHA-512': 'sha512',
}


def read_spreadsheet(file_path: str) -> tuple[list[str], list[list[str]]]:
    """Return (headers, rows) for a .csv or .xlsx file — the first row is
    always treated as the header row. Every cell is coerced to a plain
    str (openpyxl hands back int/float/None for numeric-looking cells;
    a whole-number float is rendered without the trailing ".0", a common
    nuisance when a spreadsheet stores an ID-like column as a number).
    Raises ValueError for an unsupported extension, and whatever the
    underlying csv/openpyxl call itself raises for a malformed/unreadable
    file — the caller (ImportBookmarksDialog) shows that message directly
    rather than masking it, since a parse failure here means the examiner
    picked the wrong file or it's genuinely corrupt."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.csv':
        return _read_csv(file_path)
    elif ext == '.xlsx':
        return _read_xlsx(file_path)
    raise ValueError(f"Unsupported spreadsheet type: {ext or file_path}")


def _read_csv(file_path: str) -> tuple[list[str], list[list[str]]]:
    with open(file_path, 'r', newline='', encoding='utf-8-sig', errors='replace') as f:
        reader = csv.reader(f)
        rows = [row for row in reader if any(c.strip() for c in row)]
    if not rows:
        return [], []
    headers = rows[0]
    width = len(headers)
    data_rows = [(row + [''] * (width - len(row)))[:width] for row in rows[1:]]
    return headers, data_rows


def _cell_to_str(value) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _read_xlsx(file_path: str) -> tuple[list[str], list[list[str]]]:
    import openpyxl  # lazy — see requirements.txt's own comment on this
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return [], []
        headers = [_cell_to_str(c) for c in header_row]
        width = len(headers)
        data_rows = []
        for row in rows_iter:
            cells = [_cell_to_str(c) for c in row]
            if not any(c.strip() for c in cells):
                continue
            data_rows.append((cells + [''] * (width - len(cells)))[:width])
        return headers, data_rows
    finally:
        wb.close()


def combine_filepath_and_filename(raw_filepath: str, raw_filename: str) -> str:
    """Joins a separate filepath + filename column pair into one raw path
    BEFORE normalize_ffs_path sees it. Added 2026-10-10, real bug fix:
    when "Filename is its own column" is used, the filepath column holds
    only the containing DIRECTORY (confirmed against a real Griffeye
    case-analysis export, whose own "File Path"/"File Name" columns
    split exactly this way) — every prior release used the filepath
    column ALONE as the file to match against the archive, which
    resolved to a FOLDER rather than a file and so could never match
    anything, while display_name (the only other place the filename
    column was read) looked completely unaffected. Blank raw_filename
    is a no-op (returns raw_filepath unchanged) for a row that left the
    column empty rather than genuinely wanting a directory match."""
    filepath = raw_filepath.strip()
    filename = raw_filename.strip()
    if not filename:
        return filepath
    if not filepath:
        return filename
    return filepath.rstrip('/\\') + '/' + filename.lstrip('/\\')


def normalize_ffs_path(raw_path: str, archive_basename: str,
                       strip_parent_folder: bool = False) -> str:
    """Turn a spreadsheet's own path text into a candidate FFS ui_path:
    backslashes -> forward slashes, surrounding whitespace/slashes
    trimmed, and a leading path segment that names the loaded archive
    itself (e.g. "EXTRACTION_FFS.zip\\data\\data\\...", with or without
    the ".zip" suffix, case-insensitive) stripped — real export tools
    commonly prefix every path with the archive's own filename, which
    this app's own ui_path convention never includes (confirmed against
    a real ProjectVic differences report, 2026-10-07: every path cell
    started with "EXTRACTION_FFS.zip\\...").

    strip_parent_folder (added 2026-10-10, direct request/real report):
    some grading tools (confirmed against a real Griffeye export) prefix
    paths with ANOTHER segment above that — the folder the FFS zip
    itself was sitting in on disk, e.g.
    "CaseFolder\\EXTRACTION_FFS.zip\\data\\..." — which the archive-name
    check above can't remove on its own, since it only ever looks at
    the FIRST segment and that extra folder's own name has nothing to
    do with the archive's name. When True, one leading segment is
    removed UNCONDITIONALLY (whatever it is) BEFORE the archive-name
    check runs, so that check still correctly fires against whatever
    segment is now first — composing the two strips handles exactly
    this "two extra segments" shape. A no-op (like the archive-name
    check) when there's nothing left to strip."""
    path = raw_path.strip().replace('\\', '/').strip('/')
    if not path:
        return path
    if strip_parent_folder:
        _, _, rest = path.partition('/')
        if rest:
            path = rest
    first_seg, _, rest = path.partition('/')
    archive_stem = os.path.splitext(archive_basename)[0].lower()
    if first_seg.lower() in (archive_basename.lower(), archive_stem) and rest:
        return rest
    return path


def _collapse_private_var_prefix(path: str) -> str | None:
    """If *path* STARTS WITH "private/var/mobile/" or "private/var/root/"
    (or equals one of those two bare), returns it collapsed to
    "mobile/..."/"root/..." — this app's own ui_path convention for
    those two well-known per-user home directories. None if *path*
    doesn't start that way (prefix-anchored deliberately, not a
    substring match anywhere — these two names only mean anything
    structural at the TOP of a relative path; a coincidental nested
    occurrence deeper in a real path should never be rewritten)."""
    for user in ('mobile', 'root'):
        prefix = f'private/var/{user}/'
        if path.startswith(prefix):
            return user + '/' + path[len(prefix):]
        if path == f'private/var/{user}':
            return user
    return None


def ios_path_candidates(ui_path: str) -> list[str]:
    """Returns [ui_path] plus iOS-specific fallback candidates, for the
    caller to try in order until one actually resolves in the archive.

    Confirmed against a real Cellebrite-format iOS archive + a real
    Griffeye case-analysis export, 2026-10-10 — TWO separate real
    quirks, both needed together to match Griffeye's own "File Path"
    column:

    1. This app's own ui_path convention collapses "private/var/mobile"
       and "private/var/root" down to just "mobile"/"root" for those two
       well-known per-user home directories — baked into how folder_map
       itself gets built from the raw archive, not something exposed as
       a reusable string transform (FfsAdapter.strip_display_prefix only
       strips the archive's own internal format prefix, e.g.
       "filesystem1/", never this).

    2. Griffeye ALSO prefixes every path with its own generic "root"
       segment meaning "the top of the extracted device filesystem" —
       e.g. "root\\private\\var\\mobile\\Library\\...". This is NOT the
       same "root" as (1)'s /private/var/root — a real, confusing name
       collision between Griffeye's own generic extraction-root marker
       and this app's own specific per-user-home meaning for the exact
       same word. Passing a path with Griffeye's "root/" still attached
       straight to FfsAdapter.resolve() produces a nonsense physical
       path (confirmed directly: resolve() re-inserts "private/var/"
       before a recognized "root"/"mobile" first segment, so a leftover
       Griffeye "root/" ahead of a real "private/var/mobile/..." tail
       resolves to ".../private/var/root/private/var/mobile/..." —
       doubled and wrong). Tried as its own fallback (with and without
       collapsing (1) on what's left) since this marker is never
       confirmable from the path text alone — only by which candidate
       actually resolves."""
    candidates = [ui_path]
    collapsed = _collapse_private_var_prefix(ui_path)
    if collapsed is not None:
        candidates.append(collapsed)
    if ui_path.startswith('root/'):
        without_marker = ui_path[len('root/'):]
        candidates.append(without_marker)
        collapsed2 = _collapse_private_var_prefix(without_marker)
        if collapsed2 is not None:
            candidates.append(collapsed2)
    return candidates


def normalize_hash(raw_hash: str) -> str:
    """Lowercase hex, surrounding whitespace stripped. Spreadsheets occasionally
    carry a stray "0x" prefix on hash columns copied from other tooling."""
    h = raw_hash.strip().lower()
    if h.startswith('0x'):
        h = h[2:]
    return h


def compute_hash(data: bytes, algo_name: str) -> str:
    """*algo_name* is a hashlib algorithm name (HASH_ALGORITHMS' values),
    not the display label."""
    return hashlib.new(algo_name, data).hexdigest()
