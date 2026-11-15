"""Files moved into Files by Google's Safe folder (safe_folder_db).

databases/safe_folder_db, table safe_folder_metadata_table: one row per file in
the Safe folder, with the path it was moved from, its path in the Safe folder
(files/.FilesByGoogle/<name> in the app's own storage) and the file's modified
time in Unix seconds. In the lab (Files 1.13157, 2026-11-13) the Safe folder
copies were byte-for-byte identical to the originals: not encrypted.
"""

name = "Safe Folder"
app_group_label = "Google Files"
group_sort_key = 4
warning = (
    "Safe folder files are hidden behind a PIN in the app but were not "
    "encrypted in the version tested (1.13157): the copy in Files by Google's "
    "storage is the original file. File Modified is the file's own modified "
    "time, not when it was moved; the copy's change time (ctime) and the media "
    "store log are closer to the move."
)
description = (
    "Files in Files by Google's Safe folder: where each was moved from, and "
    "where the copy is. Copy In Archive links to the copy when the extraction "
    "has it."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {"safe": "databases/safe_folder_db"}
optional_files = {
    "safe_wal": "databases/safe_folder_db-wal",
    "safe_shm": "databases/safe_folder_db-shm",
}
timestamp_fields = {"file_modified": "s"}
core_fields = ["original_path", "safe_folder_path", "file_modified", "copy_in_archive"]
media_fields = ["copy_link"]
record_source = {
    "label": "Safe folder row",
    "file_key": "safe",
    "table": "safe_folder_metadata_table",
    "rowid_fields": ["raw_id"],
}

_STORAGE = {1: "/storage/emulated/0"}


def run(paths):
    from artifact_runner import archive_has, device_path_to_ui_path, log_note, open_db_readonly

    conn = open_db_readonly(paths["safe"])
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "safe_folder_metadata_table" not in tables:
            log_note("safe_folder_db has no safe_folder_metadata_table; nothing read")
            return []
        rows = []
        for r in conn.execute("SELECT * FROM safe_folder_metadata_table ORDER BY id"):
            relative = r["original_file_root_relative_path"] or ""
            root = _STORAGE.get(r["original_file_storage_location"])
            original = f"{root}/{relative.lstrip('/')}" if root else relative
            safe_path = r["file_safe_folder_path"] or ""
            ui = device_path_to_ui_path(safe_path)
            present = archive_has(paths, ui)
            rows.append({
                "original_path": original,
                "safe_folder_path": safe_path,
                "file_modified": r["file_last_modified_timestamp"],
                "copy_in_archive": "Yes" if present else "No",
                "storage_location": r["original_file_storage_location"],
                "copy_link": ui if present else "",
                "raw_id": r["id"],
            })
    finally:
        conn.close()
    return rows
