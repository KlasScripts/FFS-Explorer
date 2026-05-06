"""db_utils.py — shared case-database utilities.

One casedata.db per case folder.  Each case folder belongs to exactly one
exhibit (FFS archive), so no zip_path key is needed in any table — all rows
implicitly belong to that one archive.
"""

import os
import sqlite3

import msgpack

# Per-key blob versions — increment a version string to invalidate stale data.
_FOLDER_DATA_VERSION    = '6'
_SEARCH_ENTRIES_VERSION = '1'

# Bump whenever the database schema changes incompatibly.  Old databases
# (user_version != _SCHEMA_VERSION) are auto-deleted and rebuilt rather than
# migrated — all content is reconstructable cache or per-case user data.
_SCHEMA_VERSION = 4


class OldSchemaError(Exception):
    """Raised when casedata.db was created by an older incompatible app version."""


def _open_case_db(cache_dir: str) -> sqlite3.Connection:
    """Open (or create) the per-case database inside *cache_dir*.

    Tables:
      thumbnails    — cached media thumbnails
      search_index  — normalised keyword → id lookup
      search_results— hit rows keyed by search_index.id (term_id)
      device_info   — per-field device metadata
      recent_searches— MRU list of search terms
      header_types  — detected file types for 'Other' entries
      guid_bundle   — GUID → bundle-ID map
      blobs         — key/version/data store for msgpack-encoded caches:
                        'folder_data'    → {folder_path: [count, size_bytes]}
                        'search_entries' → [[filename, data_offset, file_size], ...]

    Raises ValueError if cache_dir is falsy.
    Raises OldSchemaError if casedata.db has an incompatible schema.
    """
    if not cache_dir:
        raise ValueError("cache_dir must be set; no global fallback exists")
    os.makedirs(cache_dir, exist_ok=True)
    db_path = os.path.join(cache_dir, 'casedata.db')

    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')

    _ver = conn.execute('PRAGMA user_version').fetchone()[0]
    if _ver == 0:
        _tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if _tables:
            conn.close()
            raise OldSchemaError(
                "casedata.db was created by an older version of this app — "
                "please delete casedata.db from the case folder and reopen "
                "the archive to rebuild it."
            )
    elif _ver != _SCHEMA_VERSION:
        conn.close()
        raise OldSchemaError(
            f"casedata.db schema version {_ver} is not compatible with this "
            f"version of the app (expected {_SCHEMA_VERSION}) — please delete "
            "casedata.db from the case folder and reopen the archive to rebuild it."
        )

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
        CREATE TABLE IF NOT EXISTS search_index (
            id      INTEGER PRIMARY KEY,
            keyword TEXT    NOT NULL UNIQUE
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS device_info (
            field_name TEXT NOT NULL PRIMARY KEY,
            data       TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT ''
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS recent_searches (
            term    TEXT    NOT NULL,
            used_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (term)
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
        CREATE TABLE IF NOT EXISTS blobs (
            key     TEXT NOT NULL PRIMARY KEY,
            version TEXT NOT NULL,
            data    BLOB NOT NULL
        )
    ''')

    conn.execute(f'PRAGMA user_version = {_SCHEMA_VERSION}')
    conn.commit()
    return conn


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


# ── Schema check (lightweight, no table creation) ─────────────────────────────

def check_schema(cache_dir: str) -> None:
    """Raise OldSchemaError if casedata.db exists with an incompatible schema."""
    db_path = os.path.join(cache_dir, 'casedata.db')
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        ver = conn.execute('PRAGMA user_version').fetchone()[0]
        if ver == _SCHEMA_VERSION:
            conn.close()
            return
        if ver == 0:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            conn.close()
            if not tables:
                return  # brand new empty DB
        else:
            conn.close()
    except Exception:
        return
    raise OldSchemaError(
        "casedata.db was created by an older version of this app — "
        "please delete casedata.db from the case folder and reopen "
        "the archive to rebuild it."
    )
