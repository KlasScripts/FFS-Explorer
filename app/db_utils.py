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

import os
import sqlite3

import msgpack

# Per-key blob versions — increment a version string to invalidate stale data.
_FOLDER_DATA_VERSION    = '6'
_SEARCH_ENTRIES_VERSION = '1'

# Bump whenever the schema changes incompatibly.
# Cache DB is auto-deleted on mismatch; results DB raises OldSchemaError.
_CACHE_SCHEMA_VERSION   = 2
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

    ver = conn.execute('PRAGMA user_version').fetchone()[0]
    if ver != 0 and ver != _CACHE_SCHEMA_VERSION:
        conn.close()
        try:
            os.remove(db_path)
        except OSError:
            pass
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')

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
    rows = conn.execute('SELECT guid, bundle_id FROM guid_bundle').fetchall()
    return {guid: bid for guid, bid in rows}


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
    rows = conn.execute('SELECT ui_path, detected_type FROM header_types').fetchall()
    return {ui_path: t for ui_path, t in rows}


def clear_header_types(conn: 'sqlite3.Connection') -> None:
    """Delete all header_types rows (used before a full rescan)."""
    conn.execute('DELETE FROM header_types')
    conn.commit()


# ── Run log ───────────────────────────────────────────────────────────────────

def start_run_log(conn: 'sqlite3.Connection', run_type: str,
                  total: int | None = None,
                  notes: str | None = None) -> int:
    """Insert an in-progress run record and return its id.

    Call this when a scan/artifact run begins, then call complete_run_log()
    when it finishes.  run_type should be 'header_scan' or 'artifact_<script_name>'.
    """
    cur = conn.execute(
        'INSERT INTO run_log (run_type, total, complete, notes) VALUES (?, ?, 0, ?)',
        (run_type, total, notes),
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
        'SELECT run_at, completed_at, total, processed, output_rows, complete, notes '
        'FROM run_log WHERE run_type=? ORDER BY id DESC LIMIT 1',
        (run_type,),
    ).fetchone()
    if row is None:
        return None
    return {
        'run_at': row[0], 'completed_at': row[1], 'total': row[2],
        'processed': row[3], 'output_rows': row[4],
        'complete': bool(row[5]), 'notes': row[6],
    }


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


# ── Folder sizes and counts ───────────────────────────────────────────────────

def load_folder_data(conn: 'sqlite3.Connection') -> tuple[dict, dict]:
    """Return (counts, sizes) from the blob.

    counts — {folder_path: int}
    sizes  — {folder_path: int}
    Both empty if not cached or version mismatch.
    """
    raw = load_blob(conn, 'folder_data', _FOLDER_DATA_VERSION)
    if raw is None:
        return {}, {}
    combined = msgpack.unpackb(raw, raw=False)
    counts: dict = {}
    sizes:  dict = {}
    for path, value in combined.items():
        if isinstance(value, list) and len(value) == 2:
            counts[path] = value[0]
            sizes[path]  = value[1]
    return counts, sizes


def save_folder_sizes(conn: 'sqlite3.Connection', sizes: dict) -> None:
    """Merge {folder_path: total_bytes} into the blob, preserving existing counts."""
    existing_counts, _ = load_folder_data(conn)
    combined = {path: [existing_counts.get(path, 0), size] for path, size in sizes.items()}
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
