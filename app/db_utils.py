"""db_utils.py — shared case-database utilities."""

import os
import sqlite3

import msgpack

# Increment whenever the content-status logic changes (e.g. new status values).
# Cached status dicts whose version marker doesn't match are discarded so the
# folder scan re-runs with the current logic.
_FOLDER_CACHE_VERSION = '5'

# Bump whenever the database schema changes incompatibly.  Old databases
# (user_version=0 with existing tables, or any version != _SCHEMA_VERSION)
# raise OldSchemaError instead of attempting a migration.
_SCHEMA_VERSION = 1


class OldSchemaError(Exception):
    """Raised when casedata.db was created by an older incompatible app version."""


def _open_case_db(cache_dir: str) -> sqlite3.Connection:
    """Open (or create) the per-case database inside *cache_dir*.

    Tables:
      thumbnails      — cached media thumbnails
      search_index    — normalised (zip_path, keyword) → id lookup
      search_results  — hit rows keyed by search_index.id (term_id)
      device_info     — cached device labels per zip
      recent_searches — MRU list of search terms

    Raises ValueError if cache_dir is falsy so callers that hold a None
    case_dir are forced to guard before calling.
    """
    if not cache_dir:
        raise ValueError("cache_dir must be set; no global fallback exists")
    os.makedirs(cache_dir, exist_ok=True)
    db_path = os.path.join(cache_dir, 'casedata.db')

    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')

    # Detect databases created by older app versions and refuse to open them.
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
            zip_path   TEXT    NOT NULL,
            ui_path    TEXT    NOT NULL,
            file_size  INTEGER NOT NULL,
            thumb_size INTEGER NOT NULL,
            data       BLOB    NOT NULL,
            PRIMARY KEY (zip_path, ui_path, file_size, thumb_size)
        )
    ''')

    # Normalised search index — deduplicates the long zip_path + keyword strings
    # that would otherwise repeat on every result row.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS search_index (
            id       INTEGER PRIMARY KEY,
            zip_path TEXT    NOT NULL,
            keyword  TEXT    NOT NULL,
            UNIQUE (zip_path, keyword)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS device_info (
            zip_path    TEXT PRIMARY KEY,
            make        TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            ios_version TEXT NOT NULL DEFAULT '',
            hw_id       TEXT NOT NULL DEFAULT ''
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
        CREATE TABLE IF NOT EXISTS search_entries (
            zip_path    TEXT    NOT NULL,
            zip_size    INTEGER NOT NULL,
            filename    TEXT    NOT NULL,
            data_offset INTEGER NOT NULL,
            file_size   INTEGER NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_search_entries_zip
        ON search_entries (zip_path, zip_size)
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
            zip_path      TEXT NOT NULL,
            ui_path       TEXT NOT NULL,
            detected_type TEXT NOT NULL,
            PRIMARY KEY (zip_path, ui_path)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS guid_bundle (
            zip_path  TEXT NOT NULL,
            guid      TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            PRIMARY KEY (zip_path, guid)
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS folder_metadata (
            zip_path    TEXT    NOT NULL,
            folder_path TEXT    NOT NULL,
            count       INTEGER,
            status      TEXT,
            total_size  INTEGER,
            PRIMARY KEY (zip_path, folder_path)
        )
    ''')
    # Blob store for folder sizes — one row per zip, much faster than 500k individual rows.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS folder_sizes_blob (
            zip_path TEXT PRIMARY KEY,
            version  TEXT NOT NULL,
            data     BLOB NOT NULL
        )
    ''')

    conn.execute(f'PRAGMA user_version = {_SCHEMA_VERSION}')
    conn.commit()
    return conn


def save_guid_bundle_map(conn: 'sqlite3.Connection', zip_path: str, mapping: dict) -> None:
    """Persist {guid: bundle_id} into the guid_bundle table."""
    conn.execute('DELETE FROM guid_bundle WHERE zip_path=?', (zip_path,))
    conn.executemany(
        'INSERT INTO guid_bundle (zip_path, guid, bundle_id) VALUES (?,?,?)',
        [(zip_path, guid, bid) for guid, bid in mapping.items()],
    )
    conn.commit()


def load_guid_bundle_map(conn: 'sqlite3.Connection', zip_path: str) -> dict:
    """Return {guid: bundle_id} previously saved for zip_path, or {} if none."""
    rows = conn.execute(
        'SELECT guid, bundle_id FROM guid_bundle WHERE zip_path=?', (zip_path,),
    ).fetchall()
    return {guid: bid for guid, bid in rows}



def save_header_types(conn: 'sqlite3.Connection', zip_path: str, types: dict) -> None:
    """Persist {ui_path: detected_type} into the header_types table."""
    conn.executemany(
        'INSERT OR REPLACE INTO header_types (zip_path, ui_path, detected_type) VALUES (?,?,?)',
        [(zip_path, ui_path, t) for ui_path, t in types.items()],
    )
    conn.commit()


def load_header_types(conn: 'sqlite3.Connection', zip_path: str) -> dict:
    """Return {ui_path: detected_type} previously saved for zip_path."""
    rows = conn.execute(
        'SELECT ui_path, detected_type FROM header_types WHERE zip_path=?',
        (zip_path,),
    ).fetchall()
    return {ui_path: t for ui_path, t in rows}


def save_folder_sizes(conn: 'sqlite3.Connection', zip_path: str, sizes: dict) -> None:
    """Persist {folder_path: total_bytes} as a single msgpack blob for fast loading.

    Legacy rows in folder_metadata (total_size column) are deleted on save so
    the DB doesn't carry duplicate data after migration.
    """
    blob = msgpack.packb(sizes, use_bin_type=True)
    conn.execute(
        'INSERT OR REPLACE INTO folder_sizes_blob (zip_path, version, data) VALUES (?,?,?)',
        (zip_path, _FOLDER_CACHE_VERSION, blob),
    )
    # Remove legacy per-row sizes from folder_metadata (counts are left intact).
    conn.execute(
        "DELETE FROM folder_metadata WHERE zip_path=? AND folder_path='__v__'",
        (zip_path,),
    )
    conn.execute(
        'UPDATE folder_metadata SET total_size=NULL WHERE zip_path=?',
        (zip_path,),
    )
    conn.commit()


def save_folder_counts(conn: 'sqlite3.Connection', zip_path: str, counts: dict) -> None:
    """Upsert {folder_path: count} into folder_metadata.

    Called after background precomputation.  Uses ON CONFLICT DO UPDATE so that
    status values written by save_folder_status are preserved.
    """
    conn.executemany(
        '''INSERT INTO folder_metadata (zip_path, folder_path, count, status)
           VALUES (?, ?, ?, NULL)
           ON CONFLICT(zip_path, folder_path) DO UPDATE SET count=excluded.count''',
        [(zip_path, p, c) for p, c in counts.items()],
    )
    conn.commit()


def load_folder_metadata(conn: 'sqlite3.Connection', zip_path: str) -> tuple[dict, dict]:
    """Return (counts, sizes) previously saved for zip_path.

    counts — {folder_path: int}  (only rows where count IS NOT NULL)
    sizes  — {folder_path: int}  (empty if not cached or version mismatch)

    Sizes are stored as a single msgpack blob in folder_sizes_blob for fast
    loading.  Legacy per-row sizes in folder_metadata are read as a fallback
    for databases written before the blob format was introduced.
    """
    # Counts are still stored per-row (written incrementally as folders are scanned).
    count_rows = conn.execute(
        'SELECT folder_path, count FROM folder_metadata WHERE zip_path=? AND count IS NOT NULL',
        (zip_path,),
    ).fetchall()
    counts = {p: c for p, c in count_rows}

    # Sizes: fast blob path first.
    blob_row = conn.execute(
        'SELECT version, data FROM folder_sizes_blob WHERE zip_path=?',
        (zip_path,),
    ).fetchone()
    if blob_row is not None:
        version, data = blob_row
        if version == _FOLDER_CACHE_VERSION:
            return counts, msgpack.unpackb(data, raw=False)
        return counts, {}

    # Legacy fallback — databases written before the blob format.
    legacy_rows = conn.execute(
        'SELECT folder_path, status, total_size FROM folder_metadata WHERE zip_path=?',
        (zip_path,),
    ).fetchall()
    version = next((s for p, s, ts in legacy_rows if p == '__v__'), None)
    if version != _FOLDER_CACHE_VERSION:
        return counts, {}
    sizes = {p: ts for p, s, ts in legacy_rows if ts is not None and p != '__v__'}
    return counts, sizes


def check_schema(cache_dir: str) -> None:
    """Raise OldSchemaError if casedata.db exists but has an incompatible schema.

    Lightweight check — opens the DB read-only, reads user_version, closes.
    Call this early so the user gets a clear message before any work is done.
    """
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
                return  # new empty DB — not yet stamped
        else:
            conn.close()
    except Exception:
        return  # unreadable — let normal open handle it
    raise OldSchemaError(
        "casedata.db was created by an older version of this app — "
        "please delete casedata.db from the case folder and reopen "
        "the archive to rebuild it."
    )
