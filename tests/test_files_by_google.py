"""Files by Google reports: index with open counts, opens, Safe folder, sessions, scans."""
import importlib.util
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHOTO_UI = "data/media/0/Download/sample-birch-400x300.jpg"
SAFE_UI = "data/data/com.google.android.apps.nbu.files/files/.FilesByGoogle/sample-hut-400x300.png"


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
        CREATE TABLE scan_log_table(id INTEGER PRIMARY KEY AUTOINCREMENT, start_timestamp INTEGER NOT NULL DEFAULT 0,
            end_timestamp INTEGER NOT NULL DEFAULT 0, updated_items_count INTEGER NOT NULL DEFAULT 0,
            scan_type INTEGER NOT NULL, scan_result INTEGER NOT NULL DEFAULT 0);
        INSERT INTO files_master_table VALUES (7, 80, '/storage/emulated/0', 'Download/sample-birch-400x300.jpg',
            'sample-birch-400x300.jpg', 121036, 1794609164000, 0, 'image/jpeg', 1,
            'content://media/external/file/80', 0, 'sample-birch-400x300', NULL, NULL, 'Download');
        INSERT INTO files_master_table VALUES (19, 92, '/storage/emulated/0', 'Download/sample-5s.mp4',
            'sample-5s.mp4', 2848208, 1794609461000, 0, 'video/mp4', 3,
            'content://media/external/file/92', 0, 'sample-5s', NULL, NULL, 'Download');
        INSERT INTO file_open_info_table VALUES (7, 1794612530779), (7, 1794612600000), (19, 1794612612059);
        INSERT INTO scan_log_table VALUES (3, 1794609178924, 1794609178996, 0, 3, 1);
    """)
    conn.commit()
    return conn


def _paths(tmp_path, **files):
    return {**files, "_zip_names": [PHOTO_UI, SAFE_UI], "_read_zip_bytes": lambda n: b"x",
            "_ui_paths": {"sessions": "data/data/com.google.android.apps.nbu.files/files/activityTimeLogStoreName.pb"}}


def test_index_counts_opens_and_links_present_files(tmp_path):
    db = str(tmp_path / "files_master_database")
    _master(db).close()
    rows = _parser("files_by_google_index").run(_paths(tmp_path, master=db))
    birch, video = rows
    assert birch["path"] == "/storage/emulated/0/Download/sample-birch-400x300.jpg"
    assert (birch["times_opened"], birch["last_opened"], birch["media_type"]) == (2, 1794612600000, "Image")
    assert birch["file_link"] == PHOTO_UI and video["file_link"] == ""
    assert video["media_type"] == "Video" and video["raw_file_id"] == 19


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


def test_scans(tmp_path):
    db = str(tmp_path / "files_master_database")
    _master(db).close()
    rows = _parser("files_by_google_scans").run(_paths(tmp_path, master=db))
    assert rows == [{"start": 1794609178924, "end": 1794609178996, "duration_s": 0.072, "updated_items": 0,
                     "scan_type": 3, "result": 1, "raw_scan_id": 3}]


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
