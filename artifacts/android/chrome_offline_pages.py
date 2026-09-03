name = "Chrome Offline Pages"
app_group_label = "Chrome"
group_sort_key = 6
description = (
    "Full-page offline snapshots (.mhtml -- complete HTML + inline "
    "resources, openable/readable directly) Chrome automatically saves "
    "for recently viewed tabs, from app_chrome/Default/Offline Pages/"
    "metadata/OfflinePages.db (cache/Offline Pages/archives/*.mhtml holds "
    "the actual page content this metadata points at). client_namespace "
    "'last_n' on every real row seen so far is Chrome's own \"last N "
    "tabs\" automatic snapshot policy -- this is NOT something the user "
    "explicitly saved or requested; it is triggered by ordinary tab "
    "browsing. "
    "Confirmed real and common on an actual device, not a one-off: the "
    "Joshua Hickman documented Android 14 test image has 8 total archives "
    "across all three of its installed Chromium-family browsers (4 "
    "Chrome, 2 Brave, 2 Microsoft Edge -- only Chrome's own is parsed "
    "here, same com.android.chrome-only scope as this project's other "
    "Chrome reports), including a full snapshot of a THIRD-PARTY app's "
    "sign-in page (Wickr's OAuth login flow, complete with client_id/"
    "code_challenge tokens in the URL) that the user almost certainly did "
    "not expect Chrome to archive. "
    "Confirmed on a GTLAB run too (google-search-delete-one-row): a "
    "snapshot of a Google search results page was saved even though "
    "neither GTLAB script step asked for one -- two otherwise-identical "
    "GTLAB search runs (chrome-bbc-google-001, google-search-clear-"
    "history) produced ZERO archives, so this appears to fire "
    "intermittently/heuristically, not on every page view -- absence "
    "here is not evidence a page was never viewed, only that it was not "
    "snapshotted. "
    "archive_ui_path is constructed from the database's own file_path "
    "column, which is stored using the device's on-disk /data/user/0/... "
    "convention rather than this archive's own data/data/... ui_path -- "
    "the com.android.chrome/ segment is used as the split point and "
    "verified byte-for-byte to resolve to the real archive file on both "
    "the real device image and the GTLAB run above before being trusted "
    "here. "
    "Deleted-row recovery not yet checked -- no GTD run available that "
    "deletes an Offline Pages entry specifically, so recoverable_tables "
    "is deliberately left undeclared rather than guessed at; this "
    "database uses the classic rollback-journal mode (confirmed via a "
    "real -journal sidecar on the GTLAB runs), not WAL, so it would need "
    "its own check rather than assuming the History-table findings "
    "elsewhere in this project apply here too."
)
app_path = "data/data/com.android.chrome"
files = {
    "metadata_db": "app_chrome/Default/Offline Pages/metadata/OfflinePages.db",
}
optional_files = {
    "metadata_db_journal": "app_chrome/Default/Offline Pages/metadata/OfflinePages.db-journal",
}

timestamp_fields = {
    "creation_time": "webkit_us",
    "last_access_time": "webkit_us",
    "file_missing_time": "webkit_us",
}

media_fields = ["archive_ui_path"]
# What page was snapshotted, its real URL, and when are the essentials;
# client_namespace/access_count/file_size/etc. are useful detail, not
# needed for a first pass.
core_fields = ["title", "online_url", "creation_time"]

# offline_id is confirmed "INTEGER PRIMARY KEY NOT NULL" in the real
# schema (a genuine rowid alias).
record_source = {
    "label": "Offline Page",
    "file_key": "metadata_db",
    "table": "offlinepages_v1",
    "rowid_fields": ["offline_id"],
}


def run(paths):
    import sqlite3

    conn = sqlite3.connect(f'file:{paths["metadata_db"]}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT offline_id, title, online_url, original_url, file_path,
               file_size, creation_time, last_access_time, access_count,
               client_namespace, client_id, request_origin, file_missing_time
        FROM offlinepages_v1
    """).fetchall()
    conn.close()

    base = paths.get("_app_base_ui_path", "")
    out = []
    for r in rows:
        # The db stores the device's own /data/user/0/com.android.chrome/...
        # path, not this archive's data/data/... ui_path convention --
        # rebuild relative to this app's own container base (see
        # description above for the verification this was checked
        # against, not assumed).
        fp = r["file_path"] or ""
        marker = "com.android.chrome/"
        archive_ui_path = f"{base}/{fp.split(marker, 1)[1]}" if marker in fp else ""

        out.append({
            "title": r["title"],
            "online_url": r["online_url"],
            "original_url": r["original_url"],
            "archive_ui_path": archive_ui_path,
            "file_path": fp,
            "file_size": r["file_size"],
            "creation_time": r["creation_time"],
            "last_access_time": r["last_access_time"],
            "access_count": r["access_count"],
            "client_namespace": r["client_namespace"],
            "client_id": r["client_id"],
            "request_origin": r["request_origin"],
            "file_missing_time": r["file_missing_time"],
            "offline_id": r["offline_id"],
        })
    return out
