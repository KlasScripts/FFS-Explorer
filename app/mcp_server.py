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
from dataclasses import dataclass, field
from typing import Callable

import research_store as _research

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

    # ── Tier 3: raw SQLite access (opt-in — see CaseContext.raw_content_enabled) ──

    @tool
    def get_sqlite_schema(path: str) -> dict:
        """[Raw content — opt-in] Schema of a SQLite database anywhere in the
        archive: every table's columns (name/type/notnull/pk), foreign keys,
        and row count. Extracted to a locked-down read-only temp copy (WAL/SHM
        sidecars included when present, never checkpointed/altered). Use this
        to explore a database behind an app container that has no artifact
        parser yet — find it first with find_paths, e.g.
        find_paths('databases') under a bundle's container path."""
        if not ctx.raw_content_enabled:
            return {'error': 'raw content access is not enabled for this case — '
                              'the examiner must opt in via the AI Access dialog'}
        try:
            conn, tmpdir = _extract_sqlite_ro(ctx, path)
        except Exception as exc:
            return {'error': str(exc)}
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out = {}
            for t in tables:
                cols = [{'name': c[1], 'type': c[2], 'notnull': bool(c[3]),
                         'pk': bool(c[5])}
                        for c in conn.execute(f'PRAGMA table_info("{t}")')]
                fks = [{'from': f[3], 'to_table': f[2], 'to_column': f[4]}
                       for f in conn.execute(f'PRAGMA foreign_key_list("{t}")')]
                count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                out[t] = {'columns': cols, 'foreign_keys': fks, 'row_count': count}
            return {'path': path, 'tables': out}
        finally:
            conn.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    @tool
    def sample_sqlite_rows(path: str, table: str, limit: int = 20,
                           offset: int = 0) -> dict:
        """[Raw content — opt-in] Up to 50 sample rows at a time from one
        table of a SQLite database anywhere in the archive — enough to read
        real column values (enum codes, timestamp units/epoch, NULL
        patterns, indirection tables) when drafting a parser. Call
        get_sqlite_schema first to get table/column names, and page with
        `offset` for tables with more real content than the 50-row cap
        (rows come back in storage order, not any particular sort). Not a
        full dump — once a parser exists, use query_artifact instead."""
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
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not exists:
                return {'error': f'no table {table!r} in {path!r} — check get_sqlite_schema'}
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
            "Audit parser coverage for this case: call get_case_overview and "
            "list_app_containers, then report every app container above "
            "10 MB with no corresponding artifact table, sorted by size. "
            "For the top gaps, use find_paths to identify their main "
            "databases (.sqlite/.db files) an examiner should review "
            f"manually.\n{_VERIFY}")

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
            "a real local path, declare a module-level `media_fields: "
            "list[str]` naming the output field(s) that hold it, and build "
            "each one as a full archive ui_path in `run()` — using this "
            "case's actual container base (from list_app_containers, or "
            "the `_app_base_ui_path` key already present in the `paths` "
            "dict passed to run() for the multi-file API) plus whatever "
            "the database's own path column contributes, which sometimes "
            "needs an extra fixed segment the column alone doesn't show "
            "(e.g. WhatsApp iOS's ZMEDIALOCALPATH needing a 'Message/' "
            "prefix — verify with find_paths against a real filename from "
            "sample_sqlite_rows, don't assume the column is already a "
            "complete path). This makes the app's thumbnail/full-view "
            "media viewer (see CLAUDE.md's Conventions section) work for "
            "the app being drafted, the same as it already does for "
            "WhatsApp/SMS/Photos/Google Messages. A column with no locally "
            "resolvable file is a real, reportable finding — say so in the "
            "script's `description`, don't silently omit media_fields "
            "without explaining why.\n"
            "7. Write the script matching this project's artifact-parser "
            "plugin format (multi-file API): module-level `name` (human "
            "label), `app_path` (container base path), `files` "
            "dict[key -> subpath] for required databases, optional "
            "`optional_files` for sidecars like -wal/-shm, optional "
            "`media_fields` (step 6), and `run(paths) -> list[dict]` "
            "receiving extracted on-disk paths. Output the full script as "
            "a single code block for the examiner to review and save "
            "under artifacts/ios/ or artifacts/android/ themselves — "
            "never claim it has been installed or run against the case; "
            "nothing here writes to the artifacts/ directory.\n"
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
