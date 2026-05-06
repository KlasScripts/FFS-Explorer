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
_CACHE_SCHEMA_VERSION   = 1
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
        'INSERT INTO device_info (field_name, data, source) VALUES (?,?,?)',
        fields,
    )
    conn.commit()


def load_device_info(conn: 'sqlite3.Connection') -> list[tuple[str, str, str]]:
    """Return [(field_name, data, source)] in insertion order, or []."""
    return conn.execute(
        'SELECT field_name, data, source FROM device_info ORDER BY rowid'
    ).fetchall()
