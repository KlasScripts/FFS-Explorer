"""Files opened in Files by Google's viewer (file_open_info_table).

files_master_database, table file_open_info_table (file_id, file_open_time_stamp_ms):
one row each time a file was opened in Files by Google's viewer. file_id is
files_master_table.id, declared with ON DELETE CASCADE, so removing a file's
index row (for example by moving it to the Safe folder) deletes its open
records too. In the lab (2026-11-13) the stored time was 0.4–0.6 s after the
viewer screen appeared in Usage Stats.
"""

name = "Files by Google - Opened Files"
app_group_label = "Files by Google"
group_sort_key = 2
warning = (
    "An open record shows the file was opened in Files by Google's viewer at "
    "that time. It does not show how long it was viewed, or that a video was "
    "played. Moving a file to the Safe folder deletes its open records."
)
description = (
    "Each time a file was opened in Files by Google's viewer, with the time "
    "to the millisecond. Path and details come from Files by Google's own "
    "index row for the file; an open whose index row is missing is shown with "
    "its stored file id."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {"master": "databases/files_master_database"}
optional_files = {
    "master_wal": "databases/files_master_database-wal",
    "master_shm": "databases/files_master_database-shm",
}
timestamp_fields = {"opened": "ms"}
core_fields = ["opened", "path", "mime_type", "size"]
media_fields = ["file_link"]
record_source = {
    "label": "Index row",
    "file_key": "master",
    "table": "files_master_table",
    "rowid_fields": ["raw_file_id"],
    "presence_fields": ["raw_file_id"],
}


def run(paths):
    from artifact_runner import archive_has, device_path_to_ui_path, log_note, open_db_readonly

    conn = open_db_readonly(paths["master"])
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "file_open_info_table" not in tables:
            log_note("files_master_database has no file_open_info_table; nothing read")
            return []
        rows = []
        for r in conn.execute(
                "SELECT o.file_id, o.file_open_time_stamp_ms, m.id AS master_id, m.root_path, "
                "m.root_relative_file_path, m.mime_type, m.size FROM file_open_info_table o "
                "LEFT JOIN files_master_table m ON m.id = o.file_id ORDER BY o.file_open_time_stamp_ms"):
            if r["master_id"] is None:
                path = f"[no index row — file_id={r['file_id']}]"
                ui = ""
            else:
                root = (r["root_path"] or "").rstrip("/")
                rel = (r["root_relative_file_path"] or "").lstrip("/")
                path = f"{root}/{rel}" if root else rel
                ui = device_path_to_ui_path(path)
            rows.append({
                "opened": r["file_open_time_stamp_ms"],
                "path": path,
                "mime_type": r["mime_type"] or "",
                "size": r["size"],
                "file_id": r["file_id"],
                "file_link": ui if ui and archive_has(paths, ui) else "",
                "raw_file_id": r["master_id"],
            })
    finally:
        conn.close()
    return rows
