"""header_scan.py — magic-byte and text-content file type detection.

Reads zip entries directly by file offset, bypassing zipfile for speed
(FFS archives are always stored, never compressed).

Detected types match _get_file_type() category strings.

candidates passed to scan_entries are (ui_path, data_offset, file_size) triples.
file_size is the uncompressed byte count; it gates the full-text check.
"""

import struct

from zip_reader import ZipReader

# (signature, category) — first match wins.
_SIGNATURES: list[tuple[bytes, str | None]] = [
    (b'SQLite format 3\x00', 'Database'),
    (b'\xff\xd8\xff',        'Picture'),   # JPEG
    (b'\x89PNG\r\n\x1a\n',  'Picture'),   # PNG
    (b'GIF87a',              'Picture'),
    (b'GIF89a',              'Picture'),
    (b'II*\x00',             'Picture'),   # TIFF LE
    (b'MM\x00*',             'Picture'),   # TIFF BE
    (b'%PDF',                'Document'),
    (b'PK\x03\x04',         'Archive'),    # ZIP
    (b'PK\x05\x06',         'Archive'),    # empty ZIP
    (b'\x1f\x8b',           'Compressed'), # gzip
    (b'BZh',                'Compressed'), # bzip2
    (b'\xfd7zXZ\x00',       'Compressed'), # xz
    (b'SEGB',               'Biome Stream'),  # iOS Biome event stream (v1 & v2)
    (b'bplist',             'Property List'),
    (b'<?xml',              'XML'),
    (b'<html',              'Web / Data'),
    (b'<HTML',              'Web / Data'),
    (b'<!DOC',              'Web / Data'),
    (b'ID3',                'Audio'),     # MP3 with ID3
    (b'\xff\xfb',           'Audio'),     # MP3
    (b'\xff\xf3',           'Audio'),     # MP3
    (b'\xff\xf2',           'Audio'),     # MP3
    (b'OggS',              'Audio'),
    (b'fLaC',              'Audio'),
    (b'RIFF',              None),         # WAV or AVI — disambiguate below
]

_AUDIO_BRANDS = {b'M4A ', b'M4P ', b'f4a ', b'aac '}

# Maximum file size for the full-content text check (512 KB).
TEXT_SIZE_LIMIT = 512 * 1024

# Bytes that are valid in plain ASCII text: printable 0x20–0x7E plus
# tab, LF, CR.  translate(None, _TEXT_DELETE) strips these; anything
# left over is binary.
_TEXT_DELETE = bytes(range(0x20, 0x7F)) + b'\x09\x0A\x0D'


def classify_magic(header: bytes) -> str | None:
    """Return a category string from the first 16 bytes, or None."""
    if len(header) < 4:
        return None
    for sig, cat in _SIGNATURES:
        if header.startswith(sig):
            if cat is not None:
                return cat
            if len(header) >= 12:
                form = header[8:12]
                if form == b'WAVE':
                    return 'Audio'
                if form in (b'AVI ', b'AVIX'):
                    return 'Video'
            return None
    if len(header) >= 12 and header[4:8] == b'ftyp':
        brand = header[8:12]
        return 'Audio' if brand in _AUDIO_BRANDS else 'Video'
    # JSON heuristic — checked last to minimise false positives.
    first = header.lstrip(b' \t\r\n')
    if first and first[0] in (ord('{'), ord('[')):
        return 'JSON'
    return None


def _is_text(data: bytes) -> bool:
    """Return True if every byte in data is printable ASCII or whitespace."""
    if not data:
        return False
    # translate(None, delete) removes all valid-text bytes; non-empty result
    # means at least one binary byte is present.
    return len(data.translate(None, _TEXT_DELETE)) == 0


def scan_entries(
    zip_path: str,
    candidates: list[tuple[str, int, int]],
    progress_cb=None,
    cancel_check=None,
    batch_size: int = 200,
) -> dict[str, str]:
    """Return {ui_path: detected_type} for entries with a positively identified type.

    candidates: list of (ui_path, data_offset, file_size).
      data_offset — byte position where the entry's raw data starts in zip_path.
      file_size   — uncompressed size in bytes; controls the text-check limit.

    For each entry: reads up to TEXT_SIZE_LIMIT bytes in a single I/O, runs
    classify_magic(), then falls back to a full-content text check if the file
    is small enough and unrecognised.  Entries are processed concurrently using
    up to 8 threads (each opens its own file handle).

    progress_cb(remaining: int) called after each batch.
    """
    reader = ZipReader(zip_path)

    def _scan_one(item: tuple[str, int, int]) -> str | None:
        ui_path, data_offset, file_size = item
        max_b = TEXT_SIZE_LIMIT if 0 < file_size <= TEXT_SIZE_LIMIT else 16
        buf = reader.read_at(data_offset, file_size, max_bytes=max_b)
        detected = classify_magic(buf)
        if detected is None and 0 < file_size <= TEXT_SIZE_LIMIT:
            if _is_text(buf):
                detected = 'Text'
        return detected

    results: dict[str, str] = {}
    _pcb = (lambda d, t: progress_cb(t - d)) if progress_cb else None
    for (ui_path, _, __), detected in reader.run_parallel(
            candidates, _scan_one,
            progress_cb=_pcb,
            cancel_check=cancel_check,
            batch_size=batch_size):
        if detected:
            results[ui_path] = detected
    return results
