"""report_columns_store.py — per-report column ORDER and VISIBILITY state
for the Artifact Viewer's Report table (added 2026-08-30), keyed by bare
script_name (same convention as ai_summary_store.py/parser_versions.py —
a script's own name, no platform prefix, since a report is addressed the
same way everywhere in this app).

The two halves are DELIBERATELY persisted differently, per direct design
instruction — this is not an oversight, don't "fix" it into one shape:

  - Column ORDER is a genuine, permanent preference — "for that artifact
    type for all uses of the application" — written to
    report_columns.json (global/cross-case, same dev/frozen-path
    convention as research_store.py) and restored on every future run,
    same as this project's other persisted display settings.

  - Column VISIBILITY (which columns are shown/hidden) is SESSION-ONLY —
    held in the plain in-memory _session_visible dict below, never
    written to disk. Reopening the same report later in the SAME run of
    the app restores exactly what the examiner had; restarting the app
    always starts fresh — Core columns if the parser declares
    core_fields, otherwise every column. On top of that, every report
    hides a column that is blank on every row currently on screen,
    including one conversation opened from Chat Groups (see
    default_visible_columns, and ArtifactViewerMixin._setup_report_filter_ui
    in artifact_viewer.py). A stored 0 is a value, not a blank. This is a deliberate
    anti-footgun measure: a hidden-column choice that silently persisted
    forever across every future session could leave an examiner
    permanently unaware they're missing a column with material evidence
    in it, having long forgotten they ever hid it. Reordering carries no
    such risk — every column is still there either way — so it's fine,
    and desirable, for it to stick permanently.
"""

import json
import os
import sys

_cache = {"stat": None, "data": None}

# Session-only column visibility -- see the module docstring for why this
# is deliberately NOT part of the on-disk store. Lives only as long as
# this run of the app; a fresh process always starts with an empty dict,
# which is exactly what makes "resets to Core/All on restart" work for
# free -- get_visible_columns has no persisted state to find.
_session_visible: dict[str, list] = {}


def store_path() -> str:
    """Location of report_columns.json (next to exe when frozen, else
    config/) — same convention as research_store.py/ai_summary_store.py.
    Holds column ORDER only — see the module docstring for why visibility
    is intentionally not part of this file."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "report_columns.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "report_columns.json")


def load() -> dict:
    """Return the whole settings dict; cached by file mtime+size, same
    pattern as research_store.load()/ai_summary_store.load()."""
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache["stat"], _cache["data"] = None, {}
        return {}
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    _cache["stat"], _cache["data"] = stat, data
    return data


def save(data: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        st = os.stat(path)
        _cache["stat"], _cache["data"] = (st.st_mtime_ns, st.st_size), data
    except OSError:
        _cache["stat"] = None


def cell_has_value(value) -> bool:
    """True when a report cell is worth a column.

    None and blank text are empty. A number, including 0, is a value:
    "0 calls" is data, and latitude 0 is a real coordinate."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (bytes, bytearray)):
        return len(value) > 0
    return True


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def nonempty_column_names(conn, table: str, columns: list,
                          where: str | None = None, args=()) -> set | None:
    """Column names with at least one non-blank cell, or None if the
    check itself failed (the caller then keeps the usual columns).

    *where* limits the scan to the rows on screen, such as one chat.
    A column filled only on other rows then counts as empty. One scan.
    A table with no rows reports nothing, and default_visible_columns
    then keeps the usual columns rather than hiding every header on an
    empty report."""
    if not columns:
        return set()
    exprs = []
    for name in columns:
        quoted = _quote_ident(name)
        exprs.append(
            f"MAX(CASE WHEN {quoted} IS NOT NULL AND "
            f"TRIM(CAST({quoted} AS TEXT)) != '' THEN 1 ELSE 0 END)")
    sql = f"SELECT {', '.join(exprs)} FROM {_quote_ident(table)}"
    if where:
        sql += f" WHERE {where}"
    try:
        row = conn.execute(sql, tuple(args)).fetchone()
    except Exception:
        return None
    if row is None:
        return set()
    return {name for name, flag in zip(columns, row) if flag}


def nonempty_from_rows(columns: list, rows) -> set:
    """Same rule as nonempty_column_names, for an in-memory table."""
    found = set()
    ncols = len(columns)
    for row in rows:
        for i, name in enumerate(columns):
            if name in found or i >= len(row):
                continue
            if cell_has_value(row[i]):
                found.add(name)
        if len(found) == ncols:
            break
    return found


def default_visible_columns(all_columns: list, core_columns, populated) -> list:
    """Columns shown the first time a report is opened this session.

    Starts from core_fields when the parser declares them, otherwise
    every column, then drops any column that is not in *populated*.
    *populated* None (the check failed) or empty (no row has any value,
    including a report with no rows) keeps that starting set, so the
    table never opens with every column hidden."""
    base = [c for c in (core_columns or all_columns) if c in all_columns]
    if not base:
        base = list(all_columns)
    if not populated:
        return base
    narrowed = [c for c in base if c in populated]
    return narrowed or base


def columns_for_this_view(chosen, populated, one_chat=False, direct=False) -> list:
    """Columns for the rows on screen.

    Drops any chosen column that is blank on those rows. Inside one
    chat, also drops chat_subject: a one-to-one row leaves it blank, and
    a group writes the same name on every message, which is already the
    title of this view. On a one-to-one chat, chat_jid repeats phone_jid
    or lid, so it is dropped there only. A group keeps chat_jid: that
    is the group address."""
    visible = default_visible_columns(chosen, [], populated)
    if not one_chat:
        return visible
    drop = {"chat_subject"}
    if direct:
        drop.add("chat_jid")
    narrowed = [c for c in visible if c not in drop]
    return narrowed or visible


def get_visible_columns(script_name: str, all_columns: list):
    """The CURRENT-SESSION visible-column list for *script_name*,
    filtered to only names still present in *all_columns* (drops a stale
    name the same way ai_summary_store.get_report_settings does — a
    parser's own columns can change between versions). Returns None
    (meaning "never customized THIS session") if the examiner hasn't
    picked All/Core/None/a custom subset for this report since the app
    was last started — the caller applies the actual default (Core, or
    every column, minus columns that are blank on every row) from that.
    Returns a list — possibly EMPTY, meaning
    "None" was deliberately chosen — otherwise; None and [] are
    deliberately different states, not the same "nothing to show" case,
    so callers must check `is None` rather than truthiness.

    Deliberately in-memory only (_session_visible), never read from disk
    — see the module docstring for why visibility resets every restart
    while column order does not."""
    saved = _session_visible.get(script_name)
    if saved is None:
        return None
    return [c for c in saved if c in all_columns]


def set_visible_columns(script_name: str, columns) -> None:
    """*columns*: a list of column names to show (possibly empty — "None"
    is a real, in-session choice), or None to clear this session's
    customization and go back to whatever the caller's own default is
    (Core, or every column). Deliberately in-memory only — see the
    module docstring; never persisted to report_columns.json, so it
    cannot outlive this run of the app."""
    if columns is None:
        _session_visible.pop(script_name, None)
    else:
        _session_visible[script_name] = list(columns)


def get_column_order(script_name: str, all_columns: list):
    """The saved left-to-right column order for *script_name* — a list
    naming every column that still exists, in the examiner's own chosen
    order, or None if never customized (display order then just follows
    the parser's own natural column order). A saved name no longer in
    *all_columns* is dropped; any CURRENT column missing from the saved
    order (e.g. added by a newer parser version) is appended at the end
    rather than silently omitted. Persisted to report_columns.json —
    survives every future run, unlike get_visible_columns above (see the
    module docstring for why the two are treated differently)."""
    saved = load().get("reports", {}).get(script_name, {}).get("column_order")
    if not saved:
        return None
    order = [c for c in saved if c in all_columns]
    order += [c for c in all_columns if c not in order]
    return order


def set_column_order(script_name: str, order: list) -> None:
    data = load()
    reports = dict(data.get("reports", {}))
    saved = dict(reports.get(script_name, {}))
    saved["column_order"] = list(order)
    reports[script_name] = saved
    data["reports"] = reports
    save(data)
