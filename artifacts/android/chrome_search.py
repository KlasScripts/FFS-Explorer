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
    import urllib.parse

    from artifact_runner import open_db_readonly

    conn = open_db_readonly(paths["history"])

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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'chrome_search_deleted',
  'name': 'Chrome Search — Deleted & Changed Search Terms',
  'description': "Deleted and changed search-term records from Chrome's History (table "
                 'keyword_search_terms): the text typed into the address bar as a search '
                 '(term), the search engine (keyword_id) and the History url row of the '
                 'results page (url_id). A deleted record is a search cleared from history. '
                 'Live search terms are in Chrome Search.\n'
                 '\n'
                 'Each row is one record as found in the keyword_search_terms table, never '
                 'joined to another table (ASSUMPTIONS.md A-95): ids are the stored ids. '
                 'Status says Deleted (no longer in the table), Changed (an earlier version of '
                 'a row still there, listed only when one of the columns that matter changed, '
                 'with the current value beside it as "(now)") or an unfinished write/delete '
                 'under a hot journal. How found says how each record was recovered and how '
                 'sure that is; a record found by pattern has no row id. Values are as stored: '
                 'no code is turned into a word here unless it was checked against a test '
                 'image.\n'
                 '\n'
                 'Chrome writes this database with a rollback journal, not a WAL: records come '
                 'from freed space and freed pages, and from the -journal file when one was '
                 'extracted.\n'
                 '\n'
                 "This table has no id column of its own: SQLite's row id is used, and SQLite "
                 "gives a new row the highest row id + 1, so a deleted row's row id can be "
                 'handed to a new one. A record is therefore tied to a live row by the columns '
                 'Chrome keeps unique (url_id and keyword_id together), not by its row id: a '
                 'record with the same url_id and keyword_id as a live row is an earlier '
                 'version of it ("tied to live row" in how_found).\n'
                 '\n'
                 'linked_url: where the urls row this record names (url_id) was found, live or '
                 'in Chrome — Deleted & Changed URLs; its last visit time and the results page '
                 'are there. Only ids are compared. How the link can be wrong: urls.id is '
                 'AUTOINCREMENT (checked 2026-11-27 on the three Android images), so SQLite '
                 'does not hand out an id again, but an app that re-inserts rows with their '
                 'old ids (a restore from backup) can still put a different record under the '
                 'same id; an earlier version found in the WAL and a live row with the same id '
                 'were stored at different times; a record found by pattern has no row id and '
                 'cannot be linked; several copies of one record can carry the same id.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman and CTF26 Android 14 and CTF25 '
                 'Android 15 images: no deleted or changed search term (2, 16 and 3 live).',
  'timestamp_fields': {},
  'core_fields': ['status', 'term', 'linked_url', 'changed_fields', 'how_found', 'copies'],
  'file_key': 'history',
  'table': 'keyword_search_terms',
  'label': 'Search term',
  'fields': {'keyword_id': 'keyword_id',
             'url_id': 'url_id',
             'term': 'term',
             'normalized_term': 'normalized_term'},
  'identity': ['url_id', 'keyword_id'],
  'version_fields': ['term', 'normalized_term'],
  'links': [{'name': 'linked_url',
             'what': 'url',
             'column': 'url_id',
             'table': 'urls',
             'key': 'id',
             'report': 'chrome_deleted_history_urls'}]}]
