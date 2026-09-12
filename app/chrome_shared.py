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

`query_rows` itself is NOT Chrome-specific in its own logic — it's
plain connect/row_factory/close boilerplate any SQL-based parser needs,
regardless of app — it only ever lived in this Chrome-named file
because that's the batch of parsers where the duplication was first
noticed (2026-09-03 gap sweep). It's kept here, as a thin wrapper around
the real, universal implementation in artifact_runner.open_db_readonly,
so every existing Chrome parser that already imports `query_rows` from
this module keeps working unchanged — but a NON-Chrome parser should
import `open_db_readonly` directly from artifact_runner instead of
reaching into this module for it. Only `url_set`/`history_visits` below
are genuinely Chrome-schema-specific (Chrome History's own urls/visits
tables) and belong here for real."""

import sqlite3

from artifact_runner import open_db_readonly


def query_rows(db_path: str, sql: str) -> list[sqlite3.Row]:
    """Every row of *sql* against the sqlite file at *db_path*, as
    dict-like sqlite3.Row objects (row["col_name"], never a bare tuple
    a caller has to hand-count positions for) — the connect/row_factory/
    close boilerplate every single-table Chrome parser in this project
    was otherwise writing out by hand. Opens read-only via
    artifact_runner.open_db_readonly — see that function's own
    docstring for why a live-query connect must never be read-write."""
    conn = open_db_readonly(db_path)
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
