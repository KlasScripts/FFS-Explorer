"""How many files each header-scan tier reads (_header_tier_counts),
saved per case in casecache.db (_header_tier_counts_cached). Moved out of
ffs-explorer.py (2026-11-22): the first-open parse process counts once and
saves, so case open only reads the result; counted in the window's process
it was ~27 s of "Preparing case" on 2.19 million entries. Qt-free."""
import hashlib
import json
import os
import time
from contextlib import closing

from db_utils import _open_cache_db, load_blob, save_blob
from file_types import _get_file_type


def _header_candidate_matches(ui_path: str, scan_folders: tuple, tier: int,
                               excludes: tuple = ()) -> bool:
    """Does *ui_path* need a header read under *tier*? Shared predicate for
    both the counting and collecting passes, so the three tiers can never
    silently drift apart between "how many will this scan" (dialog display)
    and "which ones actually get read" (the real scan) — see TODO.md item 20
    for the full three-tier design this implements:
      1 — unknown-extension files only, within scan_folders() (today's
          original, only behavior; free extension check first, byte read
          only for the genuinely ambiguous case)
      2 — every file, within scan_folders() (also catches a DELIBERATELY
          mislabeled extension, still cost-bounded to app/user-writable
          areas — never re-reads the huge OS/system trees outside them)
      3 — every file, everywhere (no folder restriction — the exhaustive
          backstop for "the curated scan_folders() list itself might be
          wrong or incomplete for this case," not a routine default)

    *excludes* — see FfsAdapter.scan_excludes() (e.g. mobile/Library/
    Caches/ minus its own confirmed-noise subfolders) — tiers 1/2 only,
    same as scan_folders() itself; tier 3 is already unrestricted and
    ignores it.
    """
    if tier <= 0:
        return False
    if tier >= 3:
        return True
    if not ui_path.startswith(scan_folders):
        return False
    if excludes and ui_path.startswith(excludes):
        return False
    if tier == 2:
        return True
    return _get_file_type(ui_path.rsplit('/', 1)[-1]) == 'Other'


def _header_tier_counts(items, ffs_adapter, z, with_tier3: bool = False,
                        pause: bool = True) -> dict | None:
    """How many files each header-scan tier reads, in one pass:
    {'files': {1: n, 2: n[, 3: n]}, 'legacy': {1: n, 2: n, 3: n}}.

    files[3] only *with_tier3*: it needs a zip lookup for every entry (on
    IOS17 2.9 s against 0.75 s for tiers 1-2, and the GUI thread's Python
    ran ~3.7x slower beside it), and Tier 3 is rarely used, so it is
    counted only when picked or when the case already completed it.

    'files' is exactly what HeaderScanWorker reads: a key whose physical
    name is a file in the main zip (z.getinfo, the same lookup
    _resolve_file_candidates does). Folders, metadata-only entries and
    files unpacked from nested archives are left out; the last are typed
    when unpacked (ASSUMPTIONS.md A-30). 'legacy' is what
    _count_header_candidates gives (every matching key, folders included),
    the meaning header_scan_candidate_count was stored with before
    2026-11-01, so an older case can still be checked against it.

    *items* is a list of (ui_path, meta); the caller copies it so the GUI
    can keep changing ui_metadata. Built once per case on a background
    thread (FastZipBrowser._start_header_tier_count): on IOS17 the dialog
    used to count on the GUI thread at every open, 0.3 s idle but 4.5 s
    with one busy Python thread alongside (GIL). The loop pauses every
    2,000 entries so the GUI thread gets the GIL. None if *z* is None."""
    if z is None:
        return None
    scan_folders = tuple(ffs_adapter.scan_folders())
    excludes = tuple(ffs_adapter.scan_excludes())
    resolve, getinfo = ffs_adapter.resolve, z.getinfo
    has_file = getattr(z, 'has_file', None)   # CachedZipView: no entry made per path
    files = {1: 0, 2: 0, 3: 0} if with_tier3 else {1: 0, 2: 0}
    legacy = {1: 0, 2: 0, 3: len(items)}
    for n, (ui_path, _meta) in enumerate(items):
        if pause and n % 2000 == 1999:
            time.sleep(0.001)   # a real pause: sleep(0) left the GUI waiting
        in_scope = _header_candidate_matches(ui_path, scan_folders, 2, excludes)
        if not in_scope and not with_tier3:
            continue
        unknown = in_scope and _header_candidate_matches(ui_path, scan_folders, 1, excludes)
        legacy[2] += in_scope
        legacy[1] += unknown
        if has_file is not None:
            if not has_file(resolve(ui_path)):
                continue
        else:
            try:
                info = getinfo(resolve(ui_path))
            except KeyError:
                continue
            if info.is_dir():
                continue
        if with_tier3:
            files[3] += 1
        files[2] += in_scope
        files[1] += unknown
    return {'files': files, 'legacy': legacy}


_TIER_COUNTS_KEY = 'header_tier_counts'
_TIER_COUNTS_VERSION = '1'   # bump when _header_tier_counts or _header_candidate_matches change


def _add_tier_counts(a: dict, b: dict) -> dict:
    """Two _header_tier_counts results for disjoint sets of paths, added:
    every figure in them is a per-path count (legacy[3] is len(items))."""
    return {part: {t: a[part][t] + b[part].get(t, 0) for t in a[part]} for part in ('files', 'legacy')}


def _header_tier_counts_cached(ui_metadata, ffs_adapter, z, with_tier3: bool,
                               case_dir: str | None, pause: bool = True) -> dict | None:
    """_header_tier_counts for the whole file list, with the part that comes
    from the extraction's own files (the file table, which never changes for
    a case) saved in casecache.db and reused at every later open: 3-9 s of
    the synthetic iPhone's open, every time (2026-11-18). Files added while
    the case is open (unpacked from nested archives) are counted fresh and
    added. Same result as counting everything (checked on IOS17, Magnet and
    the synthetic iPhone). A plain dict file list (no file table) is counted
    as before."""
    import file_table as _ft
    if z is None:
        return None
    if not isinstance(ui_metadata, _ft.FileList) or ui_metadata.gone or not case_dir:
        return _header_tier_counts([(p, None) for p in ui_metadata], ffs_adapter, z,
                                   with_tier3=with_tier3)
    table = ui_metadata.table
    index = getattr(getattr(z, '_by_name', None), '_index', None)
    ident = hashlib.sha1(repr((
        os.path.basename(table.path), table.n_files, getattr(index, 'cd_sha256', None),
        list(ffs_adapter.scan_folders()), list(ffs_adapter.scan_excludes()),
        ffs_adapter.format, ffs_adapter.user_prefix, ffs_adapter.sys_prefix,
        ffs_adapter.old_layout, bool(with_tier3))).encode()).hexdigest()
    version = f'{_TIER_COUNTS_VERSION}|{ident}'
    stored = None
    try:
        with closing(_open_cache_db(case_dir)) as db:
            raw = load_blob(db, _TIER_COUNTS_KEY + ('3' if with_tier3 else ''), version)
        if raw is not None:
            d = json.loads(raw)
            stored = {part: {int(t): n for t, n in d[part].items()} for part in ('files', 'legacy')}
    except Exception:
        stored = None
    if stored is None:
        stored = _header_tier_counts([(p, None) for p, _i in ui_metadata._live()],
                                     ffs_adapter, z, with_tier3=with_tier3, pause=pause)
        if stored is None:
            return None
        try:
            with closing(_open_cache_db(case_dir)) as db:
                save_blob(db, _TIER_COUNTS_KEY + ('3' if with_tier3 else ''), version,
                          json.dumps(stored).encode())
        except Exception:
            pass
    added = [(p, None) for p in list(ui_metadata.top) if p in ui_metadata._added]
    if not added:
        return stored
    return _add_tier_counts(stored, _header_tier_counts(added, ffs_adapter, z, with_tier3=with_tier3))
