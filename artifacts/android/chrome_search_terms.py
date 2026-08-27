name = "Chrome Search Terms"
description = (
    "Search queries recovered by pattern-matching Chrome's stored URLs for "
    "the literal substring \"search?q=\" and decoding the value between "
    "\"q=\" and the next \"&\" -- the same heuristic ALEAPP's chrome.py "
    "(get_chromeSearchTerms) uses, ported here for parity. This is a "
    "URL-shape heuristic, not a confirmed-search flag: it fires on any "
    "urls row containing that substring, whatever the domain, so it can "
    "in principle match a URL that merely contains that text without "
    "being a real search -- a source-verified assumption carried over "
    "from ALEAPP, not yet seen to misfire on real data. See the separate "
    "\"Chrome Keyword Search Terms\" report for a narrower, exact "
    "alternative: searches Chrome itself recorded via its own "
    "default-search-engine bookkeeping. The two overlap but are not "
    "identical -- this report also catches a search typed straight into "
    "a non-default engine's own search box, which Keyword Search Terms "
    "would miss. "
    "Validated against Joshua Hickman's documented Android 14 test image: "
    "correctly extracted both of its documented real (non-Incognito) "
    "omnibox searches -- \"mobile phone forensics\" and \"shelley "
    "duvall\" -- with the right url/title/visit_count; the three "
    "documented Incognito searches are confirmed absent for the same "
    "reason as the History report (Incognito mode never writes to this "
    "database)."
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
    "table": "urls",
    "rowid_fields": ["raw_url_id"],
}


def run(paths):
    import sqlite3
    import urllib.parse

    conn = sqlite3.connect(paths["history"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, url, title, visit_count, last_visit_time
        FROM urls
        WHERE url LIKE '%search?q=%'
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            search = r["url"].split("search?q=", 1)[1].split("&", 1)[0]
            search = urllib.parse.unquote(search).replace("+", " ")
        except IndexError:
            search = ""
        out.append({
            "search_term": search,
            "url": r["url"],
            "title": r["title"],
            "visit_count": r["visit_count"],
            "last_visit_time": r["last_visit_time"],
            "raw_url_id": r["id"],
        })
    return out
