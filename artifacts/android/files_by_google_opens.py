"""Files opened in Files by Google's viewer (file_open_info_table).

files_master_database, table file_open_info_table (file_id, file_open_time_stamp_ms):
one row each time a file was opened in Files by Google's viewer. file_id is
files_master_table.id, declared with ON DELETE CASCADE, so removing a file's
index row (for example by moving it to the Safe folder) deletes its open
records too. In the lab (2026-11-13) the stored time was 0.4–0.6 s after the
viewer screen appeared in Usage Stats.
"""

name = "Opened Files"
app_group_label = "Google Files"
group_sort_key = 1
warning = (
    "An open record shows the file was opened in Files by Google's viewer at "
    "that time. It does not show how long it was viewed, or that a video was "
    "played. Moving a file to the Safe folder deletes its open records; a "
    "full-screen thumbnail made earlier can still appear in Full-screen Views."
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
timeline_fields = {
    "opened": {
        "meaning": "File opened in Files by Google's viewer",
        "app_name": "Files by Google", "event_name": "Opened in viewer",
        "detail": ["path", "mime_type"],
        "caution": "Not how long it was viewed, and not that a video was played.",
    },
}
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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'opened_files_deleted',
  'name': 'Opened Files — Deleted Open Records',
  'description': "Deleted records from Files by Google's files_master_database (table "
                 'file_open_info_table): one row each time a file was opened in Files, with '
                 'the time to the millisecond and the file (file_id, a files_master_table id). '
                 'A deleted record is an open no longer in the history. Live opens are in '
                 'Opened Files.\n'
                 '\n'
                 'Each row is one record as found in the file_open_info_table table, never '
                 'joined to another table (ASSUMPTIONS.md A-95): ids are the stored ids. '
                 'Status says Deleted (no longer in the table), Changed (an earlier version of '
                 'a row still there, listed only when one of the columns that matter changed, '
                 'with the current value beside it as "(now)") or an unfinished write/delete '
                 'under a hot journal. How found says how each record was recovered and how '
                 'sure that is; a record found by pattern has no row id. Values are as stored: '
                 'no code is turned into a word here unless it was checked against a test '
                 'image.\n'
                 '\n'
                 'file_open_time_stamp_ms is Unix milliseconds, as in Opened Files.\n'
                 '\n'
                 "This table has no id column of its own: SQLite's row id is used, and can be "
                 'handed to a new row, so a record is tied to a live row by its file and time '
                 'together ("tied to live row" in how_found).\n'
                 '\n'
                 'linked_file: where the files_master_table row this open names was found, '
                 'live or recovered (that table has no deleted report of its own; only the id '
                 'is compared; its path is in Opened Files for a live file). '
                 'files_master_table.id is AUTOINCREMENT, so it is not handed out again; the '
                 "table drops a file's row when the file goes (sqlite_sequence: 745 ids for "
                 '434 live files on CTF25), and its opens go with it (ON DELETE CASCADE), so a '
                 'recovered open can name a file that is gone.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman Android 14 and CTF25 Android 15 images: no deleted open record (4 and 43 live).',
  'timestamp_fields': {'file_open_time_stamp_ms': 'ms'},
  'core_fields': ['status',
                  'file_open_time_stamp_ms',
                  'file_id',
                  'linked_file',
                  'how_found',
                  'copies'],
  'file_key': 'master',
  'table': 'file_open_info_table',
  'label': 'File open',
  'fields': {'file_id': 'file_id', 'file_open_time_stamp_ms': 'file_open_time_stamp_ms'},
  'identity': ['file_id', 'file_open_time_stamp_ms'],
  'stable_id': ['file_id', 'file_open_time_stamp_ms'],
  'version_fields': ['file_id', 'file_open_time_stamp_ms'],
  'links': [{'name': 'linked_file',
             'what': 'file',
             'column': 'file_id',
             'table': 'files_master_table',
             'key': 'id'}]}]
