"""Tests for sqlite_carve.build_page_map / locate_offset / identify_structure
against a small, real (genuinely-written-by-sqlite3) fixture covering the
two real bugs found and fixed this session:
  - a WITHOUT ROWID table is physically index-shaped despite
    sqlite_master.type == 'table' (the real Chromium clusters_and_visits
    bug) -- kv_store in basic_db_raw reproduces this directly.
  - locate_offset's cached path must exclude sqlite_master itself, to
    stay in parity with its own pre-cache fallback scope.
"""
import os
import sqlite3
import tempfile

import pytest

from sqlite_carve import build_page_map, locate_offset, identify_structure, locate_live_row


def _schema_rootpages(raw: bytes) -> dict:
    """Independent ground truth for {name: (type, rootpage)}, read via a
    plain sqlite3 connection rather than anything under test."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, type, rootpage FROM sqlite_master "
                "WHERE type IN ('table','index')").fetchall()
        finally:
            conn.close()
        return {name: (typ, rootpage) for name, typ, rootpage in rows}
    finally:
        os.remove(path)


def _page_size(raw: bytes) -> int:
    return (raw[16] << 8) | raw[17]


def test_build_page_map_classifies_ordinary_table_and_index(basic_db_raw):
    schema = _schema_rootpages(basic_db_raw)
    page_size = _page_size(basic_db_raw)
    page_map = build_page_map(basic_db_raw)
    assert page_map is not None

    messages_root = schema["messages"][1]
    index_root = schema["idx_messages_body"][1]
    assert page_map[messages_root]["kind"] == "table"
    assert page_map[messages_root]["name"] == "messages"
    assert page_map[index_root]["kind"] == "index"
    assert page_map[index_root]["name"] == "idx_messages_body"


def test_build_page_map_classifies_without_rowid_table_as_table(basic_db_raw):
    """The real bug: kv_store is WITHOUT ROWID, so it's physically an
    index-shaped b-tree, but sqlite_master.type still says 'table'. A
    page-shape-only classifier would misreport it as an index (or miss
    it entirely) -- build_page_map must trust the schema's own `type`
    column, not the page's own byte shape."""
    schema = _schema_rootpages(basic_db_raw)
    page_map = build_page_map(basic_db_raw)
    kv_root = schema["kv_store"][1]
    assert schema["kv_store"][0] == "table"
    assert page_map[kv_root]["kind"] == "table"
    assert page_map[kv_root]["name"] == "kv_store"


def test_build_page_map_includes_sqlite_master_synthetic_entry(basic_db_raw):
    page_map = build_page_map(basic_db_raw)
    assert page_map[1]["kind"] == "table"
    assert page_map[1]["name"] == "sqlite_master"


def test_locate_offset_round_trip_ordinary_row(basic_db_raw):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(basic_db_raw)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rowid = conn.execute(
                "SELECT id FROM messages WHERE body = 'hello world 5'").fetchone()[0]
        finally:
            conn.close()
    finally:
        os.remove(path)

    located = locate_live_row(basic_db_raw, "messages", rowid)
    assert located is not None

    result = locate_offset(basic_db_raw, located["abs_offset"])
    assert result is not None
    assert result["table"] == "messages"
    assert result["rowid"] == rowid


def test_locate_offset_cached_and_fresh_agree(basic_db_raw):
    page_map = build_page_map(basic_db_raw)
    page_size = _page_size(basic_db_raw)
    mismatches = 0
    for offset in range(0, len(basic_db_raw), 137):  # arbitrary stride, not page-aligned
        fresh = locate_offset(basic_db_raw, offset)
        cached = locate_offset(basic_db_raw, offset, page_map=page_map)
        if fresh != cached:
            mismatches += 1
    assert mismatches == 0


def test_locate_offset_excludes_sqlite_master_even_when_cached(basic_db_raw):
    """The real, second divergence found this session: build_page_map
    marks sqlite_master's own pages as kind='table' (for
    identify_structure's benefit), but locate_offset's original,
    pre-cache scope never covered sqlite_master rows at all -- the
    cached path must keep excluding them explicitly."""
    page_map = build_page_map(basic_db_raw)
    page_size = _page_size(basic_db_raw)
    # Page 1 holds sqlite_master's own root -- scan every offset on it
    # and confirm none resolve to table == 'sqlite_master'.
    for offset in range(100, page_size):
        result = locate_offset(basic_db_raw, offset, page_map=page_map)
        if result is not None:
            assert result["table"] != "sqlite_master"


def test_identify_structure_reports_index_kind(basic_db_raw):
    schema = _schema_rootpages(basic_db_raw)
    page_size = _page_size(basic_db_raw)
    index_root = schema["idx_messages_body"][1]
    offset = (index_root - 1) * page_size + 100
    result = identify_structure(basic_db_raw, offset)
    assert result is not None
    assert result["kind"] == "index"
    assert result["name"] == "idx_messages_body"


def test_identify_structure_reports_without_rowid_table_as_table(basic_db_raw):
    schema = _schema_rootpages(basic_db_raw)
    page_size = _page_size(basic_db_raw)
    kv_root = schema["kv_store"][1]
    offset = (kv_root - 1) * page_size + 100
    result = identify_structure(basic_db_raw, offset)
    assert result is not None
    assert result["kind"] == "table"
    assert result["name"] == "kv_store"


def test_identify_structure_file_header(basic_db_raw):
    result = identify_structure(basic_db_raw, 50)
    assert result == {"kind": "file_header", "page": 1}


def test_rowid_alias_substitution_in_locate_offset_row_values(basic_db_raw):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(basic_db_raw)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rowid = conn.execute(
                "SELECT id FROM messages WHERE body = 'hello world 5'").fetchone()[0]
        finally:
            conn.close()
    finally:
        os.remove(path)

    located = locate_live_row(basic_db_raw, "messages", rowid)
    result = locate_offset(basic_db_raw, located["abs_offset"])
    assert result["row_values"] is not None
    # `id` is a plain INTEGER PRIMARY KEY -- a rowid-alias column, stored
    # as a 0-byte NULL placeholder in the record body -- locate_offset
    # must substitute the real, already-known cell rowid in its place.
    assert result["row_values"]["id"] == rowid
    assert result["row_values"]["body"] == "hello world 5"
