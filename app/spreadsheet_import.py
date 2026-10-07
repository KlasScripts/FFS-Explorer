"""Reads CSV/XLSX hash-list spreadsheets (e.g. a ProjectVic export/diff
report) for the "Import Bookmarks…" dialog, and the path/hash normalizing
logic that turns one of its rows into something that can be looked up in
the FFS. Pure Python, no Qt — see ffs-explorer.py's ImportBookmarksDialog
and ImportBookmarksWorker for the UI and the actual archive-matching run.

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


def normalize_ffs_path(raw_path: str, archive_basename: str) -> str:
    """Turn a spreadsheet's own path text into a candidate FFS ui_path:
    backslashes -> forward slashes, surrounding whitespace/slashes
    trimmed, and a leading path segment that names the loaded archive
    itself (e.g. "EXTRACTION_FFS.zip\\data\\data\\...", with or without
    the ".zip" suffix, case-insensitive) stripped — real export tools
    commonly prefix every path with the archive's own filename, which
    this app's own ui_path convention never includes (confirmed against
    a real ProjectVic differences report, 2026-10-07: every path cell
    started with "EXTRACTION_FFS.zip\\...")."""
    path = raw_path.strip().replace('\\', '/').strip('/')
    if not path:
        return path
    first_seg, _, rest = path.partition('/')
    archive_stem = os.path.splitext(archive_basename)[0].lower()
    if first_seg.lower() in (archive_basename.lower(), archive_stem) and rest:
        return rest
    return path


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
