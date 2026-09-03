name = "Chrome Top Sites"
app_group_label = "Chrome"
group_sort_key = 13
description = (
    "Chrome's own 'frequently visited' shortlist (app_chrome/Default/"
    "Top Sites, the `top_sites` table) -- url/url_rank/title, one row "
    "per site Chrome has ranked as worth suggesting (New Tab page "
    "tiles). A small, separate store from History with its own "
    "retention/ranking logic -- worth checking even when it duplicates "
    "History, since it can outlive a partial History clear. Real, "
    "checked property of this project's own Android 14 JoshHickman "
    "case: only 1 row exists (mlb.com) despite far more real History "
    "activity -- Top Sites' own ranking threshold, not a parsing gap; "
    "don't read a short list here as \"this is everything the user "
    "visited.\""
)
app_path = "data/data/com.android.chrome"
files = {
    "top_sites_db": "app_chrome/Default/Top Sites",
}
optional_files = {
    "top_sites_db_journal": "app_chrome/Default/Top Sites-journal",
}

core_fields = ["url_rank", "title", "url"]

record_source = {
    "label": "Top Sites Entry",
    "file_key": "top_sites_db",
    "table": "top_sites",
    "rowid_fields": ["raw_rowid"],
}


def run(paths):
    import chrome_shared

    rows = chrome_shared.query_rows(paths["top_sites_db"], """
        SELECT rowid AS rid, url, url_rank, title FROM top_sites
        ORDER BY url_rank
    """)
    return [{
        "url_rank": r["url_rank"],
        "title": r["title"],
        "url": r["url"],
        "raw_rowid": r["rid"],
    } for r in rows]
