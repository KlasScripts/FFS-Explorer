name = "Chrome Keyword Search Terms"
description = (
    "Search queries Chrome itself recorded via its own default-search-"
    "engine bookkeeping (keyword_search_terms, joined to urls) -- "
    "narrower than the \"Chrome Search Terms\" report (a URL-shape "
    "heuristic that also catches a search typed straight into a "
    "non-default engine's own search box) but exact: every row here is a "
    "search Chrome's own logic classified as one, not a guess from URL "
    "shape. keyword_id joins to the separate Web Data database's "
    "keywords table (search-engine definitions, e.g. \"Google\") -- not "
    "read here, since url/term alone already identify what was searched "
    "and where; add a Web Data join if the engine name itself becomes "
    "needed. keyword_search_terms has no declared INTEGER PRIMARY KEY of "
    "its own (a composite keyword_id+url_id natural key) -- record_source "
    "below uses SQLite's implicit rowid directly instead (SELECT rowid), "
    "confirmed valid since the table's own CREATE TABLE statement has no "
    "WITHOUT ROWID clause. "
    "Validated against Joshua Hickman's documented Android 14 test image: "
    "both of its 2 rows are the same two documented real (non-Incognito) "
    "omnibox searches already confirmed in the History/Search Terms "
    "reports (\"mobile phone forensics\", \"shelley duvall\"), both "
    "correctly attributed to keyword_id 2, confirmed = \"Google\" in that "
    "image's own Web Data database; the three documented Incognito "
    "searches are confirmed absent for the same reason as the History "
    "report."
)
app_path = "data/data/com.android.chrome"
files = {
    "history": "app_chrome/Default/History",
}
optional_files = {
    "history_journal": "app_chrome/Default/History-journal",
}

timestamp_fields = {"last_visit_time": "webkit_us"}

record_source = {
    "file_key": "history",
    "table": "keyword_search_terms",
    "rowid_fields": ["raw_rowid"],
}


def run(paths):
    import sqlite3

    conn = sqlite3.connect(paths["history"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT keyword_search_terms.rowid AS rid, keyword_id, term, url, title, last_visit_time
        FROM keyword_search_terms
        JOIN urls ON keyword_search_terms.url_id = urls.id
    """).fetchall()
    conn.close()

    return [
        {
            "term": r["term"],
            "url": r["url"],
            "title": r["title"],
            "keyword_id": r["keyword_id"],
            "last_visit_time": r["last_visit_time"],
            "raw_rowid": r["rid"],
        }
        for r in rows
    ]
