name = "Chrome Autofill"
app_group_label = "Chrome"
group_sort_key = 4
description = (
    "Plain-text form-field values Chrome saved for autocomplete: Web "
    "Data's own `autofill` table, plus its synced counterpart Account "
    "Web Data (same schema, tagged separately in `source`). "
    "IMPORTANT, confirmed by checking the real schema directly: classic "
    "autofill has NO site/URL association at all -- Chromium's plain-"
    "text-field autofill is a suggestion pool keyed by the form field's "
    "own `name` attribute (e.g. \"username\"), reusable on ANY site with "
    "a matching field name, not tied to where it was first entered. "
    "There is no url/origin column anywhere in this table's real schema "
    "to report -- this is a real property of the data, not a parsing "
    "gap. Payment-card autofill (credit_cards/masked_credit_cards/"
    "local_stored_cvc etc.) is a deliberate, explicit gap -- a separate, "
    "more sensitive schema not covered here. "
    "Field-tested against Joshua Hickman's documented Android 14 image: "
    "4 real rows recovered (an email address, a username, and a first/"
    "last name pair), all correctly attributed to Web Data (the device's "
    "Account Web Data was empty on that image, a real, checked "
    "negative). "
    "record_source is scoped to Web Data's own autofill table only, not "
    "Account Web Data -- those are two genuinely different source files "
    "per row, not several joined views of the same row (the case "
    "record_source's own multi-entry picker is designed for), so "
    "pointing it at the wrong one risked citing the wrong bytes; an "
    "Account Web Data row shows \"not available\" in Record mode "
    "instead, honestly, rather than guessing. "
    "inferred_site_title/inferred_site_url are NOT part of the autofill "
    "table's own real schema (see above -- it has no site column to "
    "report) -- they're this parser's OWN inference, added per direct "
    "request: 'show the history item before the data was entered ... "
    "not verified but ... this is likely to be the site that the form "
    "variable was entered into.' Computed by finding the LATEST Chrome "
    "History visit (History's own visits/urls, read here ONLY for this "
    "comparison -- see 'Chrome Web History' for that data reported in "
    "full) whose visit_time is at or before this row's own date_created "
    "-- i.e. whatever page was open immediately before this value was "
    "first saved. inferred_site_seconds_before states the actual gap in "
    "seconds so the examiner can judge strength directly rather than "
    "trust a uniform claim across every row: a few seconds is a strong "
    "match, hours/days is weak (Chrome closed and reopened, unrelated "
    "browsing in between) -- shown for every row with any prior visit "
    "at all, never gated by an assumed cutoff, since that threshold "
    "would be this tool's own unverified guess, not the examiner's."
)
warning = (
    "UNVERIFIED: inferred_site_title/inferred_site_url are this parser's "
    "OWN inference, not a value Chrome itself recorded -- the autofill "
    "table has no site/URL column at all (see description). They name "
    "whatever page History shows as visited immediately before this "
    "value's date_created, which is USUALLY where a value was typed but "
    "is not guaranteed: the value could have been autofilled (not "
    "retyped) on a later, different site reusing the same field name "
    "(the exact reuse behavior this description already explains classic "
    "autofill has by design), or Chrome/History could be incomplete "
    "around that moment. Treat as a lead to verify against the rest of "
    "the case, never as a confirmed fact on its own -- check "
    "inferred_site_seconds_before before trusting a match: a large gap "
    "is real evidence the inference is weak for that specific row, not "
    "just noise to ignore."
)
app_path = "data/data/com.android.chrome"
files = {
    "web_data": "app_chrome/Default/Web Data",
}
optional_files = {
    "web_data_journal": "app_chrome/Default/Web Data-journal",
    "account_web_data": "app_chrome/Default/Account Web Data",
    "account_web_data_journal": "app_chrome/Default/Account Web Data-journal",
    # Read here ONLY to populate inferred_site_title/inferred_site_url
    # below -- this parser never re-reports a History row itself, that's
    # chrome_web_history.py's job. Genuinely optional: without History,
    # the inference columns simply stay empty rather than blocking the
    # rest of this report.
    "history": "app_chrome/Default/History",
    "history_journal": "app_chrome/Default/History-journal",
}

timestamp_fields = {"date_created": "s", "date_last_used": "s"}
# What field name, what value, when it was last used, and (unverified)
# what site it likely belongs to are the essentials; count/source/
# date_created are useful detail, not needed for a first pass.
core_fields = ["field_name", "value", "date_last_used", "inferred_site_title", "inferred_site_seconds_before"]

# autofill has no declared INTEGER PRIMARY KEY of its own (composite
# name+value natural key) -- uses SQLite's implicit rowid directly, same
# approach as keyword_search_terms elsewhere in this project.
record_source = {
    "label": "Autofill Entry",
    "file_key": "web_data",
    "table": "autofill",
    "rowid_fields": ["raw_rowid"],
}


def _autofill_rows(db_path, source_label):
    import chrome_shared

    rows = chrome_shared.query_rows(
        db_path, "SELECT rowid AS rid, name, value, date_created, date_last_used, count FROM autofill")
    return [{
        "field_name": r["name"],
        "value": r["value"],
        "count": r["count"],
        "date_created": r["date_created"],
        "date_last_used": r["date_last_used"],
        "source": source_label,
        "raw_rowid": r["rid"],
    } for r in rows]


def _nearest_prior_visit(events, target_seconds):
    """Latest (unix_seconds, url, title) in *events* at or before
    *target_seconds* -- i.e. whatever page History shows as open right
    before this autofill value's own date_created. None if there's no
    History coverage at or before that moment at all (device just set
    up, History cleared since, autofill predates this History file's
    own retention window, ...)."""
    import bisect

    if not events or target_seconds is None:
        return None
    times = [e[0] for e in events]
    idx = bisect.bisect_right(times, target_seconds) - 1
    if idx < 0:
        return None
    return events[idx]


def run(paths):
    import chrome_shared

    out = _autofill_rows(paths["web_data"], "Web Data")
    if "account_web_data" in paths:
        out += _autofill_rows(paths["account_web_data"], "Account Web Data (synced)")

    history_events = chrome_shared.history_visits(paths["history"]) if "history" in paths else None
    for row in out:
        title = url = ""
        seconds_before = None
        if history_events is not None:
            match = _nearest_prior_visit(history_events, row["date_created"])
            if match is not None:
                event_seconds, url, title = match
                seconds_before = row["date_created"] - event_seconds
        row["inferred_site_title"] = title
        row["inferred_site_url"] = url
        row["inferred_site_seconds_before"] = seconds_before

    return out
