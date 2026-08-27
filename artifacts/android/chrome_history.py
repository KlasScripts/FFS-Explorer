name = "Chrome Web History Summary"
description = (
    "Every URL Chrome has visited at least once, from com.android.chrome's "
    "own History database (urls table) -- one row per distinct URL, not "
    "per visit; see the separate \"Chrome Web History Full\" report for "
    "the per-visit timeline. visit_count/typed_count are Chrome's own "
    "running counters, not derived here. "
    "Field-by-field validated against two independent ground truths: "
    "Joshua Hickman's documented Android 14 test image (thebinaryhick.blog) "
    "-- all 6 of its documented non-Incognito Chrome actions on 2024-07-13 "
    "matched exactly by url/title/visit_count/typed_count -- and a GTLAB "
    "Android-emulator coach run (chrome-bbc-google-001). Confirmed absent, "
    "as expected: none of that same ground truth's 4 documented "
    "Incognito-tab actions appear anywhere in this table -- Chromium's "
    "Incognito mode does not write to History by design, so their absence "
    "here is not a parser gap. Also confirmed on a second GTLAB run "
    "(google-search-clear-history): using Chrome's own \"Clear browsing "
    "data > Browsing history\" empties this table completely (0 rows) and "
    "leaves no recoverable trace even at the raw-byte level of the History "
    "file itself (freelist_count 0, no matching text found by direct "
    "string search) -- so an empty report from this parser is real "
    "evidence history was cleared or never existed, not a parsing "
    "failure. Deleted-row recovery checked directly, via a THIRD GTLAB "
    "run (google-search-delete-one-row): deleting a single history entry "
    "through Chrome's own History UI (not a full clear) leaves ZERO "
    "recoverable trace either -- freelist_count 0, no matching raw bytes "
    "anywhere in the file for the deleted search term, and FFS "
    "Explorer's own recover_deleted_rows() (app/sqlite_carve.py) "
    "confirmed 0 rows recoverable from this table on that same file. A "
    "real, tested negative, not an unexamined gap -- Chrome on Android "
    "appears to rebuild/vacuum the History file on both a single-row "
    "delete and a full clear. recoverable_tables is still declared below "
    "so a future re-check on different data (a different Chrome version, "
    "a deletion caught mid-transaction) finds it automatically instead "
    "of needing another one-off investigation. Scoped "
    "to com.android.chrome only -- Brave/Edge/Samsung Internet ship the "
    "same schema under their own app_chrome-style folders (confirmed on "
    "the same test image) but need their own parser instance; not "
    "covered here."
)
app_path = "data/data/com.android.chrome"
files = {
    "history": "app_chrome/Default/History",
}
optional_files = {
    # Android Chrome's History db uses the classic rollback-journal mode,
    # not WAL -- confirmed on the extraction this was built against across
    # THREE chromium-family browsers (Chrome, Brave, Microsoft Edge all
    # shipped "History-journal", none shipped "History-wal"). Declared
    # anyway in case a future Chrome version switches modes.
    "history_journal": "app_chrome/Default/History-journal",
}

timestamp_fields = {"last_visit_time": "webkit_us"}

# urls.id is confirmed "INTEGER PRIMARY KEY AUTOINCREMENT" in the real
# schema (a genuine rowid alias), so a plain "table" entry is safe here.
# "id" (tried second) is the raw column name a recoverable_tables-carved
# row carries instead of this module's own "raw_url_id".
record_source = {
    "file_key": "history",
    "table": "urls",
    "rowid_fields": ["raw_url_id", "id"],
}

# Checked, not skipped -- see description above: a single deleted history
# entry (GTLAB google-search-delete-one-row) and a full history clear
# (GTLAB google-search-clear-history) both left nothing for
# recover_deleted_rows() to find on the real files this was tested
# against. Declared anyway per this project's own convention (a real,
# tested negative is still worth declaring) so different data -- a
# different Chrome version, or a deletion caught mid-transaction --
# surfaces automatically if it ever does leave something recoverable.
recoverable_tables = ["urls"]


def run(paths):
    import sqlite3

    conn = sqlite3.connect(paths["history"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, url, title, visit_count, typed_count, hidden, last_visit_time
        FROM urls
    """).fetchall()
    conn.close()

    # last_visit_time is passed through RAW (webkit epoch microseconds) --
    # see timestamp_fields above and WRITING_ARTIFACT_PARSERS.md's
    # webkit_us note for why this must not be hand-converted here: a
    # recoverable_tables-carved row carries this same raw value under the
    # same column name, and a hand-conversion here would only ever be
    # correct for the live-row path.
    return [
        {
            "url": r["url"],
            "title": r["title"],
            "visit_count": r["visit_count"],
            "typed_count": r["typed_count"],
            "hidden": bool(r["hidden"]),
            "last_visit_time": r["last_visit_time"],
            "raw_url_id": r["id"],
        }
        for r in rows
    ]
