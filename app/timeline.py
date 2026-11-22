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

Files are on the Timeline too (file_events): every User Media file's
own times, as the archive records them (Created, Changed, Modified; never
Accessed, which phones often don't update: owner decision 2026-11-15),
and its EXIF/video taken time when it has a UTC offset. A photo's taken
time without an offset is local time at an unknown offset and is counted
in the Sources list, never placed as UTC. Proven-identical copies (the
duplicate scan's MD5 groups) with the same time are one event naming how
many copies; a copy whose time differs is its own event.

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

# File events (file_events below).
FILES_SCRIPT = '__files__'        # timeline_events.script of a file event
FILES_SOURCE = 'Files: User Media'
CACHE_SCRIPT = '__files_cache__'  # media from web cache records (A-75)
CACHE_SOURCE = 'Files: web cache media'
ARCHIVE_SCRIPT = '__files_archive__'   # unpacked nested archives, one event each
ARCHIVE_SOURCE = 'Files: archives that arrived on the device'
DETAIL_SCRIPT = '__files_detail__'    # detailed mode: every file in the evidence folders
DETAIL_SOURCE = 'Files: evidence folders (detailed)'
FILE_SCRIPTS = (FILES_SCRIPT, CACHE_SCRIPT, ARCHIVE_SCRIPT, DETAIL_SCRIPT)
_DETAIL_CAUTION = ("Detailed mode: every file in the evidence folders, not only media. "
                   "Many of these files are written by the system or by apps on their "
                   "own, without the user doing anything.")
_CACHE_CAUTION = ("The cache record's own times (A-75): when the browser or app "
                  "downloaded this picture to show a page. The user saw it, or the page "
                  "loaded it; it was not necessarily saved.")
_ARCHIVE_CAUTION = ("The archive's own times on the phone. The files inside arrived "
                    "with it; their own times inside the zip are local, zone unknown "
                    "(File Browser), and are not on the Timeline. A downloaded file's "
                    "Modified time can come from the server; Created/Changed is closer "
                    "to when it arrived.")
FILES_BUILD_VERSION = 1
_FILE_STAGES = ('header_scan', 'media_metadata', 'duplicate_detection')
# What each file-system time means, with its known catch (POSIX/APFS/ext4
# semantics; how a copy, download or restore sets them varies by app).
FILE_TIMES = {
    'btime': ("Created (file system)",
              "When this copy of the file was created on the phone; a copied, "
              "downloaded or restored file gets the time of that, not of the original."),
    'ctime': ("Changed (file system)",
              "Last change to the file's metadata or content (rename, move, "
              "permissions, rewrite); not when it was created."),
    'mtime': ("Modified (file system)",
              "Last write to the content; a copy, download or restore can keep "
              "the original's time or set a new one."),
}
LEFT_OUT_TIMES = {
    'atime': "Accessed: phones often don't update it, so it is not trustworthy "
             "enough for the Timeline (owner decision, 2026-11-15)",
}


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


def fingerprint(results_conn, modules: dict, files_key=None) -> str:
    """A hash that changes when anything the Timeline is built from does:
    the last artifact run, each report's row count, each parser's time
    declarations and, for file events, the stages they read (header scan
    for file types, EXIF/video metadata, duplicates) and *files_key*
    (what the caller's file list depends on, e.g. the ignore list)."""
    h = hashlib.sha256()
    h.update(f"v{BUILD_VERSION}".encode())
    if files_key is not None:
        h.update(f"files v{FILES_BUILD_VERSION} {files_key}".encode())
        for rt in _FILE_STAGES:
            try:
                last = results_conn.execute(
                    "SELECT MAX(id) FROM run_log WHERE run_type=?", (rt,)).fetchone()[0]
            except sqlite3.Error:
                last = None
            h.update(f"{rt}={last}".encode())
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
    # The file a file event is about (FILES_SCRIPT rows); '' for reports.
    try:
        cache_conn.execute("ALTER TABLE timeline_events ADD COLUMN path TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists


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


def build(results_conn, cache_conn, modules: dict, cancelled=lambda: False,
          files=None) -> dict:
    """Rebuild the Timeline in *cache_conn* from the artifact tables in
    *results_conn*. *modules*: {script_name: parser module}. *files*: None,
    or {'key': files_key for fingerprint(), 'collect': a callable giving
    file_events' (paths, metadata, time_cols, ignored)} for the file
    events. Returns {'events': n, 'sources': n_on_timeline} or
    {'cancelled': True}."""
    ensure_schema(cache_conn)
    fp = fingerprint(results_conn, modules, files['key'] if files else None)
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
                    _clean(spec.get('caution')), ''))
                n += 1
            status = ON_TIMELINE if n else (NO_ROWS if not rows else NO_READABLE)
            if stale and n:
                status = STALE
            sources.append((name, label, field, status, n, blank, zero, bad, odd, ''))
    if files is not None:
        if cancelled():
            return {'cancelled': True}
        got = files['collect']()
        if isinstance(got, tuple):          # (paths, metadata, time_cols, ignored)
            got = dict(zip(('paths', 'metadata', 'time_cols', 'ignored'), got))
        metadata, time_cols = got['metadata'], got['time_cols']
        runs = {rt: _last_run_of(results_conn, rt) for rt in _FILE_STAGES}
        exif, md5 = _load_exif(cache_conn), _load_md5(cache_conn)
        f_events, f_sources = file_events(
            got['paths'], metadata, time_cols, exif, md5,
            ignored=got.get('ignored', 0), runs=runs)
        events += f_events
        sources += f_sources
        if got.get('cache_paths'):
            url = lambda p: (metadata.get(p) or {}).get('source_url') or ''
            c_events, c_sources = file_events(
                got['cache_paths'], metadata, time_cols, exif, md5, runs=runs,
                script=CACHE_SCRIPT, source=CACHE_SOURCE, with_taken=False,
                detail_of=lambda p: f"{p}  {url(p)}".strip() if url(p) else p,
                extra_caution=_CACHE_CAUTION)
            events += c_events
            sources += c_sources
        if got.get('detail_paths'):
            d_events, d_sources = file_events(
                got['detail_paths'], metadata, time_cols, exif, md5, runs=runs,
                script=DETAIL_SCRIPT, source=DETAIL_SOURCE, with_taken=False,
                extra_caution=_DETAIL_CAUTION)
            events += d_events
            sources += d_sources
        archives = got.get('archives') or {}
        if archives:
            def _arrived(p):
                n, media = archives[p]
                return (f"Archive arrived on the device: {p.rsplit('/', 1)[-1]}, {n:,} file(s)"
                        + (f" ({media:,} pictures/videos)" if media else ''))
            a_events, a_sources = file_events(
                sorted(archives), metadata, time_cols, exif, {}, runs=runs,
                script=ARCHIVE_SCRIPT, source=ARCHIVE_SOURCE, with_taken=False,
                event_of=_arrived, extra_caution=_ARCHIVE_CAUTION)
            events += a_events
            sources += a_sources
    with cache_conn:
        cache_conn.execute('DELETE FROM timeline_events')
        cache_conn.execute('DELETE FROM timeline_sources')
        cache_conn.executemany(
            'INSERT INTO timeline_events (utc_ms, script, report_rowid, field, raw, unit, '
            'meaning, source, app, event, detail, record, caution, path) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', events)
        cache_conn.executemany(
            'INSERT INTO timeline_sources VALUES (?,?,?,?,?,?,?,?,?,?)', sources)
        cache_conn.execute(
            "INSERT OR REPLACE INTO timeline_meta VALUES ('fingerprint', ?)", (fp,))
    return {'events': len(events),
            'sources': len({s[0] for s in sources if s[3] in HAS_EVENTS and s[4]})}


COLUMNS = ['utc_ms', 'meaning', 'source', 'app', 'event', 'detail', 'record',
           'caution', 'raw', 'unit', 'script', 'report_rowid', 'field', 'path']


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
                       ' OR source LIKE ? OR record LIKE ? OR path LIKE ?)')
        args.extend([like] * 7)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', args


def count(cache_conn, start_ms=None, end_ms=None, scripts=None, app=None, text=None) -> int:
    where, args = _where(start_ms, end_ms, scripts, app, text)
    return cache_conn.execute(f'SELECT COUNT(*) FROM timeline_events{where}', args).fetchone()[0]


def count_before(cache_conn, center_ms, start_ms=None, end_ms=None, scripts=None,
                 app=None, text=None, descending=False) -> int:
    """How many matching events come before *center_ms* in the list order
    (earlier ones; later ones when *descending*) — the offset that centres
    a capped list on that time."""
    where, args = _where(start_ms, end_ms, scripts, app, text)
    cmp_ = '>' if descending else '<'
    where = (where + ' AND ' if where else ' WHERE ') + f'utc_ms {cmp_} ?'
    return cache_conn.execute(f'SELECT COUNT(*) FROM timeline_events{where}',
                              args + [int(center_ms)]).fetchone()[0]


def query(cache_conn, start_ms=None, end_ms=None, scripts=None, app=None,
          text=None, descending=False, limit=100_000, offset=0):
    """(rows, total): matching events in time order (ties kept in report
    order), at most *limit* rows from *offset*, and how many matched in
    all — so a capped list can say so (A-51)."""
    where, args = _where(start_ms, end_ms, scripts, app, text)
    total = cache_conn.execute(f'SELECT COUNT(*) FROM timeline_events{where}', args).fetchone()[0]
    order = 'DESC' if descending else 'ASC'
    rows = cache_conn.execute(
        f"SELECT {', '.join(COLUMNS)} FROM timeline_events{where} "
        f"ORDER BY utc_ms {order}, script, report_rowid, field LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)]).fetchall()
    return rows, total


def file_time_ms(value):
    """UTC milliseconds for a file-system time as the archive metadata
    stores it (whole seconds, or nanoseconds for the msgpack formats —
    the same rule the File Browser's own display uses: above 1e10 is
    nanoseconds). None for blank or zero, which mean "no time"."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not v or v != v:
        return None
    return int(round(v / 1e6)) if v > 1e10 else int(round(v * 1000))


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


# ── file events ─────────────────────────────────────────────────────────

def _last_run_of(results_conn, run_type):
    """(complete, parser_version) of the last *run_type* run, or None."""
    try:
        row = results_conn.execute(
            'SELECT complete, parser_version FROM run_log WHERE run_type=? '
            'ORDER BY id DESC LIMIT 1', (run_type,)).fetchone()
    except sqlite3.Error:
        return None
    return (bool(row[0]), row[1]) if row else None


def _load_exif(cache_conn) -> dict:
    try:
        from db_utils import load_media_metadata
        return load_media_metadata(cache_conn)
    except sqlite3.Error:
        return {}


def _load_md5(cache_conn) -> dict:
    try:
        # confirmed duplicates only: a 'possible' row (size + CRC32, not
        # read) never merges two files' events
        return dict(cache_conn.execute(
            "SELECT ui_path, md5 FROM duplicate_files WHERE md5 NOT LIKE 'possible:%'"))
    except sqlite3.Error:
        return {}


def _copies_text(n: int) -> str:
    return f"+{n - 1} identical cop{'y' if n == 2 else 'ies'} with this time"


def file_events(paths, metadata, time_cols, exif, md5, ignored=0, runs=None,
                script=FILES_SCRIPT, source=FILES_SOURCE, with_taken=True,
                event_of=None, detail_of=None, extra_caution=''):
    """(events, sources) for the files *paths* (User Media by default;
    the same rules serve web cache media and unpacked archives, as their
    own *script*/*source*).

    *event_of*(path) / *detail_of*(path): the Event / Detail text (default
    the file name / its path). *extra_caution*: added to every event's
    caution. *with_taken*: also the EXIF/video taken time.

    *metadata*: {ui_path: archive metadata} (the File Browser's).
    *time_cols*: [(header, key)] the archive has (the File Browser's time
    columns); Accessed is left out (LEFT_OUT_TIMES). *exif*:
    media_metadata rows. *md5*: {ui_path: md5} of the duplicate scan's
    confirmed copies. *ignored*: files left out by the User Media ignore
    list. *runs*: {run_type: (complete, parser_version) or None}."""
    from media_metadata_scan import taken_utc_seconds, TAKEN_RULE
    runs = runs or {}
    cols = [(h, k) for h, k in time_cols if k not in LEFT_OUT_TIMES]
    # (group key, field, utc_ms) -> [paths]; a group is a set of proven
    # copies (same MD5), or the file alone.
    hits: dict = {}
    raw_of: dict = {}
    count = {k: [0, 0, 0] for _h, k in cols}        # events, blank, implausible
    taken = {'n': 0, 'local': 0, 'odd': 0}
    in_archive = 0
    for p in paths:
        meta = metadata.get(p) or {}
        grp = md5.get(p) or p
        # A file inside an unpacked archive: its own times are from inside
        # the zip (changes before it was zipped, often local), not when it
        # reached the phone; the archive's own event says that.
        if meta.get('_archive_path'):
            in_archive += 1
        for _h, key in (() if meta.get('_archive_path') else cols):
            ms = file_time_ms(meta.get(key))
            if ms is None:
                count[key][1] += 1
                continue
            count[key][0] += 1
            if not (_PLAUSIBLE_FROM * 1000 <= ms < _PLAUSIBLE_TO * 1000):
                count[key][2] += 1
            # One event per file and time: the kinds that share it are
            # named together ("Created, Changed and Modified").
            hits.setdefault((grp, 'file', ms), {}).setdefault(p, []).append(key)
            raw_of[(p, 'file', ms)] = meta.get(key)
        f = exif.get(p) if with_taken else None
        if f and f.get('taken'):
            secs = taken_utc_seconds(f)
            if secs is None:
                taken['local'] += 1
                continue
            ms = int(round(secs * 1000))
            taken['n'] += 1
            if not (_PLAUSIBLE_FROM * 1000 <= ms < _PLAUSIBLE_TO * 1000):
                taken['odd'] += 1
            hits.setdefault((grp, 'taken', ms), {}).setdefault(p, []).append('taken')
            raw_of[(p, 'taken', ms)] = f"{f['taken']} {f.get('taken_offset')}"

    events = []
    order = [k for _h, k in cols]
    for (grp, kind, ms), by_path in hits.items():
        group = sorted(by_path, key=lambda q: (len(q), q))
        rep = group[0]
        f = exif.get(rep) or {}
        if kind == 'taken':
            key = 'taken'
            video = f.get('duration_s') is not None
            meaning = ("Recorded (video container time, UTC)" if video else
                       f"Taken (EXIF DateTimeOriginal, offset {f.get('taken_offset')})")
            caution = ("The camera's clock and time zone setting when the "
                       "picture was taken; editing apps can rewrite it.")
            unit = 'exif'
        else:
            kinds = sorted({k for ks in by_path.values() for k in ks}, key=order.index)
            key = '+'.join(kinds)
            names = [FILE_TIMES[k][0].replace(' (file system)', '') for k in kinds]
            meaning = (names[0] if len(names) == 1 else
                       ', '.join(names[:-1]) + ' and ' + names[-1]) + ' (file system)'
            caution = ' '.join(f"{n}: {FILE_TIMES[k][1]}" if len(kinds) > 1 else FILE_TIMES[k][1]
                               for n, k in zip(names, kinds))
            unit = 'file'
        if extra_caution:
            caution = f"{extra_caution} {caution}"
        notes = []
        if not (_PLAUSIBLE_FROM * 1000 <= ms < _PLAUSIBLE_TO * 1000):
            notes.append('time outside 2000–2100: check the value')
        base = detail_of(rep) if detail_of else rep
        detail = base if len(group) == 1 else f"{base}  ({_copies_text(len(group))})"
        event = event_of(rep) if event_of else rep.rsplit('/', 1)[-1]
        events.append((ms, script, 0, key, _clean(raw_of.get((rep, kind, ms))), unit,
                       meaning, source, '', event, detail,
                       '; '.join(notes), caution, rep))

    sources = []
    dup = runs.get('duplicate_detection')
    dup_note = ('' if dup and dup[0] else
                'Duplicate detection not run: identical copies are listed separately. ')
    ign = f"{ignored:,} files on the User Media ignore list left out. " if ignored else ''
    if in_archive:
        ign += (f"{in_archive:,} files inside unpacked archives: their own times are not "
                f"used (see {ARCHIVE_SOURCE}). ")
    for header, key in cols:
        n, blank, odd = count[key]
        sources.append((script, source, header,
                        ON_TIMELINE if n else NO_READABLE, n, blank, None, None, odd,
                        (dup_note + ign).strip()))
    for header, key in time_cols:
        if key in LEFT_OUT_TIMES:
            sources.append((script, source, header, NOT_AN_EVENT,
                            None, None, None, None, None, LEFT_OUT_TIMES[key]))
    if not with_taken:
        return events, sources
    mm = runs.get('media_metadata')
    if not (mm and mm[0] and (mm[1] or 0) >= TAKEN_RULE):
        sources.append((script, source, 'Taken (EXIF / video)', NOT_RUN,
                        None, None, None, None, None,
                        'Run EXIF/video metadata in Process Case (a run before '
                        '2026-11-15 read the wrong EXIF time).'))
    else:
        note = (f"{taken['local']:,} photos have a taken time without a UTC offset "
                "(local time): not placed on the Timeline; see the File Browser's "
                "Taken (EXIF) column." if taken['local'] else '')
        sources.append((script, source, 'Taken (EXIF / video)',
                        ON_TIMELINE if taken['n'] else NO_READABLE, taken['n'],
                        None, None, None, taken['odd'], note))
    return events, sources
