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
from db_utils import (_open_cache_db, load_nested_archives, load_nested_archive_entries, save_nested_archive,
                      save_nested_archive_entries, save_nested_archive_failure)
from zip_cd_cache import CachedZipView, load as _zcd_load

_MEM_LIMIT = 100 * 1024 * 1024   # 100 MB — larger files written to temp first


def classify_unpacked(name: str, data: bytes, get_file_type) -> str:
    """Type one unpacked file the way the main archive's files are typed:
    the extension first (the File Browser's own precedence); if that says
    'Other', the header scan's byte check (header_scan.classify_magic, then
    the text check for a small file — the same steps as scan_entries).
    Done at unpack time, with the bytes already in memory, so files that
    only exist inside a nested archive get their types without waiting for
    the next header scan (which runs before unpacking in Process Case and
    never sees them). Added 2026-10-22 (owner report)."""
    ft = get_file_type(name)
    if ft != 'Other' or not data:
        return ft
    detected = header_scan.classify_magic(data[:16])
    if detected is None and len(data) <= header_scan.TEXT_SIZE_LIMIT and header_scan.is_text(data):
        detected = 'Text'
    return detected or 'Other'


def unpacked_file_index(case_dir: str) -> dict:
    """{ui_path: (stored_path, entry_path, file_type)} for every file inside
    an already-unpacked nested archive, keyed by the same virtual ui_path the
    File Browser shows (f"{archive_ui_path}/{entry_path}", see
    ffs-explorer.py's _inject_nested_archives). For Process Case stages that
    run after unpacking and must type and READ those files — they don't exist
    in the main zip, so a main-zip read silently fails."""
    index = {}
    if not case_dir:
        return index
    out_dir = os.path.join(case_dir, 'nested_archives')
    try:
        with closing(_open_cache_db(case_dir)) as db:
            for rec in load_nested_archives(db):
                stored = os.path.join(out_dir, rec['stored_filename'])
                if not os.path.isfile(stored):
                    continue
                for e in load_nested_archive_entries(db, rec['ui_path']):
                    ep = e['entry_path']
                    if ep and not ep.endswith('/'):
                        index[f"{rec['ui_path']}/{ep}"] = (stored, ep, e.get('file_type') or 'Other')
    except Exception:
        return {}
    return index


class _SizeCrc:
    """The two ZipInfo fields duplicate_scan groups on, for an unpacked file
    stored as a plain blob (a decompressed gzip) rather than inside a zip."""
    __slots__ = ('file_size', 'CRC')

    def __init__(self, file_size: int, crc: int):
        self.file_size = file_size
        self.CRC = crc


def unpacked_duplicate_candidates(case_dir: str) -> list:
    """duplicate_scan candidates for every file unpacked from a nested archive
    (owner request, 2026-10-22), in the same shape as the main archive's
    (ui_path, physical, info, inode, devid, sha256, reader) so both are grouped
    together: a photo inside an unpacked zip that equals one in DCIM lands in
    the same group. Size and CRC come free from the repacked zip's own
    directory; a gzip's blob has none, so its CRC is computed once here. The
    reader reads the bytes from the unpacked archive (zip_reader)."""
    import zlib
    from zip_reader import read_nested_entry
    by_archive: dict = {}
    for vpath, (stored, entry, _ft) in unpacked_file_index(case_dir).items():
        by_archive.setdefault(stored, []).append((vpath, entry))
    out = []
    for stored, members in by_archive.items():
        try:
            with zipfile.ZipFile(stored) as zf:
                infos = {i.filename: i for i in zf.infolist()}
        except zipfile.BadZipFile:
            infos = None
        except OSError:
            continue
        for vpath, entry in members:
            if infos is not None:
                info = infos.get(entry)
                if info is None:
                    continue
            else:
                try:
                    with open(stored, 'rb') as f:
                        blob = f.read()
                except OSError:
                    continue
                info = _SizeCrc(len(blob), zlib.crc32(blob))
            out.append((vpath, f"{stored}::{entry}", info, 0, 0, '',
                        lambda s=stored, e=entry: read_nested_entry(s, e)))
    return out


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
        # Read via the local .zcd central-directory cache only — never a
        # raw zipfile.ZipFile on the main archive (this project's own
        # standing Convention has no exception for this case). The main
        # FFS archive itself is never compressed, so this is a direct
        # offset seek rather than a second full central-directory read
        # over what can be a large network-hosted zip. case_dir is
        # already a required parameter here (out_dir is built from it
        # unconditionally above), so .zcd is already guaranteed present
        # by the time this ever runs — raises honestly if it somehow
        # isn't, caught by this function's own surrounding try/except
        # (returns a real error_message rather than crashing silently).
        infos = _zcd_load(zip_path, case_dir)
        if infos is None:
            raise RuntimeError("Local .zcd cache not available")
        with CachedZipView(zip_path, infos) as zf:
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
            ft = classify_unpacked(child_name, decompressed, get_file_type)
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
                        ft = classify_unpacked(name.rsplit('/', 1)[-1], data, get_file_type)
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
