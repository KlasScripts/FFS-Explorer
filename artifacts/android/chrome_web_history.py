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
    "Deleted-row recovery is declared for both underlying tables (Chrome "
    "History's own visits, and Segmentation Platform's urls) and pinned "
    "to the correct file for each via file_key -- the two databases have "
    "a literal table-name collision (both define a table called `urls`), "
    "confirmed to silently pick only one of them without pinning; the "
    "file_key parameter (sqlite_carve.recover_deleted_rows) exists "
    "specifically because of this real case. History's own visits/urls "
    "recovery is a confirmed, tested negative on real data (a single "
    "deleted entry and a full history clear both left nothing "
    "recoverable -- Chrome appears to rebuild/vacuum the file on both). "
    "Segmentation Platform's urls recovery is a confirmed POSITIVE on "
    "real data: 6 rows recovered via WAL history from the same GTLAB "
    "clear-history run, including a Google anti-bot \"sorry\" redirect "
    "and the actual visited result page -- none of it visible anywhere "
    "else once the clear finished. Recovered rows carry the raw column "
    "name of whichever table they came from (visits' own `visit_time`, "
    "or urls' own `url_id`/`last_timestamp`) rather than this report's "
    "own live-row field names -- expected raggedness for a recovered row "
    "only, not a bug. "
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

# "visits" is a bare string -- only History.db has a table by that name, so
# no collision. ("ukm_db", "urls") is pinned -- see description above for
# the real table-name collision this avoids.
recoverable_tables = ["visits", ("ukm_db", "urls")]

# A carved row never runs through run() below, so it can't set "source"
# itself -- the runner (artifact_runner.py) fills it in from this table
# using these labels, e.g. "Carved -- Chrome History (visits table)", with
# " (unverified match)" appended automatically for a header_signature carve
# (no rowid at all -- the weakest of the four carving paths, and the only
# one that can be a truncated/false-positive match rather than a
# structurally-confirmed row). See recovery_source_labels in
# artifact_runner.py's docstring.
#
# What to expect from a GENUINE carved row of each table, so a blank field
# alone is never mistaken for a false-positive signal: `visits` has no
# `title` column at all (real schema: id/url/visit_time/from_visit/
# transition/segment_id/visit_duration/... -- title only ever lives in the
# separate `urls` table, joined for live rows only) -- a carved visits row
# with no title is expected, not suspicious. ukm_db's `urls` DOES have a
# `title` column (real schema: url_id/url/last_timestamp/counter/title/
# profile_id) but it's nullable there too -- a real anti-bot/redirect page
# genuinely has none (see the description above's own recovered example).
# The one field a genuine carved `visits` row should NOT be missing is
# `transition` itself (NOT NULL in the real schema, always some integer) --
# an absent or zero-length `transition` on a row whose `source` says
# "(unverified match)" is the actual sign worth treating with suspicion,
# not a missing title.
recovery_source_labels = {
    "visits": "Chrome History (visits table)",
    "urls": "Segmentation Platform (UKM, urls table)",
}

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
    import sqlite3

    out = []

    conn = sqlite3.connect(paths["history"])
    conn.row_factory = sqlite3.Row
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
        conn = sqlite3.connect(f'file:{paths["ukm_db"]}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
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
