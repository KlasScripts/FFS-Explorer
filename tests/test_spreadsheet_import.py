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


@pytest.mark.parametrize("raw,archive,expected", [
    # Real shape confirmed against an actual Griffeye case-analysis
    # export, 2026-10-10: the folder the FFS zip itself was sitting in
    # on disk, ON TOP of the zip's own filename -- two extra segments,
    # not one.
    (r"CaseFolder\EXTRACTION_FFS.zip\data\data\kik.android\x.mp4",
     "EXTRACTION_FFS.zip", "data/data/kik.android/x.mp4"),
    # Still works with no archive-name segment to find afterward.
    (r"CaseFolder\data\x.mp4", "EXTRACTION_FFS.zip", "data/x.mp4"),
    # Nothing left after stripping the parent -- no crash, no archive
    # segment to match either.
    ("CaseFolder", "EXTRACTION_FFS.zip", "CaseFolder"),
    ("", "EXTRACTION_FFS.zip", ""),
])
def test_normalize_ffs_path_strip_parent_folder(raw, archive, expected):
    assert si.normalize_ffs_path(raw, archive, strip_parent_folder=True) == expected


@pytest.mark.parametrize("raw_path,raw_name,expected", [
    # Real shape confirmed against an actual Griffeye export, 2026-10-10:
    # "File Path" holds only the containing directory, "File Name" the
    # leaf -- joining them is required before the path can be matched
    # against the archive at all (a bug fixed this same day: the
    # filename column used to be read ONLY for the bookmark's own
    # display name, never joined into the path actually looked up).
    (r"EXTRACTION_FFS.zip\data\data\kik.android", "x.mp4",
     r"EXTRACTION_FFS.zip\data\data\kik.android/x.mp4"),
    # Trailing separator on the directory doesn't double up.
    ("EXTRACTION_FFS.zip\\data\\data\\kik.android\\", "x.mp4",
     "EXTRACTION_FFS.zip\\data\\data\\kik.android/x.mp4"),
    # Blank filename -- no-op, filepath used as-is (so an ordinary
    # combined-column row with "Filename is its own column" accidentally
    # left checked still works rather than silently breaking).
    (r"data\x.mp4", "", r"data\x.mp4"),
    # Blank filepath -- filename alone (a root-level file).
    ("", "x.mp4", "x.mp4"),
])
def test_combine_filepath_and_filename(raw_path, raw_name, expected):
    assert si.combine_filepath_and_filename(raw_path, raw_name) == expected


@pytest.mark.parametrize("ui_path,expected", [
    # Real shape confirmed against a real Cellebrite-format iOS archive
    # + a real Griffeye export, 2026-10-10: TWO real quirks stacked —
    # Griffeye's own generic "root/" extraction-root marker (unrelated
    # to this app's OWN "root" = /private/var/root meaning), on top of
    # the literal "private/var/mobile/..." this app's convention
    # collapses to "mobile/...". All three interpretations offered;
    # only the last ("mobile/Library/ContactsMetadata") is the one that
    # actually resolves in a real archive.
    ("root/private/var/mobile/Library/ContactsMetadata",
     ["root/private/var/mobile/Library/ContactsMetadata",
      "private/var/mobile/Library/ContactsMetadata",
      "mobile/Library/ContactsMetadata"]),
    # Already-collapsed form (no Griffeye "root/" marker) -- the
    # straight private/var/mobile collapse alone is enough.
    ("private/var/mobile/Library/X",
     ["private/var/mobile/Library/X", "mobile/Library/X"]),
    ("private/var/root/Library/X",
     ["private/var/root/Library/X", "root/Library/X"]),
    ("private/var/mobile", ["private/var/mobile", "mobile"]),
    # Griffeye's own root/ marker with NO private/var underneath --
    # just the marker-stripped candidate, no collapse possible.
    ("root/Library/Preferences/x.plist",
     ["root/Library/Preferences/x.plist", "Library/Preferences/x.plist"]),
    # private/var/mobile appearing mid-path (not as a prefix) must NOT
    # be rewritten -- this is prefix-anchored, not a substring match.
    ("data/backup/private/var/mobile/x",
     ["data/backup/private/var/mobile/x"]),
    # No "private/var/mobile|root" segment and no "root/" marker at all
    # -- just the one candidate, unchanged.
    ("data/data/com.example/file.db", ["data/data/com.example/file.db"]),
    ("", [""]),
])
def test_ios_path_candidates(ui_path, expected):
    assert si.ios_path_candidates(ui_path) == expected


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
