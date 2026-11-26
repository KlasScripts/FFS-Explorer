"""ffs_metadata.py — archive metadata parsing, free of any Qt dependency.

The heavy first-open work (central-directory parse, ui_metadata build, folder
tree, folder sizes, metadata-only scan) lives here so it can run either:

  • in the GUI's background QThread (the in-process fallback), or
  • in a separate *process* via parse_archive_metadata / _parse_child — the
    common path.  A child process gets its own Python interpreter and GIL, so
    the CPU-bound parse runs on a real core instead of starving the main
    thread.  That is what keeps the UI responsive during first open of a large
    FFS, which a QThread cannot do for pure-Python CPU work (one shared GIL).

The pure folder helpers (_build_folder_tree, _compute_folder_sizes, …) also
live here; ffs-explorer.py imports them back so existing call sites are
unchanged.  This module intentionally imports nothing from the Qt GUI module
so a spawned child never has to drag Qt parsing logic along the critical path.
"""

import os
import re
import time
import zipfile
from collections import defaultdict
from contextlib import closing
from itertools import islice

import msgpack

from adapters import FfsAdapter
from db_utils import (_open_cache_db, load_guid_bundle_map, load_folder_data,
                      save_blob, save_guid_bundle_map, save_folder_sizes,
                      save_app_registry, open_blob)
from zip_cd_cache import (CachedZipView, load as _cd_cache_load,
                          probe_delta as _probe_delta)

# Snapshot identity — must stay identical to what the GUI reads back, so it is
# defined here and imported into ffs-explorer.py (single source of truth).
# v2: the two big dicts (ui_metadata, folder_map) are stored as a stream of
# chunks so the GUI can deserialize them incrementally and yield the GIL between
# chunks (a single 500 MB+ unpackb held the GIL ~1-2 s and froze the UI).
SNAPSHOT_KEY     = 'load_snapshot'
SNAPSHOT_VERSION = '6'   # 3: earlier captures of repeated paths get their own entry (A-72)
                         # 4: the iOS system partition is not listed (A-82)
                         # 5: a GrayKey entry without a UT block gets no time, never
                         #    its DOS time read as UTC (A-10)
                         # 6: ui_metadata and folder_map are in a file table beside
                         #    casecache.db (app/file_table.py), not in the blob

# Items per snapshot chunk — small enough that one chunk's unpack is well under a
# frame, so yielding the GIL between chunks keeps the UI animating.
_SNAP_CHUNK = 40_000

# When these passes run in the in-process QThread fallback (header-scan
# runs, subprocess unavailable), pure-Python loops starve the GUI
# thread of the GIL.  time.sleep(0) forces a GIL handoff; every ~4k iterations
# keeps stalls well under a frame while adding negligible overhead — and it is
# a no-op cost in the subprocess path.
_YIELD_EVERY = 4096


def _chunked(d: dict, n: int):
    """Yield successive dicts of at most n items from d."""
    it = iter(d.items())
    while True:
        block = dict(islice(it, n))
        if not block:
            return
        yield block


def _empty_folder_entries(adapter_args, zip_names, ui_metadata, folder_map) -> list:
    """The paths the archive stores as a bare folder entry (name ending in
    "/") that have nothing under them: what FastZipBrowser's empty-folder
    scan used to work out at every open (Cellebrite gives such a folder its
    directory size, so its size cannot tell it from a file). Fixed by the
    extraction, so worked out once here and kept in the snapshot."""
    resolve = FfsAdapter(adapter_args[0], adapter_args[1], adapter_args[2],
                         bool(adapter_args[3])).resolve
    names = zip_names if isinstance(zip_names, (set, frozenset)) else set(zip_names)
    folders = set(folder_map)
    out = []
    for n, p in enumerate(ui_metadata):
        if n % _YIELD_EVERY == 0:
            time.sleep(0)
        if p not in folders and (resolve(p) + '/') in names:
            out.append(p)
    return out


def _zip_names_index(zip_names, zip_path, case_dir, made_from=None):
    """{'file', 'cd_sha256', 'n'} naming the case's zip index (.ffsidx) when
    its set of names is exactly *zip_names*, so the snapshot need not hold
    the names (0.54 GB as a set, most of the blob, synthetic iPhone); else
    None and they are stored as before."""
    if not (zip_path and case_dir):
        return None
    try:
        import zip_index
        path = zip_index.index_path(zip_path, case_dir)
        idx = zip_index.ZipIndex(path)
        try:
            if (made_from is not None and made_from.cd_sha256 == idx.cd_sha256
                    and len(made_from) == len(idx)):
                # *zip_names* was made from this same index (parse stage 4,
                # _sizes_and_stored): equal by construction. Comparing them
                # took 7.2 s on 2.5 million names (2026-11-22).
                return {'file': os.path.basename(path), 'cd_sha256': idx.cd_sha256,
                        'n': len(zip_names)}
            names = set(idx.all_names())      # one decode of the pool, not one per name
            want = zip_names if isinstance(zip_names, (set, frozenset)) else set(zip_names)
            if names != want:
                return None
            return {'file': os.path.basename(path), 'cd_sha256': idx.cd_sha256, 'n': len(names)}
        finally:
            idx.close()
    except Exception:
        return None


def _names_from_index(info: dict, case_dir: str, yield_cb=None) -> list:
    """The zip names the snapshot left to the index (_zip_names_index):
    each distinct name once. ValueError if the index is not the one the
    snapshot named."""
    import zip_index
    idx = zip_index.ZipIndex(os.path.join(case_dir, os.path.basename(info['file'])))
    try:
        if idx.cd_sha256 != info['cd_sha256']:
            raise ValueError('the zip index is not the one this snapshot was made with')
        # each distinct name once, in index order: one decode of the name
        # pool (ZipIndex.all_names) instead of one per name
        names = idx.all_names()
        if yield_cb:
            yield_cb()
        out = list(dict.fromkeys(names))
        del names
        if yield_cb:
            yield_cb()
        if len(out) != info['n']:
            raise ValueError('the zip index does not match the snapshot')
        return out
    finally:
        idx.close()


def _leveldb_dirs(folder_map) -> list:
    """The parsed folders that look like LevelDB/IndexedDB folders
    (leveldb_viewer._looks_like_leveldb_dir), in folder_map order: fixed
    by the extraction, so worked out once here. The GUI checks only the
    folders added or replaced since (FastZipBrowser._leveldb_dirs_now); a
    pass over every folder's children on the GUI thread froze the window
    0.8 s at Process Case on IOS17 (2026-11-18)."""
    from leveldb_viewer import _looks_like_leveldb_dir
    out = []
    for n, p in enumerate(folder_map):
        if n % _YIELD_EVERY == 0:
            time.sleep(0)
        if _looks_like_leveldb_dir(folder_map, p):
            out.append(p)
    return out


def pack_snapshot(adapter_args, delta, zip_names, zip_ui_paths,
                  ui_metadata, folder_map, metadata_only, file_table_dir=None,
                  zip_path=None, names_from_index=None) -> bytes:
    """Pack a load snapshot as a header object followed by chunked ui_metadata
    and folder_map objects (see SNAPSHOT_VERSION).  Concatenated msgpack
    objects, read back by unpack_snapshot.

    With *file_table_dir* (the case folder) ui_metadata and folder_map are
    written to a file table there instead (app/file_table.py) and the header
    names it; if they would not read back exactly from one (file_table.write
    refuses), they go in the blob as before."""
    table_name = None
    ui_in_table = False
    if file_table_dir:
        import file_table
        path = file_table.new_path(file_table_dir)
        try:
            try:
                # zip_ui_paths as a bit per file (file_table.ZipPaths)
                file_table.write(ui_metadata, folder_map, path, in_zip=zip_ui_paths)
                ui_in_table = zip_ui_paths is not None
            except ValueError:
                file_table.write(ui_metadata, folder_map, path)
            table_name = os.path.basename(path)
            file_table.remove_others(file_table_dir, keep=path)
        except ValueError:
            table_name = None
            ui_in_table = False
    names_index = (_zip_names_index(zip_names, zip_path, file_table_dir, names_from_index)
                   if table_name else None)
    if table_name is None:
        ui_parts = [msgpack.packb(c, use_bin_type=True)
                    for c in _chunked(ui_metadata, _SNAP_CHUNK)]
        fm_parts = [msgpack.packb(c, use_bin_type=True)
                    for c in _chunked(folder_map, _SNAP_CHUNK)]
    else:
        ui_parts, fm_parts = [], []
    header = msgpack.packb({
        'empty_folders': _empty_folder_entries(adapter_args, zip_names, ui_metadata, folder_map),
        'leveldb_dirs':  _leveldb_dirs(folder_map),
        'adapter':       adapter_args,
        'delta':         delta,
        'zip_names':     None if names_index else list(zip_names),
        'zip_names_index': names_index,
        'zip_ui_paths':  (list(zip_ui_paths) if zip_ui_paths is not None and not ui_in_table else None),
        'zip_ui_in_table': ui_in_table,
        'metadata_only': list(metadata_only),
        'n_ui':          len(ui_parts),
        'n_fm':          len(fm_parts),
        'file_table':    table_name,
        'n_files':       len(ui_metadata),
        'n_folders':     len(folder_map),
    }, use_bin_type=True)
    return b''.join([header] + ui_parts + fm_parts)


_STREAM_YIELD = 20_000   # list items read between yield_cb() calls


def _stream_value(unp, yield_cb):
    """The next msgpack value from *unp*; a list is read item by item,
    calling yield_cb() every _STREAM_YIELD items. One unpack of the
    snapshot header's lists (IOS17: 830k zip names, 474k ui_paths) held the
    GIL 0.55 s and froze the window (2026-11-03); same format, read in steps."""
    try:
        n = unp.read_array_header()
    except ValueError:              # not a list: left unread, read whole
        return unp.unpack()
    out = []
    append, unpack = out.append, unp.unpack
    for i in range(n):
        append(unpack())
        if yield_cb and i % _STREAM_YIELD == _STREAM_YIELD - 1:
            yield_cb()
    return out


def frozenset_in_steps(items: list, yield_cb=None) -> frozenset:
    """frozenset(items), built _STREAM_YIELD items at a time with
    yield_cb() between: one frozenset() of IOS17's 830k zip names held the
    GIL 0.1 s; the final copy of the built set takes ~0.02 s."""
    s: set = set()
    for i in range(0, len(items), _STREAM_YIELD):
        s.update(items[i:i + _STREAM_YIELD])
        if yield_cb:
            yield_cb()
    return frozenset(s)


def _stream_map(unp, yield_cb):
    """The next msgpack map, its list values read by _stream_value."""
    n = unp.read_map_header()
    out = {}
    for _ in range(n):
        key = unp.unpack()
        out[key] = _stream_value(unp, yield_cb)
    return out


def unpack_snapshot(raw, yield_cb=None, case_dir=None) -> dict:
    """Inverse of pack_snapshot.  Calls yield_cb() (if given) after each chunk
    so a caller on a worker thread can release the GIL and keep the GUI alive.

    *raw* is either the packed bytes or a readable file-like (e.g. a sqlite3
    Blob from db_utils.open_blob) — the file-like form streams the snapshot
    into the unpacker instead of holding a second full copy in memory."""
    if isinstance(raw, (bytes, bytearray, memoryview)):
        unp = msgpack.Unpacker(raw=False, strict_map_key=False, max_buffer_size=0)
        unp.feed(raw)
    else:
        unp = msgpack.Unpacker(raw, raw=False, strict_map_key=False,
                               max_buffer_size=0)
    header = _stream_map(unp, yield_cb)
    if header.get('file_table'):
        # ui_metadata / folder_map are in the case's file table: mapped, not
        # unpacked. Missing, unreadable or not the one this snapshot wrote
        # (other counts) -> an exception, and the caller parses again.
        import file_table
        if not case_dir:
            raise ValueError('snapshot names a file table but no case folder was given')
        name = os.path.basename(header['file_table'])
        ui_metadata, folder_map = file_table.open_maps(os.path.join(case_dir, name))
        if len(ui_metadata) != header['n_files'] or len(folder_map) != header['n_folders']:
            raise ValueError('the file table does not match the snapshot')
        zip_names = header['zip_names']
        if zip_names is None and header.get('zip_names_index'):
            zip_names = _names_from_index(header['zip_names_index'], case_dir, yield_cb)
        zip_ui_paths = header['zip_ui_paths']
        if header.get('zip_ui_in_table'):
            if not ui_metadata.table.has_in_zip:
                raise ValueError('the file table has no in-zip bits')
            zip_ui_paths = file_table.ZipPaths(ui_metadata.table)
        return {
            'empty_folders': header.get('empty_folders'),
            'leveldb_dirs':  header.get('leveldb_dirs'),
            'adapter':       header['adapter'],
            'delta':         header['delta'],
            'zip_names':     zip_names,
            'zip_ui_paths':  zip_ui_paths,
            'metadata_only': header['metadata_only'],
            'ui_metadata':   ui_metadata,
            'folder_map':    folder_map,
        }
    ui_metadata: dict = {}
    for _ in range(header['n_ui']):
        ui_metadata.update(next(unp))
        if yield_cb:
            yield_cb()
    folder_map: dict = {}
    for _ in range(header['n_fm']):
        folder_map.update(next(unp))
        if yield_cb:
            yield_cb()
    return {
        'empty_folders': header.get('empty_folders'),
        'adapter':       header['adapter'],
        'delta':         header['delta'],
        'zip_names':     header['zip_names'],
        'zip_ui_paths':  header['zip_ui_paths'],
        'metadata_only': header['metadata_only'],
        'ui_metadata':   ui_metadata,
        'folder_map':    folder_map,
    }


def load_snapshot_from_case(case_dir: str) -> dict | None:
    """Load the persisted load-snapshot straight from case_dir, with no
    live GUI/window object involved — added 2026-09-15 for
    `artifact_runner.py`'s `device_wide` parser capability (see its own
    docstring), which needs `ui_metadata`/`folder_map` for a parser like
    `artifacts/ios/app_report.py` that scans every app on the device
    rather than one app's own `files`. Reuses the exact same read this
    module's own snapshot fast-path already does for an instant re-open
    (ffs-explorer.py's `_try_load_from_snapshot`) — this is the SAME
    persisted data, just reachable from a Qt-free context (a parser
    script must stay headless/testable like every other one, never reach
    into the live window object). Returns None if no snapshot exists yet
    (the case was never fully opened) — the caller decides how to report
    that; this function never raises for a missing/corrupt snapshot,
    matching every other cache-read in this project's own established
    style."""
    try:
        with closing(_open_cache_db(case_dir)) as db:
            blob = open_blob(db, SNAPSHOT_KEY, SNAPSHOT_VERSION)
            if blob is None:
                return None
            with closing(blob):
                return unpack_snapshot(blob, case_dir=case_dir)
    except Exception:
        return None


# iOS container metadata files: a folder that contains only these (and nothing
# else) is labelled "Folder - Metadata Only" rather than a regular folder.
_SYSTEM_METADATA_NAMES: frozenset = frozenset({
    ".com.apple.mobile_container_manager.metadata.plist",
    ".com.apple.FairPlay.MachineIdentifier",
    ".com.apple.springboard.shortcuts",
})

_UUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
)


# ── Pure folder helpers (moved from ffs-explorer.py) ────────────────────────────

def _compute_folder_sizes(
    folder_map: dict,
    zip_names: frozenset,
    zip_sizes: dict,
    ffs_adapter,
    zip_ui_paths: frozenset | None = None,
    progress_cb=None,
) -> dict:
    """Compute total bytes per folder using a bottom-up (leaf-first) algorithm.

    Returns {folder_path: total_bytes} for every folder except the root ('').

    Step 1 accumulates direct file sizes into each immediate parent.
    Step 2 propagates child totals upward sorted deepest-first so every
    subtotal is resolved before its parent is visited.  The root ('') is
    intentionally excluded — it would just be the sum of every file and
    provides no actionable insight in the UI.
    """
    folder_bytes: dict[str, int] = {fp: 0 for fp in folder_map}
    total = len(folder_map)
    done = [0]
    seen = 0
    _REPORT_EVERY = 2000

    # Step 1: direct file contributions into each immediate parent.
    # With no size data every child would be skipped anyway — don't pay
    # n resolve() calls to find that out.
    if zip_sizes:
        for fp, children in folder_map.items():
            for child in children:
                if child in folder_map:
                    continue  # subfolder — handled in step 2
                if zip_ui_paths is not None:
                    if child not in zip_ui_paths:
                        continue
                    raw = ffs_adapter.resolve(child)
                else:
                    raw = ffs_adapter.resolve(child)
                    if raw not in zip_names and raw.lstrip('/') not in zip_names:
                        continue
                # Bare directory entries are keyed with a trailing slash in zip_names.
                if (raw + '/') in zip_names or (raw.lstrip('/') + '/') in zip_names:
                    continue
                size = zip_sizes.get(raw, zip_sizes.get(raw.lstrip('/'), 0))
                folder_bytes[fp] += size
                seen += 1
                if seen % _YIELD_EVERY == 0:
                    time.sleep(0)  # a single folder can hold 10k+ children

            done[0] += 1
            if progress_cb and done[0] % _REPORT_EVERY == 0:
                progress_cb(done[0], total)
                time.sleep(0)

    # Step 2: propagate child totals up to parents, deepest folders first.
    for fp in sorted(folder_bytes, key=lambda p: p.count('/'), reverse=True):
        if not fp:
            continue  # never propagate from root
        parent = fp.rsplit('/', 1)[0] if '/' in fp else ''
        if parent and parent in folder_bytes:
            folder_bytes[parent] += folder_bytes[fp]

    folder_bytes.pop('', None)  # remove root
    if progress_cb:
        progress_cb(total, total)
    return folder_bytes


def _find_metadata_only_folders(
    folder_map: dict,
    zip_names: frozenset,
    zip_ui_paths: frozenset | None,
    ffs_adapter,
) -> set:
    """Return the set of iOS folder paths whose only in-zip content is Apple container metadata.

    A folder qualifies if every file it (recursively) contains is in
    _SYSTEM_METADATA_NAMES and at least one such file is present.  Truly
    empty subfolders are ignored — they do not disqualify the parent.
    Only call this for iOS archives (Graykey / Cellebrite FFS), not Android.
    """
    cache: dict[str, str] = {}  # fp -> "metadata_only" | "empty" | "content"
    seen = [0]  # children examined — drives periodic GIL yields

    def _check(fp: str) -> str:
        if fp in cache:
            return cache[fp]
        cache[fp] = "empty"  # cycle guard
        has_metadata = False
        for child in folder_map.get(fp, []):
            seen[0] += 1
            if seen[0] % _YIELD_EVERY == 0:
                time.sleep(0)  # yield GIL — this walk previously never did
            if child in folder_map:
                cs = _check(child)
                if cs == "content":
                    cache[fp] = "content"
                    return "content"
                if cs == "metadata_only":
                    has_metadata = True
                # "empty" subfolder — does not disqualify
            else:
                if zip_ui_paths is not None:
                    if child not in zip_ui_paths:
                        continue
                    raw = ffs_adapter.resolve(child)
                else:
                    raw = ffs_adapter.resolve(child)
                    if raw not in zip_names and raw.lstrip('/') not in zip_names:
                        continue
                if (raw + '/') in zip_names or (raw.lstrip('/') + '/') in zip_names:
                    continue  # bare directory entry — skip
                if child.split('/')[-1] in _SYSTEM_METADATA_NAMES:
                    has_metadata = True
                else:
                    cache[fp] = "content"
                    return "content"
        result = "metadata_only" if has_metadata else "empty"
        cache[fp] = result
        return result

    for fp in folder_map:
        if fp:  # skip root
            _check(fp)

    return {fp for fp, v in cache.items() if v == "metadata_only"}


def _find_missing_plists(folder_map: dict, ffs_adapter, guid_to_bundle: dict) -> list:
    """Return UUID container folders whose MCM metadata plist is unresolved."""
    parents = ffs_adapter.container_parents(folder_map)
    return [
        p for p in folder_map
        if p.rsplit('/', 1)[0] in parents
        and _UUID_RE.match(p.split('/')[-1])
        and p.split('/')[-1] not in guid_to_bundle
    ]


_MCM_PLIST = ".com.apple.mobile_container_manager.metadata.plist"


def _build_folder_tree(ui_metadata: dict, status_cb=None) -> dict:
    """Build and return folder_map from ui_metadata paths."""
    if status_cb:
        status_cb(f"Building folder tree ({len(ui_metadata):,} entries)...")
    # Each folder's children go straight into a list; one set says which
    # paths are already in their parent's list (every file-list key is,
    # after the first pass). The same children as the earlier version
    # (a set per folder, then lists): ~3x faster on 2.19 million entries
    # (17 s of the synthetic iPhone's parse, 2026-11-22). Children are in
    # file-list order then the order folders were linked; a set's order was
    # arbitrary (it changed with every process), so nothing relies on it.
    folder_map: dict[str, list] = {}
    get = folder_map.get
    for i, ui_path in enumerate(ui_metadata):
        if not ui_path:
            continue  # skip empty-string keys from malformed archives
        k = ui_path.rfind('/')
        parent = ui_path[:k] if k >= 0 else ""
        kids = get(parent)
        if kids is None:
            folder_map[parent] = [ui_path]
        else:
            kids.append(ui_path)
        if not i & 0x3FFFF:
            time.sleep(0)   # yield GIL (the in-process fallback runs beside the GUI)
    if status_cb:
        status_cb("Resolving folder hierarchy...")
    linked = set(ui_metadata)
    for i, path in enumerate(list(folder_map)):
        current = path
        while current and current not in linked:
            k = current.rfind('/')
            parent = current[:k] if k >= 0 else ""
            kids = get(parent)
            if kids is None:
                folder_map[parent] = [current]
            else:
                kids.append(current)
            linked.add(current)
            current = parent
        if not i & 0x3FFFF:
            time.sleep(0)
    return folder_map


# ── Full archive parse (used by subprocess and in-process fallback) ──────────────

def _save_header_tier_counts(zip_path: str, case_dir: str, ffs_adapter, keys=None) -> None:
    """Count the header-scan tiers here, in the parse process, and save them
    in casecache.db (header_counts._header_tier_counts_cached), against the
    file table just written and the case's zip index: the window's own
    count at case open then finds them saved under the same key. Counted in
    the window's process it was ~27 s of "Preparing case" on 2.19 million
    entries, beside the window (2026-11-22). A failure only means the
    window counts as before."""
    import glob
    try:
        import file_table
        import header_counts
        tables = glob.glob(os.path.join(case_dir, 'files-*.ffsfiles'))
        if len(tables) != 1:
            return
        infos = _cd_cache_load(zip_path, case_dir)
        if infos is None:
            return
        ui_list, _fm = file_table.open_maps(tables[0])
        try:
            # *keys*: the table's own names, in its order (they were just
            # written from them), so they are not read back from it
            header_counts._header_tier_counts_cached(
                ui_list, ffs_adapter, CachedZipView(zip_path, infos), False, case_dir,
                pause=False, keys=keys)
        finally:
            ui_list.table.close()
    except Exception as e:
        import startup_log
        startup_log.message('warning', f"header-scan counts not saved at parse "
                            f"({type(e).__name__}: {e}); the window counts them instead")


def _sizes_and_stored(infos) -> tuple[dict, list]:
    """({name: uncompressed size} for every entry, in archive order, and the
    stored (uncompressed) entries with data, for _probe_delta). From the
    case's mapped index the names are cut from its name pool and the sizes
    and methods read from its columns, and only the stored entries are
    made: making all 2.5 million entries for this took ~5.6 s of a first
    open (2026-11-22). Otherwise from the entries themselves. The same
    result either way."""
    index = getattr(infos, '_index', None)
    if index is None or not hasattr(index, 'name_start'):
        sizes = {i.filename: i.file_size for i in infos}
        return sizes, [i for i in infos if i.compress_type == zipfile.ZIP_STORED and i.file_size > 0]
    names = index.all_names()
    sizes = dict(zip(names, index.file_size))
    stored = [infos[k] for k, (method, size) in enumerate(zip(index.compress_type, index.file_size))
              if method == zipfile.ZIP_STORED and size > 0]
    return sizes, stored


def parse_archive_metadata(zip_path: str, case_dir: str, status_cb=None) -> dict:
    """Parse an FFS archive from its local .zcd cache.

    Returns a result dict with the live objects plus the packed snapshot:
      adapter_args, delta, zip_names, zip_ui_paths, ui_metadata, folder_map,
      metadata_only, guid_to_bundle, folder_sizes, snapshot_blob,
      need_save_guid, need_save_sizes.

    This is the heavy, CPU-bound work; run it in a child process to keep the
    GUI responsive.  Requires a valid local central-directory cache (.zcd) —
    raises if absent.  The header scan is not part of this parse (Process
    Case runs it).
    """
    def status(msg):
        if status_cb:
            status_cb(msg)

    cached_infos = _cd_cache_load(zip_path, case_dir)
    if not cached_infos:
        raise RuntimeError("central directory cache (.zcd) not available")

    status("Reading zip directory...")
    z_ctx = CachedZipView(zip_path, cached_infos)
    try:
        zip_sizes, stored = _sizes_and_stored(cached_infos)
        zip_names = frozenset(zip_sizes)
        # the index zip_names was made from, if it was (pack_snapshot skips
        # checking the two against each other)
        names_from_index = getattr(cached_infos, '_index', None)
        ffs_adapter = FfsAdapter.detect(z_ctx, zip_names)
        delta = _probe_delta(zip_path, stored)

        cached_guid_bundle = None
        try:
            with closing(_open_cache_db(case_dir)) as _db:
                _cached = load_guid_bundle_map(_db)
            if _cached:
                cached_guid_bundle = _cached
        except Exception:
            pass

        # Only Cellebrite iOS uses the Pass-1 zip-entry precompute.
        precomputed = None
        if ffs_adapter.format == FfsAdapter.FORMAT_CELLEBRITE:
            # ~500k-entry pass — without its own status it sits under a stale
            # "Reading zip directory..." message.
            status(f"Scanning user partition ({ffs_adapter.user_prefix})…")
            precomputed = ffs_adapter.build_zip_entries(zip_names)
        ui_metadata, guid_to_bundle, zip_ui_paths = ffs_adapter.build_ui_metadata(
            zip_path, zip_names,
            z=z_ctx,
            status_cb=status,
            guid_to_bundle=cached_guid_bundle,
            zip_entries=precomputed,
        )
    finally:
        z_ctx.__exit__(None, None, None)

    # LaunchServices csstore: a single central registry (bundle id, both
    # container GUIDs, App Group entitlements) independent of the
    # per-container metadata plists build_ui_metadata's guid_to_bundle
    # came from — confirmed on real casework to sometimes be missing on
    # GrayKey extractions. Reuses z_ctx (a CachedZipView) — its own
    # __exit__ is a no-op, so it's still perfectly usable here despite
    # the earlier `finally` block having "closed" it; passing it avoids
    # a raw zipfile.ZipFile fallback on the main archive (this project's
    # own standing Convention has no exception for this case). Its
    # guid_map takes priority on overlap (the richer, independently-
    # verified source — see CLAUDE.md); it never REMOVES an entry the
    # per-container plist method already resolved, only adds to or
    # confirms it.
    status("Building app registry from LaunchServices…")
    app_registry_rows, ls_guid_map = ffs_adapter.build_app_registry(zip_path, zip_names, z=z_ctx)
    if ls_guid_map:
        guid_to_bundle = {**guid_to_bundle, **ls_guid_map}

    folder_map = _build_folder_tree(ui_metadata, status_cb=status)

    cached_sizes = None
    try:
        with closing(_open_cache_db(case_dir)) as _db:
            _, _fs = load_folder_data(_db)
        if _fs:
            cached_sizes = _fs
    except Exception:
        pass

    if cached_sizes is not None:
        status("Folder sizes loaded from cache.")
        folder_sizes = cached_sizes
    else:
        status("Computing folder sizes...")
        folder_sizes = _compute_folder_sizes(
            folder_map, zip_names, zip_sizes, ffs_adapter,
            zip_ui_paths=zip_ui_paths,
            progress_cb=lambda done, total: status(
                f"Computing folder sizes… {done:,}/{total:,}"))

    is_ios = (
        ffs_adapter.format == FfsAdapter.FORMAT_CELLEBRITE
        or (ffs_adapter.format == FfsAdapter.FORMAT_GRAYKEY
            and 'private/var' in folder_map)
    )
    if is_ios:
        status("Identifying metadata-only folders...")
        metadata_only = _find_metadata_only_folders(
            folder_map, zip_names, zip_ui_paths, ffs_adapter)
    else:
        metadata_only = set()

    # About 75 s for 2.19 million entries (2026-11-21), with no figure to
    # show: say what it is for, so a long wait is not read as a hang.
    status(f"Saving the list of {len(ui_metadata):,} files to the case folder, so the next "
           "open takes seconds (on a big extraction this takes a few minutes)…")
    adapter_args = [ffs_adapter.format, ffs_adapter.user_prefix,
                    ffs_adapter.sys_prefix, ffs_adapter.old_layout]
    snapshot_blob = pack_snapshot(
        adapter_args, delta, zip_names, zip_ui_paths,
        ui_metadata, folder_map, metadata_only, file_table_dir=case_dir or None,
        zip_path=zip_path, names_from_index=names_from_index)
    if case_dir:
        status(f"Counting the files each header-scan tier would read "
               f"({len(ui_metadata):,} entries)…")
        _save_header_tier_counts(zip_path, case_dir, ffs_adapter, keys=list(ui_metadata))

    return {
        'adapter_args':   [ffs_adapter.format, ffs_adapter.user_prefix,
                           ffs_adapter.sys_prefix, ffs_adapter.old_layout],
        'delta':          delta,
        'zip_names':      zip_names,
        'zip_ui_paths':   zip_ui_paths,
        'ui_metadata':    ui_metadata,
        'folder_map':     folder_map,
        'metadata_only':  metadata_only,
        'guid_to_bundle': guid_to_bundle,
        'folder_sizes':   folder_sizes,
        'snapshot_blob':  snapshot_blob,
        'app_registry':   app_registry_rows,
        # Also true (not just "no cache existed") whenever the LaunchServices
        # merge resolved anything — otherwise an already-cached guid_bundle
        # would silently swallow the enrichment, since the ORIGINAL condition
        # alone only fires on a cold cache.
        'need_save_guid': (cached_guid_bundle is None and bool(guid_to_bundle))
                          or bool(ls_guid_map),
        'need_save_sizes': cached_sizes is None,
        'need_save_app_registry': bool(app_registry_rows),
    }


def persist_result(case_dir: str, result: dict) -> None:
    """Write a parse result's snapshot, guid map, folder sizes, and app
    registry into casecache.db, so the GUI can pick it up via the normal
    snapshot fast-path."""
    if not case_dir:
        return
    with closing(_open_cache_db(case_dir)) as db:
        if result.get('need_save_guid'):
            save_guid_bundle_map(db, result['guid_to_bundle'])
        if result.get('need_save_sizes'):
            save_folder_sizes(db, result['folder_sizes'])
        if result.get('need_save_app_registry'):
            save_app_registry(db, result['app_registry'])
        if result.get('snapshot_blob') is not None:
            save_blob(db, SNAPSHOT_KEY, SNAPSHOT_VERSION, result['snapshot_blob'])


def _parse_child(conn, zip_path: str, case_dir: str) -> None:
    """Child-process entry point.

    Parses the archive, persists the result to casecache.db, and reports only
    small control messages over the one-way Connection: ('status', msg)*, then
    ('done',) on success or ('error', traceback_str) on failure.  The large
    result is handed off via the case DB (the parent re-reads it through the
    snapshot fast-path) rather than piped — keeps IPC and peak memory small.
    Must be module-level so 'spawn' can pickle it.
    """
    import child_priority
    child_priority.lower_this_process()   # the window is served first
    # No garbage collection in this process: it holds millions of objects,
    # and each collection pass walked them all (the folder tree took 9.6 s
    # instead of 1.6 s on 2.19 million entries, 2026-11-22). Nothing here
    # makes cycles worth collecting, and the process ends when done.
    import gc
    gc.disable()
    try:
        def status(msg):
            try:
                conn.send(('status', msg))
            except Exception:
                pass
        result = parse_archive_metadata(zip_path, case_dir, status_cb=status)
        status("Writing case cache…")
        persist_result(case_dir, result)
        conn.send(('done',))
    except Exception:
        import traceback
        try:
            conn.send(('error', traceback.format_exc()))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    child_priority.end_now(0)   # everything is saved; skip freeing it all
