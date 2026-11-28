name = "Chrome Web History"
app_group_label = "Chrome"
group_sort_key = 1
# Clicking the bold "Chrome" parent node itself shows THIS report's
# generated AI Summary (rendered as markdown/HTML), not a raw stats
# dashboard -- chrome_overview.py (a one-row counts table) was removed
# 2026-08-29 per direct feedback that it "does not really add anything";
# an interpretive narrative of what the activity actually shows is more
# useful as the group's landing view than a bare count table. See
# artifact_viewer.py's _refresh_artifact_tab/_on_art_tree_clicked for the
# is_group_overview + group_overview_mode mechanism, and ai_summary.py's
# save_summary/load_summary for where the persisted text comes from.
is_group_overview = True
group_overview_mode = "ai_summary"
description = (
    "Every page Chrome visited, one row per visit event -- typed vs "
    "clicked-link vs omnibox-search vs redirect is decoded from "
    "Chromium's own transition value (page_transition_types.h), and "
    "from_url resolves the referring page where one exists. Merges TWO "
    "sources, tagged in the `source` column so origin is never hidden: "
    "com.android.chrome's own History database (source=\"Chrome "
    "History\"), and its separate segmentation_platform/ukm_db "
    "(source=\"Segmentation Platform (UKM)\") -- Chrome's own on-device "
    "personalization component, which keeps an independent URL log that "
    "survives Chrome's own \"Clear Web History\" action untouched (field-"
    "tested: a GTLAB run that cleared history left the search phrase "
    "recoverable ONLY in this second source, nowhere else in the "
    "archive). The two live queries return the same shape (transition_"
    "type/qualifiers/from_url are always empty for a Segmentation "
    "Platform row -- it has no navigation-chain concept, just a URL and "
    "a timestamp) so the two sources read as one consistent table, not a "
    "ragged join. A Segmentation Platform row is suppressed when a Chrome "
    "History visit to the SAME url exists within DUP_WINDOW_US (1 second) "
    "of it -- exact-timestamp equality was tried and tested wrong: on real "
    "data UKM's last_timestamp for the same navigation always lands "
    "12.7-40ms after History's own visit_time, never identical, while the "
    "next-closest gap between any same-URL pair that really was a separate "
    "re-visit was 814ms. History wins the suppressed pair since it's the "
    "richer, primary source (has transition/qualifiers/from_url; UKM never "
    "does), so the surviving row is never the one with less information. "
    "Live rows only: deleted and changed records of both sources are "
    "in \"Chrome — Deleted History Records\" (2026-11-21), including "
    "the Segmentation Platform rows recovered after a history clear. "
    "Field-by-field validated against Joshua Hickman's documented "
    "Android 14 test image: all 6 documented non-Incognito Chrome "
    "actions on 2024-07-13 matched exactly by url/title/transition; the "
    "4 documented Incognito actions are confirmed absent, as expected -- "
    "Incognito mode never writes to either source. Scoped to "
    "com.android.chrome only -- Brave/Edge/Samsung Internet ship the "
    "same schemas under their own folders but need their own parser "
    "instance."
)
app_path = "data/data/com.android.chrome"
files = {
    "history": "app_chrome/Default/History",
}
optional_files = {
    "history_journal": "app_chrome/Default/History-journal",
    # May not exist on every case (Chrome version/build dependent) -- see
    # description above for why it's still worth reading when present.
    "ukm_db": "app_chrome/segmentation_platform/ukm_db",
    "ukm_db_wal": "app_chrome/segmentation_platform/ukm_db-wal",
}

timestamp_fields = {"timestamp": "webkit_us", "visit_time": "webkit_us", "last_timestamp": "webkit_us"}
# Drives the Report table's "Columns > Core Columns" preset (see
# WRITING_ARTIFACT_PARSERS.md) -- when, what, and what it was called are
# the essentials of a browsing-activity row; transition_type and source
# are also core because this report merges two independent sources (see
# description above) and how/why a row got there is part of reading it
# correctly, not optional detail. qualifiers/from_url stay non-core.
core_fields = ["timestamp", "url", "title", "transition_type", "source"]

record_source = [
    # Chrome History rows JOIN two tables (visits + its own urls, for the
    # url/title text -- see run() below); Segmentation Platform rows read
    # from one table in a different file, no join at all. `source_match`
    # scopes each entry to the query that actually produced the row -- the
    # combo (and the row's own default table on selection) is built fresh
    # per row from whichever entries match its own `source` field, never
    # the full list regardless of which query built it. The FIRST matching
    # entry for a given source is what a plain row click jumps to by
    # default (until the examiner picks a different one -- see
    # _art_record_source_sticky in artifact_viewer.py, sticky across every
    # OTHER row of the same source until the report reloads); "Chrome
    # History URL" is listed first, ahead of "Chrome History Visit", per
    # direct instruction -- the URL/title text is what an examiner wants
    # to verify by default; visits' own row (transition/timestamp/etc,
    # never text -- see the real-schema note below) is the one reached via
    # the dropdown instead.
    {"label": "Chrome History URL", "file_key": "history", "table": "urls",
     "rowid_fields": ["raw_url_id"], "source_match": ["Chrome History"]},
    {"label": "Chrome History Visit", "file_key": "history", "table": "visits",
     "rowid_fields": ["raw_visit_id", "id"], "source_match": ["Chrome History"]},
    {"label": "Segmentation Platform URL", "file_key": "ukm_db", "table": "urls",
     "rowid_fields": ["raw_url_id", "url_id"], "source_match": ["Segmentation Platform (UKM)"]},
]



_CORE_TRANSITIONS = {
    0: "LINK", 1: "TYPED", 2: "AUTO_BOOKMARK", 3: "AUTO_SUBFRAME",
    4: "MANUAL_SUBFRAME", 5: "GENERATED", 6: "START_PAGE", 7: "FORM_SUBMIT",
    8: "RELOAD", 9: "KEYWORD", 10: "KEYWORD_GENERATED",
}
_QUALIFIERS = [
    (0x00800000, "BLOCKED"), (0x01000000, "FORWARD_BACK"),
    (0x02000000, "FROM_ADDRESS_BAR"), (0x04000000, "HOME_PAGE"),
    (0x08000000, "FROM_API"), (0x10000000, "CHAIN_START"),
    (0x20000000, "CHAIN_END"), (0x40000000, "CLIENT_REDIRECT"),
    (0x80000000, "SERVER_REDIRECT"),
]


def _decode_transition(value):
    value = value or 0
    core = _CORE_TRANSITIONS.get(value & 0xFF, f"[unknown core type: {value & 0xFF}]")
    quals = [name for mask, name in _QUALIFIERS if value & mask]
    return core, ", ".join(quals)


def run(paths):
    from artifact_runner import open_db_readonly

    out = []

    conn = open_db_readonly(paths["history"])
    urls_by_id = {r["id"]: (r["url"], r["title"])
                 for r in conn.execute("SELECT id, url, title FROM urls")}
    visit_rows = conn.execute("""
        SELECT id, url, visit_time, from_visit, transition
        FROM visits
    """).fetchall()
    conn.close()

    visit_url_id_by_visit_id = {r["id"]: r["url"] for r in visit_rows}
    for r in visit_rows:
        url, title = urls_by_id.get(r["url"], (None, None))
        core, quals = _decode_transition(r["transition"])
        from_visit_url_id = visit_url_id_by_visit_id.get(r["from_visit"]) if r["from_visit"] else None
        from_url = urls_by_id.get(from_visit_url_id, (None, None))[0] if from_visit_url_id else None
        out.append({
            "timestamp": r["visit_time"],
            "url": url,
            "title": title,
            "transition_type": core,
            "qualifiers": quals,
            "from_url": from_url,
            "source": "Chrome History",
            "raw_visit_id": r["id"],
            # r["url"] is visits' own url column -- an INTEGER foreign key
            # into History's OWN urls table (id/url/title/...; NOT ukm_db's
            # same-named table), not text itself -- confirmed against the
            # real schema. Previously left None here since nothing consumed
            # it; now the rowid the "Chrome History URL" record_source
            # entry needs to jump to that row's own on-disk cell, which is
            # where the actual url/title TEXT this report already displays
            # (via the urls_by_id join two lines up) really lives.
            "raw_url_id": r["url"],
        })

    if "ukm_db" in paths:
        # UKM's last_timestamp is never bit-identical to History's visit_time
        # for the same navigation -- UKM's own write always lands a beat
        # later. Field-tested on a real case (Android 14 JoshHickman): every
        # genuine duplicate pair was 12.7-40ms apart; the next-closest gap
        # between any same-URL pair that was actually a separate re-visit
        # was 814ms. So treat same-URL rows within DUP_WINDOW_US as one
        # event (History wins, since it's the primary/richer source) and
        # keep anything further apart as the distinct visit it is.
        DUP_WINDOW_US = 1_000_000  # 1 second -- see field-tested gap above
        history_times_by_url = {}
        for o in out:
            history_times_by_url.setdefault(o["url"], []).append(o["timestamp"])
        conn = open_db_readonly(paths["ukm_db"])
        for r in conn.execute("SELECT url_id, url, title, last_timestamp FROM urls"):
            candidates = history_times_by_url.get(r["url"], [])
            if any(abs(r["last_timestamp"] - t) <= DUP_WINDOW_US
                   for t in candidates if t is not None and r["last_timestamp"] is not None):
                continue
            out.append({
                "timestamp": r["last_timestamp"],
                "url": r["url"],
                "title": r["title"],
                "transition_type": None,
                "qualifiers": None,
                "from_url": None,
                "source": "Segmentation Platform (UKM)",
                "raw_visit_id": None,
                "raw_url_id": r["url_id"],
            })
        conn.close()

    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'chrome_deleted_history',
  'name': 'Chrome — Deleted History Visits',
  'description': 'Deleted and changed Chrome visits from History (table visits: one row per '
                 'visit; its url is the id of a urls row). Live history is in Chrome Web '
                 'History; deleted URL records are in Chrome — Deleted History URLs, and '
                 "Chrome's own URL log (ukm_db) in Chrome — Deleted URL Log (Segmentation "
                 'Platform).\n'
                 '\n'
                 'Each row is one record as found in this table, never joined to another table '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id; damaged says why a record is not '
                 'whole. Values are as stored.\n'
                 '\n'
                 'linked_url: where the urls row this visit names was found: in the table now '
                 '(live: the URL is in Chrome Web History), in the records Chrome — Deleted '
                 'History URLs finds (recovered, with its row id) or not found. Only ids are '
                 'compared: no value of the URL row is brought in.\n'
                 'How the link can be wrong: urls.id and visits.id are AUTOINCREMENT (checked '
                 '2026-11-27 on the Josh Hickman Android 14, CTF26 and CTF25 images: the '
                 "highest id of each is its sqlite_sequence value, and every live visit's url "
                 'is a live urls id), so SQLite does not hand out an id again, but Chrome '
                 'rebuilding the file, or a sync restore, can put a different URL under an old '
                 'id; an earlier version found in the WAL and a live row with the same id were '
                 'stored at different times; a record found by pattern has no row id and '
                 'cannot be linked to.\n'
                 '\n'
                 'visit_time is Chromium microseconds since 1601-01-01 UTC, as in Chrome Web '
                 'History. transition is the stored number (core type plus qualifier bits); '
                 'Chrome Web History decodes it for live visits.\n'
                 '\n'
                 'Found earlier (carried over from the combined report): a tested negative for '
                 'History itself (a single deleted entry and a full clear left nothing; Chrome '
                 'appears to rebuild the file). Checked 2026-11-27 on the Josh Hickman Android '
                 '14, CTF25 and CTF26 images: 0 records.\n',
  'core_fields': ['status',
                  'visit_time',
                  'url_id',
                  'linked_url',
                  'transition',
                  'changed_fields',
                  'damaged',
                  'how_found',
                  'copies'],
  'timestamp_fields': {'visit_time': 'webkit_us'},
  'file_key': 'history',
  'table': 'visits',
  'label': 'Visit (History)',
  'fields': {'id': 'visit_id',
             'url': 'url_id',
             'visit_time': 'visit_time',
             'from_visit': 'from_visit',
             'transition': 'transition',
             'visit_duration': 'visit_duration'},
  'version_fields': ['url', 'transition'],
  'links': [{'name': 'linked_url',
             'what': 'URL',
             'column': 'url',
             'table': 'urls',
             'key': 'id',
             'report': 'chrome_deleted_history_urls'}]},
 {'script': 'chrome_deleted_history_urls',
  'name': 'Chrome — Deleted History URLs',
  'description': 'Deleted and changed Chrome URL records from History (table urls: one row per '
                 'URL, with its title and visit counts). Live history is in Chrome Web '
                 'History; deleted visits are in Chrome — Deleted History Visits.\n'
                 '\n'
                 'Each row is one record as found in this table, never joined to another table '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id; damaged says why a record is not '
                 'whole. Values are as stored.\n'
                 '\n'
                 "linked_visits: where the visits whose url is this record's id were found: in "
                 'the table now (live), in the records Chrome — Deleted History Visits finds '
                 '(recovered, with its row id) or not found. One URL can have many visits. '
                 'Only ids are compared: no value of a visit is brought in.\n'
                 'How the link can be wrong: urls.id and visits.id are AUTOINCREMENT (checked '
                 '2026-11-27 on the Josh Hickman Android 14, CTF26 and CTF25 images: the '
                 "highest id of each is its sqlite_sequence value, and every live visit's url "
                 'is a live urls id), so SQLite does not hand out an id again, but Chrome '
                 'rebuilding the file, or a sync restore, can put a different URL under an old '
                 'id; an earlier version found in the WAL and a live row with the same id were '
                 'stored at different times; a record found by pattern has no row id and '
                 'cannot be linked to.\n'
                 '\n'
                 'last_visit_time is Chromium microseconds since 1601-01-01 UTC, as in Chrome '
                 'Web History.\n'
                 '\n'
                 'Found earlier (carried over from the combined report): a tested negative for '
                 'History itself (a single deleted entry and a full clear left nothing; Chrome '
                 'appears to rebuild the file). Checked 2026-11-27 on the Josh Hickman Android '
                 '14, CTF25 and CTF26 images: 0 records.\n',
  'core_fields': ['status',
                  'last_visit_time',
                  'url',
                  'title',
                  'linked_visits',
                  'changed_fields',
                  'url (now)',
                  'damaged',
                  'how_found',
                  'copies'],
  'timestamp_fields': {'last_visit_time': 'webkit_us'},
  'file_key': 'history',
  'table': 'urls',
  'label': 'URL (History)',
  'fields': {'id': 'url_id',
             'url': 'url',
             'title': 'title',
             'visit_count': 'visit_count',
             'typed_count': 'typed_count',
             'last_visit_time': 'last_visit_time',
             'hidden': 'hidden'},
  'version_fields': ['url', 'title'],
  'links': [{'name': 'linked_visits',
             'what': 'visit',
             'column': 'id',
             'table': 'visits',
             'key': 'url',
             'report': 'chrome_deleted_history'}]},
 {'script': 'chrome_deleted_ukm_urls',
  'name': 'Chrome — Deleted URL Log (Segmentation Platform)',
  'description': "Deleted and changed records of Chrome's own URL log, "
                 'segmentation_platform/ukm_db (table urls), when the phone has it. It '
                 'survives "Clear browsing data": the main report recorded 6 rows recovered '
                 'via WAL history after a clear-history run on a GTLAB test image, none of it '
                 'visible elsewhere (carried over). Live history is in Chrome Web History; '
                 "History's own deleted records are in Chrome — Deleted History Visits and "
                 'Chrome — Deleted History URLs. ukm_db is a separate database: its url_id is '
                 'not a History id, and no link is made.\n'
                 '\n'
                 'Each row is one record as found in this table, never joined to another table '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id; damaged says why a record is not '
                 'whole. Values are as stored.\n'
                 '\n'
                 'last_timestamp is Chromium microseconds since 1601-01-01 UTC.\n'
                 '\n'
                 'Checked 2026-11-27 on the Josh Hickman Android 14, CTF25 and CTF26 images: '
                 'each has a ukm_db (22, 4 and 38 live URLs), and 0 deleted or changed '
                 'records. (The combined report said the Josh Hickman Android 14 image had no '
                 'ukm_db; it has one.)',
  'core_fields': ['status',
                  'last_timestamp',
                  'url',
                  'title',
                  'changed_fields',
                  'url (now)',
                  'damaged',
                  'how_found',
                  'copies'],
  'timestamp_fields': {'last_timestamp': 'webkit_us'},
  'file_key': 'ukm_db',
  'table': 'urls',
  'label': 'URL (Segmentation Platform)',
  'fields': {'url_id': 'url_id',
             'url': 'url',
             'title': 'title',
             'last_timestamp': 'last_timestamp',
             'counter': 'counter',
             'profile_id': 'profile_id'},
  'version_fields': ['url', 'title']}]
