name = "Chrome Web History Full"
description = (
    "Every individual visit event in Chrome's History database (visits "
    "joined to urls), decoding the transition value into Chromium's core "
    "type + qualifier bits (page_transition_types.h) -- this is the "
    "per-visit timeline; see the separate \"Chrome Web History Summary\" "
    "report for the one-row-per-URL summary. "
    "Field-by-field validated against Joshua Hickman's documented Android "
    "14 test image: all 7 real visit rows behind the 6 documented "
    "non-Incognito actions on 2024-07-13 decoded to a transition label "
    "consistent with the documented action -- e.g. typing \"mlb.com\" "
    "decoded to TYPED+FROM_ADDRESS_BAR, going back to the same page "
    "decoded to TYPED+FORWARD_BACK+FROM_ADDRESS_BAR, an omnibox search "
    "decoded to GENERATED+FROM_ADDRESS_BAR, and a clicked link decoded to "
    "LINK. Only the TYPED, LINK, AUTO_BOOKMARK, RELOAD and GENERATED core "
    "types, and the FROM_ADDRESS_BAR/FORWARD_BACK/CHAIN_START/CHAIN_END "
    "qualifiers, have actually been observed this way against real data; "
    "the remaining core types and qualifier bits below are ported from "
    "Chromium's own source (page_transition_types.h, via ALEAPP's own "
    "chrome.py) but not yet exercised against a real extraction -- "
    "source-verified, not data-proven. Incognito visits are confirmed "
    "absent here for the same reason as the Summary report (Incognito "
    "mode never writes to this database). from_url resolves visits.from_visit "
    "through a second visits row to that row's own url -- confirmed correct "
    "against the Ohtani-article visit, whose from_url correctly resolves "
    "back to https://www.mlb.com/. Same com.android.chrome-only scope as "
    "the Summary report. Deleted-row recovery checked directly, via a "
    "GTLAB run (google-search-delete-one-row) that deleted a single "
    "history entry through Chrome's own History UI: FFS Explorer's own "
    "recover_deleted_rows() (app/sqlite_carve.py) found 0 recoverable "
    "visits rows on that file -- same real, tested negative as the "
    "Summary report's urls table, not an unexamined gap. "
    "recoverable_tables is still declared below for the same reason "
    "given there."
)
app_path = "data/data/com.android.chrome"
files = {
    "history": "app_chrome/Default/History",
}
optional_files = {
    # See chrome_history.py -- Android Chrome ships History-journal, not
    # History-wal, on every browser checked on the test image this was
    # built against.
    "history_journal": "app_chrome/Default/History-journal",
}

timestamp_fields = {"visit_time": "webkit_us"}

# visits.id is confirmed "INTEGER PRIMARY KEY AUTOINCREMENT" in the real
# schema (a genuine rowid alias). "id" (tried second) is the raw column
# name a recoverable_tables-carved row carries instead of this module's
# own "raw_visit_id".
record_source = {
    "file_key": "history",
    "table": "visits",
    "rowid_fields": ["raw_visit_id", "id"],
}

# Checked, not skipped -- see description above: deleting a single
# history entry (GTLAB google-search-delete-one-row) left nothing for
# recover_deleted_rows() to find in visits either. Declared anyway, same
# reasoning as chrome_history.py's own recoverable_tables.
recoverable_tables = ["visits"]

_CORE_TRANSITIONS = {
    0: "LINK",
    1: "TYPED",
    2: "AUTO_BOOKMARK",
    3: "AUTO_SUBFRAME",
    4: "MANUAL_SUBFRAME",
    5: "GENERATED",
    6: "START_PAGE",
    7: "FORM_SUBMIT",
    8: "RELOAD",
    9: "KEYWORD",
    10: "KEYWORD_GENERATED",
}

# The 0xC0000000 IS_REDIRECT_MASK covers the CLIENT_REDIRECT/SERVER_REDIRECT
# bits below rather than being a qualifier of its own, so it is not reported
# separately -- same note as ALEAPP's own chrome.py.
_QUALIFIERS = [
    (0x00800000, "BLOCKED"),
    (0x01000000, "FORWARD_BACK"),
    (0x02000000, "FROM_ADDRESS_BAR"),
    (0x04000000, "HOME_PAGE"),
    (0x08000000, "FROM_API"),
    (0x10000000, "CHAIN_START"),
    (0x20000000, "CHAIN_END"),
    (0x40000000, "CLIENT_REDIRECT"),
    (0x80000000, "SERVER_REDIRECT"),
]


def _decode_transition(value):
    value = value or 0
    core = _CORE_TRANSITIONS.get(value & 0xFF, f"[unknown core type: {value & 0xFF}]")
    quals = [name for mask, name in _QUALIFIERS if value & mask]
    return core, ", ".join(quals)


def run(paths):
    import sqlite3

    conn = sqlite3.connect(paths["history"])
    conn.row_factory = sqlite3.Row

    urls_by_id = {
        r["id"]: (r["url"], r["title"])
        for r in conn.execute("SELECT id, url, title FROM urls")
    }

    rows = conn.execute("""
        SELECT id, url, visit_time, from_visit, transition, visit_duration
        FROM visits
    """).fetchall()
    conn.close()

    visit_url_id_by_visit_id = {r["id"]: r["url"] for r in rows}

    out = []
    for r in rows:
        url, title = urls_by_id.get(r["url"], (None, None))
        core, quals = _decode_transition(r["transition"])

        from_visit_url_id = visit_url_id_by_visit_id.get(r["from_visit"]) if r["from_visit"] else None
        from_url = urls_by_id.get(from_visit_url_id, (None, None))[0] if from_visit_url_id else None

        out.append({
            "visit_time": r["visit_time"],
            "url": url,
            "title": title,
            "transition_type": core,
            "qualifiers": quals,
            "visit_duration_us": r["visit_duration"],
            "from_url": from_url,
            "raw_visit_id": r["id"],
        })
    return out
