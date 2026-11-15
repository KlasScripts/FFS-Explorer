"""timeline.py — the case-wide Timeline: one table of timed events from the
artifact reports, built from caseresults.db into casecache.db. Qt-free.

A report's time column is on the Timeline only if its parser says what that
time means (`timeline_fields`, beside `timestamp_fields`). A time column
without that declaration is listed as "meaning not written yet", never
silently left out, and never given a meaning guessed from its column name.

    timeline_fields = {
        "event_time": {
            "meaning": "App event as recorded by Android Usage Stats",  # required
            "app": "package",          # column holding the app, or
            "app_name": "Files by Google",   # fixed text
            "event": "event_type",     # column holding the event, or
            "event_name": "Opened in viewer",  # fixed text
            "detail": ["class_name"],  # columns joined into Detail
            "caution": "...",          # the source's known catch, on every row
        },
    }

A time column that is not an event (an expiry date, a value worked out
from the clock) is named with the reason instead, so the Sources list says
why it is left out:

    timeline_skip = {"expires": "the date the trash will be emptied, not an event"}

The unit comes from the parser's own timestamp_fields. Evidence values are
never changed: the event keeps the raw value and unit, and the UTC time is
only computed from them. A value that is blank, zero or not a number is not
an event; each is counted per source and shown in the Sources list.

The event table is rebuildable (casecache.db, A-53): it is rebuilt whenever
the reports, their parsers' declarations or this module's build rules
change (see fingerprint()).
"""

import hashlib
import json
import sqlite3

# Bump when the way events are built changes, so every case rebuilds.
BUILD_VERSION = 1

# Seconds to add to a unit's value (after scaling) to get Unix seconds.
_UNITS = {
    's':         (1.0,   0),
    'ms':        (1e-3,  0),
    'cocoa_s':   (1.0,   978307200),
    'cocoa_ns':  (1e-9,  978307200),
    # Chromium/WebKit: microseconds since 1601-01-01 UTC.
    'webkit_us': (1e-6, -11644473600),
}

# Outside these years a decoded time is kept but labelled: it is more
# likely a wrong unit or a corrupt value than a real event.
_PLAUSIBLE_FROM = 946684800      # 2000-01-01 UTC
_PLAUSIBLE_TO = 4102444800       # 2100-01-01 UTC

DETAIL_MAX = 200


def to_unix_seconds(raw, unit: str):
    """Unix seconds (float, UTC) for a stored *raw* value in *unit* (one of
    timestamp_fields' units), or None if it is not a number or the unit is
    unknown. Shared with the Artifact Viewer's time columns, so a report
    and the Timeline never convert the same value differently."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:   # NaN
        return None
    scale_offset = _UNITS.get(unit)
    if scale_offset is None:
        return None
    scale, offset = scale_offset
    return value * scale + offset


def declared_fields(mod) -> dict:
    """{field: spec} from *mod*'s timeline_fields, each spec with its unit
    (from timestamp_fields) under 'unit'. A field with no meaning, or with
    no unit, is returned with 'error' set instead of being dropped."""
    out = {}
    decl = getattr(mod, 'timeline_fields', None) or {}
    units = getattr(mod, 'timestamp_fields', None) or {}
    for field, spec in decl.items():
        spec = dict(spec or {})
        spec['unit'] = units.get(field)
        if not str(spec.get('meaning') or '').strip():
            spec['error'] = 'timeline_fields entry has no meaning'
        elif spec['unit'] not in _UNITS:
            spec['error'] = (f"no known unit in timestamp_fields "
                             f"({spec['unit']!r})")
        out[field] = spec
    return out


def _tables(results_conn) -> set:
    return {r[0] for r in results_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'artifact_%'")}


def fingerprint(results_conn, modules: dict) -> str:
    """A hash that changes when anything the Timeline is built from does:
    the last artifact run, each report's row count, and each parser's
    time declarations."""
    h = hashlib.sha256()
    h.update(f"v{BUILD_VERSION}".encode())
    try:
        last = results_conn.execute(
            "SELECT MAX(id) FROM run_log WHERE run_type LIKE 'artifact_%'").fetchone()[0]
    except sqlite3.Error:
        last = None
    h.update(repr(last).encode())
    tables = _tables(results_conn)
    for name in sorted(modules):
        mod = modules[name]
        table = f"artifact_{name}"
        count = (results_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                 if table in tables else None)
        decl = {
            'timeline': getattr(mod, 'timeline_fields', None),
            'skip': getattr(mod, 'timeline_skip', None),
            'units': getattr(mod, 'timestamp_fields', None),
            'name': getattr(mod, 'name', name),
        }
        h.update(json.dumps([name, count, decl], sort_keys=True, default=str).encode())
    return h.hexdigest()


def ensure_schema(cache_conn) -> None:
    """Create the Timeline tables if this casecache.db has none yet (no
    cache schema bump: that would delete every case's thumbnails)."""
    cache_conn.executescript('''
        CREATE TABLE IF NOT EXISTS timeline_events (
            utc_ms       INTEGER NOT NULL,
            script       TEXT    NOT NULL,
            report_rowid INTEGER NOT NULL,
            field        TEXT    NOT NULL,
            raw          TEXT,
            unit         TEXT,
            meaning      TEXT,
            source       TEXT,
            app          TEXT,
            event        TEXT,
            detail       TEXT,
            record       TEXT,
            caution      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_time ON timeline_events (utc_ms);
        CREATE TABLE IF NOT EXISTS timeline_sources (
            script     TEXT,
            source     TEXT,
            field      TEXT,
            status     TEXT,
            events     INTEGER,
            blank      INTEGER,
            zero       INTEGER,
            unreadable INTEGER,
            implausible INTEGER,
            note       TEXT
        );
        CREATE TABLE IF NOT EXISTS timeline_meta (key TEXT PRIMARY KEY, value TEXT);
    ''')


def stored_fingerprint(cache_conn):
    ensure_schema(cache_conn)
    row = cache_conn.execute(
        "SELECT value FROM timeline_meta WHERE key='fingerprint'").fetchone()
    return row[0] if row else None


# Source statuses (the Sources list).
ON_TIMELINE = "On the Timeline"
NO_ROWS = "Declared, but the report has no rows in this case"
NOT_RUN = "Declared, but the report has not been run on this case"
NOT_DECLARED = "Has times, but what they mean is not written yet: not on the Timeline"
DECLARATION_ERROR = "Declaration error: not on the Timeline"
NOT_AN_EVENT = "Not an event: not on the Timeline"
RAN_NO_ROWS = "Run on this case: no rows"
RUN_FAILED = ("Run on this case: ended with an error, often a missing source file "
              "(see the parser log); no rows")
STALE = ("On the Timeline, but from an earlier run: the last run found no rows "
         "or did not finish, and the earlier rows are still stored")
NO_READABLE = "Declared, but every value is blank, zero or not a time"


HAS_EVENTS = (ON_TIMELINE, STALE)


def _last_runs(results_conn) -> dict:
    """{script: (complete, output_rows)} of each parser's latest run."""
    try:
        rows = results_conn.execute(
            "SELECT run_type, complete, output_rows FROM run_log WHERE id IN ("
            " SELECT MAX(id) FROM run_log WHERE run_type LIKE 'artifact_%' GROUP BY run_type)"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {r[0][len('artifact_'):]: (r[1], r[2]) for r in rows}


def _clean(value) -> str:
    return '' if value is None else str(value).strip()


def _detail(row: dict, fields) -> str:
    parts = [_clean(row.get(f)) for f in (fields or ())]
    text = ' · '.join(p for p in parts if p)
    text = ' '.join(text.split())    # one line
    if len(text) > DETAIL_MAX:
        text = text[:DETAIL_MAX - 1] + '…'
    return text


def build(results_conn, cache_conn, modules: dict, cancelled=lambda: False) -> dict:
    """Rebuild the Timeline in *cache_conn* from the artifact tables in
    *results_conn*. *modules*: {script_name: parser module}. Returns
    {'events': n, 'sources': n_on_timeline} or {'cancelled': True}."""
    ensure_schema(cache_conn)
    fp = fingerprint(results_conn, modules)
    tables = _tables(results_conn)
    last_runs = _last_runs(results_conn)
    events, sources = [], []
    for name in sorted(modules):
        if cancelled():
            return {'cancelled': True}
        mod = modules[name]
        label = getattr(mod, 'name', name) or name
        table = f"artifact_{name}"
        decl = declared_fields(mod)
        units = getattr(mod, 'timestamp_fields', None) or {}
        skip = getattr(mod, 'timeline_skip', None) or {}
        undeclared = [f for f in units if f not in decl and f not in skip]
        has_table = table in tables
        if undeclared and has_table:
            sources.append((name, label, ', '.join(undeclared), NOT_DECLARED,
                            None, None, None, None, None, ''))
        if has_table:
            for field, reason in skip.items():
                sources.append((name, label, field, NOT_AN_EVENT,
                                None, None, None, None, None, _clean(reason)))
        if not decl:
            continue
        last = last_runs.get(name)
        if not has_table:
            if last is None:
                status = NOT_RUN
            elif last[0]:
                status = RAN_NO_ROWS
            else:
                status = RUN_FAILED
            for field, spec in decl.items():
                sources.append((name, label, field,
                                DECLARATION_ERROR if spec.get('error') else status,
                                None, None, None, None, None, spec.get('error', '')))
            continue
        # write_artifact_results leaves the old table when a run finds no
        # rows, and a failed run writes nothing: say the rows are older.
        stale = last is not None and (not last[0] or last[1] == 0)
        cur = results_conn.execute(f'SELECT rowid, * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        for field, spec in decl.items():
            if spec.get('error'):
                sources.append((name, label, field, DECLARATION_ERROR,
                                None, None, None, None, None, spec['error']))
                continue
            if field not in cols:
                sources.append((name, label, field, NO_ROWS if not rows else DECLARATION_ERROR,
                                0, 0, 0, 0, 0, '' if not rows else f"column {field!r} not in the report"))
                continue
            n = blank = zero = bad = odd = 0
            for values in rows:
                row = dict(zip(cols, values))
                raw = _clean(row.get(field))
                if not raw:
                    blank += 1
                    continue
                secs = to_unix_seconds(raw, spec['unit'])
                if secs is None:
                    bad += 1
                    continue
                if float(raw) == 0:
                    zero += 1
                    continue
                notes = []
                src = _clean(row.get('source'))
                if src.startswith('Carved'):
                    notes.append(src)
                if stale:
                    notes.append('from an earlier run of the report (see Sources)')
                if not (_PLAUSIBLE_FROM <= secs < _PLAUSIBLE_TO):
                    odd += 1
                    notes.append('time outside 2000–2100: check the unit and value')
                app = _clean(row.get(spec['app'])) if spec.get('app') else _clean(spec.get('app_name'))
                event = (_clean(row.get(spec['event'])) if spec.get('event') else '') \
                    or _clean(spec.get('event_name'))
                events.append((
                    int(round(secs * 1000)), name, row['rowid'], field, raw, spec['unit'],
                    spec['meaning'].strip(), label, app, event,
                    _detail(row, spec.get('detail')), '; '.join(notes),
                    _clean(spec.get('caution'))))
                n += 1
            status = ON_TIMELINE if n else (NO_ROWS if not rows else NO_READABLE)
            if stale and n:
                status = STALE
            sources.append((name, label, field, status, n, blank, zero, bad, odd, ''))
    with cache_conn:
        cache_conn.execute('DELETE FROM timeline_events')
        cache_conn.execute('DELETE FROM timeline_sources')
        cache_conn.executemany(
            'INSERT INTO timeline_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', events)
        cache_conn.executemany(
            'INSERT INTO timeline_sources VALUES (?,?,?,?,?,?,?,?,?,?)', sources)
        cache_conn.execute(
            "INSERT OR REPLACE INTO timeline_meta VALUES ('fingerprint', ?)", (fp,))
    return {'events': len(events),
            'sources': len({s[0] for s in sources if s[3] in HAS_EVENTS and s[4]})}


COLUMNS = ['utc_ms', 'meaning', 'source', 'app', 'event', 'detail', 'record',
           'caution', 'raw', 'unit', 'script', 'report_rowid', 'field']


def _where(start_ms=None, end_ms=None, scripts=None, app=None, text=None):
    clauses, args = [], []
    if start_ms is not None:
        clauses.append('utc_ms >= ?')
        args.append(int(start_ms))
    if end_ms is not None:
        clauses.append('utc_ms <= ?')
        args.append(int(end_ms))
    if scripts:
        clauses.append(f"script IN ({','.join('?' for _ in scripts)})")
        args.extend(scripts)
    if app:
        clauses.append('app = ?')
        args.append(app)
    if text:
        like = f"%{text}%"
        clauses.append('(meaning LIKE ? OR app LIKE ? OR event LIKE ? OR detail LIKE ?'
                       ' OR source LIKE ? OR record LIKE ?)')
        args.extend([like] * 6)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', args


def query(cache_conn, start_ms=None, end_ms=None, scripts=None, app=None,
          text=None, descending=False, limit=100_000):
    """(rows, total): matching events in time order (ties kept in report
    order), at most *limit* rows, and how many matched in all — so a capped
    list can say so (A-51)."""
    where, args = _where(start_ms, end_ms, scripts, app, text)
    total = cache_conn.execute(f'SELECT COUNT(*) FROM timeline_events{where}', args).fetchone()[0]
    order = 'DESC' if descending else 'ASC'
    rows = cache_conn.execute(
        f"SELECT {', '.join(COLUMNS)} FROM timeline_events{where} "
        f"ORDER BY utc_ms {order}, script, report_rowid, field LIMIT ?",
        args + [int(limit)]).fetchall()
    return rows, total


def apps(cache_conn) -> list:
    return [r[0] for r in cache_conn.execute(
        "SELECT DISTINCT app FROM timeline_events WHERE app != '' ORDER BY app")]


def source_list(cache_conn) -> list:
    """[(script, source label, field, status, events, blank, zero,
    unreadable, implausible, note)] for the Sources list."""
    ensure_schema(cache_conn)
    return cache_conn.execute(
        'SELECT * FROM timeline_sources ORDER BY status, source, field').fetchall()


def time_of_row(mod, row: dict, prefer_field=None):
    """(utc_ms, field) for the time to pivot on in a report *row*: the
    clicked column if it is a time column, else the first declared
    Timeline field with a value, else the first time column with a value.
    None if the row has no readable time."""
    units = getattr(mod, 'timestamp_fields', None) or {}
    order = []
    if prefer_field in units:
        order.append(prefer_field)
    order += [f for f in (getattr(mod, 'timeline_fields', None) or {}) if f in units]
    order += list(units)
    for field in order:
        raw = _clean(row.get(field))
        if not raw:
            continue
        secs = to_unix_seconds(raw, units[field])
        if secs is not None and float(raw) != 0:
            return int(round(secs * 1000)), field
    return None
