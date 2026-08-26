"""db_utils.py — shared case-database utilities.

One case folder per exhibit (FFS archive).  The case folder contains two
SQLite databases:

  casecache.db   — reconstructable cache; can be deleted and rebuilt:
                     thumbnails, blobs (folder sizes, search entry index),
                     header_types, guid_bundle

  caseresults.db — precious user-generated results; never auto-deleted:
                     search_index, search_results, device_info,
                     artifact_<name> tables (written by artifact_db.py)
"""

import io
import json
import os
import sqlite3

import msgpack

# Per-key blob versions — increment a version string to invalidate stale data.
_FOLDER_DATA_VERSION    = '8'
_SEARCH_ENTRIES_VERSION = '1'

# Bump whenever the schema changes incompatibly.
# Cache DB is auto-deleted on mismatch; results DB raises OldSchemaError.
_CACHE_SCHEMA_VERSION   = 14
_RESULTS_SCHEMA_VERSION = 1


class OldSchemaError(Exception):
    """Raised when a case database has an incompatible schema version."""


# ── Cache DB ──────────────────────────────────────────────────────────────────

def _open_cache_db(cache_dir: str) -> sqlite3.Connection:
    """Open (or create) casecache.db inside *cache_dir*.

    Reconstructable cache — auto-deletes and recreates on schema mismatch.
    Tables: thumbnails, blobs, header_types, guid_bundle.

    Raises ValueError if cache_dir is falsy.
    """
    if not cache_dir:
        raise ValueError("cache_dir must be set")
    os.makedirs(cache_dir, exist_ok=True)
    db_path = os.path.join(cache_dir, 'casecache.db')

    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    # WAL's default synchronous=FULL fsyncs every commit — costly on Windows.
    # NORMAL is the standard WAL pairing and this DB is rebuildable anyway.
    conn.execute('PRAGMA synchronous=NORMAL')

    ver = conn.execute('PRAGMA user_version').fetchone()[0]
    if ver != 0 and ver != _CACHE_SCHEMA_VERSION:
        conn.close()
        try:
            os.remove(db_path)
        except OSError:
            pass
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS thumbnails (
            ui_path    TEXT    NOT NULL,
            file_size  INTEGER NOT NULL,
            thumb_size INTEGER NOT NULL,
            data       BLOB    NOT NULL,
            PRIMARY KEY (ui_path, file_size, thumb_size)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS blobs (
            key     TEXT NOT NULL PRIMARY KEY,
            version TEXT NOT NULL,
            data    BLOB NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS header_types (
            ui_path       TEXT NOT NULL PRIMARY KEY,
            detected_type TEXT NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS guid_bundle (
            guid      TEXT NOT NULL PRIMARY KEY,
            bundle_id TEXT NOT NULL
        )
    ''')

    # The "master registry" (app/adapters/ffs.py's LaunchServices csstore
    # extraction) — one row per resolvable app, built once at first-open
    # metadata parsing time, not re-derived per query. app_group_paths_json
    # is {group_id: guid} for THIS bundle's own declared App Groups only
    # (from its code-signing entitlements) — a PluginKit extension gets its
    # own row here too (its own bundle_id, a dotted suffix of its host
    # app's), not a nested field on the host's row; find "every container
    # for app X" via bundle_id = X OR bundle_id LIKE 'X.%'.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS app_registry (
            bundle_id             TEXT NOT NULL PRIMARY KEY,
            display_name          TEXT,
            team_id               TEXT,
            bundle_container_path TEXT,
            data_container_path   TEXT,
            app_group_paths_json  TEXT NOT NULL DEFAULT '{}',
            has_parser            INTEGER NOT NULL DEFAULT 0
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS nested_archives (
            ui_path         TEXT    NOT NULL PRIMARY KEY,
            stored_filename TEXT    NOT NULL,
            original_size   INTEGER NOT NULL,
            entry_count     INTEGER NOT NULL DEFAULT 0,
            processed_at    TEXT    NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            error_msg       TEXT
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS nested_archive_entries (
            archive_ui_path TEXT    NOT NULL,
            entry_path      TEXT    NOT NULL,
            mdate           TEXT,
            size            INTEGER NOT NULL DEFAULT 0,
            file_type       TEXT,
            PRIMARY KEY (archive_ui_path, entry_path)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_nae_archive
        ON nested_archive_entries (archive_ui_path)
    ''')

    # Per-app coverage/category/permission intelligence + interest score
    # (app/app_intelligence.py) — fully re-derivable from the archive +
    # existing parser coverage, so it belongs here, not in caseresults.db.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS app_intelligence (
            platform             TEXT    NOT NULL,
            app_id               TEXT    NOT NULL,
            display_name         TEXT,
            containers_json      TEXT    NOT NULL DEFAULT '[]',
            total_bytes          INTEGER NOT NULL DEFAULT 0,
            file_count           INTEGER NOT NULL DEFAULT 0,
            media_file_count     INTEGER NOT NULL DEFAULT 0,
            last_activity        INTEGER,
            last_activity_utc    TEXT,
            data_created          INTEGER,
            data_created_utc     TEXT,
            shared_created        INTEGER,
            shared_created_utc   TEXT,
            preferences_modified  INTEGER,
            preferences_modified_utc TEXT,
            splash_snapshot_modified INTEGER,
            splash_snapshot_modified_utc TEXT,
            has_parser           INTEGER NOT NULL DEFAULT 0,
            artifact_tables_json TEXT    NOT NULL DEFAULT '[]',
            row_count            INTEGER,
            category             TEXT,
            permissions_json     TEXT    NOT NULL DEFAULT '[]',
            score                INTEGER NOT NULL DEFAULT 0,
            score_breakdown_json TEXT    NOT NULL DEFAULT '{}',
            recently_used        INTEGER NOT NULL DEFAULT 0,
            evidence_databases_json TEXT NOT NULL DEFAULT '[]',
            evidence_databases_total INTEGER NOT NULL DEFAULT 0,
            known_location_json  TEXT,
            webview_storage_path TEXT,
            webview_storage_bytes INTEGER,
            webview_storage_other INTEGER,
            hidden_vault_storage_path TEXT,
            hidden_vault_storage_bytes INTEGER,
            hidden_vault_storage_other INTEGER,
            encryption_caveat    TEXT,
            scanned_at           INTEGER NOT NULL,
            PRIMARY KEY (platform, app_id)
        )
    ''')

    conn.execute(f'PRAGMA user_version = {_CACHE_SCHEMA_VERSION}')
    conn.commit()
    return conn


# ── Results DB ────────────────────────────────────────────────────────────────

def _open_results_db(cache_dir: str) -> sqlite3.Connection:
    """Open (or create) caseresults.db inside *cache_dir*.

    Precious results — raises OldSchemaError on schema mismatch, never
    auto-deletes.  Artifact tables (artifact_*) are written dynamically by
    artifact_db.py and are not declared here.
    Tables: search_index, search_results, device_info.

    Raises ValueError if cache_dir is falsy.
    Raises OldSchemaError if caseresults.db has an incompatible schema.
    """
    if not cache_dir:
        raise ValueError("cache_dir must be set")
    os.makedirs(cache_dir, exist_ok=True)
    db_path = os.path.join(cache_dir, 'caseresults.db')

    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    # NORMAL keeps WAL durability across app crashes (a whole-OS crash can
    # lose the last commit, which is acceptable even for the results DB).
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')

    ver = conn.execute('PRAGMA user_version').fetchone()[0]
    if ver != 0 and ver != _RESULTS_SCHEMA_VERSION:
        conn.close()
        raise OldSchemaError(
            f"caseresults.db schema version {ver} is not compatible with this "
            f"version of the app (expected {_RESULTS_SCHEMA_VERSION}). "
            "Search results and artifact data are preserved in the existing file."
        )

    conn.execute('''
        CREATE TABLE IF NOT EXISTS search_index (
            id             INTEGER PRIMARY KEY,
            keyword        TEXT    NOT NULL UNIQUE,
            used_at        INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            complete       INTEGER NOT NULL DEFAULT 1,
            files_searched INTEGER NOT NULL DEFAULT 0,
            total_files    INTEGER NOT NULL DEFAULT 0
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS search_results (
            term_id  INTEGER NOT NULL REFERENCES search_index(id) ON DELETE CASCADE,
            filename TEXT    NOT NULL,
            offset   INTEGER NOT NULL,
            context  TEXT    NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_search_results_term
        ON search_results (term_id)
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS device_info (
            field_name TEXT NOT NULL PRIMARY KEY,
            data       TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT ''
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS run_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type     TEXT    NOT NULL,
            run_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            completed_at TEXT,
            total        INTEGER,
            processed    INTEGER,
            output_rows  INTEGER,
            complete     INTEGER NOT NULL DEFAULT 0,
            notes        TEXT
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_run_log_type
        ON run_log (run_type)
    ''')
    # Migration: add completed_at to existing run_log tables created before this column existed.
    try:
        conn.execute('ALTER TABLE run_log ADD COLUMN completed_at TEXT')
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migration: add parser_version — the artifact parser's own version
    # number (parser_versions.py, hash-derived) at the time an
    # 'artifact_<script_name>' run happened, so a report opened later can
    # tell whether the parser has since changed. NULL for non-artifact
    # run_types (header_scan, mcp, etc.), which have no parser version.
    try:
        conn.execute('ALTER TABLE run_log ADD COLUMN parser_version INTEGER')
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.execute('''
        CREATE TABLE IF NOT EXISTS bookmark_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL UNIQUE,
            description TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bookmark_entries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id      INTEGER NOT NULL REFERENCES bookmark_groups(id) ON DELETE CASCADE,
            ui_path       TEXT    NOT NULL,
            display_name  TEXT,
            bookmarked_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            UNIQUE(group_id, ui_path)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bm_entries_group
        ON bookmark_entries (group_id)
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS search_scope_files (
            term_id  INTEGER NOT NULL REFERENCES search_index(id) ON DELETE CASCADE,
            ui_path  TEXT    NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_ssf_term
        ON search_scope_files (term_id)
    ''')

    # User-defined / refined protobuf schemas for SEGB streams, keyed by the
    # Biome stream name.  A user schema overrides the built-in one (segb_schemas).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS case_settings (
            key   TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS segb_schemas (
            stream_key   TEXT NOT NULL PRIMARY KEY,
            typedef_json TEXT NOT NULL DEFAULT '{}',
            labels_json  TEXT NOT NULL DEFAULT '{}',
            hints_json   TEXT NOT NULL DEFAULT '{}',
            updated      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    ''')

    conn.execute(f'PRAGMA user_version = {_RESULTS_SCHEMA_VERSION}')
    conn.commit()
    return conn


# ── Schema checks (lightweight, no table creation) ────────────────────────────

def check_cache_schema(cache_dir: str) -> None:
    """Raise OldSchemaError if casecache.db exists with an incompatible schema."""
    db_path = os.path.join(cache_dir, 'casecache.db')
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        ver  = conn.execute('PRAGMA user_version').fetchone()[0]
        conn.close()
    except Exception:
        return
    if ver != 0 and ver != _CACHE_SCHEMA_VERSION:
        raise OldSchemaError(
            f"casecache.db schema version {ver} is incompatible "
            f"(expected {_CACHE_SCHEMA_VERSION}). The cache will be rebuilt."
        )


def check_results_schema(cache_dir: str) -> None:
    """Raise OldSchemaError if caseresults.db exists with an incompatible schema."""
    db_path = os.path.join(cache_dir, 'caseresults.db')
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        ver  = conn.execute('PRAGMA user_version').fetchone()[0]
        conn.close()
    except Exception:
        return
    if ver != 0 and ver != _RESULTS_SCHEMA_VERSION:
        raise OldSchemaError(
            f"caseresults.db schema version {ver} is incompatible "
            f"(expected {_RESULTS_SCHEMA_VERSION}). "
            "Search results and artifact data are preserved."
        )


# ── GUID / bundle-ID map ──────────────────────────────────────────────────────

def save_case_setting(conn: 'sqlite3.Connection', key: str, value: str) -> None:
    """Persist one per-case setting (e.g. the AI backend chosen for this case)."""
    conn.execute('INSERT OR REPLACE INTO case_settings (key, value) VALUES (?, ?)',
                 (key, str(value)))
    conn.commit()


def load_case_setting(conn: 'sqlite3.Connection', key: str,
                      default: str | None = None) -> str | None:
    row = conn.execute('SELECT value FROM case_settings WHERE key=?',
                       (key,)).fetchone()
    return row[0] if row else default


def save_guid_bundle_map(conn: 'sqlite3.Connection', mapping: dict) -> None:
    """Persist {guid: bundle_id} into the guid_bundle table."""
    conn.execute('DELETE FROM guid_bundle')
    conn.executemany(
        'INSERT INTO guid_bundle (guid, bundle_id) VALUES (?,?)',
        list(mapping.items()),
    )
    conn.commit()


def load_guid_bundle_map(conn: 'sqlite3.Connection') -> dict:
    """Return {guid: bundle_id}, or {} if none saved."""
    return dict(conn.execute('SELECT guid, bundle_id FROM guid_bundle'))


# ── App registry (app/adapters/ffs.py's LaunchServices csstore extraction) ────

def save_app_registry(conn: 'sqlite3.Connection', rows: list) -> None:
    """Replace all app_registry rows. Each row is one
    adapters.ffs._extract_app_registry_from_launchservices() dict, plus a
    'has_parser' bool and 'app_group_paths' ({group_id: guid}, cross-
    referenced against adapters.ffs._resolve_app_group_paths()'s global
    map) added by the caller. Full replace, not upsert, matching
    save_guid_bundle_map's convention — a stale app's row should disappear
    on the next case load, not linger."""
    conn.execute('DELETE FROM app_registry')
    conn.executemany(
        'INSERT INTO app_registry (bundle_id, display_name, team_id, '
        'bundle_container_path, data_container_path, app_group_paths_json, '
        'has_parser) VALUES (?,?,?,?,?,?,?)',
        [(r['bundle_id'], r.get('display_name'), r.get('team_id'),
          r.get('bundle_container_path'), r.get('data_container_path'),
          json.dumps(r.get('app_group_paths') or {}), int(r.get('has_parser', False)))
         for r in rows],
    )
    conn.commit()


def load_app_registry(conn: 'sqlite3.Connection') -> list:
    """Return all app_registry rows as dicts, or [] if never built."""
    rows = conn.execute(
        'SELECT bundle_id, display_name, team_id, bundle_container_path, '
        'data_container_path, app_group_paths_json, has_parser '
        'FROM app_registry'
    ).fetchall()
    return [
        {'bundle_id': r[0], 'display_name': r[1], 'team_id': r[2],
         'bundle_container_path': r[3], 'data_container_path': r[4],
         'app_group_paths': json.loads(r[5]), 'has_parser': bool(r[6])}
        for r in rows
    ]


def load_app_registry_entry(conn: 'sqlite3.Connection', bundle_id: str) -> dict | None:
    """Return one app_registry row (exact bundle_id match only — callers
    wanting an app's own PluginKit extensions too should also query with
    bundle_id LIKE '<id>.%' via load_app_registry() and filter), or None."""
    row = conn.execute(
        'SELECT bundle_id, display_name, team_id, bundle_container_path, '
        'data_container_path, app_group_paths_json, has_parser '
        'FROM app_registry WHERE bundle_id = ?', (bundle_id,)
    ).fetchone()
    if row is None:
        return None
    return {'bundle_id': row[0], 'display_name': row[1], 'team_id': row[2],
            'bundle_container_path': row[3], 'data_container_path': row[4],
            'app_group_paths': json.loads(row[5]), 'has_parser': bool(row[6])}


# ── SEGB protobuf schemas (user-defined, per case) ────────────────────────────

def save_segb_schema(conn: 'sqlite3.Connection', stream_key: str, schema: dict) -> None:
    """Persist a user schema {'typedef','labels','hints'} for *stream_key*."""
    conn.execute(
        'INSERT OR REPLACE INTO segb_schemas '
        '(stream_key, typedef_json, labels_json, hints_json, updated) '
        "VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%S','now'))",
        (stream_key,
         json.dumps(schema.get('typedef', {})),
         json.dumps(schema.get('labels', {})),
         json.dumps(schema.get('hints', {}))),
    )
    conn.commit()


def load_segb_schema(conn: 'sqlite3.Connection', stream_key: str) -> dict | None:
    """Return the user schema for *stream_key* as {'typedef','labels','hints'},
    or None if none saved."""
    row = conn.execute(
        'SELECT typedef_json, labels_json, hints_json FROM segb_schemas '
        'WHERE stream_key = ?', (stream_key,)).fetchone()
    if not row:
        return None
    try:
        return {'typedef': json.loads(row[0]), 'labels': json.loads(row[1]),
                'hints': json.loads(row[2])}
    except Exception:
        return None


def delete_segb_schema(conn: 'sqlite3.Connection', stream_key: str) -> None:
    """Remove the user schema for *stream_key* (revert to built-in)."""
    conn.execute('DELETE FROM segb_schemas WHERE stream_key = ?', (stream_key,))
    conn.commit()


# ── Header types ──────────────────────────────────────────────────────────────

def save_header_types(conn: 'sqlite3.Connection', types: dict) -> None:
    """Persist {ui_path: detected_type} into the header_types table."""
    conn.executemany(
        'INSERT OR REPLACE INTO header_types (ui_path, detected_type) VALUES (?,?)',
        list(types.items()),
    )
    conn.commit()


def load_header_types(conn: 'sqlite3.Connection') -> dict:
    """Return {ui_path: detected_type} previously saved."""
    return dict(conn.execute('SELECT ui_path, detected_type FROM header_types'))


def clear_header_types(conn: 'sqlite3.Connection') -> None:
    """Delete all header_types rows (used before a full rescan)."""
    conn.execute('DELETE FROM header_types')
    conn.commit()


# ── Run log ───────────────────────────────────────────────────────────────────

def start_run_log(conn: 'sqlite3.Connection', run_type: str,
                  total: int | None = None,
                  notes: str | None = None,
                  parser_version: int | None = None) -> int:
    """Insert an in-progress run record and return its id.

    Call this when a scan/artifact run begins, then call complete_run_log()
    when it finishes.  run_type should be 'header_scan' or 'artifact_<script_name>'.
    parser_version is the artifact parser's own version at run time (see
    app/parser_versions.py) — only meaningful for an 'artifact_*' run_type;
    leave None for anything else. Recorded so a report opened later can
    tell whether the parser producing it has since changed.
    """
    cur = conn.execute(
        'INSERT INTO run_log (run_type, total, complete, notes, parser_version) '
        'VALUES (?, ?, 0, ?, ?)',
        (run_type, total, notes, parser_version),
    )
    conn.commit()
    return cur.lastrowid


def complete_run_log(conn: 'sqlite3.Connection', run_id: int,
                     processed: int, output_rows: int) -> None:
    """Mark a run as complete, recording the finish time and output counts."""
    conn.execute(
        "UPDATE run_log SET completed_at=strftime('%Y-%m-%dT%H:%M:%S','now'), "
        "processed=?, output_rows=?, complete=1 WHERE id=?",
        (processed, output_rows, run_id),
    )
    conn.commit()


def load_last_run(conn: 'sqlite3.Connection', run_type: str) -> dict | None:
    """Return the most recent run_log entry for *run_type*, or None."""
    row = conn.execute(
        'SELECT run_at, completed_at, total, processed, output_rows, complete, notes, parser_version '
        'FROM run_log WHERE run_type=? ORDER BY id DESC LIMIT 1',
        (run_type,),
    ).fetchone()
    if row is None:
        return None
    return {
        'run_at': row[0], 'completed_at': row[1], 'total': row[2],
        'processed': row[3], 'output_rows': row[4],
        'complete': bool(row[5]), 'notes': row[6], 'parser_version': row[7],
    }


def load_run_history(conn: 'sqlite3.Connection', run_type: str) -> list:
    """Return all run_log entries for *run_type*, newest first."""
    rows = conn.execute(
        'SELECT run_at, completed_at, total, processed, output_rows, complete, notes, parser_version '
        'FROM run_log WHERE run_type=? ORDER BY id DESC',
        (run_type,),
    ).fetchall()
    return [
        {'run_at': r[0], 'completed_at': r[1], 'total': r[2],
         'processed': r[3], 'output_rows': r[4],
         'complete': bool(r[5]), 'notes': r[6], 'parser_version': r[7]}
        for r in rows
    ]


def clear_run_log(conn: 'sqlite3.Connection', run_type: str) -> None:
    """Delete all run_log rows for *run_type*."""
    conn.execute('DELETE FROM run_log WHERE run_type=?', (run_type,))
    conn.commit()


# ── Generic blob store ────────────────────────────────────────────────────────

def save_blob(conn: 'sqlite3.Connection', key: str, version: str, data: bytes) -> None:
    """Write a versioned bytes blob under *key*."""
    conn.execute(
        'INSERT OR REPLACE INTO blobs (key, version, data) VALUES (?,?,?)',
        (key, version, data),
    )
    conn.commit()


def load_blob(conn: 'sqlite3.Connection', key: str, version: str) -> bytes | None:
    """Return the stored bytes for *key* if the version matches, else None."""
    row = conn.execute(
        'SELECT version, data FROM blobs WHERE key=?', (key,)
    ).fetchone()
    if row is None or row[0] != version:
        return None
    return row[1]


def open_blob(conn: 'sqlite3.Connection', key: str, version: str):
    """Return a read-only file-like over the stored blob for *key* (version
    checked), or None.

    Uses Connection.blobopen so a large blob (the load snapshot can be
    hundreds of MB) streams straight into the deserialiser instead of being
    materialised in memory first.  The caller must finish reading — and close
    the returned object — before closing *conn*.  Falls back to an in-memory
    BytesIO where blobopen is unavailable (Python < 3.11).
    """
    row = conn.execute(
        'SELECT rowid, version FROM blobs WHERE key=?', (key,)
    ).fetchone()
    if row is None or row[1] != version:
        return None
    if hasattr(conn, 'blobopen'):
        try:
            return conn.blobopen('blobs', 'data', row[0], readonly=True)
        except sqlite3.Error:
            pass
    data = load_blob(conn, key, version)
    return io.BytesIO(data) if data is not None else None


# ── App intelligence (app/app_intelligence.py) ─────────────────────────────────

def save_app_intelligence(conn: 'sqlite3.Connection', rows: list) -> None:
    """Replace all app_intelligence rows with a fresh scan_apps() result.

    Each row is the dict scan_apps() returns — this re-serialises the
    list/dict fields to JSON for storage. Full replace (not upsert) since a
    stale app that's since been uninstalled/renamed should disappear too.
    *rows* must already be in the desired display order — inserted in that
    order and read back the same way (see load_app_intelligence) since
    scan_apps()'s sort is richer than a plain score ORDER BY (it tie-breaks
    on evidence/caveat/recency too)."""
    conn.execute('DELETE FROM app_intelligence')
    conn.executemany(
        'INSERT INTO app_intelligence (platform, app_id, display_name, '
        'containers_json, '
        'total_bytes, file_count, media_file_count, last_activity, last_activity_utc, '
        'data_created, data_created_utc, '
        'shared_created, shared_created_utc, has_parser, '
        'artifact_tables_json, row_count, category, permissions_json, '
        'score, score_breakdown_json, recently_used, evidence_databases_json, '
        'evidence_databases_total, known_location_json, '
        'webview_storage_path, webview_storage_bytes, webview_storage_other, '
        'hidden_vault_storage_path, hidden_vault_storage_bytes, '
        'hidden_vault_storage_other, encryption_caveat, scanned_at, '
        'preferences_modified, preferences_modified_utc, '
        'splash_snapshot_modified, splash_snapshot_modified_utc) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        [(r['platform'], r['app_id'], r['display_name'], json.dumps(r['containers']),
          r['total_bytes'], r['file_count'], r['media_file_count'], r['last_activity'],
          r['last_activity_utc'], r['data_created'],
          r['data_created_utc'], r['shared_created'],
          r['shared_created_utc'], int(r['has_parser']),
          json.dumps(r['artifact_tables']), r['row_count'], r['category'],
          json.dumps(r['permissions_declared']), r['score'],
          json.dumps(r['score_breakdown']), int(r['recently_used']),
          json.dumps(r['evidence_databases']), r['evidence_databases_total'],
          json.dumps(r['known_location']) if r['known_location'] else None,
          (r['webview_storage'] or {}).get('path'),
          (r['webview_storage'] or {}).get('bytes'),
          (r['webview_storage'] or {}).get('other_stores'),
          (r['hidden_vault_storage'] or {}).get('path'),
          (r['hidden_vault_storage'] or {}).get('bytes'),
          (r['hidden_vault_storage'] or {}).get('other_stores'),
          r['encryption_caveat'], r['scanned_at'],
          r['preferences_modified'], r['preferences_modified_utc'],
          r['splash_snapshot_modified'], r['splash_snapshot_modified_utc'])
         for r in rows],
    )
    conn.commit()


def load_app_intelligence(conn: 'sqlite3.Connection') -> list:
    """Return the cached scan_apps()-shaped rows in their original (already
    fully sorted) order — ORDER BY rowid, not score, since save_app_intelligence
    inserts in scan_apps()'s own richer sort order and a plain score-only
    re-sort here would silently lose the evidence/caveat/recency tie-break
    on every cache-hit read."""
    rows = conn.execute(
        'SELECT platform, app_id, display_name, containers_json, total_bytes, '
        'file_count, media_file_count, last_activity, last_activity_utc, '
        'data_created, data_created_utc, '
        'shared_created, shared_created_utc, has_parser, '
        'artifact_tables_json, row_count, '
        'category, permissions_json, score, score_breakdown_json, '
        'recently_used, evidence_databases_json, evidence_databases_total, '
        'known_location_json, '
        'webview_storage_path, '
        'webview_storage_bytes, webview_storage_other, '
        'hidden_vault_storage_path, hidden_vault_storage_bytes, '
        'hidden_vault_storage_other, encryption_caveat, scanned_at, '
        'preferences_modified, preferences_modified_utc, '
        'splash_snapshot_modified, splash_snapshot_modified_utc '
        'FROM app_intelligence ORDER BY rowid'
    ).fetchall()
    out = []
    for r in rows:
        webview_storage = ({'path': r[24], 'bytes': r[25], 'other_stores': r[26]} if r[24] else None)
        hidden_vault_storage = ({'path': r[27], 'bytes': r[28], 'other_stores': r[29]} if r[27] else None)
        out.append({
            'platform': r[0], 'app_id': r[1], 'display_name': r[2],
            'containers': json.loads(r[3]),
            'total_bytes': r[4], 'file_count': r[5], 'media_file_count': r[6],
            'last_activity': r[7],
            'last_activity_utc': r[8],
            'data_created': r[9], 'data_created_utc': r[10],
            'shared_created': r[11], 'shared_created_utc': r[12],
            'has_parser': bool(r[13]), 'artifact_tables': json.loads(r[14]),
            'row_count': r[15], 'category': r[16],
            'permissions_declared': json.loads(r[17]), 'score': r[18],
            'score_breakdown': json.loads(r[19]), 'recently_used': bool(r[20]),
            'evidence_databases_total': r[22],
            'known_location': json.loads(r[23]) if r[23] else None,
            'webview_storage': webview_storage,
            'hidden_vault_storage': hidden_vault_storage,
            'evidence_databases': json.loads(r[21]), 'encryption_caveat': r[30],
            'scanned_at': r[31],
            'preferences_modified': r[32], 'preferences_modified_utc': r[33],
            'splash_snapshot_modified': r[34], 'splash_snapshot_modified_utc': r[35],
        })
    return out


# ── Folder sizes and counts ───────────────────────────────────────────────────

def load_folder_data(conn: 'sqlite3.Connection') -> tuple[dict, dict]:
    """Return (counts, sizes) from the blob.

    counts — {folder_path: int}
    sizes  — {folder_path: int}
    Both empty if not cached or version mismatch.
    Entries with count == -1 (sentinel: not yet computed) are excluded from counts.
    """
    raw = load_blob(conn, 'folder_data', _FOLDER_DATA_VERSION)
    if raw is None:
        return {}, {}
    combined = msgpack.unpackb(raw, raw=False)
    counts: dict = {}
    sizes:  dict = {}
    for path, value in combined.items():
        if isinstance(value, list) and len(value) == 2:
            if value[0] != -1:   # -1 = sentinel written by save_folder_sizes before counts known
                counts[path] = value[0]
            sizes[path] = value[1]
    return counts, sizes


def save_folder_sizes(conn: 'sqlite3.Connection', sizes: dict) -> None:
    """Merge {folder_path: total_bytes} into the blob, preserving existing counts.

    When no prior count exists for a path, stores -1 as a sentinel so that
    load_folder_data knows the count has not been computed yet (0 would be
    ambiguous with a legitimately empty folder).
    """
    existing_counts, _ = load_folder_data(conn)
    combined = {path: [existing_counts.get(path, -1), size] for path, size in sizes.items()}
    save_blob(conn, 'folder_data', _FOLDER_DATA_VERSION, msgpack.packb(combined, use_bin_type=True))


def save_folder_counts(conn: 'sqlite3.Connection', counts: dict) -> None:
    """Merge {folder_path: count} into the blob, preserving existing sizes."""
    _, existing_sizes = load_folder_data(conn)
    paths    = set(existing_sizes) | set(counts)
    combined = {path: [counts.get(path, 0), existing_sizes.get(path, 0)] for path in paths}
    save_blob(conn, 'folder_data', _FOLDER_DATA_VERSION, msgpack.packb(combined, use_bin_type=True))


# ── Device info ───────────────────────────────────────────────────────────────

def save_device_info(conn: 'sqlite3.Connection',
                     fields: list[tuple[str, str, str]]) -> None:
    """Persist [(field_name, data, source)] into device_info.

    Each row records one piece of device information and where it came from,
    e.g. ('Make', 'Apple', 'UFD') or ('iOS Version', '17.4', 'MobileGestalt.plist').
    """
    conn.execute('DELETE FROM device_info')
    conn.executemany(
        'INSERT OR REPLACE INTO device_info (field_name, data, source) VALUES (?,?,?)',
        fields,
    )
    conn.commit()


def upsert_device_info_field(conn: 'sqlite3.Connection',
                              field_name: str, data: str,
                              source: str = '') -> None:
    """Insert or replace a single device_info row without affecting others."""
    conn.execute(
        'INSERT OR REPLACE INTO device_info (field_name, data, source) VALUES (?,?,?)',
        (field_name, data, source),
    )
    conn.commit()


def load_device_info(conn: 'sqlite3.Connection') -> list[tuple[str, str, str]]:
    """Return [(field_name, data, source)] in insertion order, or []."""
    return conn.execute(
        'SELECT field_name, data, source FROM device_info ORDER BY rowid'
    ).fetchall()


# ── Nested archives ───────────────────────────────────────────────────────────

def save_nested_archive(conn: 'sqlite3.Connection',
                        ui_path: str, stored_filename: str,
                        original_size: int, entry_count: int) -> None:
    """Record one extracted nested archive in the nested_archives table."""
    conn.execute(
        'INSERT OR REPLACE INTO nested_archives '
        '(ui_path, stored_filename, original_size, entry_count) VALUES (?,?,?,?)',
        (ui_path, stored_filename, original_size, entry_count),
    )
    conn.commit()


def save_nested_archive_entries(conn: 'sqlite3.Connection',
                                archive_ui_path: str,
                                entries: list) -> None:
    """Persist [(entry_path, mdate, size, file_type)] for one nested archive."""
    conn.execute('DELETE FROM nested_archive_entries WHERE archive_ui_path=?',
                 (archive_ui_path,))
    conn.executemany(
        'INSERT INTO nested_archive_entries '
        '(archive_ui_path, entry_path, mdate, size, file_type) VALUES (?,?,?,?,?)',
        [(archive_ui_path, ep, md, sz, ft) for ep, md, sz, ft in entries],
    )
    conn.commit()


def load_nested_archives(conn: 'sqlite3.Connection') -> list:
    """Return list of dicts for all nested archive records (successes and failures)."""
    rows = conn.execute(
        'SELECT ui_path, stored_filename, original_size, entry_count, '
        'processed_at, error_msg '
        'FROM nested_archives ORDER BY processed_at'
    ).fetchall()
    return [{'ui_path': r[0], 'stored_filename': r[1], 'original_size': r[2],
             'entry_count': r[3], 'processed_at': r[4], 'error_msg': r[5]}
            for r in rows]


def save_nested_archive_failure(conn: 'sqlite3.Connection',
                                ui_path: str, error_msg: str) -> None:
    """Record a failed extraction attempt in nested_archives."""
    conn.execute(
        'INSERT OR REPLACE INTO nested_archives '
        '(ui_path, stored_filename, original_size, entry_count, error_msg) '
        'VALUES (?, \'\', 0, 0, ?)',
        (ui_path, error_msg),
    )
    conn.commit()


def load_nested_archive_entries(conn: 'sqlite3.Connection',
                                archive_ui_path: str) -> list:
    """Return list of dicts for all entries inside one nested archive."""
    rows = conn.execute(
        'SELECT entry_path, mdate, size, file_type '
        'FROM nested_archive_entries WHERE archive_ui_path=? ORDER BY entry_path',
        (archive_ui_path,),
    ).fetchall()
    return [{'entry_path': r[0], 'mdate': r[1], 'size': r[2], 'file_type': r[3]}
            for r in rows]


def clear_nested_archives(conn: 'sqlite3.Connection') -> None:
    """Delete all nested archive records (entries first, then index)."""
    conn.execute('DELETE FROM nested_archive_entries')
    conn.execute('DELETE FROM nested_archives')
    conn.commit()


# ── Bookmarks ─────────────────────────────────────────────────────────────────

def load_bookmark_groups(conn: 'sqlite3.Connection') -> list:
    """Return [{id, name, description, created_at, count}] ordered by creation."""
    rows = conn.execute(
        'SELECT g.id, g.name, g.description, g.created_at, COUNT(e.id) '
        'FROM bookmark_groups g '
        'LEFT JOIN bookmark_entries e ON e.group_id = g.id '
        'GROUP BY g.id ORDER BY g.created_at'
    ).fetchall()
    return [{'id': r[0], 'name': r[1], 'description': r[2],
             'created_at': r[3], 'count': r[4]} for r in rows]


def save_bookmark_group(conn: 'sqlite3.Connection',
                        name: str, description: str = '') -> int:
    """Create a new bookmark group and return its id."""
    cur = conn.execute(
        'INSERT INTO bookmark_groups (name, description) VALUES (?, ?)',
        (name, description or None),
    )
    conn.commit()
    return cur.lastrowid


def save_bookmark_entries(conn: 'sqlite3.Connection',
                          group_id: int,
                          entries: list) -> None:
    """Upsert bookmark entries for a group.

    *entries* is a list of (ui_path, display_name) pairs.
    INSERT OR REPLACE refreshes bookmarked_at on re-add.
    """
    conn.executemany(
        'INSERT OR REPLACE INTO bookmark_entries '
        '(group_id, ui_path, display_name) VALUES (?, ?, ?)',
        [(group_id, ui_path, display_name) for ui_path, display_name in entries],
    )
    conn.commit()


def load_bookmark_entries(conn: 'sqlite3.Connection', group_id: int) -> list:
    """Return [{ui_path, display_name, bookmarked_at}] for a group."""
    rows = conn.execute(
        'SELECT ui_path, display_name, bookmarked_at '
        'FROM bookmark_entries WHERE group_id=? ORDER BY bookmarked_at',
        (group_id,),
    ).fetchall()
    return [{'ui_path': r[0], 'display_name': r[1], 'bookmarked_at': r[2]}
            for r in rows]


def delete_bookmark_group(conn: 'sqlite3.Connection', group_id: int) -> None:
    """Delete a bookmark group and all its entries (CASCADE handles entries)."""
    conn.execute('DELETE FROM bookmark_groups WHERE id=?', (group_id,))
    conn.commit()


def delete_bookmark_entry(conn: 'sqlite3.Connection',
                          group_id: int, ui_path: str) -> None:
    """Remove a single entry from a bookmark group."""
    conn.execute(
        'DELETE FROM bookmark_entries WHERE group_id=? AND ui_path=?',
        (group_id, ui_path),
    )
    conn.commit()


def save_search_scope_files(conn: 'sqlite3.Connection',
                            term_id: int,
                            ui_paths: list) -> None:
    """Snapshot the ui_paths that were in scope when a scoped search ran."""
    conn.execute('DELETE FROM search_scope_files WHERE term_id=?', (term_id,))
    conn.executemany(
        'INSERT INTO search_scope_files (term_id, ui_path) VALUES (?,?)',
        [(term_id, p) for p in ui_paths],
    )
    conn.commit()


def load_search_scope_files(conn: 'sqlite3.Connection', term_id: int) -> list:
    """Return the snapshotted ui_paths for a scoped search, or []."""
    rows = conn.execute(
        'SELECT ui_path FROM search_scope_files WHERE term_id=? ORDER BY rowid',
        (term_id,),
    ).fetchall()
    return [r[0] for r in rows]
