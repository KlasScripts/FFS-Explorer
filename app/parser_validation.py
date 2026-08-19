"""parser_validation.py — schema + folder-structure snapshotting and diffing
for the validation-baseline feature (see validation_store.py).

Two things get captured about an app's container at GTD-validation time:

  schema    — every table's columns (name/type/notnull/pk) for each SQLite
              db the parser declares in files/optional_files. Same shape
              mcp_server.get_sqlite_schema already produces.
  structure — a GENERALIZED fingerprint of the container's folder layout,
              not a raw file listing. Confirmed against real archives
              (WhatsApp iOS/Android, Google Messages) that a flat listing is
              mostly noise: per-contact JID directories, UUID-named media,
              and hash-named cache files are unique to every device and
              would show as 100% different on every real case even when the
              app version — and the parser's correctness — hasn't changed
              at all. generalize_segment() collapses exactly that kind of
              per-device-variable token into a placeholder; a directory
              whose contents don't need any collapsing (small, stable,
              app-defined names like databases/ or shared_prefs/) keeps its
              real filenames instead, since a NEW file appearing there is
              real signal.

Row counts and per-shape instance/file counts are captured for display
context only — diff_snapshot() never compares them, since case-to-case
volume difference is expected and not a parser-correctness signal.
"""

import os
import re
import sqlite3

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_HEX_HASH_RE = re.compile(r'^[0-9a-f]{6,}$', re.I)
_HEX_SHARD_RE = re.compile(r'^[0-9a-f]{1,2}$', re.I)
_DIGIT_RUN_RE = re.compile(r'\d{6,}')


# ── Path generalization ──────────────────────────────────────────────────────

def generalize_segment(seg: str) -> str:
    """Collapse a path segment (a directory name, or a filename stem with
    its extension already split off) that looks per-device-variable into a
    placeholder, keeping app-defined literal structure intact.

    Real examples this was derived against: WhatsApp iOS's
    'Message/Media/19198887386@s.whatsapp.net/a/9/' (JID + two-level hex
    shard), Android's 'cache/image_manager_disk_cache/<64-char-hash>.0',
    'datadownloadfile_1706320099733' (Google Messages), 'IMG-20240130-WA0000'
    (WhatsApp Android media)."""
    if '@' in seg:
        return '{id}'
    if _UUID_RE.match(seg):
        return '{uuid}'
    if _HEX_SHARD_RE.match(seg):
        return '{shard}'
    if _HEX_HASH_RE.match(seg):
        return '{hash}'
    return _DIGIT_RUN_RE.sub('{n}', seg)


def generalize_filename(name: str) -> str:
    stem, ext = os.path.splitext(name)
    return generalize_segment(stem) + ext.lower()


# ── Structure snapshot ───────────────────────────────────────────────────────

def snapshot_structure(folder_map: dict, container_base: str) -> list[dict]:
    """Walk container_base recursively using the app's own already-built
    folder_map ({folder_path: [child_path, ...]}) — no new zip scan needed,
    this is the exact dict mcp_server.py's list_folder/find_paths already
    read from the host. Returns a list of generalized "shape" entries:

      {dir_pattern, dir_instances, total_files,
       file_patterns: {generalized_filename: {count, examples}}}

    A pattern with count=1 is a stable, app-defined filename (e.g.
    msgstore.db); count>1 means many real per-device instances collapsed
    into that one generalized name (e.g. '{uuid}.jpg' x 40).
    """
    container_base = container_base.strip('/')

    files_by_dir: dict[str, list[str]] = {}

    def _walk(folder):
        for child in folder_map.get(folder, []):
            if child in folder_map:
                _walk(child)
            else:
                d, _, f = child.rpartition('/')
                rel_d = (d[len(container_base):].lstrip('/')
                         if d == container_base or d.startswith(container_base + '/')
                         else d)
                files_by_dir.setdefault(rel_d, []).append(f)

    _walk(container_base)

    clusters: dict[str, dict] = {}
    for rel_d, filenames in files_by_dir.items():
        segs = [generalize_segment(s) for s in rel_d.split('/') if s]
        gen_dir = '/'.join(segs)
        c = clusters.setdefault(gen_dir, {'dir_instances': 0, 'files': {}})
        c['dir_instances'] += 1
        for f in filenames:
            gf = generalize_filename(f)
            fc = c['files'].setdefault(gf, {'count': 0, 'examples': []})
            fc['count'] += 1
            if len(fc['examples']) < 2:
                fc['examples'].append(f"{rel_d}/{f}" if rel_d else f)

    # A pattern's own count (1 = every real instance had a distinct name —
    # a stable/app-defined file like msgstore.db; >1 = many real instances
    # collapsed into this one pattern, e.g. '{uuid}.jpg' x 40) already says
    # everything a separate literal/bulk flag on the whole shape would —
    # no need to compute one.
    return [
        {'dir_pattern': gen_dir,
         'dir_instances': c['dir_instances'],
         'total_files': sum(fc['count'] for fc in c['files'].values()),
         'file_patterns': {
             gf: {'count': fc['count'], 'examples': fc['examples']}
             for gf, fc in sorted(c['files'].items())}}
        for gen_dir, c in sorted(clusters.items())
    ]


# ── Schema snapshot ───────────────────────────────────────────────────────────

def _dump_schema(sqlite_path: str) -> dict:
    conn = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True, timeout=5)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = {}
        for t in tables:
            cols = [{'name': c[1], 'type': c[2], 'notnull': bool(c[3]), 'pk': bool(c[5])}
                    for c in conn.execute(f'PRAGMA table_info("{t}")')]
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                count = None
            out[t] = {'columns': cols, 'row_count': count}
        return out
    finally:
        conn.close()


def snapshot_schema(paths: dict) -> dict:
    """paths: the same dict run_artifact() passes to a parser's own run() —
    {key: extracted_file_path, ..., '_app_base_ui_path': ...}. Dumps schema
    for every declared file that is actually a SQLite database (sidecars
    and the reserved _app_base_ui_path key are skipped)."""
    out = {}
    for key, path in paths.items():
        if key.startswith('_') or not isinstance(path, str) or not os.path.isfile(path):
            continue
        if path.endswith(('-wal', '-shm', '-journal')):
            continue
        try:
            with open(path, 'rb') as f:
                header = f.read(16)
        except OSError:
            continue
        if header != b'SQLite format 3\x00':
            continue
        out[key] = _dump_schema(path)
    return out


# ── Diff ──────────────────────────────────────────────────────────────────────

def diff_snapshot(baseline: dict, current: dict) -> dict:
    """Compare two {'schema': {...}, 'structure': [...]} snapshots. Never
    compares row_count, dir_instances, or total_files — those are expected
    to vary between the GTD image and real casework and carry no
    parser-correctness signal on their own. Only presence/absence of
    tables, columns, column types, and structural shapes."""
    diff = {'tables_added': [], 'tables_removed': [], 'columns': {},
           'shapes_added': [], 'shapes_removed': []}

    b_schema = baseline.get('schema', {})
    c_schema = current.get('schema', {})
    for db_key in sorted(set(b_schema) | set(c_schema)):
        b_tables = b_schema.get(db_key, {})
        c_tables = c_schema.get(db_key, {})
        added = sorted(set(c_tables) - set(b_tables))
        removed = sorted(set(b_tables) - set(c_tables))
        diff['tables_added'].extend(f"{db_key}.{t}" for t in added)
        diff['tables_removed'].extend(f"{db_key}.{t}" for t in removed)
        for t in sorted(set(b_tables) & set(c_tables)):
            b_cols = {c['name']: c['type'] for c in b_tables[t]['columns']}
            c_cols = {c['name']: c['type'] for c in c_tables[t]['columns']}
            col_added = sorted(set(c_cols) - set(b_cols))
            col_removed = sorted(set(b_cols) - set(c_cols))
            type_changed = sorted(
                name for name in set(b_cols) & set(c_cols)
                if b_cols[name] != c_cols[name])
            if col_added or col_removed or type_changed:
                diff['columns'][f"{db_key}.{t}"] = {
                    'added': col_added,
                    'removed': col_removed,
                    'type_changed': [f"{name} ({b_cols[name]} -> {c_cols[name]})"
                                     for name in type_changed],
                }

    # Shape-level (directory) presence/absence.
    b_by_dir = {s['dir_pattern']: s for s in baseline.get('structure', [])}
    c_by_dir = {s['dir_pattern']: s for s in current.get('structure', [])}
    diff['shapes_added'] = sorted(set(c_by_dir) - set(b_by_dir))
    diff['shapes_removed'] = sorted(set(b_by_dir) - set(c_by_dir))

    # Within a shape present on both sides, compare the set of filename
    # patterns it holds — this is what actually catches "a new/renamed/
    # removed file appeared inside an already-known folder" (e.g. a new
    # database file added to databases/), which comparing dir_pattern
    # presence alone would silently miss.
    diff['file_patterns'] = {}
    for dir_pattern in sorted(set(b_by_dir) & set(c_by_dir)):
        b_patterns = set(b_by_dir[dir_pattern].get('file_patterns', {}))
        c_patterns = set(c_by_dir[dir_pattern].get('file_patterns', {}))
        added = sorted(c_patterns - b_patterns)
        removed = sorted(b_patterns - c_patterns)
        if added or removed:
            diff['file_patterns'][dir_pattern] = {'added': added, 'removed': removed}

    return diff


def render_diff_text(diff: dict, baseline_meta: dict, current_device_os: str = '') -> str:
    """Plain-text report — deliberately not HTML, so it doubles as input a
    future offline-LLM review pass could consume with no new plumbing."""
    lines = []
    captured = baseline_meta.get('captured_at', '?')
    source = baseline_meta.get('source_case_label', '?')
    baseline_os = baseline_meta.get('device_os', '?')
    lines.append(f'Validation baseline recorded {captured} from case "{source}" ({baseline_os}).')
    if current_device_os and baseline_os and current_device_os != baseline_os:
        lines.append(
            f"NOTE: this case's device OS ({current_device_os}) differs from the "
            f"baseline's ({baseline_os}) — a difference below may simply reflect "
            "that version gap rather than an app-version change; use judgment.")

    has_diff = bool(diff['tables_added'] or diff['tables_removed'] or diff['columns']
                    or diff['shapes_added'] or diff['shapes_removed']
                    or diff['file_patterns'])
    if not has_diff:
        lines.append('\nNo schema or folder-structure differences from the baseline.')
        return '\n'.join(lines)

    lines.append('')
    if diff['tables_added']:
        lines.append('New tables (not in baseline): ' + ', '.join(diff['tables_added']))
    if diff['tables_removed']:
        lines.append('Tables missing (present in baseline, not here): '
                     + ', '.join(diff['tables_removed']))
    for table, cd in diff['columns'].items():
        lines.append(f'\n{table}:')
        if cd['added']:
            lines.append(f"  new columns: {', '.join(cd['added'])}")
        if cd['removed']:
            lines.append(f"  missing columns: {', '.join(cd['removed'])}")
        if cd['type_changed']:
            lines.append(f"  type changed: {', '.join(cd['type_changed'])}")
    if diff['shapes_added']:
        lines.append('\nNew folder-structure shapes (not in baseline):')
        lines.extend(f'  {s}' for s in diff['shapes_added'])
    if diff['shapes_removed']:
        lines.append('\nFolder-structure shapes missing (present in baseline, not here):')
        lines.extend(f'  {s}' for s in diff['shapes_removed'])
    for dir_pattern, fd in diff['file_patterns'].items():
        lines.append(f'\n{dir_pattern}/:')
        if fd['added']:
            lines.append(f"  new file pattern(s): {', '.join(fd['added'])}")
        if fd['removed']:
            lines.append(f"  missing file pattern(s): {', '.join(fd['removed'])}")
    return '\n'.join(lines)
