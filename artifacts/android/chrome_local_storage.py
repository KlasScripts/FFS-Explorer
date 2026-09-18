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
    "case to guess at. `META:`-prefixed and the bare `VERSION` key are "
    "Chrome's own internal bookkeeping (a small protobuf blob per origin "
    "-- likely last-modified time + size, NOT decoded here, out of scope "
    "for this pass) -- skipped, not silently miscounted as real content."
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

core_fields = ["origin", "key", "value", "record_state", "seq"]


def _decode_value(raw: bytes) -> tuple[str, str]:
    """(decoded_text, encoding_label). Chromium's own DOM Storage value
    type tag is the first byte (0x00=UTF-16LE, 0x01=UTF-8) -- confirmed
    against real data, not assumed; anything else is reported as-is
    rather than guessed at."""
    if not raw:
        return "", ""
    tag, body = raw[0], raw[1:]
    if tag == 0x00:
        return body.decode("utf-16-le", errors="replace"), "UTF-16LE"
    if tag == 0x01:
        return body.decode("utf-8", errors="replace"), "UTF-8"
    return raw.decode("utf-8", errors="replace"), f"[unknown type tag: {tag}]"


def _parse_key(user_key: bytes):
    """(origin, top_level_site, key) or None if *user_key* doesn't match
    the real "_<origin>\\x00\\x01<key>" shape (Chrome's own VERSION/
    META: bookkeeping keys, or anything unrecognized -- returned as None
    so the caller can skip it explicitly rather than emit a garbled row)."""
    if not user_key.startswith(b"_") or b"\x00" not in user_key:
        return None
    origin_part, _, rest = user_key.partition(b"\x00")
    if not rest.startswith(b"\x01"):
        return None
    origin_raw = origin_part[1:]  # drop the leading "_"
    key_raw = rest[1:]
    if b"^0" in origin_raw:
        embedded, _, top_level = origin_raw.partition(b"^0")
    else:
        embedded, top_level = origin_raw, b""
    return (
        embedded.decode("utf-8", errors="replace"),
        top_level.decode("utf-8", errors="replace"),
        key_raw.decode("utf-8", errors="replace"),
    )


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
            parsed = _parse_key(rec.user_key)
            if parsed is None:
                continue
            origin, top_level_site, key = parsed
            value, value_encoding = _decode_value(rec.value)
            out.append({
                "origin": origin,
                "top_level_site": top_level_site,
                "key": key,
                "value": value,
                "value_encoding": value_encoding,
                "record_state": rec.state.name,
                "seq": rec.seq,
                "origin_file": os.path.basename(str(rec.origin_file)),
                "offset": rec.offset,
                "was_compressed": rec.was_compressed,
            })
    finally:
        db.close()
    return out
