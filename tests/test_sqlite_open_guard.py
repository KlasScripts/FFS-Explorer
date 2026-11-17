"""Every sqlite3.connect() in the app is accounted for.

An evidence database is opened only through app/sqlite_reader.py (reports
via artifact_runner.open_db_readonly, the Database tab, the carver, MCP,
the media scan, parser validation), so they all see the same version of it
and say the same things about it (decision 2026-11-17, after six separate
opens disagreed on WAL and hot-journal handling).

This test lists, per file, how many connect() calls there are and why each
is allowed. A new call anywhere, or another one in a listed file, fails here
until someone checks it: an evidence open must go through sqlite_reader; a
case database (caseresults.db / casecache.db) open is fine and is added to
the list with its reason.
"""
import ast
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALLOWED = {
    'app/sqlite_reader.py': (3, 'the evidence reader itself: read-only open, its finished '
                                'copy of a hot journal, the checkpoint view'),
    'app/db_utils.py': (5, 'the case databases caseresults.db / casecache.db'),
    'app/ai_summary.py': (7, 'caseresults.db (report rows and stored summaries)'),
    'app/chat_flags.py': (4, 'caseresults.db report rows and casecache.db red_flags / text_embeddings (read-only fingerprint and kept red flags)'),
    'app/app_intelligence.py': (1, 'caseresults.db, read-only'),
    'app/artifact_viewer.py': (1, 'caseresults.db report table'),
    'app/case_names.py': (1, 'casecache.db name update'),
    'app/mcp_server.py': (2, 'caseresults.db for the MCP tools (evidence goes through '
                             '_extract_sqlite_ro -> sqlite_reader)'),
    'app/sqlite_carve.py': (2, '_readonly_connection: an in-memory (or temp) read-only copy of '
                               'the main file as stored, for its own page topology'),
    'app/sqlite_recovery.py': (2, 'worker process: immutable opens of the Database tab\'s own '
                                  'prepared copies'),
    'app/sqlite_viewer.py': (7, 'the Database tab\'s own prepared copies (committed and '
                                'checkpoint), made by sqlite_reader'),
}


def _connect_calls():
    out = {}
    files = glob.glob(os.path.join(ROOT, 'app', '**', '*.py'), recursive=True)
    files += [os.path.join(ROOT, 'ffs-explorer.py')]
    for path in files:
        rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
        if '/ccl_' in rel or rel.startswith('app/ccl_'):
            continue                               # vendored readers
        tree = ast.parse(open(path, encoding='utf-8').read())
        n = sum(1 for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'connect' and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'sqlite3')
        if n:
            out[rel] = n
    return out


def test_every_sqlite_connect_is_accounted_for():
    found = _connect_calls()
    unexpected = {f: n for f, n in found.items() if f not in ALLOWED}
    assert not unexpected, (
        f'sqlite3.connect() in files not on the list: {unexpected}. Open evidence through '
        'sqlite_reader (or artifact_runner.open_db_readonly); add a case-database open to '
        'ALLOWED with its reason.')
    changed = {f: (found.get(f, 0), ALLOWED[f][0]) for f in ALLOWED if found.get(f, 0) != ALLOWED[f][0]}
    assert not changed, (
        f'connect() count changed (found, listed): {changed}. Check each new call is not an '
        'evidence open, then update ALLOWED.')
