"""mcp_server.py — read-only MCP server exposing processed case data to AI clients.

Phase-1 surface (see also mcp_control.py for the GUI-side lifecycle):

  Tier 1 — processed content: artifact tables (sms_messages, whatsapp,
           photos_metadata, …), saved search results, bookmarks, device info,
           research-status notes.
  Tier 2 — file tree as *queryable metadata*: list_folder / find_paths /
           get_file_metadata return paths, sizes, timestamps and detected
           types — never file contents.
  Tier 3 — raw content, opt-in per case on top of Tier 1/2 (see the
           "Enable AI Access" dialog's second checkbox; ctx.raw_content_enabled
           gates every tool below). Deliberately narrow: SQLite schema/sample
           rows only (get_sqlite_schema / sample_sqlite_rows) — never
           arbitrary raw SQL and never a generic "read any file's bytes"
           tool. Exists so an AI client can draft a new artifact-parser
           script (artifacts/ios|android/ format) for an app with no parser
           yet, without an examiner shelling out to inspect the archive by
           hand. Drafted scripts are prose/code in the chat — never
           auto-installed or auto-run against the case.

Design constraints:
  * Read-only towards the case: every SQLite open uses mode=ro; the in-memory
    dicts (ui_metadata, folder_map, …) are treated as immutable snapshots.
  * Qt-free: runs on a plain background thread inside the GUI process (or any
    other host).  CaseContext carries only plain-Python data and callables.
  * Every tool call is audit-logged to caseresults.db run_log (run_type
    'mcp'), so an examiner can state exactly what an AI client accessed.
  * All row-returning tools are capped (MAX_ROWS) — MCP results land in a
    model context window; unbounded dumps help nobody and cost tokens.

The `mcp` package is imported lazily by the caller (mcp_control) so the GUI
never pays the import cost unless AI access is enabled.
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Callable

import research_store as _research
import app_intelligence
from db_utils import (_open_cache_db, save_app_intelligence, load_app_intelligence,
                      load_blob, save_blob, load_app_registry)

MAX_ROWS = 500          # hard cap for any row-returning tool
DEFAULT_ROWS = 100
MAX_SAMPLE_ROWS = 50    # Tier 3 sample_sqlite_rows cap — exploratory, not a dump

_SQLITE_MAGIC = b'SQLite format 3\x00'


# ── Case context ──────────────────────────────────────────────────────────────

@dataclass
class CaseContext:
    """Plain-data bridge from the host (the GUI) to the server tools.

    The getters return the host's *live* dicts; tools only read them.  The
    host must replace these wholesale on archive reload (which the GUI does)
    rather than mutate them in place.
    """
    case_dir: str
    zip_path: str = ''
    get_ui_metadata: Callable[[], dict] = field(default=lambda: {})
    get_folder_map: Callable[[], dict] = field(default=lambda: {})
    get_folder_sizes: Callable[[], dict] = field(default=lambda: {})
    get_guid_to_bundle: Callable[[], dict] = field(default=lambda: {})
    get_header_types: Callable[[], dict] = field(default=lambda: {})
    adapter: object = None          # FfsAdapter (Qt-free) or None
    raw_content_enabled: bool = False
    read_bytes: Callable[[str], object] = field(default=lambda path: None)
    # ^ (ui_path) -> bytes | None — the host's own zip reader; Tier 3 tools
    # only. Never mutate the returned bytes.


# ── Read-only DB access ───────────────────────────────────────────────────────

def _open_ro(case_dir: str, name: str) -> sqlite3.Connection:
    """Open a case DB strictly read-only; raises if it doesn't exist."""
    path = os.path.join(case_dir, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{name} not found in case folder")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _audit(case_dir: str, tool: str, args: dict) -> None:
    """One run_log row per tool call — the case file's record of AI access."""
    try:
        conn = sqlite3.connect(os.path.join(case_dir, 'caseresults.db'), timeout=5)
        with conn:
            conn.execute(
                "INSERT INTO run_log (run_type, complete, notes) VALUES ('mcp', 1, ?)",
                (json.dumps({'tool': tool, 'args': args}, default=str)[:2000],))
        conn.close()
    except Exception:
        pass    # auditing must never break a read


_IDENT_RE = re.compile(r'^[A-Za-z0-9_]+$')


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or DEFAULT_ROWS), MAX_ROWS))


def _blob_safe(v, _max=64):
    """BLOB columns come back from sqlite3 as raw bytes, which the MCP
    transport cannot JSON-serialize (breaks the whole response, not just
    that cell) — surface size + a hex preview instead of dropping the value."""
    if isinstance(v, (bytes, bytearray)):
        return f'<blob {len(v)} bytes, hex: {v[:_max].hex()}{"…" if len(v) > _max else ""}>'
    return v


# ── Tier 3: raw SQLite access (opt-in) ──────────────────────────────────────

def _extract_sqlite_ro(ctx: CaseContext, path: str) -> tuple[sqlite3.Connection, str]:
    """Extract *path* (a ui_path) plus any -wal/-shm sidecars to a locked-down
    temp copy and open it strictly read-only.

    Sidecars are pulled in unmodified and the copy is chmod'd read-only
    *before* sqlite3 ever touches it, then opened via a `mode=ro` URI — never
    a bare connect() — so nothing here can trigger an auto-checkpoint that
    would alter the extracted WAL (see the project's WAL-handling note: a
    live connection only ever shows current state, never what checkpointing
    might discard). Caller must close the connection and rmtree the tmpdir.
    """
    path = (path or '').strip('/')
    raw = ctx.read_bytes(path)
    if raw is None:
        raise FileNotFoundError(f'{path!r} not found or unreadable in archive')
    if raw[:16] != _SQLITE_MAGIC:
        raise ValueError(f'{path!r} is not a SQLite database (bad header)')

    tmpdir = tempfile.mkdtemp(prefix='ffs_mcp_sqlite_')
    db_path = os.path.join(tmpdir, os.path.basename(path) or 'db')
    try:
        with open(db_path, 'wb') as f:
            f.write(raw)
        for suffix in ('-wal', '-shm'):
            sidecar = ctx.read_bytes(path + suffix)
            if sidecar and sidecar[:16] != _SQLITE_MAGIC:
                with open(db_path + suffix, 'wb') as f:
                    f.write(sidecar)
        for suffix in ('', '-wal'):
            p = db_path + suffix
            if os.path.isfile(p):
                os.chmod(p, 0o444)
        # -shm deliberately left writable: it is SQLite's shared-memory
        # WAL-index (pure in-process bookkeeping, never evidence — the WAL
        # frames themselves live in -wal, which stays read-only above).
        # SQLite's own docs say a reader needs -shm write access to safely
        # use the WAL; chmod'ing it 444 here was tested and made no
        # difference on the one case exercised so far, but the risk of it
        # mattering on a different SQLite build/version is not worth taking
        # for a file that is pure bookkeeping, not evidence.
        #
        # Separately — and this is the one actually confirmed against real
        # data — reading a WAL-mode db file WITHOUT its -wal sidecar is not
        # simply "missing the newest rows": it can show MORE rows than
        # currently exist. A table here showed 8 rows read main-file-only,
        # vs. 1 row with the WAL correctly applied (matching the system
        # sqlite3 CLI + `PRAGMA integrity_check` = ok) — the extra 8 were
        # stale/superseded page content the WAL had since overwritten, not
        # real data. Always pull -wal alongside the main file; never treat
        # a WAL-less read as a safe "no worse than incomplete" fallback.
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
        return conn, tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


# ── Server factory ────────────────────────────────────────────────────────────

def build_server(ctx: CaseContext):
    """Create and return a FastMCP server with all tools/prompts registered."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "ffs-explorer",
        instructions=(
            "Read-only access to a processed forensic extraction (FFS "
            "Explorer case). Results are investigative leads — always cite "
            "the artifact/table, row values, or file path so the examiner "
            "can verify in the application. Nothing here writes to evidence."),
        stateless_http=True,
    )

    import functools
    import inspect

    def tool(fn):
        """Register fn as a tool with audit logging around it.  The explicit
        __signature__ copy matters: FastMCP builds the tool's input schema
        from the signature, and a bare *args/**kwargs wrapper would hide the
        real parameters."""
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            _audit(ctx.case_dir, fn.__name__, kw or {})
            return fn(*a, **kw)
        wrapper.__signature__ = inspect.signature(fn)
        return mcp.tool()(wrapper)

    # ── Tier 1: processed content ────────────────────────────────────────────

    @tool
    def get_case_overview() -> dict:
        """Case orientation: device info, artifact tables with row counts,
        bookmark groups, saved search terms, and research-note count.
        Call this first in a session."""
        out: dict = {'archive': os.path.basename(ctx.zip_path or '') or None}
        with _open_ro(ctx.case_dir, 'caseresults.db') as db:
            out['device'] = {r[0]: r[1] for r in db.execute(
                'SELECT field_name, data FROM device_info')}
            arts = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'artifact_%'")]
            out['artifacts'] = {
                a[len('artifact_'):]: db.execute(
                    f'SELECT COUNT(*) FROM "{a}"').fetchone()[0]
                for a in arts}
            out['bookmark_groups'] = [
                {'name': r[0], 'entries': r[1]} for r in db.execute(
                    'SELECT g.name, COUNT(e.id) FROM bookmark_groups g '
                    'LEFT JOIN bookmark_entries e ON e.group_id=g.id GROUP BY g.id')]
            out['saved_searches'] = [r[0] for r in db.execute(
                'SELECT keyword FROM search_index ORDER BY used_at DESC')]
        out['files_indexed'] = len(ctx.get_ui_metadata())
        out['research_notes'] = len(_research.load())
        return out

    @tool
    def query_artifact(name: str, contains: str = '', limit: int = DEFAULT_ROWS,
                       offset: int = 0) -> dict:
        """Rows from a parsed artifact table (see get_case_overview for names,
        e.g. 'sms_messages', 'whatsapp', 'photos_metadata').  `contains`
        filters case-insensitively across all columns.  Returns
        {columns, rows, total_matching}; rows are capped at 500 — page with
        offset."""
        if not _IDENT_RE.match(name or ''):
            return {'error': f'invalid artifact name: {name!r}'}
        limit = _clamp(limit)
        with _open_ro(ctx.case_dir, 'caseresults.db') as db:
            table = f'artifact_{name}'
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                "AND name=?", (table,)).fetchone()
            if not exists:
                return {'error': f'artifact {name!r} has not been run on this case'}
            cols = [d[1] for d in db.execute(f'PRAGMA table_info("{table}")')]
            where, params = '', []
            if contains:
                where = ' WHERE ' + ' OR '.join(
                    f'"{c}" LIKE ? COLLATE NOCASE' for c in cols)
                params = [f'%{contains}%'] * len(cols)
            total = db.execute(
                f'SELECT COUNT(*) FROM "{table}"{where}', params).fetchone()[0]
            rows = db.execute(
                f'SELECT * FROM "{table}"{where} LIMIT ? OFFSET ?',
                params + [limit, max(0, int(offset))]).fetchall()
        return {'columns': cols, 'rows': rows, 'total_matching': total}

    @tool
    def get_search_results(term: str, limit: int = DEFAULT_ROWS,
                           offset: int = 0) -> dict:
        """Hits for a keyword search previously run in FFS Explorer (see
        get_case_overview → saved_searches).  Each hit: file path, byte
        offset, and surrounding text context."""
        limit = _clamp(limit)
        with _open_ro(ctx.case_dir, 'caseresults.db') as db:
            row = db.execute('SELECT id, complete, files_searched, total_files '
                             'FROM search_index WHERE keyword=?', (term,)).fetchone()
            if row is None:
                return {'error': f'no saved search for {term!r} — run it in the app first'}
            total = db.execute('SELECT COUNT(*) FROM search_results WHERE term_id=?',
                               (row[0],)).fetchone()[0]
            hits = [{'path': r[0], 'offset': r[1], 'context': r[2]}
                    for r in db.execute(
                        'SELECT filename, offset, context FROM search_results '
                        'WHERE term_id=? LIMIT ? OFFSET ?',
                        (row[0], limit, max(0, int(offset))))]
        return {'term': term, 'complete': bool(row[1]), 'total_hits': total,
                'hits': hits}

    @tool
    def list_bookmarks() -> list:
        """All bookmark groups with their bookmarked paths — the examiner's
        own flagged items ('Evidence', 'Interesting', …)."""
        with _open_ro(ctx.case_dir, 'caseresults.db') as db:
            groups = db.execute('SELECT id, name, description FROM bookmark_groups '
                                'ORDER BY created_at').fetchall()
            return [{'group': g[1], 'description': g[2],
                     'paths': [r[0] for r in db.execute(
                         'SELECT ui_path FROM bookmark_entries WHERE group_id=? '
                         'ORDER BY bookmarked_at', (g[0],))]}
                    for g in groups]

    @tool
    def get_research_notes(name: str = '') -> list:
        """Curated cross-case knowledge about artifact value ('indicative
        behaviour'): per Biome stream / filename — outcome (of_value /
        no_value / unknown), reason with citation, iOS versions covered, and
        assessment date.  Use it to prioritise streams and avoid known noise;
        treat marks assessed on older iOS versions as needing re-verification.
        Optional `name` substring-filters the keys."""
        marks = _research.load()
        out = []
        for key, rec in marks.items():
            if name and name.lower() not in key.lower():
                continue
            out.append({'key': key, **{k: rec.get(k) for k in
                        ('outcome', 'reason', 'ios_versions', 'published',
                         'assessed')}})
        return out[:MAX_ROWS]

    # ── Tier 2: file tree as metadata ────────────────────────────────────────

    def _entry_meta(path: str, ui_metadata: dict, folder_map: dict) -> dict:
        m = ui_metadata.get(path) or {}
        is_folder = path in folder_map
        d = {'path': path, 'is_folder': is_folder}
        for k in ('size', 'mtime', 'ctime', 'btime'):
            if m.get(k):
                d[k] = m[k]
        if is_folder:
            size = ctx.get_folder_sizes().get(path)
            if size is not None:
                d['total_bytes'] = size
        else:
            ht = ctx.get_header_types().get(path)
            if ht:
                d['detected_type'] = ht
        return d

    @tool
    def list_folder(path: str = '', limit: int = MAX_ROWS) -> dict:
        """Immediate children of a folder ('' = root) with metadata: size,
        Unix timestamps (mtime/ctime/btime), detected type, and folder total
        sizes.  Metadata only — no file contents."""
        folder_map = ctx.get_folder_map()
        ui_metadata = ctx.get_ui_metadata()
        path = (path or '').strip('/')
        if path and path not in folder_map:
            return {'error': f'no such folder: {path!r} — try find_paths'}
        children = sorted(folder_map.get(path, []))
        limit = _clamp(limit)
        return {'path': path, 'total_children': len(children),
                'children': [_entry_meta(p, ui_metadata, folder_map)
                             for p in children[:limit]]}

    @tool
    def find_paths(substring: str, limit: int = 200) -> dict:
        """Case-insensitive substring search over every file/folder path in
        the archive.  Use to check whether specific files exist (databases,
        media referenced in messages, app files) and get their metadata."""
        if not substring or len(substring) < 2:
            return {'error': 'substring must be at least 2 characters'}
        ui_metadata = ctx.get_ui_metadata()
        folder_map = ctx.get_folder_map()
        needle = substring.lower()
        limit = _clamp(limit)
        matches, total = [], 0
        for p in ui_metadata:
            if needle in p.lower():
                total += 1
                if len(matches) < limit:
                    matches.append(_entry_meta(p, ui_metadata, folder_map))
        return {'total_matching': total, 'matches': matches}

    @tool
    def get_file_metadata(path: str) -> dict:
        """Full metadata for one path: size, all timestamps, detected type,
        and (for app-container paths) the owning bundle id."""
        ui_metadata = ctx.get_ui_metadata()
        folder_map = ctx.get_folder_map()
        path = (path or '').strip('/')
        if path not in ui_metadata and path not in folder_map:
            return {'error': f'path not in archive: {path!r} — try find_paths'}
        d = _entry_meta(path, ui_metadata, folder_map)
        bundle = (ctx.adapter.bundle_id_for_path(path, ctx.get_guid_to_bundle())
                 if ctx.adapter else None)
        if bundle:
            d['app_bundle'] = bundle
        return d

    @tool
    def list_app_containers(limit: int = MAX_ROWS) -> dict:
        """Installed-app containers: bundle id, container path, and total
        bytes of data, sorted largest first.  Cross-reference with
        get_case_overview → artifacts to find apps that hold data but have
        no parser output yet (coverage gaps worth manual review)."""
        guid_map = ctx.get_guid_to_bundle()
        folder_map = ctx.get_folder_map()
        sizes = ctx.get_folder_sizes()
        parents = ctx.adapter.container_parents() if ctx.adapter else []
        out = []
        for parent in parents:
            for child in folder_map.get(parent, []):
                bundle = (ctx.adapter.container_bundle_id(child, guid_map)
                         if ctx.adapter else None)
                if bundle:
                    out.append({'bundle_id': bundle, 'path': child,
                                'total_bytes': sizes.get(child, 0)})
        out.sort(key=lambda d: d['total_bytes'], reverse=True)
        return {'total': len(out), 'containers': out[:_clamp(limit)]}

    @tool
    def get_app_data_locations(bundle_id: str) -> dict:
        """Every known container location for one iOS app in a single
        call — Bundle, Data, every App Group it's entitled to (each with
        its own container path), and any PluginKit extensions (a
        Share/Notification extension etc., each with its own bundle id
        that's a dotted suffix of the main one, e.g.
        'com.example.app.ShareExtension') — the direct answer to "where is
        all of this app's data," regardless of which folder it happens to
        live in. Backed by app_registry, a case-load-time-built registry
        (see CLAUDE.md) derived from the device's own LaunchServices
        cache — independent of the per-container metadata plists
        list_app_containers/get_file_metadata use, confirmed on real
        casework to sometimes be missing on GrayKey extractions. iOS
        only — Android has no GUID-container indirection to resolve, so
        list_app_containers already gives the complete picture there.
        Returns an error if app_registry hasn't been built for this case
        (an older case reopened before this feature existed — reopening
        the case rebuilds it)."""
        with closing(_open_cache_db(ctx.case_dir)) as db:
            rows = load_app_registry(db)
        if not rows:
            return {'error': 'app_registry is empty for this case — reopen '
                             'the case to rebuild it (Android cases never '
                             'populate this table; there is no GUID-container '
                             'indirection to resolve there)'}
        matches = [r for r in rows if r['bundle_id'] == bundle_id
                  or r['bundle_id'].startswith(bundle_id + '.')]
        if not matches:
            return {'error': f'{bundle_id!r} not found in app_registry — '
                             'check the exact bundle id via list_app_containers '
                             'or list_apps'}
        main = next((r for r in matches if r['bundle_id'] == bundle_id), None)
        extensions = [r for r in matches if r is not main]
        return {
            'bundle_id': bundle_id,
            'display_name': main['display_name'] if main else None,
            'team_id': main['team_id'] if main else None,
            'bundle_container_path': main['bundle_container_path'] if main else None,
            'data_container_path': main['data_container_path'] if main else None,
            'app_groups': (main or {}).get('app_group_paths', {}),
            'extensions': [{'bundle_id': r['bundle_id'],
                           'bundle_container_path': r['bundle_container_path']}
                          for r in extensions],
        }

    @tool
    def list_apps(min_score: int = 0, limit: int = MAX_ROWS) -> dict:
        """Per-app coverage/category/permission intelligence, pre-sorted so
        the FRONT of the list needs minimal further reasoning — read the
        top entries in order and trust the ordering; do not re-derive
        priority from score alone, and do not re-investigate an app via
        find_paths/get_sqlite_schema unless something below tells you to.

        One row per APP IDENTITY, not per physical folder (changed
        2026-08-24 after a real gap: Telegram's Data/Application container
        and its Shared/AppGroup container used to be two separate,
        unrelated-looking rows — 'ph.telegra.Telegraph' showing nothing,
        while the real 73MB store sat entirely in
        'group.ph.telegra.Telegraph', which nothing on the first row
        pointed at). A row now pools every Data/Application + Shared/
        AppGroup container that belongs to the same app (resolved via the
        device's own app_registry) into one entry — `containers` lists
        each physical folder that was merged (`{app_id, path, kind}`,
        kind is 'data' or 'app_group'), and `evidence_databases`/
        `webview_storage`/`hidden_vault_storage` are pooled and re-ranked
        across ALL of them together, so a real store in the App-Group
        folder is never shadowed by noise in the Data folder or missed
        because you only checked one. PluginKit extensions (Share/
        Notification extensions etc.) still get their OWN separate rows —
        they're genuinely distinct components, and unlike an App-Group's
        opaque id, an extension's own app_id is already self-describing
        (a dotted suffix of the host app's).

        Each app carries, already computed:
        - display_name — the App Store/device-registered name (e.g.
          'TikTok' for app_id 'com.zhiliaoapp.musically'), sourced from
          the device's own LaunchServices registry (app_registry, iOS
          only — null on Android and null for any app that registry
          doesn't cover). Use this when reporting/discussing an app to a
          human — a bare bundle id alone isn't clear about "what app is
          this," which is the actual triage question this tool exists to
          answer, not just a citation key.
        - score (0-10) + score_breakdown (why) — a heuristic, not a
          factual finding, but the ranking already accounts for the fields
          below, not just this number.
        - has_parser — an app already covered needs no further look here
          for WHERE its data is: known_location (below) already names the
          confirmed container this project's own parser reads.
        - recently_used (bool) — activity within the case's own active
          window, already computed; don't re-derive this from
          last_activity/archive timestamps yourself.
        - last_activity / last_activity_utc — last_activity is a raw
          nanosecond-epoch integer (kept for programmatic comparison);
          last_activity_utc is the same value already formatted and
          labeled ('2023-06-29 16:36:42 UTC') — use last_activity_utc
          whenever reporting a date to a human, per this project's own
          Conventions on evidence timestamps (always UTC, always labeled,
          never a bare unlabeled value an epoch-unit guess could get
          wrong). null if the app has no timestamped files at all.
        - known_location — {app_path_or_group, has_media_fields} when
          has_parser is true, else null. This project's own parser for
          this app already declares exactly which container it reads
          (app_path_or_group) and whether it also tracks media/attachment
          files (has_media_fields) — added 2026-08-25 specifically so a
          has_parser=true row states what's ALREADY confirmed known
          instead of going quiet just because evidence_databases wasn't
          computed for it (which is skipped for parsed apps on purpose —
          re-discovering a location this project's own code already
          knows would be redundant work, not extra rigor).
        - evidence_databases — up to 5 candidate SQLite/db files actually
          found across EVERY container this app's data lives in (Data
          Application + every App-Group container merged into this one
          row — see the note on merged rows above), ranked (not just
          filtered) — this function does NOT try to guess a single "the"
          evidence database
          beyond excluding zero-ambiguity platform noise (Apple's own
          TipKit/HTTPStorages/SafariViewService caches, etc. — confirmed
          NEVER app content for ANY app, so these never even appear as
          candidates). Telling real app content apart from a bundled SDK's
          own telemetry/analytics file by filename alone is a genuinely
          open-ended judgment call — confirmed unbounded on real casework
          (2026-08-24): even after hand-excluding four confirmed-noise SDK
          files, two MORE unrelated ones immediately took their place on
          the same two apps. So that judgment is now YOUR job, not this
          list's. Each candidate carries:
            - path, bytes (base file size only)
            - wal_bytes, wal_present — a live WAL can hold ALL of a
              database's real content while its base file looks nearly
              empty (confirmed: Instagram's real store was a 4KB base file
              with 1.4MB in its -wal); wal_present=true + small bytes is
              NOT the same as "probably empty" — check wal_bytes too
            - shm_present — informational only, never sized (fixed-size
              shared-memory index, never real content)
            When raw_content_enabled is on, a file with NO extension at
            all also gets its header magic-byte checked (not just name-
            matched) — confirmed necessary on real casework: Telegram's
            actual message store is a file literally named 'db_sqlite'
            (underscore, not '.sqlite'), invisible to name-based matching
            entirely until this was added.
          Candidates are pre-sorted by bytes+wal_bytes descending — that's
          the only signal this list carries about which one is "more
          interesting," NOT which one is real. There is deliberately no
          citation field here (an earlier version had one,
          'known_real_store', cross-referenced against iLEAPP/ALEAPP —
          removed the same day it was added: baking a per-app answer key
          into live ranking risks testing whether the answer key is right
          rather than whether this general size/WAL/noise-filter mechanism
          actually works, and iLEAPP/ALEAPP are themselves live, actively-
          maintained projects that would make any embedded snapshot go
          stale). So: for the top candidate (or any candidate you're about
          to name as "the" evidence database), if raw_content_enabled is
          on, call get_sqlite_schema on it — and on the next 1-2 candidates
          too if they're close in size — and prefer whichever has a
          message-shaped table (columns suggesting sender/body/text/
          timestamp) over one that reads as a log/metrics/event store
          (columns suggesting event_name/session/duration/counter). If
          raw_content_enabled is off, say so explicitly rather than naming
          one with false confidence. Empty list (not null) means nothing
          survived even the zero-ambiguity filter.

          STANDING TRIAGE STEP, not just a schema check (added 2026-08-25):
          a candidate's own FILENAME can be evidence too, not just its
          schema. Before dismissing a purely-numeric or otherwise opaque
          filename (no readable name, just digits or a hex/UUID-looking
          stem), check whether that number matches an account/user/thread
          ID you've already seen elsewhere for this same app — in another
          candidate's decoded content, in a sibling container's path, or in
          a value already returned by a parser run against this app. A
          match is a real positive signal (a fixed 1:1 naming convention,
          not a coincidence), independent of and in addition to the size/
          schema check above — don't wait to happen to notice this, check
          it every time an opaque filename shows up. Confirmed real,
          2026-08-25: Instagram's actual message store is named
          '<numeric-account-id>.db' (e.g. '53079604238.db') under
          DirectSQLiteDatabase/ — the digits are that install's own
          Instagram account id, the same id that turns up as
          sender_pk/viewer_id inside the decoded message content itself
          once you're in the file. An examiner (or an LLM) that only
          schema-checks would still catch this one since it's also the
          largest/only real candidate here, but on an app with several
          per-account or per-thread files (mirroring Instagram's own
          layout, or a multi-account app), the filename-ID match is what
          tells you WHICH of several similarly-shaped files belongs to the
          account/thread you actually care about — schema shape alone
          can't distinguish two files with identical columns.
        - evidence_databases_total — the TRUE count of candidates that
          survived filtering, before the top-5 cutoff. If this is bigger
          than len(evidence_databases), you are NOT seeing everything.
          Confirmed necessary on real casework (2026-08-25): TikTok has 34
          real candidates in one container; its two actual message stores
          rank #7 and #12 by size, both invisible in the top 5 even though
          confirmed real by schema. So: if the top 5 you DO see all read
          as telemetry/log-shaped once you schema-check them (see above),
          and evidence_databases_total says there's more, that combination
          means "keep looking further down," not "no real evidence
          exists." Call list_evidence_candidates(app_id) for the SAME
          app_id to page deeper (it reuses this row's own container list)
          and keep schema-checking new entries — don't stop at the first
          5 and report nothing found while the total says otherwise.
        - webview_storage — {path, bytes, other_stores} if the container
          has Chromium-style WebView local storage (IndexedDB/LevelDB —
          common for a hybrid/WebView-based app, e.g. seen for real on
          Android under app_webview/Default/), else null. This is a
          COMPLETELY DIFFERENT storage format from evidence_databases (a
          folder of files, not one file) that a plain SQLite scan can
          never see — detected here, not read; its actual contents need a
          dedicated LevelDB/IndexedDB reader this project doesn't have
          yet. A non-null hit here is real signal even when
          evidence_databases is empty.
        - hidden_vault_storage — {path, bytes, other_stores} if the
          container has a folder matching a confirmed vault-app (hide-and-
          lock) storage signature (e.g. '.Calculator_Lock', '.galleryvault_',
          'applocker/vault'), else null. These apps typically dump raw
          media into this folder with renamed/extensionless filenames and
          NO database at all — invisible to evidence_databases by design,
          not just outranked. Treat a non-null hit as a strong signal
          regardless of what evidence_databases or category says — a vault
          app deliberately looks unremarkable everywhere else. This
          detection is by folder-NAME signature only, so it's necessarily
          NOT exhaustive — an unrecognized vault app won't be caught, since
          that's the entire point of a vault app's naming.
        - encryption_caveat — non-null means this app's local store is
          known (or reasonably inferred) to be encrypted at rest (e.g.
          Signal's SQLCipher database) — real evidence, but NOT a quick
          parser win; still requires the device's own key material.
        The sort order reflects most of this, but NOT which specific
        evidence_databases candidate is real — that part is now your call,
        per the field's own guidance above. Same score: a hidden_vault_storage
        hit ranks highest (a confirmed vault-folder signature match, not a
        size guess), ahead of a clean-but-unconfirmed top evidence_databases
        candidate, which ranks above a WebKit-fallback hit or a
        webview_storage-only hit, which both rank above nothing; a known
        encryption caveat is pushed down; then recently-used; then size.
        So has_parser false + high in the list + a non-empty
        evidence_databases = worth opening, but "which candidate" still
        needs the check described above before you name one — unless
        hidden_vault_storage is also non-null, which is worth flagging on
        its own regardless of what evidence_databases says.

        Works without raw content access (category/permissions_declared
        come back null and are scored as 'unknown' rather than 'confirmed
        absent'; evidence_databases/recently_used are unaffected — they're
        Tier 2, name/timestamp-based, no raw content needed at all — but
        the schema-peek step in evidence_databases' own guidance needs
        raw_content_enabled, so say so rather than guessing when it's off).
        Cached in casecache.db — recomputed automatically when the
        archive's indexed file count or raw-content-access state has
        changed since the last scan, OR when app_intelligence.py's own
        scan logic has changed (scan_logic_version() — added 2026-08-26
        after a real fix silently kept serving a stale pre-fix cached scan
        on an already-scanned case with no signal anything was stale)."""
        ui_metadata = ctx.get_ui_metadata()
        # Cache key covers the archive's indexed file count, whether raw
        # content access is on (a case scanned once with it off, then
        # enabled later, must rescan to pick up category/permissions rather
        # than keep serving the earlier Tier-2-only snapshot), AND a content
        # hash of this module's own source — see scan_logic_version().
        cache_key = f'{len(ui_metadata)}:{ctx.raw_content_enabled}:{app_intelligence.scan_logic_version()}'
        with closing(_open_cache_db(ctx.case_dir)) as cache_db:
            stale = load_blob(cache_db, 'app_intelligence_scan_key', '1')
            rows = load_app_intelligence(cache_db)
            if not rows or stale is None or stale.decode() != cache_key:
                rows = app_intelligence.scan_apps(ctx)
                save_app_intelligence(cache_db, rows)
                save_blob(cache_db, 'app_intelligence_scan_key', '1', cache_key.encode())
        rows = [r for r in rows if r['score'] >= min_score]
        return {'total': len(rows), 'raw_content_enabled': ctx.raw_content_enabled,
                'apps': rows[:_clamp(limit)]}

    @tool
    def list_evidence_candidates(app_id: str, limit: int = 50) -> dict:
        """[Tier 2] Every find_evidence_databases candidate for one app,
        unbounded up to *limit* — the escape hatch for list_apps'
        evidence_databases field, which is capped at 5 per app and can
        silently omit the real store on an app with a lot of noise files.
        Confirmed on real casework (2026-08-25, iOS 16.5 CTF23
        Cellebrite): TikTok had 34 real candidates in one container alone;
        its two actual message stores ranked #7 and #12 by size, both
        invisible at list_apps' top-5 cutoff even though a schema check
        confirms them real (contactName/nickname/latestChatTimestamp
        columns) against the top 5's telemetry shape (track_id/entire_log/
        session_id).

        Call this whenever list_apps' evidence_databases_total for an app
        is larger than the 5 candidates it showed you, AND get_sqlite_schema
        on those 5 (with raw_content_enabled on) shows none of them look
        message-shaped (sender/body/text/timestamp columns) — that
        combination means the real store is probably further down the
        list, not that this app has no real evidence. Keep paging deeper
        with a higher *limit* and schema-checking new entries until you
        find one that looks like real content or you've covered
        evidence_databases_total candidates — don't stop at the first
        page and conclude "no evidence" while total says there's more.

        Requires list_apps to have been called first in this session for
        this case — reuses its cached container list for *app_id* (the
        same 'containers' its list_apps row showed). Returns an error
        naming that if the app_id isn't in the cache yet."""
        with closing(_open_cache_db(ctx.case_dir)) as cache_db:
            rows = load_app_intelligence(cache_db)
        row = next((r for r in rows if r['app_id'] == app_id), None)
        if row is None:
            return {'error': f'{app_id!r} not found in the cached list_apps '
                             'result for this case — call list_apps first '
                             '(this tool reuses its container list rather '
                             'than re-deriving it).'}
        folder_map = ctx.get_folder_map()
        ui_metadata = ctx.get_ui_metadata()
        read_bytes = ctx.read_bytes if ctx.raw_content_enabled else None
        all_candidates: list = []
        for c in row['containers']:
            cands, _total = app_intelligence.find_evidence_databases(
                c['path'], folder_map, ui_metadata, limit=1000, read_bytes=read_bytes)
            all_candidates.extend(cands)
        all_candidates.sort(key=lambda e: e['bytes'] + e['wal_bytes'], reverse=True)
        clamped = _clamp(limit)
        return {'app_id': app_id, 'total_candidates': len(all_candidates),
                'candidates': all_candidates[:clamped]}

    # ── Tier 3: raw SQLite access (opt-in — see CaseContext.raw_content_enabled) ──

    @tool
    def get_sqlite_schema(path: str) -> dict:
        """[Raw content — opt-in] Schema of a SQLite database anywhere in the
        archive: every table AND VIEW's columns (name/type/notnull/pk),
        foreign keys, and row count. Extracted to a locked-down read-only
        temp copy (WAL/SHM sidecars included when present, never
        checkpointed/altered). Use this to explore a database behind an app
        container that has no artifact parser yet — find it first with
        find_paths, e.g. find_paths('databases') under a bundle's container
        path.

        Includes VIEWS, not just base tables (fixed 2026-08-25 after a real
        miss: Facebook Messenger's actual message content lives entirely
        behind a view named 'thread_messages' over its Lightspeed storage
        engine — confirmed working via iLEAPP's own facebookMessenger.py,
        which queries that exact view name — but this tool's earlier
        table-only filter meant it never appeared here at all, wrongly
        making the database look unreadable). A single table/view whose
        introspection fails (e.g. one built on a SQLite virtual-table
        module this reader doesn't have — same real case: Messenger's
        Lightspeed database also has an underlying table needing an
        'echo_document_map' module Python's stock sqlite3 doesn't ship)
        is reported as its own {'error': ...} entry rather than aborting
        the whole call — one broken table used to silently hide every
        OTHER table in the same database, including working ones like
        thread_messages itself."""
        if not ctx.raw_content_enabled:
            return {'error': 'raw content access is not enabled for this case — '
                              'the examiner must opt in via the AI Access dialog'}
        try:
            conn, tmpdir = _extract_sqlite_ro(ctx, path)
        except Exception as exc:
            return {'error': str(exc)}
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out = {}
            for t in names:
                try:
                    cols = [{'name': c[1], 'type': c[2], 'notnull': bool(c[3]),
                             'pk': bool(c[5])}
                            for c in conn.execute(f'PRAGMA table_info("{t}")')]
                    fks = [{'from': f[3], 'to_table': f[2], 'to_column': f[4]}
                           for f in conn.execute(f'PRAGMA foreign_key_list("{t}")')]
                    count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    out[t] = {'columns': cols, 'foreign_keys': fks, 'row_count': count}
                except sqlite3.Error as exc:
                    out[t] = {'error': str(exc)}
            return {'path': path, 'tables': out}
        finally:
            conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    @tool
    def sample_sqlite_rows(path: str, table: str, limit: int = 20,
                           offset: int = 0) -> dict:
        """[Raw content — opt-in] Up to 50 sample rows at a time from one
        table OR VIEW of a SQLite database anywhere in the archive — enough
        to read real column values (enum codes, timestamp units/epoch, NULL
        patterns, indirection tables) when drafting a parser. *table* can
        name a view (fixed 2026-08-25 — was table-only, silently rejecting
        a real, working view name as "no table X" — confirmed real case:
        Facebook Messenger's actual messages are only reachable through a
        view called 'thread_messages' over its Lightspeed storage engine).
        Call get_sqlite_schema first to get table/view/column names, and
        page with `offset` for tables with more real content than the
        50-row cap (rows come back in storage order, not any particular
        sort). Not a full dump — once a parser exists, use query_artifact
        instead."""
        if not ctx.raw_content_enabled:
            return {'error': 'raw content access is not enabled for this case — '
                              'the examiner must opt in via the AI Access dialog'}
        if not _IDENT_RE.match(table or ''):
            return {'error': f'invalid table name: {table!r}'}
        limit = max(1, min(int(limit or 20), MAX_SAMPLE_ROWS))
        try:
            conn, tmpdir = _extract_sqlite_ro(ctx, path)
        except Exception as exc:
            return {'error': str(exc)}
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?",
                (table,)).fetchone()
            if not exists:
                return {'error': f'no table or view {table!r} in {path!r} — check get_sqlite_schema'}
            cols = [d[1] for d in conn.execute(f'PRAGMA table_info("{table}")')]
            total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            rows = conn.execute(f'SELECT * FROM "{table}" LIMIT ? OFFSET ?',
                                (limit, max(0, int(offset)))).fetchall()
            return {'path': path, 'table': table, 'columns': cols,
                    'total_rows': total,
                    'rows': [[_blob_safe(v) for v in row] for row in rows]}
        finally:
            conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── Prompts (pre-curated, with user slots) ───────────────────────────────

    _VERIFY = ("Present findings as investigative leads, each citing the "
               "artifact table + identifying row values, or the file path, "
               "so the examiner can verify in FFS Explorer. State clearly "
               "when data is absent rather than inferring.")

    @mcp.prompt()
    def triage_summary() -> str:
        """First-pass triage of the whole case."""
        return (
            "You are assisting a forensic examiner with first-pass triage of "
            "a mobile device extraction.\n"
            "1. Call get_case_overview and summarise the device and what has "
            "been processed.\n"
            "2. Call list_app_containers and cross-reference against the "
            "artifact tables: list apps holding significant data that have "
            "NO parser output (coverage gaps), largest first.\n"
            "3. Call get_research_notes and note which known artifacts on "
            "this device are marked of-value vs known-noise, flagging any "
            "marks whose tested iOS versions don't cover this device.\n"
            "4. Call list_bookmarks and summarise what the examiner has "
            "already flagged.\n"
            f"{_VERIFY}")

    @mcp.prompt()
    def message_review(contact: str, date_range: str) -> str:
        """Review communications with a contact over a period."""
        return (
            f"Review communications involving '{contact}' during "
            f"{date_range}, using query_artifact on every messaging artifact "
            "listed by get_case_overview (e.g. sms_messages, whatsapp; use "
            "`contains` to filter and page with offset).\n"
            "- Build a chronological account of the exchanges.\n"
            "- For any referenced media/attachments, use find_paths to check "
            "whether the file exists in the archive and whether its "
            "timestamps corroborate the message times.\n"
            "- Note gaps, deletions implied by context, or times of unusual "
            f"activity.\n{_VERIFY}")

    @mcp.prompt()
    def artifact_coverage() -> str:
        """Which apps hold data that no parser has processed?"""
        return (
            "Audit parser coverage for this case: call list_apps (sorted by "
            "score already) rather than eyeballing list_app_containers "
            "against artifact table names by hand — it deterministically "
            "resolves each parser's declared coverage and reports "
            "has_parser/score/score_breakdown per app. Report the top "
            "scoring apps with has_parser=false, citing their "
            "score_breakdown reasons, not just the number. For those, use "
            "find_paths to identify their main databases (.sqlite/.db "
            f"files) an examiner should review manually.\n{_VERIFY}")

    @mcp.prompt()
    def build_artifact_parser(bundle_id: str) -> str:
        """Draft a new artifact-parser script for an app with no coverage
        yet. Requires raw content access to be enabled for this case."""
        return (
            f"Draft a parser script for the app '{bundle_id}', which has no "
            "artifact parser yet. This requires raw content access "
            "(get_sqlite_schema / sample_sqlite_rows) — if those tools "
            "return 'not enabled', tell the examiner to opt in via the AI "
            "Access dialog and stop.\n\n"
            "1. Call list_app_containers to get the container path for "
            f"'{bundle_id}', then find_paths on that path filtered to "
            "'.db'/'.sqlite' to locate its database file(s) — check both "
            "the main container and any shared/external storage path.\n"
            "2. For each candidate database, call get_sqlite_schema. "
            "Identify the table(s) holding user content and look for "
            "identity-mapping/indirection tables (e.g. a contact-alias or "
            "id-remap table) that only matter for some rows — missing one "
            "produces silent under-resolution, not a visible error.\n"
            "3. Call sample_sqlite_rows on the relevant tables to see real "
            "values: timestamp units/epoch, enum/flag codes, NULL patterns, "
            "and whether a foreign key ever points at nothing (orphaned "
            "row — must be surfaced with its raw id, e.g. '[no chat record "
            "— raw chat_row_id=-1]', never silently dropped or blanked).\n"
            "4. Preserve raw identifiers alongside any resolved display "
            "value (e.g. both a raw row id and a resolved name) — an "
            "examiner needs to independently verify/cite, so resolving away "
            "the raw id is a regression even when the resolved value is "
            "correct.\n"
            "5. For any INNER-vs-LEFT JOIN or include/exclude call "
            "(e.g. rows with zero attachments/parts), don't default to "
            "'keep everything is safer' — inspect what the excluded rows "
            "actually contain via sample_sqlite_rows before deciding; state "
            "the reasoning as a code comment either way.\n"
            "6. Check whether this app has photos/videos/voice notes/other "
            "attachments, and whether the column pointing at one is a real "
            "local path rather than a remote URL (http/https — the file "
            "isn't in the archive at all, e.g. GroupMe/Burner media) or a "
            "runtime-only reference (content://, e.g. some Viber/Google "
            "Messages fields — meaningless outside a live device). If it's "
            "a real local path, verify the exact ui_path with find_paths "
            "against a real filename from sample_sqlite_rows BEFORE writing "
            "the script — don't assume the database column is already a "
            "complete path (WhatsApp iOS's ZMEDIALOCALPATH needing a "
            "'Message/' prefix the column alone doesn't show is the "
            "reference example). A column with no locally resolvable file "
            "is a real, reportable finding — say so in the script's "
            "`description`, don't silently omit `media_fields` without "
            "explaining why.\n"
            "7. Write the script matching this project's artifact-parser "
            "plugin format — see WRITING_ARTIFACT_PARSERS.md at the repo "
            "root for the exact format and every optional declaration "
            "(`media_fields`, `timestamp_fields`, `recoverable_tables`, "
            "`hidden_fields`, `record_source`) with real examples; read it "
            "rather than re-deriving the format from memory, since it's "
            "kept current and this prompt isn't. Declare whichever optional "
            "attributes actually apply to this app based on what steps 1-6 "
            "found — timestamp columns need `timestamp_fields`, a "
            "confirmed real media path from step 6 needs `media_fields`, "
            "checked-for deleted content needs `recoverable_tables`, and if "
            "the query joins more than one table consider `record_source` "
            "(one entry per joined table) so the examiner can cite exactly "
            "where a value came from — but per that doc's own warning, only "
            "declare `record_source` for a table whose rowid column is "
            "confirmed via get_sqlite_schema to be a genuine `INTEGER "
            "PRIMARY KEY`, never guessed. Output the full script as a "
            "single code block for the examiner to review and save under "
            "artifacts/ios/ or artifacts/android/ themselves — never claim "
            "it has been installed or run against the case; nothing here "
            "writes to the artifacts/ directory.\n"
            f"{_VERIFY}")

    return mcp


def build_http_app(ctx: CaseContext, token: str):
    """Streamable-HTTP Starlette app with bearer-token auth wrapped around
    every request.  127.0.0.1-only binding is the caller's job."""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    server = build_server(ctx)
    app = server.streamable_http_app()
    expected = f'Bearer {token}'

    class _TokenAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.headers.get('authorization') != expected:
                return JSONResponse({'error': 'unauthorized'}, status_code=401)
            return await call_next(request)

    app.add_middleware(_TokenAuth)
    return app
