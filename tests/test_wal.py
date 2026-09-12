"""Tests for sqlite_carve.locate_wal_offset / identify_wal_structure against
the wal_db fixture -- a real, un-checkpointed WAL genuinely holding a row
that's since been deleted from the live base file, the same recovery shape
this session's real LINE fts_message_content docid=27 case demonstrated."""
import os
import sqlite3
import struct
import tempfile

import pytest

from sqlite_carve import build_page_map, locate_wal_offset, identify_wal_structure


def _wal_page_size(wal_raw: bytes) -> int:
    return struct.unpack(">I", wal_raw[8:12])[0]


def test_wal_fixture_base_file_has_no_deleted_row(wal_db):
    """Sanity check on the fixture itself: the live base file must NOT
    see the deleted row -- otherwise this isn't testing WAL recovery at
    all, just an ordinary live query."""
    base_raw, wal_raw, deleted_rowid, deleted_text = wal_db
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base_raw)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT * FROM notes").fetchall()
        finally:
            conn.close()
    finally:
        os.remove(path)
    assert rows == [(1, "kept row")]


def test_locate_wal_offset_recovers_deleted_row(wal_db):
    base_raw, wal_raw, deleted_rowid, deleted_text = wal_db
    page_size = _wal_page_size(wal_raw)
    base_page_map = build_page_map(base_raw)
    assert base_page_map is not None

    fd, base_path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base_raw)
        base_conn = sqlite3.connect(f"file:{base_path}?mode=ro", uri=True)
        try:
            needle = deleted_text.encode()
            byte_offset = wal_raw.find(needle)
            assert byte_offset != -1, "fixture's own deleted text not found in the WAL bytes"

            result = locate_wal_offset(wal_raw, byte_offset, page_size,
                                       base_page_map, base_conn)
            assert result is not None
            assert result["table"] == "notes"
            assert result["rowid"] == deleted_rowid
            assert result["is_wal"] is True
            assert result["row_values"]["text"] == deleted_text
            # rowid-alias substitution: `id` is a plain INTEGER PRIMARY
            # KEY, stored as a body placeholder NULL -- must be
            # substituted with the real, already-known cell rowid.
            assert result["row_values"]["id"] == deleted_rowid
        finally:
            base_conn.close()
    finally:
        os.remove(base_path)


def test_locate_wal_offset_excludes_sqlite_master(wal_db):
    base_raw, wal_raw, deleted_rowid, deleted_text = wal_db
    page_size = _wal_page_size(wal_raw)
    base_page_map = build_page_map(base_raw)

    fd, base_path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(base_raw)
        base_conn = sqlite3.connect(f"file:{base_path}?mode=ro", uri=True)
        try:
            for offset in range(32, len(wal_raw), 251):  # arbitrary stride
                result = locate_wal_offset(wal_raw, offset, page_size,
                                           base_page_map, base_conn)
                if result is not None:
                    assert result["table"] != "sqlite_master"
        finally:
            base_conn.close()
    finally:
        os.remove(base_path)


def test_identify_wal_structure_resolves_table_frame(wal_db):
    base_raw, wal_raw, deleted_rowid, deleted_text = wal_db
    page_size = _wal_page_size(wal_raw)
    base_page_map = build_page_map(base_raw)

    needle = deleted_text.encode()
    byte_offset = wal_raw.find(needle)
    result = identify_wal_structure(wal_raw, byte_offset, page_size, base_page_map)
    assert result is not None
    assert result["is_wal"] is True
    assert result["kind"] == "table"
    assert result["name"] == "notes"
