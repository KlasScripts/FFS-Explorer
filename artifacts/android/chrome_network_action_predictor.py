name = "Chrome Typed URLs (Network Action Predictor)"
app_group_label = "Chrome"
group_sort_key = 12
description = (
    "Every DISTINCT omnibox-autocomplete prediction Chrome has learned "
    "(app_chrome/Default/Network Action Predictor, the "
    "`network_action_predictor` table): user_text is text the user "
    "actually TYPED into the omnibox, url is what Chrome learned that "
    "prefix resolves to, number_of_hits/number_of_misses is how often "
    "that prediction was accepted vs not. Survives independently of "
    "Chrome History's own retention -- this is Chrome's own typed-text "
    "learning store, a separate table/file, not derived from History "
    "at query time. "
    "A real HIT row is DROPPED here when it's a strict prefix of "
    "ANOTHER hit row for the SAME url (e.g. real raw data on this "
    "project's own Android 14 JoshHickman case: \"mlb.\"/\"mlb.c\"/"
    "\"mlb.co\"/\"mlb.com\" were four separate real rows, all hits, all "
    "resolving to https://www.mlb.com/ -- only \"mlb.com\", the "
    "longest, is kept) -- per direct instruction not to waste an "
    "examiner's time on rows that add no real information over a "
    "longer one already shown for the same url. This is a deliberate "
    "exception to this project's usual 'show the raw table as-is' "
    "convention, safe specifically because a prefix hit is ENTIRELY "
    "implied by a longer hit for the same url, never a distinct fact on "
    "its own -- the dropped rows' own raw_rowid/id are simply not "
    "individually reachable via this report's Record mode as a result; "
    "the full raw table remains on-device/exportable regardless. "
    "MISS rows are NEVER dropped this way, even when one miss's own "
    "user_text is a prefix of another -- a miss records the predictor "
    "guessing WRONG for that exact prefix, a genuinely distinct fact "
    "each time, not a redundant fragment of a hit (real raw data, same "
    "case: \"m\"/\"mo\"/\"s\" all miss against mlb.com, kept as three "
    "separate rows)."
)
app_path = "data/data/com.android.chrome"
files = {
    "nap_db": "app_chrome/Default/Network Action Predictor",
}
optional_files = {
    "nap_db_journal": "app_chrome/Default/Network Action Predictor-journal",
}

core_fields = ["user_text", "url", "number_of_hits", "number_of_misses"]

record_source = {
    "label": "Network Action Predictor Entry",
    "file_key": "nap_db",
    "table": "network_action_predictor",
    "rowid_fields": ["raw_rowid"],
}


def _drop_redundant_hit_prefixes(rows: list) -> list:
    """A hit row is redundant when its own user_text is a strict prefix
    of ANOTHER hit row's user_text for the SAME url -- that longer row
    already proves the shorter prefix was typed en route, so the
    shorter one adds nothing. Miss rows are untouched regardless: a
    miss is the predictor guessing wrong for that exact prefix, a real
    fact on its own, never implied by any other row."""
    hits_by_url: dict[str, list] = {}
    for r in rows:
        if r["number_of_hits"] > 0:
            hits_by_url.setdefault(r["url"], []).append(r)

    # raw_rowid (each row's own real SQLite rowid) as the set key --
    # meaningful and stable, unlike Python's own id() builtin, which
    # would work here too but names an unrelated concept to anyone
    # reading it and shadows a name this project also uses as a real
    # column ("id").
    redundant_rowids = {
        a["raw_rowid"]
        for hit_rows in hits_by_url.values()
        for a in hit_rows
        if any(b["user_text"].startswith(a["user_text"]) and len(b["user_text"]) > len(a["user_text"])
               for b in hit_rows)
    }
    return [r for r in rows if r["raw_rowid"] not in redundant_rowids]


def run(paths):
    import chrome_shared

    db_rows = chrome_shared.query_rows(paths["nap_db"], """
        SELECT rowid AS rid, id, user_text, url, number_of_hits, number_of_misses
        FROM network_action_predictor
    """)
    rows = [{
        "user_text": r["user_text"],
        "url": r["url"],
        "number_of_hits": r["number_of_hits"],
        "number_of_misses": r["number_of_misses"],
        "raw_rowid": r["rid"],
        "id": r["id"],
    } for r in db_rows]
    return _drop_redundant_hit_prefixes(rows)
