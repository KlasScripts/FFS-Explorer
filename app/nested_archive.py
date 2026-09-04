"""nested_archive.py — extracts a single embedded/nested archive (ZIP or
gzip) from an FFS zip's raw bytes, repacks/decompresses it into
case_dir/nested_archives/, and records it in casecache.db.

Added 2026-08-30. Qt-free core logic factored out of
ffs-explorer.py's own NestedArchiveWorker._process_one (the examiner's
manual "Extract as Nested Archive" / batch action in ProcessDialog) so
there is exactly ONE implementation of "how to extract a nested archive"
— shared by that GUI action and by artifact_runner.py's own
requires_nested_extraction (a parser declaring it needs a SPECIFIC
embedded archive extracted before its own files/optional_files
resolution can find what it needs — see WRITING_ARTIFACT_PARSERS.md).
Per this project's own core design principle ("not doing work for work's
sake"): extraction is never automatic or blanket here either way — the
examiner triggers it manually for arbitrary files, or a parser triggers
it for the ONE specific path it declared it needs, never "extract
everything, just in case."

app/ modules never import from the top-level ffs-explorer.py script (the
reverse is the normal direction), so this module takes its file-type
classifier as an injected callable (*get_file_type*) rather than
importing ffs-explorer.py's own extension-table-based `_get_file_type` —
see extract_one's own docstring for what a caller without that richer
classifier can pass instead.
"""

import gzip
import hashlib
import io
import os
import struct
import zipfile
from contextlib import closing
from datetime import datetime, timezone

import header_scan
from db_utils import (_open_cache_db, load_nested_archives, save_nested_archive,
                      save_nested_archive_entries, save_nested_archive_failure)
from zip_cd_cache import CachedZipView, load as _zcd_load

_MEM_LIMIT = 100 * 1024 * 1024   # 100 MB — larger files written to temp first


def already_extracted(case_dir: str, ui_path: str) -> bool:
    """True if *ui_path* has already been fully extracted (a DB record
    exists, its output file is still on disk, and its recorded entry
    count matches — the exact same idempotency check
    NestedArchiveWorker._load_completed already uses for the examiner's
    own manual extraction, reused here so a parser's own
    requires_nested_extraction never re-extracts something the examiner
    — or an earlier parser run — already did)."""
    try:
        out_dir = os.path.join(case_dir, 'nested_archives')
        with closing(_open_cache_db(case_dir)) as db:
            for row in load_nested_archives(db):
                if row['ui_path'] != ui_path:
                    continue
                if row.get('error_msg'):
                    return False
                disk_path = os.path.join(out_dir, row['stored_filename'])
                return os.path.isfile(disk_path)
        return False
    except Exception:
        return False


def extracted_path(case_dir: str, ui_path: str) -> str | None:
    """The on-disk path of an already-extracted archive for *ui_path*, or
    None if it hasn't been extracted (or extraction previously failed).
    Callers (e.g. artifact_runner.py) use this to populate the paths
    dict's reserved `_nested_archives` entry after confirming
    already_extracted()."""
    try:
        out_dir = os.path.join(case_dir, 'nested_archives')
        with closing(_open_cache_db(case_dir)) as db:
            for row in load_nested_archives(db):
                if row['ui_path'] == ui_path and not row.get('error_msg'):
                    disk_path = os.path.join(out_dir, row['stored_filename'])
                    if os.path.isfile(disk_path):
                        return disk_path
        return None
    except Exception:
        return None


def extract_one(zip_path: str, case_dir: str, ui_path: str, physical_path: str,
                file_size: int, get_file_type) -> tuple[bool, str | None, str | None]:
    """Extract one Archive- or Compressed-type entry from *zip_path*.

    *ui_path* is the archive-display path recorded in casecache.db (what
    the examiner sees / a parser declared); *physical_path* is its real
    zip entry name (already resolved by the caller's own adapter —
    ffs-explorer.py's FfsAdapter.resolve or artifact_runner.py's own
    equivalent). *get_file_type* classifies a plain filename by extension
    ('Other' for anything it doesn't recognize) — ffs-explorer.py's own
    NestedArchiveWorker passes its real, rich extension table; a caller
    without that (artifact_runner.py, which has no equivalent import
    available — app/ modules don't import from the top-level script) can
    pass a minimal `lambda name: 'Other'` instead: real per-entry typing
    still happens via header_scan.classify_magic() as a fallback either
    way, so extraction correctness is unaffected — only the metadata
    LABEL richness for an entry whose extension is itself missing/wrong
    differs slightly between the two callers, an accepted, honest
    trade-off rather than duplicating the full extension-table constants
    into this module.

    Returns (success, compound_type, error_message). compound_type is
    set for gzip files where the decompressed content type is known
    (e.g. 'JSON — gzip'); None for zip files, an unknown type, or on
    failure. error_message is None on success.

    Idempotent in the sense that it always re-extracts when called —
    callers wanting to skip an already-done extraction should check
    already_extracted() themselves first (this function doesn't, since
    ffs-explorer.py's own batch worker has its own progress-reporting
    reasons to check that at a different layer)."""
    out_dir = os.path.join(case_dir, 'nested_archives')
    os.makedirs(out_dir, exist_ok=True)
    try:
        # Read via the local .zcd central-directory cache when available
        # (same pattern as device_timezone.py's own detect_handset_zone) --
        # the main FFS archive itself is never compressed, so this is a
        # direct offset seek rather than a second full central-directory
        # read over what can be a large network-hosted zip. Falls back to
        # a plain zipfile.ZipFile only when the cache isn't built yet.
        _view = None
        infos = _zcd_load(zip_path, case_dir)
        if infos is not None:
            _view = CachedZipView(zip_path, infos)
        with (_view if _view is not None else zipfile.ZipFile(zip_path, 'r')) as zf:
            raw = zf.open(physical_path).read()

        key = hashlib.sha1(ui_path.encode()).hexdigest()[:12]
        basename = os.path.basename(ui_path) or 'content'
        stored_filename = f"{key}_{basename}"
        out_path = os.path.join(out_dir, stored_filename)

        if raw[:2] == b'\x1f\x8b':
            # ── Gzip: decompress and store the raw decompressed file ──
            decompressed = gzip.decompress(raw)
            with open(out_path, 'wb') as f:
                f.write(decompressed)
            child_name = (basename[:-3] if basename.lower().endswith('.gz')
                          else basename)
            ft = get_file_type(child_name)
            if ft == 'Other' and decompressed:
                ft = header_scan.classify_magic(decompressed[:16]) or 'Other'
            gz_mtime = struct.unpack_from('<I', raw, 4)[0] if len(raw) >= 8 else 0
            gz_mdate = (datetime.fromtimestamp(gz_mtime, tz=timezone.utc)
                        .strftime('%Y-%m-%d %H:%M:%S') if gz_mtime else None)
            content_rows = [(child_name, gz_mdate, len(decompressed), ft)]
            entry_count = 1
            compound_type = f"{ft} — gzip" if ft != 'Other' else None

        else:
            # ── ZIP: repack as ZIP_STORED ───────────────────────────────
            tmp_path = out_path + '.tmp'
            if file_size > _MEM_LIMIT:
                with open(tmp_path, 'wb') as f:
                    f.write(raw)
                src_arg = tmp_path
            else:
                src_arg = None

            entry_count = 0
            content_rows = []
            compound_type = None
            src_handle = open(src_arg, 'rb') if src_arg else io.BytesIO(raw)
            try:
                with zipfile.ZipFile(src_handle, 'r') as src_zip, \
                     zipfile.ZipFile(out_path, 'w',
                                     compression=zipfile.ZIP_STORED) as dst_zip:
                    for info in src_zip.infolist():
                        if info.filename.endswith('/'):
                            continue   # skip directory entries
                        data = src_zip.read(info.filename)
                        dst_zip.writestr(info, data, compress_type=zipfile.ZIP_STORED)
                        entry_count += 1
                        name = info.filename
                        ft = get_file_type(name.rsplit('/', 1)[-1])
                        if ft == 'Other' and data:
                            ft = header_scan.classify_magic(data[:16]) or 'Other'
                        dt = info.date_time
                        mdate = (f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d} "
                                f"{dt[3]:02d}:{dt[4]:02d}:{dt[5]:02d}"
                                if any(dt) else None)
                        content_rows.append((info.filename, mdate,
                                             info.file_size, ft))
            finally:
                if src_arg:
                    src_handle.close()
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        with closing(_open_cache_db(case_dir)) as db:
            save_nested_archive(db, ui_path, stored_filename, file_size, entry_count)
            save_nested_archive_entries(db, ui_path, content_rows)
        return True, compound_type, None

    except Exception as exc:
        msg = str(exc)
        try:
            with closing(_open_cache_db(case_dir)) as db:
                save_nested_archive_failure(db, ui_path, msg)
        except Exception:
            pass
        return False, None, msg
