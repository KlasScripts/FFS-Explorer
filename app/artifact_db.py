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
