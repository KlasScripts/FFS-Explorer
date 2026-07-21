"""mcp_server.py — read-only MCP server exposing processed case data to AI clients.

Phase-1 surface (see also mcp_control.py for the GUI-side lifecycle):

  Tier 1 — processed content: artifact tables (sms_messages, whatsapp,
           photos_metadata, …), saved search results, bookmarks, device info,
           research-status notes.
  Tier 2 — file tree as *queryable metadata*: list_folder / find_paths /
           get_file_metadata return paths, sizes, timestamps and detected
           types — never file contents.
  There is deliberately NO raw-file-read tool in phase 1.

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
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

import research_store as _research

MAX_ROWS = 500          # hard cap for any row-returning tool
DEFAULT_ROWS = 100


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
        for part in path.split('/'):
            bundle = ctx.get_guid_to_bundle().get(part)
            if bundle:
                d['app_bundle'] = bundle
                break
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
                guid = child.rsplit('/', 1)[-1]
                bundle = guid_map.get(guid)
                if bundle:
                    out.append({'bundle_id': bundle, 'path': child,
                                'total_bytes': sizes.get(child, 0)})
        out.sort(key=lambda d: d['total_bytes'], reverse=True)
        return {'total': len(out), 'containers': out[:_clamp(limit)]}

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
