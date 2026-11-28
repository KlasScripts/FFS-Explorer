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
        # Account Web Data doesn't always have an autofill table: absent on
        # Android 15 CTF25 (schema 141), present but empty on CTF26 (145)
        # and Android 14 JoshHickman (122). A missing table used to fail
        # the whole parser, losing the Web Data rows too.
        tables = {r["name"] for r in chrome_shared.query_rows(
            paths["account_web_data"], "SELECT name FROM sqlite_master WHERE type='table'")}
        if "autofill" in tables:
            out += _autofill_rows(paths["account_web_data"], "Account Web Data (synced)")
        else:
            from artifact_runner import log_note
            log_note("Account Web Data has no autofill table on this device "
                     "(Chrome doesn't always create it), so no synced-account "
                     "autofill entries could be read; Web Data was read as normal.")

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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'chrome_autofill_deleted',
  'name': 'Chrome Autofill — Deleted Entries',
  'description': "Deleted autofill records from Chrome's Web Data (table autofill): a value "
                 'typed into a web form field (name is the field, value what was typed), when '
                 'first and last used and how often. A deleted record is a value cleared from '
                 'Chrome or removed by it. Live entries are in Chrome Autofill.\n'
                 '\n'
                 'Each row is one record as found in the autofill table, never joined to '
                 'another table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says '
                 'Deleted (no longer in the table), Changed (an earlier version of a row still '
                 'there, listed only when one of the columns that matter changed, with the '
                 'current value beside it as "(now)") or an unfinished write/delete under a '
                 'hot journal. How found says how each record was recovered and how sure that '
                 'is; a record found by pattern has no row id. Values are as stored: no code '
                 'is turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'Chrome writes this database with a rollback journal, not a WAL: records come '
                 'from freed space and freed pages, and from the -journal file when one was '
                 'extracted.\n'
                 '\n'
                 'date_created and date_last_used are Unix seconds, as in Chrome Autofill.\n'
                 '\n'
                 "This table has no id column of its own: SQLite's row id is used, and SQLite "
                 "gives a new row the highest row id + 1, so a deleted row's row id can be "
                 'handed to a new one. A record is therefore tied to a live row by the columns '
                 'Chrome keeps unique (name and value), not by its row id: a record with the '
                 'same name and value as a live row is an earlier version of it ("tied to live '
                 'row" in how_found). Only name and value matter here: an earlier version '
                 'differing only in its use count or last-used time is counted in the run log, '
                 'not listed.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman and CTF26 Android 14 and CTF25 '
                 'Android 15 images: no deleted autofill record (4, 6 and 1 live).',
  'timestamp_fields': {'date_created': 's', 'date_last_used': 's'},
  'core_fields': ['status',
                  'field_name',
                  'value',
                  'date_last_used',
                  'count',
                  'how_found',
                  'copies'],
  'file_key': 'web_data',
  'table': 'autofill',
  'label': 'Autofill entry',
  'fields': {'name': 'field_name',
             'value': 'value',
             'date_created': 'date_created',
             'date_last_used': 'date_last_used',
             'count': 'count'},
  'identity': ['name', 'value'],
  'stable_id': ['name', 'value'],
  'version_fields': ['name', 'value'],
  'links': []}]
