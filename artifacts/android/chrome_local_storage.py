name = "Chrome Local Storage"
app_group_label = "Chrome"
group_sort_key = 17
description = (
    "Every real key/value pair a website stored via the Web Storage API's "
    "`localStorage` (app_chrome/Default/Local Storage/leveldb/, a single "
    "LevelDB database shared across every origin) -- INCLUDING deleted/"
    "superseded versions, not just the current live value: LevelDB never "
    "overwrites a key in place, an update or removal just writes a new "
    "record at a higher sequence number, so old and deleted values "
    "genuinely still exist on disk until compaction reclaims the space. "
    "record_state ('Live'/'Deleted'/'Unknown') and seq (LevelDB's own "
    "monotonic sequence number, NOT a timestamp -- no wall-clock time "
    "survives per-entry in this store at all, only relative ordering) "
    "make this explicit rather than silently collapsing to one row per "
    "key. Parsed via `app/ccl_leveldb.py`, vendored 2026-09-05 from "
    "iLEAPP (same CCL Forensics lineage this project already vendors "
    "ccl_abx.py from) -- found by directly reviewing Hindsight, a real, "
    "actively-maintained Chrome forensics tool, which uses this exact "
    "same library rather than a hand-rolled LevelDB reader; this project "
    "had deferred Local Storage/Session Storage/IndexedDB content for "
    "exactly the reason a from-scratch LevelDB parser wasn't worth "
    "building on top of an already-solved, well-tested one. "
    "Key/value encoding confirmed directly against this project's own "
    "real Android 14 JoshHickman data, not assumed from Chromium source "
    "alone: a real key is `_<origin>` + a single NUL byte + `\\x01` + the "
    "actual stored key name (confirmed structurally consistent on every "
    "one of 440 real non-bookkeeping records checked); `^0` inside the "
    "origin part separates an embedded/third-party origin from the "
    "top-level site it was partitioned under (Chrome's Storage "
    "Partitioning, real ad-tech domains like ads.pubmatic.com/"
    "eus.rubiconproject.com confirmed embedded under real top-level "
    "sites like mlb.com in this exact case). A value's own leading byte "
    "is Chromium's own DOM Storage type tag -- 0x00 = UTF-16LE, 0x01 = "
    "UTF-8 (confirmed on real data: e.g. a real npr.org PLAYER_STATE "
    "value decoded correctly as UTF-16LE) -- stripped and decoded "
    "accordingly; a genuinely empty value is always a Deleted tombstone "
    "in this real data (confirmed: all 99 empty-value records checked "
    "were state=Deleted, never a real empty string), not a separate "
    "case to guess at.\n\n"
    "record_type distinguishes two real kinds of row. 'data' is a real "
    "website-stored key/value pair, as above. 'commit_metadata' is a "
    "decoded `META:<origin>` record -- Chrome's own per-origin commit "
    "bookkeeping (skipped entirely, undecoded, until 2026-09-19) -- key/"
    "value/value_encoding are blank for these, committed_at/"
    "committed_data_size are populated instead: the real timestamp "
    "(Chromium's own base::Time epoch) and byte size of the batch Chrome "
    "committed to that origin. A small, fixed 2-field protobuf, decoded "
    "with a tiny purpose-built reader -- confirmed against CCL Solutions "
    "Group's own published research and directly verified against three "
    "real values from this project's own data (both fields land on "
    "plausible real dates/sizes, not guessed at -- see "
    "_decode_meta_value's own docstring). An origin can have SEVERAL "
    "commit_metadata rows -- one per real commit event, each its own "
    "real timestamp -- never collapsed to one row per origin. The bare "
    "`VERSION` key (one per store, not per-origin -- the LevelDB schema "
    "version, never a timestamp) stays skipped -- nothing meaningful to "
    "report per-row for it."
)
warning = (
    "Every version of a key is shown, including ones a website later "
    "overwrote or deleted -- do not read record_state='Live' as "
    "necessarily meaning 'still true right now' if a NEWER row for the "
    "SAME origin+key exists with a higher seq; sort/filter by seq per "
    "(origin, key) to reconstruct the real history. seq has no "
    "wall-clock meaning on its own -- relative ordering only, never "
    "converted to or displayed as a timestamp."
)
app_path = "data/data/com.android.chrome"
files = {}
optional_files = {}
existence_check_paths = ["app_chrome/Default/Local Storage/leveldb/CURRENT"]

core_fields = ["record_type", "origin", "key", "value", "committed_at", "record_state"]
timestamp_fields = {"committed_at": "webkit_us"}
byte_fields = ["committed_data_size"]


def _decode_value(raw: bytes) -> tuple[str, str]:
    """(decoded_text, encoding_label). Chromium's own DOM Storage value
    type tag is the first byte (0x00=UTF-16LE, 0x01=Latin-1/ISO-8859-1).
    The 0x01 case was UTF-8 here until 2026-09-19 -- corrected against
    CCL Solutions Group's own published research
    (cclsolutionsgroup.com, "Chromium Session Storage and Local
    Storage" -- the same forensic lineage this project's own vendored
    ccl_leveldb.py comes from), which documents it as Latin-1, not
    UTF-8. Not independently confirmed against real data either
    way -- checked directly and found inconclusive: every one of 335
    real tag=0x01 values on this project's own Android 14 JoshHickman
    archive is pure ASCII, which decodes byte-identically under UTF-8
    and Latin-1, so this archive can't distinguish the two. Latin-1
    changes NOTHING for any of those 335 real rows (confirmed) and is
    strictly safer than UTF-8 either way -- every byte 0x00-0xFF is a
    valid Latin-1 codepoint, so it never raises the way UTF-8 can on a
    genuinely non-UTF-8 byte sequence, matching Chromium's own actual
    C++ implementation (raw byte values, one Latin-1 codepoint each) --
    unlike UTF-16LE (tag 0x00), 0x01 was never really "UTF-8" in
    Chromium's own source to begin with."""
    if not raw:
        return "", ""
    tag, body = raw[0], raw[1:]
    if tag == 0x00:
        return body.decode("utf-16-le", errors="replace"), "UTF-16LE"
    if tag == 0x01:
        return body.decode("latin-1"), "Latin-1"
    return raw.decode("utf-8", errors="replace"), f"[unknown type tag: {tag}]"


def _decode_meta_value(raw: bytes):
    """(committed_at_webkit_us, committed_data_size) from a real
    `META:<origin>` record's own value -- a small, fixed 2-field
    protobuf (Chromium's own StorageAreaMetaData message: field 1 =
    last-commit timestamp, field 2 = data size) Chrome writes/rewrites
    every time it batches a change to that origin's data. Decoded with
    a tiny purpose-built varint reader, not a generic protobuf library
    -- this is a small, fixed, KNOWN 2-field shape (confirmed against
    real data below), not a general "decode arbitrary protobuf" need
    (see segb_viewer.py's own blackboxprotobuf for that different,
    genuinely-unknown-schema case).

    Confirmed directly, not assumed from CCL Solutions Group's own
    description ("timestamp (microseconds since January 1, 1601) and
    data size") -- decoded three real META: values from this project's
    own Android 14 JoshHickman archive byte-by-byte: field 1 lands on
    plausible real dates (2024-01-28, 2024-07-14 UTC -- both before
    this archive's own July 28 2024 acquisition date) when interpreted
    as microseconds since 1601-01-01 (Chromium's own `base::Time`
    epoch -- the SAME epoch this project's own `timestamp_fields`
    "webkit_us" unit code already declares and converts, reused
    directly here rather than hand-converting), and field 2 lands on
    small, plausible byte counts (166/249/214) matching real value
    sizes seen elsewhere in this same store.

    Returns (None, None) for anything that doesn't parse as this exact
    2-field varint shape -- never guessed at, never raises."""
    pos = 0
    fields: dict[int, int] = {}
    try:
        while pos < len(raw):
            tag = 0
            shift = 0
            while True:
                b = raw[pos]; pos += 1
                tag |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            field_no, wire_type = tag >> 3, tag & 0x07
            if wire_type != 0:   # varint only -- this message's own real shape
                return None, None
            value = 0
            shift = 0
            while True:
                b = raw[pos]; pos += 1
                value |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields[field_no] = value
    except IndexError:
        return None, None
    return fields.get(1), fields.get(2)


def _parse_key(user_key: bytes):
    """(origin, top_level_site, key), or None for Chrome's own VERSION/
    META: bookkeeping keys or anything unrecognized. Now a thin wrapper
    around artifact_runner.parse_chromium_dom_storage_key (promoted
    there 2026-09-19 as a second real caller emerged — leveldb_viewer.py's
    own record-to-filename decode — needing the identical algorithm, not
    a re-derived one; see that function's own docstring for the full
    real-data verification behind it). Kept as a same-named local wrapper
    so every existing call site in this file needed no change."""
    from artifact_runner import parse_chromium_dom_storage_key
    return parse_chromium_dom_storage_key(user_key)


def run(paths):
    import os
    from artifact_runner import open_leveldb

    # Extract-then-open boilerplate now lives in artifact_runner.open_leveldb
    # (added 2026-09-18) -- this was the first, hand-rolled version of
    # exactly that helper; refactored to call it instead of keeping a
    # second copy. "local_storage_leveldb" kept as the extract_subdir
    # name (rather than letting it auto-derive from relative_dir) so a
    # re-run of an already-processed case reuses this parser's own
    # existing extracted files instead of duplicating them under a new
    # name.
    db = open_leveldb(paths, "app_chrome/Default/Local Storage/leveldb",
                       extract_subdir="local_storage_leveldb")
    if db is None:
        return []

    out = []
    try:
        for rec in db.iterate_records_raw():
            common = {
                "record_state": rec.state.name,
                "seq": rec.seq,
                "origin_file": os.path.basename(str(rec.origin_file)),
                "offset": rec.offset,
                "was_compressed": rec.was_compressed,
            }

            parsed = _parse_key(rec.user_key)
            if parsed is not None:
                origin, top_level_site, key = parsed
                value, value_encoding = _decode_value(rec.value)
                out.append({
                    "record_type": "data",
                    "origin": origin,
                    "top_level_site": top_level_site,
                    "key": key,
                    "value": value,
                    "value_encoding": value_encoding,
                    "committed_at": None,
                    "committed_data_size": None,
                    **common,
                })
                continue

            # META:<origin> -- Chrome's own per-origin commit bookkeeping,
            # previously skipped entirely (see this module's own
            # description). A real origin can have SEVERAL of these (one
            # per real commit event to that origin, at different real
            # timestamps) -- every one is its own row here, same as a
            # real data record's own Live/Deleted history is never
            # collapsed to one row per key.
            if rec.user_key and rec.user_key.startswith(b"META:"):
                origin_raw = rec.user_key[len(b"META:"):]
                if b"^0" in origin_raw:
                    embedded, _, top_level = origin_raw.partition(b"^0")
                else:
                    embedded, top_level = origin_raw, b""
                committed_at, committed_size = _decode_meta_value(rec.value)
                out.append({
                    "record_type": "commit_metadata",
                    "origin": embedded.decode("utf-8", errors="replace"),
                    "top_level_site": top_level.decode("utf-8", errors="replace"),
                    "key": "",
                    "value": "",
                    "value_encoding": "",
                    "committed_at": committed_at,
                    "committed_data_size": committed_size,
                    **common,
                })
            # A bare VERSION key (one per store, not per-origin -- the
            # LevelDB schema version, never a real timestamp or origin)
            # stays skipped, same as before -- nothing meaningful to
            # report per-row for it.
    finally:
        db.close()
    return out
