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

import re
import time
import zipfile
from collections import defaultdict
from contextlib import closing
from itertools import islice

import msgpack

from adapters import FfsAdapter
from db_utils import (_open_cache_db, load_guid_bundle_map, load_folder_data,
                      save_blob, save_guid_bundle_map, save_folder_sizes)
from zip_cd_cache import (CachedZipView, load as _cd_cache_load,
                          probe_delta as _probe_delta)

# Snapshot identity — must stay identical to what the GUI reads back, so it is
# defined here and imported into ffs-explorer.py (single source of truth).
# v2: the two big dicts (ui_metadata, folder_map) are stored as a stream of
# chunks so the GUI can deserialize them incrementally and yield the GIL between
# chunks (a single 500 MB+ unpackb held the GIL ~1-2 s and froze the UI).
SNAPSHOT_KEY     = 'load_snapshot'
SNAPSHOT_VERSION = '2'

# Items per snapshot chunk — small enough that one chunk's unpack is well under a
# frame, so yielding the GIL between chunks keeps the UI animating.
_SNAP_CHUNK = 40_000

# When these passes run in the in-process QThread fallback (streaming archives,
# header-scan runs, subprocess unavailable), pure-Python loops starve the GUI
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


def pack_snapshot(adapter_args, delta, zip_names, zip_ui_paths,
                  ui_metadata, folder_map, metadata_only) -> bytes:
    """Pack a load snapshot as a header object followed by chunked ui_metadata
    and folder_map objects (see SNAPSHOT_VERSION).  Concatenated msgpack
    objects, read back by unpack_snapshot."""
    ui_parts = [msgpack.packb(c, use_bin_type=True)
                for c in _chunked(ui_metadata, _SNAP_CHUNK)]
    fm_parts = [msgpack.packb(c, use_bin_type=True)
                for c in _chunked(folder_map, _SNAP_CHUNK)]
    header = msgpack.packb({
        'adapter':       adapter_args,
        'delta':         delta,
        'zip_names':     list(zip_names),
        'zip_ui_paths':  (list(zip_ui_paths) if zip_ui_paths is not None else None),
        'metadata_only': list(metadata_only),
        'n_ui':          len(ui_parts),
        'n_fm':          len(fm_parts),
    }, use_bin_type=True)
    return b''.join([header] + ui_parts + fm_parts)


def unpack_snapshot(raw: bytes, yield_cb=None) -> dict:
    """Inverse of pack_snapshot.  Calls yield_cb() (if given) after each chunk
    so a caller on a worker thread can release the GIL and keep the GUI alive."""
    unp = msgpack.Unpacker(raw=False, strict_map_key=False, max_buffer_size=0)
    unp.feed(raw)
    header = next(unp)
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
        'adapter':       header['adapter'],
        'delta':         header['delta'],
        'zip_names':     header['zip_names'],
        'zip_ui_paths':  header['zip_ui_paths'],
        'metadata_only': header['metadata_only'],
        'ui_metadata':   ui_metadata,
        'folder_map':    folder_map,
    }

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
    # With no size data (streaming archives) every child would be skipped
    # anyway — don't pay n resolve() calls to find that out.
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
    parents = ffs_adapter.container_parents()
    return [
        p for p in folder_map
        if p.rsplit('/', 1)[0] in parents
        and _UUID_RE.match(p.split('/')[-1])
        and p.split('/')[-1] not in guid_to_bundle
    ]


def _build_folder_tree(ui_metadata: dict, status_cb=None) -> dict:
    """Build and return folder_map from ui_metadata paths."""
    if status_cb:
        status_cb(f"Building folder tree ({len(ui_metadata):,} entries)...")
    # defaultdict avoids allocating a throwaway set() per iteration the way
    # setdefault(parent, set()) would — this loop runs once per archive entry.
    folder_map_sets: defaultdict[str, set] = defaultdict(set)
    for i, ui_path in enumerate(ui_metadata):
        if not ui_path:
            continue  # skip empty-string keys from malformed archives
        parent = ui_path.rsplit('/', 1)[0] if '/' in ui_path else ""
        folder_map_sets[parent].add(ui_path)
        if i % _YIELD_EVERY == 0:
            time.sleep(0)   # yield GIL so the main thread can process events
    if status_cb:
        status_cb("Resolving folder hierarchy...")
    for i, path in enumerate(list(folder_map_sets.keys())):
        current = path
        while current:
            parent = current.rsplit('/', 1)[0] if '/' in current else ""
            if parent not in folder_map_sets:
                folder_map_sets[parent] = {current}
            elif current not in folder_map_sets[parent]:
                folder_map_sets[parent].add(current)
            else:
                break
            current = parent
        if i % _YIELD_EVERY == 0:
            time.sleep(0)
    return {k: list(v) for k, v in folder_map_sets.items()}


# ── Full non-streaming parse (used by subprocess and in-process fallback) ────────

def parse_archive_metadata(zip_path: str, case_dir: str, status_cb=None) -> dict:
    """Parse a non-streaming FFS archive from its local .zcd cache.

    Returns a result dict with the live objects plus the packed snapshot:
      adapter_args, delta, zip_names, zip_ui_paths, ui_metadata, folder_map,
      metadata_only, guid_to_bundle, folder_sizes, snapshot_blob,
      need_save_guid, need_save_sizes.

    This is the heavy, CPU-bound work; run it in a child process to keep the
    GUI responsive.  Requires a valid local central-directory cache (.zcd) —
    raises if absent.  Streaming archives and header scans are NOT handled here
    (the GUI keeps those on its in-thread path).
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
        infolist  = z_ctx.infolist()
        zip_sizes = {info.filename: info.file_size for info in infolist}
        zip_names = frozenset(zip_sizes)
        ffs_adapter = FfsAdapter.detect(z_ctx, zip_names)
        stored = [i for i in infolist
                  if i.compress_type == zipfile.ZIP_STORED and i.file_size > 0]
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
            streaming_index=None,
            status_cb=status,
            guid_to_bundle=cached_guid_bundle,
            zip_entries=precomputed,
        )
    finally:
        z_ctx.__exit__(None, None, None)

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

    status("Packing snapshot…")
    adapter_args = [ffs_adapter.format, ffs_adapter.user_prefix,
                    ffs_adapter.sys_prefix, ffs_adapter.old_layout]
    snapshot_blob = pack_snapshot(
        adapter_args, delta, zip_names, zip_ui_paths,
        ui_metadata, folder_map, metadata_only)

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
        'need_save_guid': cached_guid_bundle is None and bool(guid_to_bundle),
        'need_save_sizes': cached_sizes is None,
    }


def persist_result(case_dir: str, result: dict) -> None:
    """Write a parse result's snapshot, guid map and folder sizes into
    casecache.db, so the GUI can pick it up via the normal snapshot fast-path."""
    if not case_dir:
        return
    with closing(_open_cache_db(case_dir)) as db:
        if result.get('need_save_guid'):
            save_guid_bundle_map(db, result['guid_to_bundle'])
        if result.get('need_save_sizes'):
            save_folder_sizes(db, result['folder_sizes'])
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
