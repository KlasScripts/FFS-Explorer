"""db_utils.py — shared case-database utilities."""

import os
import sqlite3


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

    # Migration must run before search_results is created so it can safely
    # drop the old flat table (with keyword column) before we attempt to add
    # the new index on term_id — which doesn't exist in the old schema.
    _migrate(conn)

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

    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrate old flat search_results schema (zip_path, keyword columns) to the
    normalised search_index / search_results schema if needed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(search_results)")}
    if 'keyword' not in cols:
        return  # already on new schema

    # Migrate existing rows into search_index + new search_results
    rows = conn.execute(
        'SELECT zip_path, keyword, filename, offset, context FROM search_results'
    ).fetchall()

    conn.execute('DROP TABLE IF EXISTS search_results')
    conn.execute('''
        CREATE TABLE search_results (
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

    for zip_path, keyword, filename, offset, context in rows:
        conn.execute(
            'INSERT OR IGNORE INTO search_index (zip_path, keyword) VALUES (?, ?)',
            (zip_path, keyword)
        )
        (term_id,) = conn.execute(
            'SELECT id FROM search_index WHERE zip_path=? AND keyword=?',
            (zip_path, keyword)
        ).fetchone()
        conn.execute(
            'INSERT INTO search_results (term_id, filename, offset, context) VALUES (?,?,?,?)',
            (term_id, filename, offset, context)
        )
