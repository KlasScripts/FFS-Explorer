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
import shutil
import struct
import tempfile

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
TILE_KINDS = frozenset({SCAN, RECENTS, GRID, LIST})
KIND_FIELDS = {
    SCAN: "thumb_indexing_scan",
    RECENTS: "thumb_recents_home",
    GRID: "thumb_grid_scrolled_into_view",
    LIST: "thumb_list_row",
    VIEWER: "thumb_viewer_full_size",
}

# Link-source labels (shown in reports).
SOURCE_INDEX = "Index"
SOURCE_MEDIA_STORE = "Media store"
SOURCE_SAFE = "Safe folder"
SOURCE_ID_ABSENT = "Id not in the current media store"
SOURCE_CARVED = "Media store (carved)"
SOURCE_CONTENT_MATCH = "Content match to scan thumb"

# Picture-match (inferred viewer). Fingerprint: 16×16 grayscale box
# downscale (each cell = mean grey of 1/256 of the picture). Distance: the
# LARGEST cell difference / 255, so a local difference is not averaged away.
# Calibrated 2026-11-15 on every Glide-proven viewer↔own-scan pair (lab 3,
# Pixel 4, Poco 15): own pairs 0.0–0.024; closest different picture 0.082
# (nearly flat, dropped by _MIN_DETAIL), otherwise ≥0.318; lab colour
# samples 0.035–0.051 apart. Identical pictures saved as different files
# score like own pairs — only the rival gate and the absent-id rule help.
_FP_GRID = 16
_MATCH_MAX = 0.05      # best scan must be at or under this
_RIVAL_MAX = 0.10      # another file's scan at or under this → ambiguous, skip
_MIN_DETAIL = 10.0     # std-dev of the 16×16 grey cells; flatter → not matchable
_ASPECT_TOL = 0.08     # |a1-a2|/max(a1,a2)

_PACK = struct.Struct(">ii").pack


def _track_temp(paths, directory: str) -> None:
    paths.setdefault("_glide_temp_dirs", []).append(directory)


def cleanup_temps(paths) -> None:
    """Remove temp dirs created while linking (external.db / app DB copies)."""
    for directory in paths.pop("_glide_temp_dirs", []):
        shutil.rmtree(directory, ignore_errors=True)


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


def _extract_external_db(paths) -> str | None:
    """Copy external.db (+ WAL) into a tracked temp dir; return main db path."""
    zip_names = paths.get("_zip_names") or []
    read = paths.get("_read_zip_bytes")
    names = [n for n in zip_names
             if n.endswith("com.google.android.providers.media.module/databases/external.db")]
    names.sort(key=lambda n: (0 if "/data/data/" in n else 1, len(n)))
    if not names or read is None:
        return None
    tmp = tempfile.mkdtemp(prefix="ffs_glide_ms_")
    _track_temp(paths, tmp)
    main = os.path.join(tmp, "external.db")
    for sfx in ("", "-wal"):
        data = read(names[0] + sfx)
        if data:
            with open(main + sfx, "wb") as fh:
                fh.write(data)
    return main if os.path.isfile(main) else None


def mediastore_files(paths) -> list:
    """Files from the media store database (external.db), as link() inputs:
    content://media/external/file/<_id>, _size, date_modified in ms."""
    from artifact_runner import open_db_readonly

    main = _extract_external_db(paths)
    if not main:
        return []
    out = []
    conn = open_db_readonly(main)
    try:
        for r in conn.execute("SELECT _id, _data, _size, date_modified FROM files "
                              "WHERE _size IS NOT NULL AND date_modified IS NOT NULL"):
            out.append({"uri": f"content://media/external/file/{r[0]}", "size": r[2],
                        "modified_ms": r[3] * 1000, "path": r[1] or "", "media_store_id": r[0],
                        "source": SOURCE_MEDIA_STORE})
        ids = {r[0] for r in conn.execute("SELECT _id FROM files")}
        deleted = []
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "deleted_media" in tables:
            deleted = [r[0] for r in conn.execute("SELECT old_id FROM deleted_media")]
    finally:
        conn.close()
    paths["_glide_free_media_ids"] = free_media_ids(ids)
    paths["_glide_deleted_media_ids"] = deleted
    paths["_glide_external_db"] = main
    return out


MAX_ID_SEARCH = 200_000


def free_media_ids(ids: set) -> list:
    """Ids in the span of live media-store ids that are not currently in
    use, plus 1,000 above the highest. A search window for Glide URI keys —
    not proof these ids were ever used, and ids below the lowest live id
    are not searched. Newest first, at most MAX_ID_SEARCH."""
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
    read, adapter = paths.get("_read_zip_bytes"), paths.get("_adapter")
    if read is None or adapter is None:
        return None
    tmp = tempfile.mkdtemp(prefix="ffs_glide_db_")
    _track_temp(paths, tmp)
    main = None
    for sfx in ("", "-wal"):
        data = read(adapter.resolve(f"{app_base_ui}/databases/{rel}{sfx}"))
        if data:
            with open(os.path.join(tmp, "db" + sfx), "wb") as fh:
                fh.write(data)
            if not sfx:
                main = os.path.join(tmp, "db")
    return open_db_readonly(main) if main else None


def carve_absent_id_viewers(entries: dict, linked: dict, paths) -> dict:
    """If media-store WAL/free pages still hold size+mtime for an id that is
    no longer live, rebuild the Glide viewer key. Lab: Safe-delete id 130
    carved from WAL and matched the full-screen cache entry. Josh everyday
    deletes with no residue: no rows. Returns new {name: (VIEWER, file, dims)}."""
    import sqlite_carve as carve

    want = set(paths.get("_glide_deleted_media_ids") or [])
    for _kind, f, _dims in linked.values():
        if f.get("source") == SOURCE_ID_ABSENT and f.get("media_store_id") is not None:
            want.add(f["media_store_id"])
    if not want:
        return {}
    main = paths.get("_glide_external_db") or _extract_external_db(paths)
    if not main:
        return {}
    try:
        recovered = carve.recover_deleted_rows(
            {"external": main}, "files", file_key="external")
    except Exception:
        return {}
    files = []
    for r in recovered:
        rid = r.get("_id")
        if rid not in want:
            continue
        size, mt = r.get("_size"), r.get("date_modified")
        if size is None or mt is None:
            continue
        mt_ms = mt * 1000 if mt < 10**11 else mt
        path = r.get("_data") or ""
        files.append({
            "uri": f"content://media/external/file/{rid}",
            "size": size,
            "modified_ms": mt_ms,
            "path": path,
            "media_store_id": rid,
            "opened": True,
            "source": SOURCE_CARVED,
        })
    if not files:
        return {}
    rest = {k: v for k, v in entries.items() if k not in linked}
    return link(rest, files, viewer=True, scan=True)


def link_all(paths, app_base_ui: str) -> tuple[dict, dict]:
    """(entries, linked) for the whole cache: index rows first, then the
    media store, then Safe folder copies by id search, then background scan
    thumbnails for ids not in the current media store, then viewer keys from
    carved media-store rows when residue remains. Cached on *paths*."""
    if "_glide_links" in paths:
        return paths["_glide_links"]
    entries = read_cache(paths, app_base_ui)
    linked = {}
    try:
        if entries:
            index = []
            conn = _app_db(paths, app_base_ui, "files_master_database")
            if conn is not None:
                try:
                    tables = {r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    opened = set()
                    if "file_open_info_table" in tables:
                        opened = {r[0] for r in conn.execute(
                            "SELECT DISTINCT file_id FROM file_open_info_table")}
                    for r in conn.execute(
                            "SELECT id, uri, size, file_date_modified_ms, root_path, "
                            "root_relative_file_path, media_store_id FROM files_master_table"):
                        root = (r[4] or "").rstrip("/")
                        index.append({
                            "uri": r[1], "size": r[2], "modified_ms": r[3],
                            "path": f"{root}/{r[5]}" if root else (r[5] or ""),
                            "media_store_id": r[6], "index_id": r[0],
                            "opened": r[0] in opened, "source": SOURCE_INDEX,
                        })
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
                    for r in conn.execute(
                            "SELECT original_file_root_relative_path, "
                            "original_file_storage_location, file_safe_folder_path, "
                            "file_last_modified_timestamp FROM safe_folder_metadata_table"):
                        leaf = (r[2] or "").rsplit("/", 1)[-1]
                        data = paths["_read_zip_bytes"](paths["_adapter"].resolve(
                            f"{app_base_ui}/files/.FilesByGoogle/{leaf}"))
                        if data is None or r[3] is None:
                            continue
                        mt = r[3] * 1000 if r[3] < 10**11 else r[3]
                        root = "/storage/emulated/0/" if r[1] == 1 else ""
                        safe.append({
                            "size": len(data), "modified_ms": mt,
                            "path": root + (r[0] or ""),
                            "safe_folder_path": r[2], "source": SOURCE_SAFE,
                        })
                finally:
                    conn.close()
            linked.update(search_media_ids(entries, safe, free_ids, linked))
            linked.update(scan_free_ids(entries, free_ids, linked))
            linked.update(carve_absent_id_viewers(entries, linked, paths))
    finally:
        cleanup_temps(paths)
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


def _size_px(size) -> str:
    if not size:
        return ""
    return f"{size[0]}\u00d7{size[1]}"


def _cache_image_bytes(paths, ui_path: str) -> bytes | None:
    read, adapter = paths.get("_read_zip_bytes"), paths.get("_adapter")
    if read is None or adapter is None or not ui_path:
        return None
    return read(adapter.resolve(ui_path))


def _fingerprint(paths, ui_path: str):
    """16×16 grayscale cell means (bytes), or None if undecodable."""
    cache = paths.setdefault("_glide_fingerprints", {})
    if ui_path in cache:
        return cache[ui_path]
    data = _cache_image_bytes(paths, ui_path)
    fp = None
    if data:
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("L")
            fp = im.resize((_FP_GRID, _FP_GRID), Image.Resampling.BOX).tobytes()
        except Exception:
            fp = None
    cache[ui_path] = fp
    return fp


def _distance(a: bytes, b: bytes) -> float:
    """Largest cell difference / 255 (1.0 if not comparable)."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return max(abs(x - y) for x, y in zip(a, b)) / 255.0


def _detail(fp: bytes) -> float:
    n = len(fp)
    mean = sum(fp) / n
    return (sum((x - mean) ** 2 for x in fp) / n) ** 0.5


def _aspect(size) -> float | None:
    if not size or not size[1]:
        return None
    return size[0] / size[1]


def _aspect_close(a, b) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) / max(a, b) <= _ASPECT_TOL


def _screen_boxes(linked: dict) -> set:
    """Requested (width, height) of every Glide-proven viewer key in this
    case — the screen box Files asked Glide to fit the picture into."""
    return {tuple(dims) for kind, _f, dims in linked.values() if kind == VIEWER and dims}


def _screen_gate(size, boxes: set) -> bool:
    """FitCenter fills the box edge to edge in width or in height and never
    exceeds it. No rotation or cross-matching of sides."""
    if not size or not boxes:
        return False
    w, h = size
    return any(w <= bw and h <= bh and (w == bw or h == bh) for bw, bh in boxes)


def _identity(f) -> tuple:
    if f.get("media_store_id") is not None:
        return ("id", f["media_store_id"])
    if f.get("index_id") is not None:
        return ("index", f["index_id"])
    return ("path", f.get("path") or f.get("safe_folder_path") or f.get("uri") or "")


def likely_viewer_rows(paths, app_base_ui: str) -> list:
    """Inferred full-screen leftovers: picture match to a Glide-proven scan.

    Gates, all required:
      * this case has a Glide-proven viewer key; the candidate fills that
        key's requested screen box in width or height (_screen_gate);
      * the candidate is not Glide-linked at all;
      * enough detail to match (_MIN_DETAIL);
      * same shape as the scan (_ASPECT_TOL) and distance ≤ _MATCH_MAX;
      * the best file's scan has source "Id not in the current media
        store". A file still in the index or media store has a known size
        and modified time: if it had been viewed, its viewer key would
        normally have been proven, so a picture match to it points to
        another copy of the picture, not to that file;
      * no other file's scan (any source) within _RIVAL_MAX.
    Scans are grouped per file (a file can have a 640 and a 256 scan).
    Two leftovers matching the same file are both listed, each flagged.
    Returns [] if the case has no Glide-proven viewer key.
    """
    from artifact_runner import archive_has, device_path_to_ui_path

    if "_glide_likely_viewer" in paths:
        return paths["_glide_likely_viewer"]

    entries, linked = link_all(paths, app_base_ui)
    boxes = _screen_boxes(linked)
    if not boxes:
        paths["_glide_likely_viewer"] = []
        return []

    files = {}   # identity -> {"file", "scans": [(name, entry, fp, aspect)]}
    for name, (kind, f, _dims) in linked.items():
        if kind != SCAN:
            continue
        entry = entries.get(name)
        if not entry or not entry.get("size"):
            continue
        fp = _fingerprint(paths, entry["ui_path"])
        if not fp:
            continue
        slot = files.setdefault(_identity(f), {"file": f, "scans": []})
        slot["scans"].append((name, entry, fp, _aspect(entry["size"])))
    if not files:
        paths["_glide_likely_viewer"] = []
        return []

    rows = []
    for name, entry in entries.items():
        if name in linked:
            continue  # Glide already explained this cache file
        size = entry.get("size")
        if not _screen_gate(size, boxes):
            continue
        fp = _fingerprint(paths, entry["ui_path"])
        if not fp or _detail(fp) < _MIN_DETAIL:
            continue
        cand_aspect = _aspect(size)
        scored = []   # (distance, identity, scan name, scan entry)
        for ident, slot in files.items():
            best = None
            for sname, sentry, sfp, sasp in slot["scans"]:
                if not _aspect_close(cand_aspect, sasp):
                    continue
                d = _distance(fp, sfp)
                if best is None or d < best[0]:
                    best = (d, ident, sname, sentry)
            if best:
                scored.append(best)
        if not scored:
            continue
        scored.sort(key=lambda x: x[0])
        best_d, ident, _sname, sentry = scored[0]
        if best_d > _MATCH_MAX:
            continue
        if len(scored) > 1 and scored[1][0] <= _RIVAL_MAX:
            continue  # another file's scan looks like this picture too
        f = files[ident]["file"]
        if f.get("source") != SOURCE_ID_ABSENT:
            continue
        media_store_id = f.get("media_store_id")
        path = f.get("path") or ""
        if not path and media_store_id is not None:
            path = f"[no path — media store id {media_store_id}]"
        file_ui = device_path_to_ui_path(f.get("path") or "") if f.get("path") else ""
        file_link = file_ui if file_ui and archive_has(paths, file_ui) else ""
        rows.append({
            "path": path,
            "thumbnail": entry["ui_path"],
            "paired_scan_thumb": sentry["ui_path"],
            "confidence": "Inferred",
            "link_method": SOURCE_CONTENT_MATCH,
            "match_distance": round(best_d, 4),
            "runner_up_distance": round(scored[1][0], 4) if len(scored) > 1 else None,
            "size_px": _size_px(size),
            "format": entry.get("format") or "",
            "journal_order": entry.get("journal_order"),
            "journal_reads": entry.get("journal_reads") or 0,
            "media_store_id": media_store_id,
            "uri": f.get("uri") or "",
            "scan_link_source": f.get("source") or "",
            "file_link": file_link,
            "cache_key": name,
            "kind": "Likely full-screen viewer",
            "kind_source": "Content match (inferred)",
            "user_looked": (
                "Likely full-screen preview — not Glide-proven; no open time"
            ),
            "_ident": ident,
        })

    per_file = {}
    for r in rows:
        per_file[r["_ident"]] = per_file.get(r["_ident"], 0) + 1
    for r in rows:
        r["same_file_rows"] = per_file[r.pop("_ident")]
    rows.sort(key=lambda r: (
        r["journal_order"] is None,
        r["journal_order"] or 0,
        r["cache_key"],
    ))
    paths["_glide_likely_viewer"] = rows
    return rows


def likely_viewer_cache_keys(paths, app_base_ui: str) -> set:
    """Cache names claimed by likely_viewer_rows (exclude from Unknown)."""
    return {r["cache_key"] for r in likely_viewer_rows(paths, app_base_ui)}


def thumbnail_rows(paths, app_base_ui: str, kinds=None, *,
                   linked_only: bool = False, include_unlinked: bool = False,
                   exclude_keys: set | None = None) -> list:
    """Report rows for the Glide cache.

    *kinds* — if set, only Glide-linked rows whose kind is in this set.
    *linked_only* — skip every unlinked cache file (Full-screen Views).
    *include_unlinked* — keep unlinked rows as kind ``Unknown (W×H)``; never
    guess scan/grid/viewer from pixels alone.
    *exclude_keys* — cache names to omit (e.g. claimed by likely_viewer_rows).
    """
    from artifact_runner import archive_has, device_path_to_ui_path

    entries, linked = link_all(paths, app_base_ui)
    skip = exclude_keys or set()
    rows = []
    for name_key, entry in entries.items():
        if name_key in skip:
            continue
        hit = linked.get(name_key)
        if hit:
            kind, f, _dims = hit
            kind_source = "Glide key"
            path = f.get("path") or ""
            link_source = f.get("source") or ""
            media_store_id = f.get("media_store_id")
            uri = f.get("uri") or ""
            file_ui = device_path_to_ui_path(path) if path else ""
            file_link = file_ui if file_ui and archive_has(paths, file_ui) else ""
            if not file_link:
                safe = f.get("safe_folder_path") or ""
                if safe:
                    safe_ui = device_path_to_ui_path(safe)
                    if safe_ui and archive_has(paths, safe_ui):
                        file_link = safe_ui
                        if not path:
                            path = safe
            if kinds is not None and kind not in kinds:
                continue
        else:
            if linked_only or not include_unlinked:
                continue
            kind = f"Unknown ({_size_px(entry['size'])})" if entry["size"] else "Unknown"
            kind_source = "Unlinked"
            path = ""
            link_source = ""
            media_store_id = None
            uri = ""
            file_link = ""
        rows.append({
            "kind": kind,
            "path": path,
            "linked": "Yes" if hit else "No",
            "size_px": _size_px(entry["size"]),
            "format": entry.get("format") or "",
            "journal_order": entry.get("journal_order"),
            "journal_reads": entry.get("journal_reads") or 0,
            "link_source": link_source,
            "kind_source": kind_source,
            "thumbnail": entry["ui_path"],
            "file_link": file_link,
            "cache_key": name_key,
            "media_store_id": media_store_id,
            "uri": uri,
        })
    rows.sort(key=lambda r: (
        r["journal_order"] is None,
        r["journal_order"] or 0,
        r["cache_key"],
    ))
    return rows


def scan_free_ids(entries: dict, free_ids: list, linked: dict) -> dict:
    """Background-scan thumbnails whose content URI uses a media-store id
    that is not in the current files table. The hash proves that URI; it
    does not by itself prove the file was deleted (a rebuild or another
    volume can look the same)."""
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
                                   "source": SOURCE_ID_ABSENT}, dims)
                rest.discard(h)
    return found
