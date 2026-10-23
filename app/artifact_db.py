"""artifact_db.py — write artifact parser results to the case SQLite database.

Each parser produces a list of dicts.  write_artifact_results() creates (or
replaces) a table named  artifact_<script_name>  in the case database and
inserts all rows.  Column names are the union of every row's keys, in
first-seen order — not just row[0]'s (a parser's own rows and, since
artifact_runner.py's recoverable_tables pass, sqlite_carve.py's recovered
rows can carry different key sets; row[0]-only column selection would
silently drop whichever keys the other rows have that row[0] doesn't).

All values are stored as TEXT.  The table is always rebuilt from scratch so
re-running a parser updates the results cleanly.
"""

import sqlite3


def write_artifact_results(
    case_conn: sqlite3.Connection,
    script_name: str,
    rows: list[dict],
) -> int:
    """Write rows into artifact_<script_name> in the case database.

    Returns the number of rows written.  Does nothing and returns 0 if rows
    is empty.
    """
    if not rows:
        return 0

    table = f"artifact_{script_name}"
    columns = []
    seen = set()
    for row in rows:
        for c in row.keys():
            if c not in seen:
                seen.add(c)
                columns.append(c)

    col_defs = ', '.join(f'"{c}" TEXT' for c in columns)
    case_conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    case_conn.execute(f'CREATE TABLE "{table}" ({col_defs})')

    def _cell(row, c):
        # NOT `row.get(c, '') or ''` -- that turns any legitimately falsy
        # value (0, False) into '', indistinguishable from a genuinely
        # missing/unknown field. Confirmed a real, shipped bug: found via
        # chrome_overview.py's total_downloads (a real, meaningful
        # confirmed-zero count) rendering as a blank cell everywhere this
        # table is read (Report table, query_artifact, AI Summary).
        # `.get(c)` alone still correctly returns '' for a genuinely
        # missing key or a value that's already None.
        value = row.get(c)
        return str(value) if value is not None else ''

    placeholders = ', '.join('?' for _ in columns)
    case_conn.executemany(
        f'INSERT INTO "{table}" VALUES ({placeholders})',
        [tuple(_cell(row, c) for c in columns) for row in rows],
    )
    case_conn.commit()
    return len(rows)


def load_artifact_results(
    case_conn: sqlite3.Connection,
    script_name: str,
) -> tuple[list[str], list[tuple]]:
    """Return (columns, rows) for a previously written artifact table.

    Returns ([], []) if the table does not exist yet.
    """
    table = f"artifact_{script_name}"
    exists = case_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return [], []

    cursor = case_conn.execute(f'SELECT * FROM "{table}"')
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    return columns, rows


def list_completed_artifacts(case_conn: sqlite3.Connection) -> list[str]:
    """Return script_names for all artifact tables stored in the case database."""
    rows = case_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'artifact_%'"
    ).fetchall()
    return [r[0][len('artifact_'):] for r in rows]


def build_attachment_links(script_name: str, rows: list[dict],
                           media_fields: list | None, label: str) -> list[dict]:
    """[{ui_path, row_id, label}] for the File Browser's "Linked Report"
    column (added 2026-10-12, direct request: "if a[n] messaging report
    ha[s] a[n] attachment... you could filter for sms in that column
    and get all the atacha[t]ment for sms and then... right[-]click to
    see the message"). Reuses `media_fields` — the SAME declaration
    `artifact_viewer.py`'s own `_precache_media_thumbnails` already
    reads off a parser module to know which row keys hold a real
    archive ui_path — rather than inventing a second per-script
    convention: any parser that already supports thumbnail columns
    (`sms_messages.py`'s `media_fields = ["attachment_path"]`, for one)
    gets this for free, no parser script changes needed.

    *row_id* is 1-based and MUST match the implicit SQLite rowid
    write_artifact_results' own INSERT assigns that row — true as long
    as this is called with the EXACT SAME *rows* list, in the same
    order, right after (or before) write_artifact_results(case_conn,
    script_name, rows) for that same run: that function always drops
    and recreates artifact_<script_name> with no explicit PRIMARY KEY,
    so a freshly inserted table's own rowid is 1 for the first row,
    2 for the second, and so on, deterministically, by construction —
    not re-derived from the row's own content, which could collide or
    be absent. app/artifact_viewer.py's pre-existing
    _art_jump_to_report_row(script_name, row_id) is what actually
    navigates to it later, and already handles a row_id that's gone
    stale (the parser was re-run and the row moved or vanished) with a
    graceful "no longer present" message rather than jumping to the
    wrong row.

    *label* is the report's own human display name (e.g. "SMS
    Messages") — deliberately the SAME string for every link from this
    script, not a per-row identifier, so typing a fragment of it into
    the File Browser's text filter (e.g. "sms") finds every attachment
    this report references at once; the exact row is only needed once
    an examiner has already decided to jump to it via the right-click
    menu, which labels each option by its own row_id instead."""
    if not media_fields or not rows:
        return []
    links = []
    for i, row in enumerate(rows):
        row_id = i + 1
        for field in media_fields:
            value = row.get(field)
            if not value:
                continue
            # one path, or several (a list, or one path per line: report
            # cells are stored as text) — e.g. every face crop of a photo
            values = value if isinstance(value, (list, tuple)) else str(value).split('\n')
            for path in values:
                path = path.strip() if isinstance(path, str) else path
                if path:
                    links.append({'ui_path': path, 'row_id': row_id, 'label': label})
    return links
