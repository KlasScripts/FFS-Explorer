"""chrome_shared.py — small helpers shared across the Chrome artifact
parsers under artifacts/android/chrome_*.py (Login Data, Cookies,
Network Action Predictor, Top Sites, Shortcuts, Favicons, Autofill, ...).

Each of those parser scripts is meant to stay a thin declaration — its
own table's SQL plus how to shape each row into this project's own
column dict — not a place to re-derive "how do I open a sqlite file" or
"how do I read Chrome History for a cross-reference" every time. That
plumbing lives here once, the same "one shared Qt-free core module,
imported by name" pattern app/chrome_cache.py already established for
chrome_cache_media.py/chrome_cache_pages.py.
"""

import sqlite3


def query_rows(db_path: str, sql: str) -> list[sqlite3.Row]:
    """Every row of *sql* against the sqlite file at *db_path*, as
    dict-like sqlite3.Row objects (row["col_name"], never a bare tuple
    a caller has to hand-count positions for) — the connect/row_factory/
    close boilerplate every single-table Chrome parser in this project
    was otherwise writing out by hand."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def url_set(db_path: str | None, table: str = "urls", column: str = "url") -> set[str] | None:
    """Every distinct value of *column* in *table* — e.g. Chrome
    History's own urls.url, or Segmentation Platform's ukm_db urls.url
    (identical column name, different file/schema, both handled the
    same way since this only ever needs the one column to exist).
    Answers "is this URL known to this table at all" — a membership
    check, not a read of the row's own content.

    None if *db_path* is falsy or the file can't be read (missing,
    corrupt, wrong schema) — that case matters to a caller cross-
    referencing an OPTIONAL file: it means "couldn't check", never to
    be conflated with "checked and genuinely found nothing"."""
    if not db_path:
        return None
    try:
        return {r[column] for r in query_rows(db_path, f"SELECT DISTINCT {column} FROM {table}")}
    except sqlite3.Error:
        return None


def history_visits(history_db_path: str) -> list[tuple]:
    """(unix_seconds, url, title) for every real visit in Chrome's own
    History file, sorted ascending. History's own visit_time is
    webkit_us (microseconds since 1601-01-01 UTC) — converted here to
    plain Unix seconds once, since every OTHER Chrome timestamp field
    this project compares it against (autofill's date_created, etc.)
    already uses that epoch; callers get a directly-comparable value,
    not a second copy of the same conversion formula to re-derive."""
    urls_by_id = {
        r["id"]: (r["url"], r["title"])
        for r in query_rows(history_db_path, "SELECT id, url, title FROM urls")
    }
    events = []
    for r in query_rows(history_db_path, "SELECT url, visit_time FROM visits"):
        url, title = urls_by_id.get(r["url"], (None, None))
        if url is None or r["visit_time"] is None:
            continue
        events.append((r["visit_time"] / 1e6 - 11644473600, url, title or ""))
    events.sort(key=lambda e: e[0])
    return events
