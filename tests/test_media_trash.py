"""Media Trash: trash time from the expiry, the recovery-log extension, and
the Photos, Files by Google and Usage Stats matches."""
import base64
import hashlib
import importlib.util
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = 86400
TRASHED_AT = 1_793_904_033
EXPIRY = TRASHED_AT + 30 * DAY
PHOTO = b"\xff\xd8 not really a jpeg"
PHOTO_UI = f"data/media/0/Pictures/.trashed-{EXPIRY}-IMG_1.jpg"


def _parser():
    spec = importlib.util.spec_from_file_location(
        "media_trash", os.path.join(ROOT, "artifacts", "android", "media_trash.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        out.append(piece | 0x80 if value else piece)
        if not value:
            return bytes(out)


def _field(number: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _external_db(tmp_path) -> str:
    path = str(tmp_path / "external.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE files (_id INTEGER PRIMARY KEY, _data TEXT, _display_name TEXT, "
        "is_trashed INTEGER, is_pending INTEGER, date_expires INTEGER, "
        "owner_package_name TEXT, mime_type TEXT, _size INTEGER, date_added INTEGER, "
        "date_modified INTEGER)")
    conn.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (81, f"/storage/emulated/0/Pictures/.trashed-{EXPIRY}-IMG_1.jpg", "IMG_1.jpg",
         1, 0, EXPIRY, "com.android.camera2", "image/jpeg", len(PHOTO), 1, 2),
        (82, "/storage/emulated/0/Download/.pending-1800000000-part.bin", "part.bin",
         0, 1, 1800000000, "com.android.chrome", "", 0, 3, 3),
        (83, "/storage/emulated/0/Download/kept.jpg", "kept.jpg",
         0, 0, None, "com.android.chrome", "image/jpeg", 1, 4, 4),
    ])
    conn.commit()
    conn.close()
    return path


def _photos_db(local_media: bool, action_time: int) -> bytes:
    import tempfile
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "gphotos-1.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE local_media (_id INTEGER PRIMARY KEY, media_store_id INTEGER, "
                 "dedup_key TEXT, trash_timestamp INTEGER, purge_timestamp INTEGER, "
                 "filepath TEXT, filename TEXT)")
    conn.execute("CREATE TABLE action_queue (entity_type TEXT, entity_blob BLOB, "
                 "fetch_count INTEGER, schedule_timestamp INTEGER, creation_timestamp INTEGER, "
                 "cancellation_status INTEGER)")
    key = base64.urlsafe_b64encode(hashlib.sha1(PHOTO).digest()).decode().rstrip("=")
    if local_media:
        conn.execute("INSERT INTO local_media VALUES (1, 81, ?, ?, ?, ?, 'x')",
                     (key, TRASHED_AT * 1000, EXPIRY * 1000, "/storage/emulated/0/Pictures/x"))
    blob = _field(2, key.encode()) + _field(2, b"another-file-key-000000000") + _field(4, _field(1, 5))
    conn.execute("INSERT INTO action_queue VALUES (?, ?, 0, 0, ?, 0)",
                 ("com.google.android.apps.photos.trash.delete.move_to_trash_optimistic_action",
                  blob, action_time))
    conn.commit()
    conn.close()
    with open(path, "rb") as f:
        return f.read()


def _run(tmp_path, archive: dict):
    mod = _parser()
    return mod.run({
        "external_db": _external_db(tmp_path),
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_parser_files_dir": str(tmp_path / "parser"),
    })


def test_trash_time_is_expiry_minus_30_days_and_pending_has_none(tmp_path):
    rows = _run(tmp_path, {PHOTO_UI: PHOTO})
    assert [(r["media_store_id"], r["status"]) for r in rows] == [(81, "In trash"), (82, "Pending")]
    trashed, pending = rows
    assert trashed["trashed_time"] == TRASHED_AT
    assert trashed["trashed_time_basis"] == "Expiry minus 30 days"
    assert trashed["original_path"] == "/storage/emulated/0/Pictures/IMG_1.jpg"
    assert trashed["file_link"] == PHOTO_UI
    assert trashed["raw_files_id"] == 81
    assert trashed["expiry_extended"] == ""
    assert pending["trashed_time"] is None and pending["expires"] == 1800000000


def test_photos_action_matched_through_local_media(tmp_path):
    photos = "data/data/com.google.android.apps.photos/databases/gphotos-1.db"
    rows = _run(tmp_path, {PHOTO_UI: PHOTO, photos: _photos_db(True, TRASHED_AT * 1000 + 708)})
    row = rows[0]
    assert row["photos_action_time"] == TRASHED_AT * 1000 + 708
    assert row["photos_action"] == "move_to_trash_optimistic_action"
    assert row["photos_action_files"] == 2
    assert row["photos_match"] == "Photos media store id"


def test_photos_action_matched_by_file_sha1_without_local_media(tmp_path):
    photos = "data/data/com.google.android.apps.photos/databases/gphotos-1.db"
    rows = _run(tmp_path, {PHOTO_UI: PHOTO, photos: _photos_db(False, (TRASHED_AT + 5000) * 1000)})
    assert rows[0]["photos_match"] == "File SHA-1"
    assert rows[0]["photos_action_time"] == (TRASHED_AT + 5000) * 1000


def test_photos_action_in_the_same_second_is_labelled_as_such(tmp_path):
    photos = "data/data/com.google.android.apps.photos/databases/gphotos-1.db"
    rows = _run(tmp_path, {photos: _photos_db(False, TRASHED_AT * 1000 + 400)})
    assert rows[0]["photos_match"] == "Same second only"
    later = tmp_path / "later"
    later.mkdir()
    rows = _run(later, {photos: _photos_db(False, TRASHED_AT * 1000 + 1400)})
    assert rows[0]["photos_action_time"] is None


def test_files_by_google_session_covering_the_trash(tmp_path):
    def stamp(ms):
        return _field(1, ms // 1000) + _field(2, (ms % 1000) * 1_000_000)
    session = _field(1, stamp(TRASHED_AT * 1000 - 50_000)) + _field(2, stamp(TRASHED_AT * 1000 + 600_000))
    other = _field(1, stamp(1000)) + _field(2, stamp(2000))
    log = _field(1, other) + _field(1, session) + _field(2, b"1.13157.1011540316.0-release")
    path = "data/data/com.google.android.apps.nbu.files/files/activityTimeLogStoreName.pb"
    row = _run(tmp_path, {path: log})[0]
    assert (row["files_open_from"], row["files_open_to"]) == (
        TRASHED_AT * 1000 - 50_000, TRASHED_AT * 1000 + 600_000)


def test_recovery_log_extension_and_restore():
    mod = _parser()
    a, b, c = 1796455596, 1797838292, 1799000000
    folder = "/storage/emulated/0/Download"
    extended = mod._trash_history([
        (1, f"{folder}/Example.jpg", 80),
        (2, f"{folder}/.trashed-{a}-Example.jpg", 80),
        (3, f"{folder}/.trashed-{a}-Example.jpg", 80),
        (4, f"{folder}/.trashed-{b}-Example.jpg", 80),
    ])[80]
    assert extended["run"] == [a, b] and extended["earlier_runs"] == 0 and extended["still_trashed"]
    row = mod._row()
    mod._apply_history(row, extended, b)
    assert row["trashed_time"] == a - 30 * DAY
    assert row["expiry_extended"] == "Yes" and row["earlier_expiries"] == str(a)

    restored = mod._trash_history([
        (1, f"{folder}/.trashed-{a}-Example.jpg", 80),
        (2, f"{folder}/Example.jpg", 80),
        (3, f"{folder}/.trashed-{c}-Example.jpg", 80),
    ])[80]
    assert restored["run"] == [c] and restored["earlier_runs"] == 1
    row = mod._row()
    mod._apply_history(row, restored, c)
    assert row["trashed_time"] == c - 30 * DAY and row["expiry_extended"] == "No"
    assert row["trashed_before"] == 1

    row = mod._row()
    mod._apply_history(row, restored, 1_234_567_890)
    assert row["expiry_extended"] == "[current name not in the recovery log]"
    assert row["trashed_time"] == 1_234_567_890 - 30 * DAY


def test_app_on_screen_uses_the_latest_resumed_activity():
    mod = _parser()
    ev = lambda t, kind, pkg: {"event_time": t, "event_type": kind, "package": pkg, "class_name": pkg + ".A"}
    screen = mod._Screen([
        ev(100, "ACTIVITY_RESUMED", "com.whatsapp"),
        ev(200, "ACTIVITY_RESUMED", "com.google.android.apps.photos"),
        ev(1500, "ACTIVITY_RESUMED", "launcher"),
        ev(1600, "ACTIVITY_PAUSED", "com.google.android.apps.photos"),
        ev(3000, "ACTIVITY_PAUSED", "launcher"),
    ])
    assert screen.at(1000, 1999) == "com.google.android.apps.photos, then launcher"
    assert screen.at(1000, 1000) == "com.google.android.apps.photos"
    assert screen.at(50, 50) == "[before the first Usage Stats event]"
    assert screen.at(3500, 3500) == "[after the last Usage Stats event]"
    assert screen.at(3000, 3000) == "[no app on screen]"
    assert mod._Screen([]).at(1, 1) == "[no Usage Stats activity events]"
