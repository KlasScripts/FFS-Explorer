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


def _photos_db(local_media: bool, action_time: int | None, name: str = "gphotos-1.db") -> bytes:
    import tempfile
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, name)
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
    if action_time is not None:
        conn.execute("INSERT INTO action_queue VALUES (?, ?, 0, 0, ?, 0)",
                     ("com.google.android.apps.photos.trash.delete.move_to_trash_optimistic_action",
                      blob, action_time))
    conn.commit()
    conn.close()
    with open(path, "rb") as f:
        return f.read()


def _run(tmp_path, archive: dict):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mod = _parser()
    return mod.run({
        "external_db": _external_db(tmp_path),
        "_zip_names": list(archive),
        "_read_zip_bytes": archive.get,
        "_parser_files_dir": str(tmp_path / "parser"),
    })


PHOTOS = "data/data/com.google.android.apps.photos/databases/gphotos-1.db"
APP_LOG = "data/data/com.google.android.providers.media.module/app_logs/"


def test_trash_time_from_the_expiry_with_its_source(tmp_path):
    rows = _run(tmp_path, {PHOTO_UI: PHOTO})
    assert [(r["media_store_id"], r["status"]) for r in rows] == [(81, "In trash")]   # no pending row
    row = rows[0]
    assert row["trashed_time"] == TRASHED_AT
    assert row["trashed_time_source"] == (
        f"external.db files.date_expires {EXPIRY} (2026-12-05 18:40:33 UTC) minus 30 days")
    assert row["original_path"] == "/storage/emulated/0/Pictures/IMG_1.jpg"
    assert row["file_link"] == PHOTO_UI
    assert row["raw_files_id"] == 81
    assert row["expiry_extended"] == "Not known"
    assert row["extended_source"] == "No media store recovery log in this archive"
    assert row["trashed_by"] == "Unknown"


def test_photos_action_matched_through_local_media(tmp_path):
    rows = _run(tmp_path, {PHOTO_UI: PHOTO, PHOTOS: _photos_db(True, TRASHED_AT * 1000 + 708)})
    row = rows[0]
    assert row["photos_action_time"] == TRASHED_AT * 1000 + 708
    assert row["photos_action"] == "move_to_trash_optimistic_action"
    assert row["photos_action_files"] == 2
    assert row["photos_match"] == "Photos media store id"
    assert row["trashed_by"] == "Google Photos"
    assert row["trashed_by_reason"] == (
        "Google Photos gphotos-1.db action_queue: a move to trash queued at "
        "2026-11-05 18:40:33.708 UTC lists this file (the dedup_key Photos holds for media store id 81)")


def test_photos_action_in_an_account_database_is_read(tmp_path):
    account = "data/data/com.google.android.apps.photos/databases/gphotos0.db"
    rows = _run(tmp_path, {PHOTO_UI: PHOTO, PHOTOS: _photos_db(False, None),
                           account: _photos_db(False, TRASHED_AT * 1000 + 300, "gphotos0.db")})
    assert rows[0]["photos_match"] == "File SHA-1"
    assert rows[0]["photos_db"] == "gphotos0.db"


def test_photos_action_for_the_same_content_at_another_time_is_not_this_trash(tmp_path):
    # The key is the content's SHA-1: an identical copy trashed in Photos a
    # day earlier is not this file's trash.
    rows = _run(tmp_path, {PHOTO_UI: PHOTO, PHOTOS: _photos_db(False, (TRASHED_AT - DAY) * 1000)})
    row = rows[0]
    assert row["photos_action_time"] is None
    assert row["photos_match"] == ("[a Photos trash action at 2026-11-04 18:40:33.000 UTC lists "
                                   "the same content, not at this trash]")
    assert row["trashed_by"] == "Unknown"
    rows = _run(tmp_path / "x", {PHOTO_UI: PHOTO, PHOTOS: _photos_db(False, TRASHED_AT * 1000 + 1400)})
    assert rows[0]["photos_match"] == "File SHA-1"     # the next second still counts


def test_time_only_match_needs_a_gone_file_and_an_unexplained_key(tmp_path):
    # No file in the archive: the content is unknown, so time alone links it.
    rows = _run(tmp_path, {PHOTOS: _photos_db(False, TRASHED_AT * 1000 + 400)})
    assert rows[0]["photos_match"] == "Time only (file content unknown)"
    assert rows[0]["trashed_by"] == "Probably Google Photos"
    rows = _run(tmp_path / "late", {PHOTOS: _photos_db(False, TRASHED_AT * 1000 + 2400)})
    assert rows[0]["photos_action_time"] is None
    # The file is there and its key is not in the action: no time-only match.
    other = PHOTO + b"edited"
    rows = _run(tmp_path / "known", {PHOTO_UI: other, PHOTOS: _photos_db(False, TRASHED_AT * 1000 + 400)})
    assert rows[0]["photos_action_time"] is None


def test_files_by_google_screen_covering_the_trash(tmp_path):
    def stamp(ms):
        return _field(1, ms // 1000) + _field(2, (ms % 1000) * 1_000_000)
    session = _field(1, stamp(TRASHED_AT * 1000 - 50_000)) + _field(2, stamp(TRASHED_AT * 1000 + 600_000))
    other = _field(1, stamp(1000)) + _field(2, stamp(2000))
    log = _field(1, other) + _field(1, session) + _field(2, b"1.13157.1011540316.0-release")
    path = "data/data/com.google.android.apps.nbu.files/files/activityTimeLogStoreName.pb"
    row = _run(tmp_path, {path: log})[0]
    assert (row["files_open_from"], row["files_open_to"]) == (
        TRASHED_AT * 1000 - 50_000, TRASHED_AT * 1000 + 600_000)
    assert row["trashed_by"] == "Probably Files by Google"
    assert row["trashed_by_reason"].startswith("Files by Google activityTimeLogStoreName.pb: a Files screen was open")


def test_recovery_log_extension_and_restore():
    mod = _parser()
    a, b, c = 1796455596, 1797838292, 1799000000
    folder = "/storage/emulated/0/Download"
    extended = mod._trash_history([
        (1, f"{folder}/Example.jpg", 80, "log"),
        (2, f"{folder}/.trashed-{a}-Example.jpg", 80, "log"),
        (3, f"{folder}/.trashed-{a}-Example.jpg", 80, "log"),
        (4, f"{folder}/.trashed-{b}-Example.jpg", 80, "log"),
    ])[80]
    assert extended["run"] == [a, b] and extended["earlier_runs"] == 0 and extended["still_trashed"]
    row = mod._row()
    mod._apply_history(row, extended, b)
    assert row["trashed_time"] == a - 30 * DAY
    assert row["expiry_extended"] == "Yes" and row["earlier_expiries"] == str(a)
    assert row["extended_source"] == (
        f"log: the file was named .trashed-{a}- before .trashed-{b}-, with no plain name between")

    restored = mod._trash_history([
        (1, f"{folder}/.trashed-{a}-Example.jpg", 80),
        (2, f"{folder}/Example.jpg", 80),
        (3, f"{folder}/.trashed-{c}-Example.jpg", 80),
    ])[80]
    assert restored["run"] == [c] and restored["earlier_runs"] == 1
    row = mod._row()
    mod._apply_history(row, restored, c)
    assert row["trashed_time"] == c - 30 * DAY and row["expiry_extended"] == "No sign of it"
    assert row["trashed_before"] == 1

    row = mod._row()
    mod._apply_history(row, restored, 1_234_567_890)
    assert row["expiry_extended"] == "Not known"
    assert row["extended_source"].endswith("does not have this file's current name")
    assert row["trashed_time"] == 1_234_567_890 - 30 * DAY


def test_extension_found_from_app_logs_when_the_old_name_is_gone():
    # media-trash-expiry-extend-001: idle maintenance at 07:31:32.581 UTC
    # renamed the file to .trashed-1797838292-; compaction can drop the old
    # name, leaving only the new one in the recovery log.
    mod = _parser()
    log = (b"2026-11-04 23:01:15.526 Scanned internal due to REASON_MOUNTED\n"
           b"2026-12-14 07:31:32.581 Idle maintenance on external, 32 items taking 162ms, 0 stale, 0 expired\n")
    name = APP_LOG + "1793833275526"
    runs = mod._idle_runs({"_zip_names": [name], "_read_zip_bytes": {name: log}.get})
    end = (1797838292 - 7 * DAY) * 1000 + 581
    assert runs == [(end - 162 - 1000, end, name)]
    hist = mod._trash_history([(74, "/s/.trashed-1797838292-Example.jpg", 80, "log")])[80]
    row = mod._row()
    mod._apply_history(row, hist, 1797838292, runs)
    assert row["trashed_time"] is None and row["expiry_extended"] == "Probably"
    assert row["extended_source"].startswith(f"{name}: idle maintenance ran at 2026-12-14 07:31:32.581 UTC")
    assert "Trashed before 2026-11-07 07:31:32 UTC" in row["trashed_time_source"]


def test_app_logs_lines_are_local_time():
    # Poco X7 Pro: the file name is UTC, its first line local time (UTC-4).
    mod = _parser()
    name = APP_LOG + "1758123221916"
    log = (b"2025-09-17 11:33:41.917 Observed non-standard /product/media\n"
           b"2025-09-17 12:35:01.633 Idle maintenance on external, 525 items taking 660ms, 0 stale, 0 expired\n")
    runs = mod._idle_runs({"_zip_names": [name], "_read_zip_bytes": {name: log}.get})
    assert runs[0][1] == 1758126901633     # 2025-09-17 16:35:01.633 UTC


def test_app_on_screen_keeps_every_resumed_activity():
    mod = _parser()
    ev = lambda t, kind, pkg: {"event_time": t, "event_type": kind, "package": pkg, "class_name": pkg + ".A"}
    screen = mod._Screen([
        ev(100, "ACTIVITY_RESUMED", "camera"),
        ev(200, "ACTIVITY_RESUMED", "launcher"),
        ev(300, "ACTIVITY_PAUSED", "launcher"),          # camera is still resumed
        ev(400, "ACTIVITY_PAUSED", "camera"),
        ev(500, "ACTIVITY_RESUMED", "com.google.android.apps.photos"),
        ev(1500, "ACTIVITY_RESUMED", "launcher"),        # resumed before Photos pauses
        ev(1600, "ACTIVITY_PAUSED", "com.google.android.apps.photos"),
        ev(3000, "ACTIVITY_PAUSED", "launcher"),
        ev(3100, "DEVICE_STARTUP", "android"),
        ev(3200, "ACTIVITY_RESUMED", "com.whatsapp"),
    ])
    assert screen.at(350, 350) == "camera"
    assert screen.at(1000, 1999) == "com.google.android.apps.photos, then launcher"
    assert screen.at(1550, 1550) == "launcher + com.google.android.apps.photos"
    assert screen.at(450, 450) == "[no app on screen]"
    assert screen.at(50, 50) == "[before the first Usage Stats event]"
    assert screen.at(3500, 3500) == "[after the last Usage Stats event]"
    assert screen.shows("com.google.android.apps.photos", 1550, 1550) is True
    assert screen.shows("camera", 1550, 1550) is False
    assert mod._Screen([]).at(1, 1) == "[no Usage Stats activity events]"
