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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'safe_folder_deleted',
  'name': 'Safe Folder — Deleted & Changed Records',
  'description': "Deleted and changed records from Files by Google's safe_folder_db (table "
                 'safe_folder_metadata_table): a file the user moved into the Safe Folder, '
                 'with where it was before (original_file_root_relative_path) and where it is '
                 'kept now. A deleted record is a file taken out of the Safe Folder or deleted '
                 'from it. Live records are in Safe Folder.\n'
                 '\n'
                 'Each row is one record as found in the safe_folder_metadata_table table, '
                 'never joined to another table (ASSUMPTIONS.md A-95): ids are the stored ids. '
                 'Status says Deleted (no longer in the table), Changed (an earlier version of '
                 'a row still there, listed only when one of the columns that matter changed, '
                 'with the current value beside it as "(now)") or an unfinished write/delete '
                 'under a hot journal. How found says how each record was recovered and how '
                 'sure that is; a record found by pattern has no row id. Values are as stored: '
                 'no code is turned into a word here unless it was checked against a test '
                 'image.\n'
                 '\n'
                 'file_last_modified_timestamp is shown as in Safe Folder (Unix seconds). '
                 'original_file_storage_location 1 is internal storage (/storage/emulated/0), '
                 'as in Safe Folder.\n'
                 '\n'
                 'Ids: id is AUTOINCREMENT (checked 2026-11-28 on the Josh Hickman Android 14 and CTF25 Android 15 images), so SQLite does not hand out an id '
                 'again, but an app that re-inserts rows with their old ids (a restore from '
                 'backup) can still put a different record under the same id; an earlier '
                 'version found in the WAL and a live row with the same id were stored at '
                 'different times; a record found by pattern has no row id and cannot be '
                 'linked; several copies of one record can carry the same id.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman Android 14 and CTF25 Android 15 images: no deleted or changed record (1 live on Josh Hickman and '
                 'CTF25); sqlite_sequence says 1 id was ever handed out on each.',
  'timestamp_fields': {'file_last_modified_timestamp': 's'},
  'core_fields': ['status',
                  'original_path',
                  'safe_folder_path',
                  'file_last_modified_timestamp',
                  'changed_fields',
                  'how_found',
                  'copies'],
  'file_key': 'safe',
  'table': 'safe_folder_metadata_table',
  'label': 'Safe Folder file',
  'fields': {'id': 'record_id',
             'original_file_root_relative_path': 'original_path',
             'original_file_storage_location': 'original_file_storage_location',
             'file_safe_folder_path': 'safe_folder_path',
             'file_last_modified_timestamp': 'file_last_modified_timestamp'},
  'version_fields': ['original_file_root_relative_path',
                     'original_file_storage_location',
                     'file_safe_folder_path'],
  'links': []}]
