name = "Chrome Search"
app_group_label = "Chrome"
group_sort_key = 2
description = (
    "Every search query Chrome recorded, one row per search, merging two "
    "detection methods rather than showing them as separate reports: "
    "keyword_search_terms (Chrome's own default-search-engine "
    "bookkeeping -- exact, but only ever covers a search made through "
    "the currently-set default engine) and a URL-shape heuristic (any "
    "urls row containing the literal substring \"search?q=\" -- broader, "
    "also catches a search typed straight into a non-default engine's "
    "own search box, but a URL-shape match rather than a confirmed-"
    "search flag). `confirmed_by` says which method(s) found each row -- "
    "\"Keyword + URL pattern\" for the common case where both agree, "
    "\"Keyword bookkeeping\" or \"URL pattern\" alone when only one did. "
    "search_term prefers Chrome's own parsed term (keyword_search_terms) "
    "when available, since it is exact; falls back to decoding the term "
    "out of the URL's own q= parameter otherwise. "
    "Field-by-field validated against Joshua Hickman's documented "
    "Android 14 test image: both of its documented real (non-Incognito) "
    "omnibox searches (\"mobile phone forensics\", \"shelley duvall\") "
    "are correctly merged into one row each, both confirmed by both "
    "methods and both correctly attributed to keyword_id 2 = Google; the "
    "three documented Incognito searches are confirmed absent -- "
    "Incognito mode never writes to either underlying table. "
    "Deleted/recovered search entries are NOT separately declared here: "
    "a recovered row from Chrome's segmentation_platform/ukm_db has no "
    "way to be filtered down to \"was this one a search\" before it's "
    "appended (recoverable_tables has no content filter, only a table "
    "name) -- any recovered search activity still shows up, unfiltered "
    "alongside non-search URLs, in the separate \"Chrome Web History\" "
    "report instead; check there for anything not visible here. Scoped "
    "to com.android.chrome only."
)
app_path = "data/data/com.android.chrome"
files = {
    "history": "app_chrome/Default/History",
}
optional_files = {
    "history_journal": "app_chrome/Default/History-journal",
}

timestamp_fields = {"last_visit_time": "webkit_us"}
# When and what was searched for are the essentials -- confirmed_by/
# keyword_id/url/title/visit_count are useful provenance/detail, not
# needed for a first pass.
core_fields = ["last_visit_time", "search_term"]

record_source = {
    "label": "Search URL",
    "file_key": "history",
    "table": "urls",
    "rowid_fields": ["raw_url_id", "id"],
}


def run(paths):
    import sqlite3
    import urllib.parse

    conn = sqlite3.connect(paths["history"])
    conn.row_factory = sqlite3.Row

    keyword_rows = conn.execute("""
        SELECT keyword_search_terms.rowid AS rid, keyword_id, term, url_id, last_visit_time
        FROM keyword_search_terms
        JOIN urls ON keyword_search_terms.url_id = urls.id
    """).fetchall()
    keyword_by_url_id = {r["url_id"]: r for r in keyword_rows}

    pattern_rows = conn.execute("""
        SELECT id, url, title, visit_count, last_visit_time
        FROM urls
        WHERE url LIKE '%search?q=%'
    """).fetchall()
    pattern_by_url_id = {r["id"]: r for r in pattern_rows}

    urls_by_id = {r["id"]: (r["url"], r["title"], r["visit_count"], r["last_visit_time"])
                 for r in conn.execute("SELECT id, url, title, visit_count, last_visit_time FROM urls")}
    conn.close()

    out = []
    for url_id in set(keyword_by_url_id) | set(pattern_by_url_id):
        kw = keyword_by_url_id.get(url_id)
        pat = pattern_by_url_id.get(url_id)
        url, title, visit_count, last_visit_time = urls_by_id.get(url_id, (None, None, None, None))

        if kw and pat:
            confirmed_by = "Keyword + URL pattern"
        elif kw:
            confirmed_by = "Keyword bookkeeping"
        else:
            confirmed_by = "URL pattern"

        if kw:
            search_term = kw["term"]
        else:
            try:
                search_term = url.split("search?q=", 1)[1].split("&", 1)[0]
                search_term = urllib.parse.unquote(search_term).replace("+", " ")
            except (AttributeError, IndexError):
                search_term = ""

        out.append({
            "search_term": search_term,
            "confirmed_by": confirmed_by,
            "keyword_id": kw["keyword_id"] if kw else None,
            "url": url,
            "title": title,
            "visit_count": visit_count,
            "last_visit_time": last_visit_time,
            "raw_url_id": url_id,
        })
    return out
