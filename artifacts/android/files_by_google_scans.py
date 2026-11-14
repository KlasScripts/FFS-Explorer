"""Files by Google's background scans of shared storage (scan_log_table).

files_master_database, table scan_log_table: one row per scan, with start and
end in Unix ms, the number of items updated, a scan type and a result code.
In the lab (Files 1.13157, 2026-11-13) a type 3 scan ran 6–14 s after each new
download, with the app never opened. The meaning of the type and result codes
was not traced; they are shown as stored.
"""

name = "Files by Google - Background Scans"
app_group_label = "Files by Google"
group_sort_key = 5
description = (
    "Scans Files by Google ran over shared storage. A scan runs on its own "
    "after new files arrive; it does not show the app was opened. Scan Type "
    "and Result are shown as stored (not decoded)."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {"master": "databases/files_master_database"}
optional_files = {
    "master_wal": "databases/files_master_database-wal",
    "master_shm": "databases/files_master_database-shm",
}
timestamp_fields = {"start": "ms", "end": "ms"}
core_fields = ["start", "end", "updated_items", "scan_type", "result"]
record_source = {
    "label": "Scan row",
    "file_key": "master",
    "table": "scan_log_table",
    "rowid_fields": ["raw_scan_id"],
}


def run(paths):
    from artifact_runner import log_note, open_db_readonly

    conn = open_db_readonly(paths["master"])
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "scan_log_table" not in tables:
            log_note("files_master_database has no scan_log_table; nothing read")
            return []
        return [{
            "start": r["start_timestamp"] or None,
            "end": r["end_timestamp"] or None,
            "duration_s": round((r["end_timestamp"] - r["start_timestamp"]) / 1000, 3)
            if r["start_timestamp"] and r["end_timestamp"] else None,
            "updated_items": r["updated_items_count"],
            "scan_type": r["scan_type"],
            "result": r["scan_result"],
            "raw_scan_id": r["id"],
        } for r in conn.execute("SELECT * FROM scan_log_table ORDER BY start_timestamp, id")]
    finally:
        conn.close()
