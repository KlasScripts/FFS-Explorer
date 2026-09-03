name = "Chrome Omnibox Shortcuts"
app_group_label = "Chrome"
group_sort_key = 14
description = (
    "Chrome's omnibox 'shortcuts' learning store (app_chrome/Default/"
    "Shortcuts, the `omni_box_shortcuts` table): text is what the user "
    "typed, url/contents/description is what they picked in response, "
    "last_access_time/number_of_hits is when and how often. Same "
    "typed-navigation category as 'Chrome Typed URLs (Network Action "
    "Predictor)' but a separate table/file with its own retention -- "
    "worth checking independently, not assumed redundant. "
    "This project's own Android 14 JoshHickman case has 0 rows here "
    "(checked directly, a real empty table, not a query failure) -- "
    "unlike this group's other parsers, this one's extraction logic is "
    "only schema-verified, not yet exercised against a real populated "
    "row; state that plainly if reporting from this specific case."
)
app_path = "data/data/com.android.chrome"
files = {
    "shortcuts_db": "app_chrome/Default/Shortcuts",
}
optional_files = {
    "shortcuts_db_journal": "app_chrome/Default/Shortcuts-journal",
}

timestamp_fields = {"last_access_time": "webkit_us"}
core_fields = ["text", "url", "last_access_time", "number_of_hits"]

record_source = {
    "label": "Omnibox Shortcut",
    "file_key": "shortcuts_db",
    "table": "omni_box_shortcuts",
    "rowid_fields": ["raw_rowid"],
}


def run(paths):
    import chrome_shared

    rows = chrome_shared.query_rows(paths["shortcuts_db"], """
        SELECT rowid AS rid, id, text, fill_into_edit, url, contents,
               description, keyword, last_access_time, number_of_hits
        FROM omni_box_shortcuts
    """)
    return [{
        "text": r["text"],
        "fill_into_edit": r["fill_into_edit"],
        "url": r["url"],
        "contents": r["contents"],
        "description": r["description"],
        "keyword": r["keyword"],
        "last_access_time": r["last_access_time"],
        "number_of_hits": r["number_of_hits"],
        "raw_rowid": r["rid"],
        "id": r["id"],
    } for r in rows]
