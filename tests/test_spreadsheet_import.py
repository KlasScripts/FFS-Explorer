"""Tests for spreadsheet_import.py (the "Import Bookmarks…" CSV/XLSX
reader + path/hash normalization) against real files written by csv and
openpyxl — never hand-encoded bytes, same methodology as the rest of
this suite."""
import csv
import hashlib

import pytest

import spreadsheet_import as si


def test_read_csv(tmp_path):
    path = tmp_path / "hashes.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["MD5", "PATH", "STATUS"])
        w.writerow(["abc123", "data/data/foo/bar.jpg", "Cat1"])
        w.writerow(["def456", "data/data/foo/baz.jpg", "Cat2"])
    headers, rows = si.read_spreadsheet(str(path))
    assert headers == ["MD5", "PATH", "STATUS"]
    assert rows == [
        ["abc123", "data/data/foo/bar.jpg", "Cat1"],
        ["def456", "data/data/foo/baz.jpg", "Cat2"],
    ]


def test_read_csv_skips_blank_rows(tmp_path):
    path = tmp_path / "hashes.csv"
    with open(path, "w", newline="") as f:
        f.write("MD5,PATH\nabc123,data/a.jpg\n\n,\ndef456,data/b.jpg\n")
    headers, rows = si.read_spreadsheet(str(path))
    assert headers == ["MD5", "PATH"]
    assert rows == [["abc123", "data/a.jpg"], ["def456", "data/b.jpg"]]


def test_read_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "hashes.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["MD5", "PATH", "ROWID"])
    ws.append(["abc123", "data/data/foo/bar.jpg", 42])
    ws.append(["def456", "data/data/foo/baz.jpg", 43.0])
    wb.save(path)
    headers, rows = si.read_spreadsheet(str(path))
    assert headers == ["MD5", "PATH", "ROWID"]
    # A whole-number float cell (43.0) renders without the trailing
    # ".0" -- openpyxl hands back native numeric types for numeric
    # cells, and a naive str() would otherwise show "43.0" for an
    # ID-like column that's really meant to read as "43".
    assert rows == [
        ["abc123", "data/data/foo/bar.jpg", "42"],
        ["def456", "data/data/foo/baz.jpg", "43"],
    ]


def test_read_spreadsheet_unsupported_extension(tmp_path):
    path = tmp_path / "hashes.xls"
    path.write_bytes(b"not a real xls")
    with pytest.raises(ValueError):
        si.read_spreadsheet(str(path))


@pytest.mark.parametrize("raw,archive,expected", [
    # Real shape confirmed against an actual ProjectVic differences
    # export, 2026-10-07: every path cell prefixed with the archive's
    # own filename and backslashes throughout.
    (r"EXTRACTION_FFS.zip\data\data\kik.android\cache\x.mp4",
     "EXTRACTION_FFS.zip", "data/data/kik.android/cache/x.mp4"),
    # Archive referenced without its own .zip suffix still strips.
    (r"EXTRACTION_FFS\data\data\kik.android\cache\x.mp4",
     "EXTRACTION_FFS.zip", "data/data/kik.android/cache/x.mp4"),
    # Case-insensitive match on the archive name.
    (r"extraction_ffs.zip\data\x.mp4",
     "EXTRACTION_FFS.zip", "data/x.mp4"),
    # Already a bare relative ui_path -- nothing to strip.
    ("data/data/kik.android/cache/x.mp4",
     "EXTRACTION_FFS.zip", "data/data/kik.android/cache/x.mp4"),
    # Surrounding whitespace and a trailing slash.
    ("  data/data/foo/  ",
     "EXTRACTION_FFS.zip", "data/data/foo"),
    ("", "EXTRACTION_FFS.zip", ""),
])
def test_normalize_ffs_path(raw, archive, expected):
    assert si.normalize_ffs_path(raw, archive) == expected


@pytest.mark.parametrize("raw,expected", [
    ("ABC123", "abc123"),
    ("  abc123  ", "abc123"),
    ("0xABC123", "abc123"),
    ("", ""),
])
def test_normalize_hash(raw, expected):
    assert si.normalize_hash(raw) == expected


def test_compute_hash_matches_hashlib():
    data = b"some file content"
    assert si.compute_hash(data, "md5") == hashlib.md5(data).hexdigest()
    assert si.compute_hash(data, "sha256") == hashlib.sha256(data).hexdigest()
