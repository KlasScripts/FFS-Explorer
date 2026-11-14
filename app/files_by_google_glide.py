"""Links Files by Google's Glide thumbnail cache to the files it shows.

cache/image_manager_disk_cache/<64 hex>.0 is Glide's disk cache. The name is
the SHA-256 of a ResourceCacheKey: signature, source key, requested width and
height as two big-endian ints, transformation id, options (none of Files'
options add bytes), and the resource class name. Order read in Files 1.13157
(decompiled class haj) and confirmed by hooking MessageDigest on the lab
emulator (2026-11-14).

Recipes by Files version (lab 1.13157; Pixel ~2024; Poco through 1.8662):

| Kind | Signature | Source key | Size / transform |
|---|---|---|---|
| Indexing scan | empty | content URI | 640×640 (1.13157) or 256×256 (older), no transform |
| Recents | size/mtime, or empty (some older UI) | URI or file://path | portrait CenterCrop |
| Grid | size/mtime, or empty | URI or file://path | square CenterCrop |
| List | size:mtime | URI or file://path | square CenterCrop |
| Viewer | size/mtime | URI or file://path | full-screen FitCenter |

Lab run files-app-usage-001: 163/163. Older phones: source key and scan size
differ; requested sizes follow the screen.
"""

import hashlib
import io
import os
import struct

CACHE_DIR = "cache/image_manager_disk_cache"

CENTER_CROP = b"com.bumptech.glide.load.resource.bitmap.CenterCrop"
FIT_CENTER = b"com.bumptech.glide.load.resource.bitmap.FitCenter"
RESOURCE_CLASSES = (b"android.graphics.drawable.BitmapDrawable", b"android.graphics.Bitmap")

SCAN = "Background scan (indexing)"
RECENTS = "Recents (home screen)"
GRID = "Grid tile (category view)"
LIST = "List row (folder or category list)"
VIEWER = "Full-screen viewer"
KINDS = (SCAN, RECENTS, GRID, LIST, VIEWER)
KIND_FIELDS = {
    SCAN: "thumb_indexing_scan",
    RECENTS: "thumb_recents_home",
    GRID: "thumb_grid_scrolled_into_view",
    LIST: "thumb_list_row",
    VIEWER: "thumb_viewer_full_size",
}

_PACK = struct.Struct(">ii").pack


def read_cache(paths, app_base_ui: str) -> dict:
    """{name: {"ui_path", "size": (w, h) or None, "format", "journal_order",
    "journal_reads"}} for every <name>.0 in Files' Glide cache."""
    zip_names = paths.get("_zip_names") or []
    read = paths.get("_read_zip_bytes")
    adapter = paths.get("_adapter")
    if not zip_names or read is None or adapter is None:
        return {}
    ui_prefix = f"{app_base_ui}/{CACHE_DIR}"
    physical_prefix = adapter.resolve(ui_prefix).rstrip("/") + "/"
    entries = {}
    journal = None
    for n in zip_names:
        if not n.startswith(physical_prefix):
            continue
        leaf = n[len(physical_prefix):]
        if leaf == "journal":
            journal = read(n)
            continue
        if "/" in leaf or not leaf.endswith(".0") or len(leaf) != 66:
            continue
        data = read(n)
        size, fmt = None, ""
        if data:
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                size, fmt = im.size, im.format or ""
            except Exception:
                pass
        entries[leaf[:-2]] = {"ui_path": f"{ui_prefix}/{leaf}", "size": size, "format": fmt,
                              "journal_order": None, "journal_reads": 0}
    if journal:
        order = 0
        for line in journal.decode("utf-8", "replace").splitlines()[5:]:
            parts = line.split(" ")
            if len(parts) < 2 or parts[1] not in entries:
                continue
            e = entries[parts[1]]
            if parts[0] == "CLEAN" and e["journal_order"] is None:
                order += 1
                e["journal_order"] = order
            elif parts[0] == "READ":
                e["journal_reads"] += 1
    return entries


SCAN_DIMS = ((640, 640), (256, 256))


def _candidate_sizes(entries: dict) -> tuple[set, set, set]:
    squares, portraits, widths = set(), set(), set()
    for e in entries.values():
        s = e["size"]
        if not s:
            continue
        w, h = s
        if w == h:
            squares.add(s)
        elif h > w:
            portraits.add(s)
        if w >= 600:
            widths.add(w)
    squares |= {(640, 640), (256, 256)}
    return squares, portraits, widths


def _check(names, base, dims, transform):
    d1 = base.copy()
    d1.update(_PACK(*dims))
    if transform:
        d1.update(transform)
    for rc in RESOURCE_CLASSES:
        d2 = d1.copy()
        d2.update(rc)
        h = d2.hexdigest()
        if h in names:
            return h
    return None


def _models(f) -> list:
    """Source keys Files has been seen to put in the Glide key."""
    out = []
    uri = f.get("uri") or ""
    if uri:
        out.append(uri)
    path = f.get("path") or ""
    if path.startswith("file://"):
        path = path[7:]
    if path and not path.startswith("/"):
        path = "/storage/emulated/0/" + path.lstrip("/")
    if path:
        out.append("file://" + path)
        out.append(path)
    # unique, stable order
    seen = set()
    uniq = []
    for m in out:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def _usable(f) -> bool:
    return bool(_models(f)) and f.get("size") is not None and f.get("modified_ms") is not None


def link(entries: dict, files: list, viewer: bool = True, scan: bool = True) -> dict:
    """Match cache names to files.

    files: [{"uri" and/or "path", "size", "modified_ms", ...}]; other keys
    are passed through. "opened": True marks a file to search for a viewer
    picture even if no other thumbnail of it is found. scan=False leaves out
    the background-scan recipe (URI-only key). Returns
    {name: (kind, file_dict, (w, h))}."""
    names = set(entries)
    if not names:
        return {}
    squares, portraits, widths = _candidate_sizes(entries)
    found = {}
    hit_files = set()
    for idx, f in enumerate(files):
        if not _usable(f):
            continue
        size, mt = f["size"], f["modified_ms"]
        for model in _models(f):
            mb = model.encode()
            if scan:
                for dims in SCAN_DIMS:
                    h = _check(names, hashlib.sha256(mb), dims, b"")
                    if h:
                        found.setdefault(h, (SCAN, f, dims))
                        hit_files.add(idx)
                # Some older UI paths put an empty signature with CenterCrop
                # (Pixel 210×210; Poco Recents-sized tiles). Still URI-only.
                for dims in squares:
                    h = _check(names, hashlib.sha256(mb), dims, CENTER_CROP)
                    if h:
                        found.setdefault(h, (GRID, f, dims))
                        hit_files.add(idx)
                for dims in portraits:
                    h = _check(names, hashlib.sha256(mb), dims, CENTER_CROP)
                    if h:
                        found.setdefault(h, (RECENTS, f, dims))
                        hit_files.add(idx)
            slash = hashlib.sha256(f"{size}/{mt}".encode() + mb)
            colon = hashlib.sha256(f"{size}:{mt}".encode() + mb)
            for dims in squares:
                h = _check(names, slash, dims, CENTER_CROP)
                if h:
                    found.setdefault(h, (GRID, f, dims))
                    hit_files.add(idx)
                h = _check(names, colon, dims, CENTER_CROP)
                if h:
                    found.setdefault(h, (LIST, f, dims))
                    hit_files.add(idx)
            for dims in portraits:
                h = _check(names, slash, dims, CENTER_CROP)
                if h:
                    found.setdefault(h, (RECENTS, f, dims))
                    hit_files.add(idx)
                h = _check(names, colon, dims, CENTER_CROP)
                if h:
                    found.setdefault(h, (RECENTS, f, dims))
                    hit_files.add(idx)
    if not viewer or not widths:
        return found
    for idx, f in enumerate(files):
        if not _usable(f) or (idx not in hit_files and not f.get("opened")):
            continue
        for model in _models(f):
            slash = hashlib.sha256(f"{f['size']}/{f['modified_ms']}".encode() + model.encode())
            for w in widths:
                for hgt in range(w, 4001):
                    h = _check(names, slash, (w, hgt), FIT_CENTER)
                    if h:
                        found.setdefault(h, (VIEWER, f, (w, hgt)))
                        break
    return found


def mediastore_files(paths) -> list:
    """Files from the media store database (external.db), as link() inputs:
    content://media/external/file/<_id>, _size, date_modified in ms."""
    from artifact_runner import open_db_readonly
    import tempfile
    zip_names = paths.get("_zip_names") or []
    read = paths.get("_read_zip_bytes")
    names = [n for n in zip_names
             if n.endswith("com.google.android.providers.media.module/databases/external.db")]
    names.sort(key=lambda n: (0 if "/data/data/" in n else 1, len(n)))
    if not names or read is None:
        return []
    tmp = tempfile.mkdtemp(prefix="ffs_glide_ms_")
    for sfx in ("", "-wal"):
        data = read(names[0] + sfx)
        if data:
            with open(os.path.join(tmp, "external.db" + sfx), "wb") as fh:
                fh.write(data)
    out = []
    conn = open_db_readonly(os.path.join(tmp, "external.db"))
    try:
        for r in conn.execute("SELECT _id, _data, _size, date_modified FROM files "
                              "WHERE _size IS NOT NULL AND date_modified IS NOT NULL"):
            out.append({"uri": f"content://media/external/file/{r[0]}", "size": r[2],
                        "modified_ms": r[3] * 1000, "path": r[1] or "", "media_store_id": r[0],
                        "source": "Media store"})
        ids = {r[0] for r in conn.execute("SELECT _id FROM files")}
    finally:
        conn.close()
    paths["_glide_free_media_ids"] = free_media_ids(ids)
    return out


MAX_ID_SEARCH = 200_000


def free_media_ids(ids: set) -> list:
    """Ids between the lowest and highest media store id that are no longer
    in use, plus 1,000 above the highest: the only ids a removed file can
    have had. Newest first, at most MAX_ID_SEARCH."""
    if not ids:
        return list(range(1000, 0, -1))
    lo, hi = min(ids), max(ids)
    free = [i for i in range(hi + 1000, lo - 1, -1) if i not in ids]
    return free[:MAX_ID_SEARCH]


def search_media_ids(entries: dict, files: list, free_ids: list, linked: dict) -> dict:
    """For files whose media store id is unknown (e.g. moved into the Safe
    folder, so the index and media store rows are gone), try each free id.
    Only unlinked cache names are considered."""
    rest = {k: v for k, v in entries.items() if k not in linked}
    if not rest:
        return {}
    found = {}
    for f in files:
        tries = []
        for i in free_ids:
            g = dict(f)
            g["uri"] = f"content://media/external/file/{i}"
            g["media_store_id"] = i
            tries.append(g)
        hits = link(rest, tries, viewer=False, scan=False)
        if hits:
            ids = {v[1]["media_store_id"] for v in hits.values()}
            hits.update(link(rest, [g for g in tries if g["media_store_id"] in ids]))
        for h, v in hits.items():
            found[h] = v
        rest = {k: v for k, v in rest.items() if k not in found}
        if not rest:
            break
    return found


def _app_db(paths, app_base_ui: str, rel: str):
    from artifact_runner import open_db_readonly
    import tempfile
    read, adapter = paths.get("_read_zip_bytes"), paths.get("_adapter")
    if read is None or adapter is None:
        return None
    tmp = tempfile.mkdtemp(prefix="ffs_glide_db_")
    main = None
    for sfx in ("", "-wal"):
        data = read(adapter.resolve(f"{app_base_ui}/databases/{rel}{sfx}"))
        if data:
            with open(os.path.join(tmp, "db" + sfx), "wb") as fh:
                fh.write(data)
            if not sfx:
                main = os.path.join(tmp, "db")
    return open_db_readonly(main) if main else None


def link_all(paths, app_base_ui: str) -> tuple[dict, dict]:
    """(entries, linked) for the whole cache: index rows first, then the
    media store, then Safe folder copies by id search, then background scan
    thumbnails of removed media store ids. Cached on *paths*."""
    if "_glide_links" in paths:
        return paths["_glide_links"]
    entries = read_cache(paths, app_base_ui)
    linked = {}
    if entries:
        index = []
        conn = _app_db(paths, app_base_ui, "files_master_database")
        if conn is not None:
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                opened = set()
                if "file_open_info_table" in tables:
                    opened = {r[0] for r in conn.execute("SELECT DISTINCT file_id FROM file_open_info_table")}
                for r in conn.execute("SELECT id, uri, size, file_date_modified_ms, root_path, "
                                      "root_relative_file_path, media_store_id FROM files_master_table"):
                    root = (r[4] or "").rstrip("/")
                    index.append({"uri": r[1], "size": r[2], "modified_ms": r[3],
                                  "path": f"{root}/{r[5]}" if root else (r[5] or ""),
                                  "media_store_id": r[6], "index_id": r[0], "opened": r[0] in opened,
                                  "source": "Index"})
            finally:
                conn.close()
        linked.update(link(entries, index))
        rest = {k: v for k, v in entries.items() if k not in linked}
        linked.update(link(rest, mediastore_files(paths)))
        free_ids = paths.get("_glide_free_media_ids", [])
        safe = []
        conn = _app_db(paths, app_base_ui, "safe_folder_db")
        if conn is not None:
            try:
                for r in conn.execute("SELECT original_file_root_relative_path, original_file_storage_location, "
                                      "file_safe_folder_path, file_last_modified_timestamp "
                                      "FROM safe_folder_metadata_table"):
                    leaf = (r[2] or "").rsplit("/", 1)[-1]
                    data = paths["_read_zip_bytes"](paths["_adapter"].resolve(
                        f"{app_base_ui}/files/.FilesByGoogle/{leaf}"))
                    if data is None or r[3] is None:
                        continue
                    mt = r[3] * 1000 if r[3] < 10**11 else r[3]
                    root = "/storage/emulated/0/" if r[1] == 1 else ""
                    safe.append({"size": len(data), "modified_ms": mt, "path": root + (r[0] or ""),
                                 "safe_folder_path": r[2], "source": "Safe folder"})
            finally:
                conn.close()
        linked.update(search_media_ids(entries, safe, free_ids, linked))
        linked.update(scan_free_ids(entries, free_ids, linked))
    paths["_glide_links"] = (entries, linked)
    return entries, linked


def thumbs_by_file(linked: dict, entries: dict, key: str) -> dict:
    """{file_dict[key]: {kind: [ui_path, ...]}} for building per-file columns."""
    out = {}
    for name, (kind, f, _dims) in linked.items():
        k = f.get(key)
        if k is None:
            continue
        out.setdefault(k, {}).setdefault(kind, []).append(entries[name]["ui_path"])
    return out


def scan_free_ids(entries: dict, free_ids: list, linked: dict) -> dict:
    """Background scan thumbnails of media store ids that are no longer in
    use: the file was removed, and only its id is known."""
    rest = {k for k in entries if k not in linked}
    found = {}
    for i in free_ids:
        if not rest:
            break
        uri = f"content://media/external/file/{i}"
        for dims in SCAN_DIMS:
            h = _check(rest, hashlib.sha256(uri.encode()), dims, b"")
            if h:
                found[h] = (SCAN, {"uri": uri, "media_store_id": i, "path": "",
                                   "source": "Removed media store id"}, dims)
                rest.discard(h)
    return found
