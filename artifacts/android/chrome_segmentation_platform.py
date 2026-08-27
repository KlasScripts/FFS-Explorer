name = "Chrome Segmentation Platform (UKM)"
description = (
    "Chrome's on-device 'Segmentation Platform' component (used for "
    "on-device personalization/feature-recommendation signals) keeps its "
    "own local UKM-style log of visited URLs -- app_chrome/"
    "segmentation_platform/ukm_db, a database entirely separate from the "
    "main History/Favicons/Shortcuts/Web Data cluster the other Chrome "
    "reports in this project read. This report covers only its `urls` "
    "table (url, title, last_timestamp, counter, profile_id) -- the "
    "`metrics`/`uma_metrics` tables (opaque hashed telemetry event/metric "
    "identifiers, not resolvable to a human name without Chromium's "
    "internal ukm.xml hash definitions, which this project does not "
    "vendor) are a deliberate, explicit gap, not silently skipped. "
    "This report exists specifically because it survives what the other "
    "Chrome reports do not: field-tested against a GTLAB run (google-"
    "search-clear-history) where a full 'Clear Web History' wiped every "
    "trace of a search from History/Favicons/Shortcuts/Web Data/Cookies, "
    "a whole-archive raw-byte search afterward found the search phrase "
    "surviving in exactly ONE place -- this database's -wal file, 18 "
    "times, in readable, structured (non-coincidental) record content. "
    "The live SQL path above will usually return few or zero rows on a "
    "short-lived case: this database's own tables (urls' root page "
    "confirmed at page 8 on the real file this was built against) are "
    "commonly never checkpointed into the main db file at all -- SQLite's "
    "default WAL auto-checkpoint threshold is ~1000 pages, and this WAL "
    "was only ~73 -- so recoverable_tables below, not the live query, is "
    "this report's real source of rows on most real cases. That gap in "
    "FFS Explorer's own carving pipeline (app/sqlite_carve.py could not "
    "discover a table whose root page was never checkpointed at all, so "
    "WAL history for it was silently never searched) was found and fixed "
    "building this parser -- see walk_table_leaf_pages' docstring. "
    "Verified end-to-end on the real file: 6 recovered urls rows via WAL "
    "history, including the full pre-clear redirect chain (a Google "
    "'sorry' anti-bot challenge page, matching the same CAPTCHA behavior "
    "documented on the chrome-bbc-google-001 GTLAB run) and the actual "
    "visited result page -- all of it gone from every other Chrome "
    "artifact by the time 'Clear Web History' finished. url_id is "
    "confirmed a genuine INTEGER PRIMARY KEY rowid alias, but Chromium "
    "assigns it a large pseudo-random-looking value, not a small "
    "sequential one -- expected, not a parsing error. counter/profile_id "
    "are reported as their own raw column names with no further meaning "
    "asserted; not independently confirmed against Chromium source."
)
app_path = "data/data/com.android.chrome"
files = {
    "ukm_db": "app_chrome/segmentation_platform/ukm_db",
}
optional_files = {
    # Almost always the real source of data -- see description above.
    "ukm_db_wal": "app_chrome/segmentation_platform/ukm_db-wal",
}

timestamp_fields = {"last_timestamp": "webkit_us"}

# url_id is confirmed "INTEGER PRIMARY KEY NOT NULL" in the real schema (a
# genuine rowid alias). "url_id" (tried second) is also the raw column
# name a recoverable_tables-carved row carries -- this module's own
# live-row output field already uses the same name, so no separate
# "raw_..." alias was needed the way the History reports needed one.
record_source = {
    "file_key": "ukm_db",
    "table": "urls",
    "rowid_fields": ["url_id"],
}

# Checked, not guessed -- see description above and walk_table_leaf_pages'
# own docstring in app/sqlite_carve.py for the exact real-file case this
# was built and fixed against.
recoverable_tables = ["urls"]


def run(paths):
    import sqlite3

    conn = sqlite3.connect(f'file:{paths["ukm_db"]}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT url_id, url, title, last_timestamp, counter, profile_id
        FROM urls
    """).fetchall()
    conn.close()

    return [
        {
            "url": r["url"],
            "title": r["title"],
            "last_timestamp": r["last_timestamp"],
            "counter": r["counter"],
            "profile_id": r["profile_id"],
            "url_id": r["url_id"],
        }
        for r in rows
    ]
