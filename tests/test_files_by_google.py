"""Files by Google reports: opens, Safe folder, sessions, thumbs."""
import hashlib
import importlib.util
import io
import os
import sqlite3
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
PHOTO_UI = "data/media/0/Download/sample-birch-400x300.jpg"
SAFE_UI = "data/data/com.google.android.apps.nbu.files/files/.FilesByGoogle/sample-hut-400x300.png"
APP_BASE = "data/data/com.google.android.apps.nbu.files"
CACHE = f"{APP_BASE}/cache/image_manager_disk_cache"


def _parser(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "artifacts", "android", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _master(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE files_master_table(id INTEGER PRIMARY KEY AUTOINCREMENT, media_store_id INTEGER,
            root_path TEXT NOT NULL DEFAULT '', root_relative_file_path TEXT NOT NULL DEFAULT '',
            file_name TEXT NOT NULL, size INTEGER NOT NULL, file_date_modified_ms INTEGER NOT NULL,
            storage_location INTEGER NOT NULL, mime_type TEXT, media_type INTEGER, uri TEXT NOT NULL,
            is_hidden INTEGER NOT NULL, title TEXT, artist TEXT, album TEXT, parent_folder_name TEXT);
        CREATE TABLE file_open_info_table(file_id INTEGER NOT NULL, file_open_time_stamp_ms INTEGER NOT NULL,
            UNIQUE(file_id, file_open_time_stamp_ms),
            FOREIGN KEY(file_id) REFERENCES files_master_table(id) ON DELETE CASCADE);
        INSERT INTO files_master_table VALUES (7, 80, '/storage/emulated/0', 'Download/sample-birch-400x300.jpg',
            'sample-birch-400x300.jpg', 121036, 1794609164000, 0, 'image/jpeg', 1,
            'content://media/external/file/80', 0, 'sample-birch-400x300', NULL, NULL, 'Download');
        INSERT INTO files_master_table VALUES (19, 92, '/storage/emulated/0', 'Download/sample-5s.mp4',
            'sample-5s.mp4', 2848208, 1794609461000, 0, 'video/mp4', 3,
            'content://media/external/file/92', 0, 'sample-5s', NULL, NULL, 'Download');
        INSERT INTO file_open_info_table VALUES (7, 1794612530779), (7, 1794612600000), (19, 1794612612059);
    """)
    conn.commit()
    return conn


def _paths(tmp_path, **files):
    return {**files, "_zip_names": [PHOTO_UI, SAFE_UI], "_read_zip_bytes": lambda n: b"x",
            "_ui_paths": {"sessions": "data/data/com.google.android.apps.nbu.files/files/activityTimeLogStoreName.pb"}}


def test_opens_follow_the_index_and_cascade_away(tmp_path):
    db = str(tmp_path / "files_master_database")
    conn = _master(db)
    rows = _parser("files_by_google_opens").run(_paths(tmp_path, master=db))
    assert [(r["opened"], r["file_id"]) for r in rows] == [(1794612530779, 7), (1794612600000, 7), (1794612612059, 19)]
    conn.execute("DELETE FROM files_master_table WHERE id = 19")
    conn.commit()
    conn.close()
    rows = _parser("files_by_google_opens").run(_paths(tmp_path, master=db))
    assert [r["file_id"] for r in rows] == [7, 7]


def test_open_without_index_row_is_labelled(tmp_path):
    db = str(tmp_path / "files_master_database")
    conn = _master(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO file_open_info_table VALUES (99, 1)")
    conn.commit()
    conn.close()
    rows = _parser("files_by_google_opens").run(_paths(tmp_path, master=db))
    orphan = rows[0]
    assert orphan["path"] == "[no index row — file_id=99]" and orphan["raw_file_id"] is None


def test_safe_folder_rows_and_copy_link(tmp_path):
    db = str(tmp_path / "safe_folder_db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE safe_folder_metadata_table(id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_file_root_relative_path STRING NOT NULL, original_file_storage_location INTEGER NOT NULL,
            file_safe_folder_path STRING NOT NULL UNIQUE, file_last_modified_timestamp INTEGER NOT NULL);
        INSERT INTO safe_folder_metadata_table VALUES (2, 'Download/sample-hut-400x300.png', 1,
            '/data/user/0/com.google.android.apps.nbu.files/files/.FilesByGoogle/sample-hut-400x300.png', 1794610959);
        INSERT INTO safe_folder_metadata_table VALUES (3, 'Download/gone.mp4', 1,
            '/data/user/0/com.google.android.apps.nbu.files/files/.FilesByGoogle/gone.mp4', 1794609461);
    """)
    conn.commit()
    conn.close()
    hut, gone = _parser("files_by_google_safe_folder").run(_paths(tmp_path, safe=db))
    assert hut["original_path"] == "/storage/emulated/0/Download/sample-hut-400x300.png"
    assert (hut["copy_in_archive"], hut["copy_link"]) == ("Yes", SAFE_UI)
    assert (gone["copy_in_archive"], gone["copy_link"]) == ("No", "")


def _varint(value):
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        out.append(piece | 0x80 if value else piece)
        if not value:
            return bytes(out)


def _field(number, value):
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def test_sessions_decode_with_byte_spans(tmp_path):
    def stamp(ms):
        return _field(1, ms // 1000) + _field(2, (ms % 1000) * 1_000_000)
    first = _field(1, _field(1, stamp(1794612611651)) + _field(2, stamp(1794612627094)))
    data = first + _field(1, _field(1, stamp(5000)) + _field(2, stamp(6500))) + _field(2, b"1.13157.1011540316.0-release")
    pb = tmp_path / "activityTimeLogStoreName.pb"
    pb.write_bytes(data)
    rows = _parser("files_by_google_sessions").run(_paths(tmp_path, sessions=str(pb)))
    assert [(r["start"], r["end"], r["duration_s"]) for r in rows] == [
        (1794612611651, 1794612627094, 15.443), (5000, 6500, 1.5)]
    assert rows[0]["app_version"] == "1.13157.1011540316.0-release"
    assert (rows[0]["raw_offset"], rows[0]["raw_length"]) == (0, len(first))
    assert rows[1]["raw_offset"] == len(first)


class _IdAdapter:
    def resolve(self, ui_path: str) -> str:
        return ui_path.lstrip("/")


def _pattern_image(w: int, h: int, seed: int = 0):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), (20 + seed, 24, 40))
    dr = ImageDraw.Draw(im)
    for i in range(0, max(w, h), 13):
        colour = ((80 + seed * 17 + i) % 200, (40 + seed * 9) % 180, (120 + i) % 200)
        dr.rectangle([i % w, 0, min(w - 1, (i % w) + 6), h - 1], fill=colour)
        dr.ellipse([0, i % h, w - 1, min(h - 1, (i % h) + 6)], outline=colour)
    # Large light/dark shapes placed by seed, so the picture has photo-like
    # detail at 16×16 (fine stripes alone average out to flat grey).
    import random
    rnd = random.Random(seed)
    for _ in range(6):
        x0, y0 = rnd.randrange(w), rnd.randrange(h)
        x1, y1 = min(w - 1, x0 + w // 3), min(h - 1, y0 + h // 3)
        shade = rnd.choice((15, 240))
        dr.rectangle([x0, y0, x1, y1], fill=(shade, shade, shade))
    return im


def _jpeg(w: int, h: int, seed: int = 0) -> bytes:
    """Patterned JPEG so content-match tests are not fooled by flat colour."""
    buf = io.BytesIO()
    _pattern_image(w, h, seed).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _jpeg_scaled(w: int, h: int, seed: int, base_w: int = 400, base_h: int = 300) -> bytes:
    """Same picture as `_jpeg(base_w, base_h, seed)`, resized — low MAD to the base."""
    from PIL import Image
    im = _pattern_image(base_w, base_h, seed).resize((w, h), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _glide_name(signature: bytes, model: str, dims, transform: bytes) -> str:
    d = hashlib.sha256(signature)
    d.update(model.encode())
    d.update(struct.Struct(">ii").pack(*dims))
    if transform:
        d.update(transform)
    d.update(b"android.graphics.drawable.BitmapDrawable")
    return d.hexdigest()


def _glide_archive(tmp_path, *, with_viewer=True, with_orphan=True):
    """Build a tiny Glide cache + index + media store for unit tests."""
    import files_by_google_glide as glide

    uri = "content://media/external/file/80"
    size, mt = 121036, 1794609164000
    path = "/storage/emulated/0/Download/sample-birch-400x300.jpg"
    slash = f"{size}/{mt}".encode()
    colon = f"{size}:{mt}".encode()
    scan = _glide_name(b"", uri, (640, 640), b"")
    grid = _glide_name(slash, uri, (345, 345), glide.CENTER_CROP)
    listing = _glide_name(colon, uri, (168, 168), glide.CENTER_CROP)
    viewer = _glide_name(slash, uri, (1080, 1920), glide.FIT_CENTER)
    orphan = "a" * 64

    journal_lines = [
        "libcore.io.DiskLruCache", "1", "1", "1", "",
        f"CLEAN {scan} 1", f"CLEAN {grid} 1", f"CLEAN {listing} 1",
    ]
    # seed 1 = birch picture family; seed 99 = unrelated leftover
    archive = {
        f"{CACHE}/{scan}.0": _jpeg(400, 300, seed=1),
        f"{CACHE}/{grid}.0": _jpeg(345, 345, seed=1),
        f"{CACHE}/{listing}.0": _jpeg(168, 168, seed=1),
        PHOTO_UI: b"x",
    }
    if with_viewer:
        journal_lines.append(f"CLEAN {viewer} 1")
        # Lab-style FitCenter output keeps photo aspect (400×300 → 1080×810)
        archive[f"{CACHE}/{viewer}.0"] = _jpeg_scaled(1080, 810, seed=1)
    if with_orphan:
        journal_lines.append(f"CLEAN {orphan} 1")
        # Same screen-side size as viewer but different picture → must not be "likely"
        archive[f"{CACHE}/{orphan}.0"] = _jpeg_scaled(1080, 810, seed=99)
    archive[f"{CACHE}/journal"] = ("\n".join(journal_lines) + "\n").encode()

    db = str(tmp_path / "files_master_database")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE files_master_table(id INTEGER PRIMARY KEY, media_store_id INTEGER,
            root_path TEXT, root_relative_file_path TEXT, file_name TEXT, size INTEGER,
            file_date_modified_ms INTEGER, storage_location INTEGER, mime_type TEXT,
            media_type INTEGER, uri TEXT, is_hidden INTEGER, title TEXT, artist TEXT,
            album TEXT, parent_folder_name TEXT);
        CREATE TABLE file_open_info_table(file_id INTEGER, file_open_time_stamp_ms INTEGER);
        INSERT INTO files_master_table VALUES (7, 80, '/storage/emulated/0',
            'Download/sample-birch-400x300.jpg', 'sample-birch-400x300.jpg', 121036,
            1794609164000, 0, 'image/jpeg', 1, 'content://media/external/file/80',
            0, 'sample-birch', NULL, NULL, 'Download');
        INSERT INTO file_open_info_table VALUES (7, 1794612530779);
    """)
    conn.commit()
    conn.close()
    with open(db, "rb") as fh:
        archive[f"{APP_BASE}/databases/files_master_database"] = fh.read()

    ms = str(tmp_path / "external.db")
    conn = sqlite3.connect(ms)
    conn.execute("CREATE TABLE files (_id INTEGER PRIMARY KEY, _data TEXT, _size INTEGER, date_modified INTEGER)")
    conn.execute("INSERT INTO files VALUES (80, ?, 121036, ?)", (path, mt // 1000))
    conn.commit()
    conn.close()
    with open(ms, "rb") as fh:
        archive["data/data/com.google.android.providers.media.module/databases/external.db"] = fh.read()

    paths = {
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_adapter": _IdAdapter(),
        "_app_base_ui_path": APP_BASE,
        "_ui_paths": {},
    }
    return paths, {
        "scan": scan, "grid": grid, "listing": listing, "viewer": viewer,
        "orphan": orphan, "path": path,
    }


def test_thumbs_link_kinds_unlinked_are_unknown(tmp_path):
    import files_by_google_glide as glide

    paths, keys = _glide_archive(tmp_path)
    rows = _parser("files_by_google_thumbs").run(paths)
    by_key = {r["cache_key"]: r for r in rows}
    assert set(by_key) == {keys["scan"], keys["grid"], keys["listing"], keys["orphan"]}
    assert keys["viewer"] not in by_key  # viewer belongs in Full-screen Views

    assert by_key[keys["scan"]]["kind"] == glide.SCAN
    assert by_key[keys["grid"]]["kind"] == glide.GRID
    assert by_key[keys["listing"]]["kind"] == glide.LIST
    assert by_key[keys["scan"]]["linked"] == "Yes"
    assert by_key[keys["scan"]]["path"] == keys["path"]
    assert by_key[keys["scan"]]["file_link"] == PHOTO_UI
    assert by_key[keys["scan"]]["kind_source"] == "Glide key"
    assert by_key[keys["scan"]]["user_looked"].startswith("No")

    assert by_key[keys["orphan"]]["linked"] == "No"
    assert by_key[keys["orphan"]]["kind"] == "Unknown (1080\u00d7810)"
    assert by_key[keys["orphan"]]["kind_source"] == "Unlinked"
    assert by_key[keys["orphan"]]["thumbnail"] == f"{CACHE}/{keys['orphan']}.0"


def test_viewer_thumbs_glide_only_no_size_guess(tmp_path):
    import files_by_google_glide as glide

    paths, keys = _glide_archive(tmp_path)
    rows = _parser("files_by_google_viewer_thumbs").run(paths)
    assert len(rows) == 1
    row = rows[0]
    assert row["cache_key"] == keys["viewer"]
    assert row["kind"] == glide.VIEWER
    assert row["linked"] == "Yes"
    assert row["kind_source"] == "Glide key"
    assert row["path"] == keys["path"]
    # Large unlinked orphan must not appear as a size-guessed viewer
    assert all(r["cache_key"] != keys["orphan"] for r in rows)


def test_source_id_absent_label(tmp_path):
    import files_by_google_glide as glide

    uri = "content://media/external/file/999"
    scan = _glide_name(b"", uri, (640, 640), b"")
    archive = {
        f"{CACHE}/journal": (
            "libcore.io.DiskLruCache\n1\n1\n1\n\n"
            f"CLEAN {scan} 1\n"
        ).encode(),
        f"{CACHE}/{scan}.0": _jpeg(400, 300),
    }
    # Index empty; media store has only id 80 so 999 is free
    db = str(tmp_path / "files_master_database")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE files_master_table(id INTEGER PRIMARY KEY, media_store_id INTEGER,
            root_path TEXT, root_relative_file_path TEXT, file_name TEXT, size INTEGER,
            file_date_modified_ms INTEGER, storage_location INTEGER, mime_type TEXT,
            media_type INTEGER, uri TEXT, is_hidden INTEGER, title TEXT, artist TEXT,
            album TEXT, parent_folder_name TEXT);
        CREATE TABLE file_open_info_table(file_id INTEGER, file_open_time_stamp_ms INTEGER);
    """)
    conn.commit()
    conn.close()
    with open(db, "rb") as fh:
        archive[f"{APP_BASE}/databases/files_master_database"] = fh.read()

    ms = str(tmp_path / "external.db")
    conn = sqlite3.connect(ms)
    conn.execute("CREATE TABLE files (_id INTEGER PRIMARY KEY, _data TEXT, _size INTEGER, date_modified INTEGER)")
    conn.execute("INSERT INTO files VALUES (80, '/storage/emulated/0/a.jpg', 1, 1)")
    conn.commit()
    conn.close()
    with open(ms, "rb") as fh:
        archive["data/data/com.google.android.providers.media.module/databases/external.db"] = fh.read()

    paths = {
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_adapter": _IdAdapter(),
        "_app_base_ui_path": APP_BASE,
        "_ui_paths": {},
    }
    rows = _parser("files_by_google_thumbs").run(paths)
    assert len(rows) == 1
    assert rows[0]["link_source"] == glide.SOURCE_ID_ABSENT
    assert rows[0]["kind"] == glide.SCAN
    assert rows[0]["path"] == ""


def test_likely_viewer_content_match_absent_id(tmp_path):
    """Unlinked screen-sized leftover matching an absent-id scan → inferred row."""
    import files_by_google_glide as glide

    # Proven viewer for a live file (screen gate only)
    live_uri = "content://media/external/file/80"
    live_size, live_mt = 121036, 1794609164000
    live_slash = f"{live_size}/{live_mt}".encode()
    live_scan = _glide_name(b"", live_uri, (640, 640), b"")
    live_viewer = _glide_name(live_slash, live_uri, (1080, 1920), glide.FIT_CENTER)

    # Absent id: scan only (Glide), plus unlinked "viewer-like" leftover of same picture
    gone_uri = "content://media/external/file/999"
    gone_scan = _glide_name(b"", gone_uri, (640, 640), b"")
    leftover = "b" * 64

    archive = {
        f"{CACHE}/journal": (
            "libcore.io.DiskLruCache\n1\n1\n1\n\n"
            f"CLEAN {live_scan} 1\nCLEAN {live_viewer} 1\n"
            f"CLEAN {gone_scan} 1\nCLEAN {leftover} 1\n"
        ).encode(),
        f"{CACHE}/{live_scan}.0": _jpeg(400, 300, seed=1),
        f"{CACHE}/{live_viewer}.0": _jpeg_scaled(1080, 810, seed=1),
        f"{CACHE}/{gone_scan}.0": _jpeg(400, 300, seed=7),
        f"{CACHE}/{leftover}.0": _jpeg_scaled(1080, 810, seed=7),  # same picture as gone_scan
        PHOTO_UI: b"x",
    }
    db = str(tmp_path / "files_master_database")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE files_master_table(id INTEGER PRIMARY KEY, media_store_id INTEGER,
            root_path TEXT, root_relative_file_path TEXT, file_name TEXT, size INTEGER,
            file_date_modified_ms INTEGER, storage_location INTEGER, mime_type TEXT,
            media_type INTEGER, uri TEXT, is_hidden INTEGER, title TEXT, artist TEXT,
            album TEXT, parent_folder_name TEXT);
        CREATE TABLE file_open_info_table(file_id INTEGER, file_open_time_stamp_ms INTEGER);
        INSERT INTO files_master_table VALUES (7, 80, '/storage/emulated/0',
            'Download/sample-birch-400x300.jpg', 'sample-birch-400x300.jpg', 121036,
            1794609164000, 0, 'image/jpeg', 1, 'content://media/external/file/80',
            0, 'sample-birch', NULL, NULL, 'Download');
    """)
    conn.commit()
    conn.close()
    with open(db, "rb") as fh:
        archive[f"{APP_BASE}/databases/files_master_database"] = fh.read()

    ms = str(tmp_path / "external.db")
    conn = sqlite3.connect(ms)
    conn.execute("CREATE TABLE files (_id INTEGER PRIMARY KEY, _data TEXT, _size INTEGER, date_modified INTEGER)")
    conn.execute(
        "INSERT INTO files VALUES (80, '/storage/emulated/0/Download/sample-birch-400x300.jpg', 121036, ?)",
        (live_mt // 1000,))
    conn.commit()
    conn.close()
    with open(ms, "rb") as fh:
        archive["data/data/com.google.android.providers.media.module/databases/external.db"] = fh.read()

    paths = {
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_adapter": _IdAdapter(),
        "_app_base_ui_path": APP_BASE,
        "_ui_paths": {},
    }
    rows = _parser("files_by_google_likely_viewer").run(paths)
    assert len(rows) == 1
    row = rows[0]
    assert row["cache_key"] == leftover
    assert row["confidence"] == "Inferred"
    assert row["link_method"] == glide.SOURCE_CONTENT_MATCH
    assert row["media_store_id"] == 999
    assert row["scan_link_source"] == glide.SOURCE_ID_ABSENT
    assert "999" in row["path"]
    assert row["paired_scan_thumb"].endswith(f"{gone_scan}.0")
    # Not mixed into Glide-proven Full-screen Views
    proven = _parser("files_by_google_viewer_thumbs").run(paths)
    assert all(r["cache_key"] != leftover for r in proven)
    # Omitted from Thumbnail Cache Unknown
    thumbs = _parser("files_by_google_thumbs").run(paths)
    assert all(r["cache_key"] != leftover for r in thumbs)


def test_likely_viewer_empty_without_proven_viewer(tmp_path):
    """No screen gate → nothing inferred (even if a large leftover exists)."""
    uri = "content://media/external/file/999"
    scan = _glide_name(b"", uri, (640, 640), b"")
    leftover = "c" * 64
    archive = {
        f"{CACHE}/journal": (
            "libcore.io.DiskLruCache\n1\n1\n1\n\n"
            f"CLEAN {scan} 1\nCLEAN {leftover} 1\n"
        ).encode(),
        f"{CACHE}/{scan}.0": _jpeg(400, 300, seed=3),
        f"{CACHE}/{leftover}.0": _jpeg(1080, 810, seed=3),
    }
    db = str(tmp_path / "files_master_database")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE files_master_table(id INTEGER PRIMARY KEY, media_store_id INTEGER,
            root_path TEXT, root_relative_file_path TEXT, file_name TEXT, size INTEGER,
            file_date_modified_ms INTEGER, storage_location INTEGER, mime_type TEXT,
            media_type INTEGER, uri TEXT, is_hidden INTEGER, title TEXT, artist TEXT,
            album TEXT, parent_folder_name TEXT);
        CREATE TABLE file_open_info_table(file_id INTEGER, file_open_time_stamp_ms INTEGER);
    """)
    conn.commit()
    conn.close()
    with open(db, "rb") as fh:
        archive[f"{APP_BASE}/databases/files_master_database"] = fh.read()
    ms = str(tmp_path / "external.db")
    conn = sqlite3.connect(ms)
    conn.execute("CREATE TABLE files (_id INTEGER PRIMARY KEY, _data TEXT, _size INTEGER, date_modified INTEGER)")
    conn.execute("INSERT INTO files VALUES (80, '/a.jpg', 1, 1)")
    conn.commit()
    conn.close()
    with open(ms, "rb") as fh:
        archive["data/data/com.google.android.providers.media.module/databases/external.db"] = fh.read()

    paths = {
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_adapter": _IdAdapter(),
        "_app_base_ui_path": APP_BASE,
        "_ui_paths": {},
    }
    assert _parser("files_by_google_likely_viewer").run(paths) == []


# ── Likely Full-screen Views: the gates (2026-11-15 review fixes) ──────────

def _likely_case(tmp_path, *, absent=(), present_scan_seed=None, leftovers=()):
    """A case with one live, Glide-proven viewer (screen box 1080×1920),
    scan-only files whose media-store id is gone (*absent*: (id, seed,
    [scan sizes])), optionally a second live indexed file whose scan shows
    picture *present_scan_seed*, and unlinked *leftovers* ((key, w, h, seed,
    base_w, base_h))."""
    import files_by_google_glide as glide

    live_uri = "content://media/external/file/80"
    live_size, live_mt = 121036, 1794609164000
    live_scan = _glide_name(b"", live_uri, (640, 640), b"")
    live_viewer = _glide_name(f"{live_size}/{live_mt}".encode(), live_uri, (1080, 1920),
                              glide.FIT_CENTER)
    archive = {
        f"{CACHE}/{live_scan}.0": _jpeg(400, 300, seed=1),
        f"{CACHE}/{live_viewer}.0": _jpeg_scaled(1080, 810, seed=1),
        PHOTO_UI: b"x",
    }
    order = [live_scan, live_viewer]
    for mid, seed, sizes in absent:
        uri = f"content://media/external/file/{mid}"
        for (w, h), req in zip(sizes, ((640, 640), (256, 256))):
            name = _glide_name(b"", uri, req, b"")
            archive[f"{CACHE}/{name}.0"] = _jpeg_scaled(w, h, seed, *sizes[0])
            order.append(name)
    index_rows = [(7, 80, "Download/sample-birch-400x300.jpg", live_size, live_mt)]
    ms_rows = [(80, "/storage/emulated/0/Download/sample-birch-400x300.jpg", live_size, live_mt // 1000)]
    if present_scan_seed is not None:
        uri2 = "content://media/external/file/81"
        scan2 = _glide_name(b"", uri2, (640, 640), b"")
        archive[f"{CACHE}/{scan2}.0"] = _jpeg(400, 300, seed=present_scan_seed)
        order.append(scan2)
        index_rows.append((8, 81, "Pictures/copy.jpg", 5555, 1794600000000))
        ms_rows.append((81, "/storage/emulated/0/Pictures/copy.jpg", 5555, 1794600000))
    for key, w, h, seed, bw, bh in leftovers:
        archive[f"{CACHE}/{key}.0"] = _jpeg_scaled(w, h, seed, bw, bh)
        order.append(key)
    archive[f"{CACHE}/journal"] = ("libcore.io.DiskLruCache\n1\n1\n1\n\n"
                                   + "".join(f"CLEAN {n} 1\n" for n in order)).encode()

    import tempfile as _tf
    tmp_path = type(tmp_path)(_tf.mkdtemp(dir=tmp_path))
    db = str(tmp_path / "files_master_database")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE files_master_table(id INTEGER PRIMARY KEY, media_store_id INTEGER,
            root_path TEXT, root_relative_file_path TEXT, file_name TEXT, size INTEGER,
            file_date_modified_ms INTEGER, storage_location INTEGER, mime_type TEXT,
            media_type INTEGER, uri TEXT, is_hidden INTEGER, title TEXT, artist TEXT,
            album TEXT, parent_folder_name TEXT);
        CREATE TABLE file_open_info_table(file_id INTEGER, file_open_time_stamp_ms INTEGER);
    """)
    for iid, mid, rel, size, mt in index_rows:
        conn.execute("INSERT INTO files_master_table (id, media_store_id, root_path, "
                     "root_relative_file_path, size, file_date_modified_ms, uri) "
                     "VALUES (?, ?, '/storage/emulated/0', ?, ?, ?, ?)",
                     (iid, mid, rel, size, mt, f"content://media/external/file/{mid}"))
    conn.commit()
    conn.close()
    with open(db, "rb") as fh:
        archive[f"{APP_BASE}/databases/files_master_database"] = fh.read()
    ms = str(tmp_path / "external.db")
    conn = sqlite3.connect(ms)
    conn.execute("CREATE TABLE files (_id INTEGER PRIMARY KEY, _data TEXT, _size INTEGER, date_modified INTEGER)")
    conn.executemany("INSERT INTO files VALUES (?, ?, ?, ?)", ms_rows)
    conn.commit()
    conn.close()
    with open(ms, "rb") as fh:
        archive["data/data/com.google.android.providers.media.module/databases/external.db"] = fh.read()
    return {
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_adapter": _IdAdapter(),
        "_app_base_ui_path": APP_BASE,
        "_ui_paths": {},
    }


def _likely(paths):
    return _parser("files_by_google_likely_viewer").run(paths)


def test_likely_viewer_not_paired_to_a_present_copy_when_true_scan_is_gone(tmp_path):
    # Real-image finding (Poco): the viewer leftover's own scan is gone and
    # another, still-indexed file shows the same picture. Pairing would name
    # the wrong file as "likely viewed".
    paths = _likely_case(tmp_path, present_scan_seed=7,
                         leftovers=[("d" * 64, 1080, 810, 7, 400, 300)])
    assert _likely(paths) == []


def test_likely_viewer_skips_when_two_files_show_the_picture(tmp_path):
    paths = _likely_case(tmp_path, absent=[(999, 7, [(400, 300)])], present_scan_seed=7,
                         leftovers=[("d" * 64, 1080, 810, 7, 400, 300)])
    assert _likely(paths) == []
    paths = _likely_case(tmp_path, absent=[(998, 7, [(400, 300)]), (999, 7, [(400, 300)])],
                         leftovers=[("d" * 64, 1080, 810, 7, 400, 300)])
    assert _likely(paths) == []


def test_likely_viewer_one_files_two_scans_are_not_rivals(tmp_path):
    # A file can have a 640 and a 256 scan; that is one file, not two.
    paths = _likely_case(tmp_path, absent=[(999, 7, [(400, 300), (256, 192)])],
                         leftovers=[("d" * 64, 1080, 810, 7, 400, 300)])
    rows = _likely(paths)
    assert [r["media_store_id"] for r in rows] == [999]
    assert rows[0]["runner_up_distance"] is None or rows[0]["runner_up_distance"] > 0.10


def test_likely_viewer_screen_gate_uses_the_requested_box(tmp_path):
    # Proven viewer: requested box 1080×1920, output 1080×810. A square
    # 810×810 image shares a side with that OUTPUT but cannot be a FitCenter
    # fill of the box, so it must not pass (old gate let it through).
    paths = _likely_case(tmp_path, absent=[(999, 7, [(300, 300)])],
                         leftovers=[("e" * 64, 810, 810, 7, 300, 300)])
    assert _likely(paths) == []
    # The same picture filling the box width does pass.
    paths = _likely_case(tmp_path, absent=[(999, 7, [(300, 300)])],
                         leftovers=[("e" * 64, 1080, 1080, 7, 300, 300)])
    assert [r["media_store_id"] for r in _likely(paths)] == [999]


def test_likely_viewer_flat_picture_not_matched(tmp_path):
    from PIL import Image
    paths = _likely_case(tmp_path, absent=[(999, 7, [(400, 300)])])
    flat = Image.new("RGB", (400, 300), (200, 30, 30))
    big = Image.new("RGB", (1080, 810), (200, 30, 30))
    names = paths["_zip_names"]
    scan = next(n for n in names if n.endswith(".0") and
                _glide_name(b"", "content://media/external/file/999", (640, 640), b"") in n)
    store = {n: paths["_read_zip_bytes"](n) for n in names}
    for name, im in ((scan, flat), (f"{CACHE}/{'f' * 64}.0", big)):
        buf = io.BytesIO()
        im.save(buf, format="JPEG")
        store[name] = buf.getvalue()
    j = f"{CACHE}/journal"
    store[j] = store[j] + f"CLEAN {'f' * 64} 1\n".encode()
    paths["_zip_names"] = list(store)
    paths["_read_zip_bytes"] = store.get
    assert _likely(paths) == []
