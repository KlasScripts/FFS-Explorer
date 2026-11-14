"""Files by Google's own index of shared-storage files (files_master_table).

com.google.android.apps.nbu.files/databases/files_master_database, table
files_master_table: one row per media store file Files by Google has scanned.
In the lab (Files 1.13157, Android 16 emulator, 2026-11-13) a row was written
for every downloaded file within seconds, without the app being opened, and
removed when the file was moved to the Safe folder. Times Opened and Last
Opened come from file_open_info_table for the same id.
"""

name = "Files by Google - Indexed Files"
app_group_label = "Files by Google"
group_sort_key = 1
description = (
    "Files by Google's index of shared-storage files. A row means Files by "
    "Google scanned the file, not that anyone opened the app or saw the file: "
    "in testing, new downloads were indexed within seconds with the app never "
    "opened. Times Opened and Last Opened are its own records of the file "
    "being opened in its viewer. Media type is the media store constant."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {"master": "databases/files_master_database"}
optional_files = {
    "master_wal": "databases/files_master_database-wal",
    "master_shm": "databases/files_master_database-shm",
}
timestamp_fields = {"file_modified": "ms", "last_opened": "ms"}
core_fields = ["path", "size", "file_modified", "media_type", "times_opened", "last_opened"]
media_fields = ["file_link"]
record_source = {
    "label": "Index row",
    "file_key": "master",
    "table": "files_master_table",
    "rowid_fields": ["raw_file_id"],
}

MEDIA_TYPES = {0: "None", 1: "Image", 2: "Audio", 3: "Video", 4: "Playlist", 5: "Subtitle", 6: "Document"}


def full_path(root: str, relative: str) -> str:
    root = (root or "").rstrip("/")
    relative = (relative or "").lstrip("/")
    return f"{root}/{relative}" if root else relative


def run(paths):
    from artifact_runner import archive_has, device_path_to_ui_path, open_db_readonly

    conn = open_db_readonly(paths["master"])
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        opens = {}
        if "file_open_info_table" in tables:
            for r in conn.execute("SELECT file_id, COUNT(*) AS n, MAX(file_open_time_stamp_ms) AS last "
                                  "FROM file_open_info_table GROUP BY file_id"):
                opens[r["file_id"]] = (r["n"], r["last"])
        rows = []
        for r in conn.execute("SELECT * FROM files_master_table ORDER BY id"):
            path = full_path(r["root_path"], r["root_relative_file_path"])
            ui = device_path_to_ui_path(path)
            count, last = opens.get(r["id"], (0, None))
            rows.append({
                "path": path,
                "file_name": r["file_name"],
                "size": r["size"],
                "file_modified": r["file_date_modified_ms"],
                "mime_type": r["mime_type"] or "",
                "media_type": MEDIA_TYPES.get(r["media_type"], "" if r["media_type"] is None else str(r["media_type"])),
                "times_opened": count,
                "last_opened": last,
                "hidden": "Yes" if r["is_hidden"] else "No",
                "folder": r["parent_folder_name"] or "",
                "media_store_id": r["media_store_id"],
                "uri": r["uri"],
                "file_link": ui if archive_has(paths, ui) else "",
                "raw_file_id": r["id"],
            })
    finally:
        conn.close()
    return rows
