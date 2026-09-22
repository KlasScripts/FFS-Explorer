"""leveldb_viewer.py — LevelDbViewerMixin: make a LevelDB directory's own
real records browsable as if they were plain files, directly in the normal
File Browser tree/table — no separate viewer tab (a first version of this
feature had one; replaced 2026-09-19 per direct user request for this
"records as files" approach instead, which reuses the file browser's own
existing preview machinery rather than a bespoke table).

Built on this project's own EXISTING nested-archive mechanism
(ffs-explorer.py's `_inject_nested_archives`/`_nested_virtual_paths`/
`full_metadata`) rather than inventing a parallel one — studied that
mechanism directly before designing this: an extracted nested archive's own
entries are injected into `folder_map` (so the archive's own ui_path
becomes a navigable "folder") and `full_metadata` (`_display_name`,
`_archive_path` marking where to read the real bytes from) as synthetic
leaf paths in `_nested_virtual_paths`. LevelDB records use the exact same
`folder_map`/`full_metadata`/`_nested_virtual_paths` machinery, marked with
`_leveldb_folder`/`_leveldb_record_index` instead of `_archive_path` — see
`ffs-explorer.py`'s own `_load_virtual_entry_preview` for the one place
that actually needs to know which of the two a given virtual path is.

Deliberately NOT sharing `_nested_archive_map` itself, unlike the leaf-path
set — checked directly, not assumed safe: `keyword_search.py`'s own
`_filter_entries_by_ui_paths` reads that map and routes each entry to
`NestedArchiveSearchWorker`, which opens the entry's own `stored_path` as a
real ZIP FILE. A LevelDB folder's own extracted copy is a DIRECTORY, not a
zip — sharing the map would either crash that worker or silently misbehave
the first time a decoded LevelDB folder fell inside a keyword-search scope.
LevelDB folders get their own `self._leveldb_folder_map` instead, and the
handful of `_nested_archive_map` checks that are purely cosmetic/UI
(never-hide-as-empty, "Archive" file-type label, precomputed folder size,
bold-italic row styling) each gained one extra, explicit
`or path in self._leveldb_folder_map` condition in `ffs-explorer.py`
instead.

Interaction model, per direct user request:
  - Double-clicking (or otherwise navigating into) a real, undecoded
    LevelDB-shaped folder decodes it IN PLACE, automatically, the first
    time — see `_maybe_decode_leveldb_on_navigate`, hooked into
    `on_folder_selected` (the one real interception point every
    navigation path — tree click, double-click-in-table via
    `navigate_tree_to_path` — already funnels through). This is the one
    deliberate departure from the nested-archive precedent (which needs
    an explicit "Extract as Nested Archive" action before double-click
    means anything) — a double-click here IS the explicit action, not
    automatic background work.
  - Once decoded, the folder's own listing PERMANENTLY becomes one
    virtual "file" per real record (its key, sanitized, as the
    filename; its value as the file's own content) for the rest of this
    session AND future case reopens (re-detected at load time from
    already-extracted files on disk — see
    `_rescan_decoded_leveldb_folders` — no separate DB table needed,
    since re-decoding from already-extracted files is cheap, unlike
    re-scanning a huge zip's own central directory, which is why nested
    archives DO persist their own entry list to a DB table and this
    doesn't need to).
  - Right-click → "View Raw LevelDB Files" is a ONE-OFF PEEK (confirmed
    directly, not assumed): shows the folder's real physical children
    (CURRENT/MANIFEST-*/*.ldb/*.log) in the file table for this one
    view only — `folder_map[ui_path]` is swapped back immediately after
    rendering, so navigating away and back shows decoded records again,
    never a persistent mode switch.
  - Selecting a decoded record "file" and previewing it (double-click,
    or the file browser's normal preview-on-select) routes its VALUE
    bytes through `FastZipBrowser._display_preview_bytes` — the exact
    same JSON/XML/bplist/ABX/plain-text/hex rendering a real file
    already gets, so a record whose value happens to be a binary plist
    (the concrete case this whole feature exists to prove) decodes
    correctly with zero special-casing here."""

import os
import re

# See ccl_leveldb.RawLevelDb.__init__/DATA_FILE_PATTERN and
# ManifestFile.MANIFEST_FILENAME_PATTERN — the real on-disk shape a
# genuine LevelDB directory has, checked directly against that class
# rather than duplicated by guesswork.
_LDB_DATA_PATTERN = re.compile(r'^[0-9]{6}\.(ldb|log|sst)$')
_LDB_MANIFEST_PATTERN = re.compile(r'^MANIFEST-[0-9A-Fa-f]{6}$')

# What the local extraction/reopen-persistence marker file is named,
# inside each case_dir/leveldb_browse/<safe_name>/ folder — holds the
# real archive ui_path that folder was decoded from, read back by
# _rescan_decoded_leveldb_folders at every future case load. A plain
# text file, not a new database table — this project's own established
# "the extracted file being there simply IS the finished-state signal"
# convention (see artifact_runner.run_artifact's own docstring), applied
# to remembering WHICH real folder a given local extraction came from.
_SOURCE_MARKER_NAME = '.ffs_leveldb_source_ui_path'

# Characters safe to keep verbatim in a synthetic vpath segment (used as
# an actual dict key and a literal path component elsewhere in this
# project) — deliberately narrow: a real LevelDB key can hold NUL bytes,
# '/', and arbitrary binary garbage, none of which are safe there.
_UNSAFE_SEGMENT_CHARS = re.compile(r'[^A-Za-z0-9 ._\-]')


def _is_indexeddb_leveldb_dir(ui_path: str) -> bool:
    """True if ui_path's own basename matches Chromium's real IndexedDB
    directory naming convention (`<origin>.indexeddb.leveldb`) — confirmed
    consistent across every real IndexedDB folder surveyed in this
    project's own real casework (Local Storage/Session Storage never end
    this way). A cheap, reliable name-based check — no need to open or
    read anything before deciding whether the REAL, schema-informed
    ccl_chromium_indexeddb decoder (see that module's own docstring) is
    even worth attempting for this folder, added 2026-09-19 directly
    prompted by a user report that this project's own schema-less
    content-type GUESS badly mislabels real IndexedDB values (see
    CLAUDE.md's own "not happy with the results of
    https_cellebrite.com_0.indexeddb..." review entry)."""
    return ui_path.rsplit('/', 1)[-1].endswith('.indexeddb.leveldb')


def _indexeddb_blob_dir_for(ui_path: str) -> str:
    """The real sibling directory Chromium writes an IndexedDB store's
    large/externally-wrapped values into (`<origin>.indexeddb.blob`,
    alongside its `<origin>.indexeddb.leveldb` directory — same parent,
    same origin stem, only the trailing component's suffix differs).
    Confirmed necessary against real data, not theoretical:
    ccl_chromium_indexeddb.IndexedDb.get_blob raises a bare ValueError
    with no blob dir supplied, and a real record on this project's own
    npr.org IndexedDB store genuinely needs one to deserialize at all —
    see _decode_indexeddb_folder's own docstring for why this is looked
    up and extracted UP FRONT, alongside the main directory, rather than
    lazily per-record."""
    parent, _, leaf = ui_path.rpartition('/')
    blob_leaf = leaf[:-len('.leveldb')] + '.blob'
    return f"{parent}/{blob_leaf}" if parent else blob_leaf


def _format_idb_key_value(value) -> str:
    """A short, readable label for an already-decoded
    ccl_chromium_indexeddb.IdbKey.value — which, per that vendored
    decoder's own real IdbKeyType handling, can genuinely be a str, a
    float (IndexedDB's own Number key type), a datetime.datetime (Date
    key type), a tuple of further IdbKey objects (Array key type,
    handled here recursively), bytes (Binary key type), or None (Null
    key type) — every one a real, already-decoded key type this
    project's own vendored decoder assigns, never guessed at here."""
    if isinstance(value, tuple):
        return '[' + ', '.join(
            _format_idb_key_value(v.value if hasattr(v, 'value') else v) for v in value
        ) + ']'
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if value is None:
        return '(null key)'
    return str(value)


class _IdbPreviewRecord:
    """One already-fully-decoded real IndexedDB record's own display
    state — parallel to a raw ccl_leveldb.Record, but everything a
    caller would otherwise compute from raw bytes (content type, preview
    bytes) is already FINAL here, computed once at decode time from the
    real deserialized value ccl_chromium_indexeddb's own
    WrappedObjectStore.iterate_records already produced — never
    re-derived from raw bytes the way a generic (non-IndexedDB)
    LevelDB record's preview is. See _load_leveldb_record_preview's and
    _reapply_leveldb_type_overrides' own `entry.get('kind') ==
    'indexeddb'` branches, the two real consumers of this."""
    __slots__ = ('preview_bytes', 'content_type')

    def __init__(self, preview_bytes: bytes, content_type: str | None):
        self.preview_bytes = preview_bytes
        self.content_type = content_type


def _looks_like_leveldb_dir(folder_map: dict, folder_ui_path: str) -> bool:
    """True if folder_ui_path's own direct children (via folder_map)
    include a real LevelDB CURRENT file and at least one MANIFEST-*/
    NNNNNN.{ldb,log,sst} data file. folder_map[path] holds ALL direct
    children (files and subfolders alike — confirmed by reading
    _get_all_children's own use of it, which distinguishes a subfolder
    from a file by checking membership in folder_map itself, not by any
    separate files-only list) — a bare basename check against each
    child is enough, no separate file-listing lookup needed."""
    children = folder_map.get(folder_ui_path)
    if not children:
        return False
    has_current = False
    has_data = False
    for child in children:
        name = child.rsplit('/', 1)[-1]
        if name == 'CURRENT':
            has_current = True
        elif _LDB_DATA_PATTERN.match(name) or _LDB_MANIFEST_PATTERN.match(name):
            has_data = True
        if has_current and has_data:
            return True
    return False


def _key_display_plausible(text: str) -> bool:
    """True if *text* (already a successful UTF-8 decode of a record's
    raw key) is worth showing AS-IS as a filename, rather than falling
    back to the honest "record_NNNNNN (N bytes, binary key)" label.

    Deliberately NOT a call to artifact_runner.text_plausible — that
    function's own 8-character floor ("nothing meaningful to judge" for
    anything shorter) is the wrong bar for a FILENAME decision
    specifically, found and fixed 2026-09-19 by a direct user report on
    real IndexedDB data: an IndexedDB key is very commonly just a few
    raw bytes (Chromium's own compact-integer key encoding — real
    examples on this project's own real cellebrite.com IndexedDB store
    include a bare `b'\\x00\\x00\\x00\\x00\\x00'`, 5 NUL bytes), which
    decodes as "valid UTF-8" (each byte is individually a valid
    single-byte codepoint) but renders as literally invisible control
    characters — under text_plausible's own 8-char floor this would be
    waved through as "too short to fail," producing a blank-looking
    filename in the file browser, which is worse than the honest
    fallback this function exists to provide. A filename with even one
    or two such bytes is already a bad filename regardless of length,
    so this checks the SAME control-character-fraction ratio
    text_plausible uses, just without its length floor."""
    if not text:
        return False
    control = sum(1 for ch in text if ord(ch) < 32 and ch not in '\t\n\r')
    return control / len(text) <= 0.15


def _sanitize_record_name(raw_key: bytes, index: int,
                          session_storage_names: dict | None = None) -> tuple[str, str]:
    """(vpath_segment, display_name) for one real record's own key bytes.

    session_storage_names — the {raw_key: (origin, top_level_site, key)}
    lookup artifact_runner.resolve_chromium_session_storage_names built
    for this SAME folder's full record set (see _decode_leveldb_folder,
    the only real caller — it always passes this, even for a folder
    that isn't Session Storage-shaped, where it's simply an empty dict).
    Checked FIRST, before anything else below — Session Storage's own
    real `map-<id>-<key>` keys would never match
    parse_chromium_dom_storage_key's Local-Storage-only shape anyway,
    but checking this dict first keeps the resolution order obvious:
    the more specific, cross-referenced answer wins over a generic
    per-key guess whenever it's actually available.

    vpath_segment — used as an actual dict key AND a literal path
    component (folder_map/full_metadata/_nested_virtual_paths all key
    off the full "parent_ui_path/vpath_segment" string) — restricted to
    a narrow safe character set with the index ALWAYS prefixed, so two
    records are never confused even if their real keys collide once
    sanitized, and a key holding a literal NUL/'/' (a real, common case
    — see chrome_local_storage.py's own "_<origin>\\x00\\x01<key>" key
    shape) can never accidentally create a bogus nested "subfolder" or
    break path handling elsewhere in this project.

    display_name — a best-effort readable rendering of the same key,
    shown as the "filename" in the file browser (meta['_display_name']
    — same convention a nested-archive entry's own relative path
    already uses there). Tries THREE things in order, not just UTF-8-
    or-hex: (1) artifact_runner.parse_chromium_dom_storage_key — a
    PROVEN, generic (not Chrome-the-app-specific) format, confirmed
    consistent across nine completely unrelated real apps' own real
    Local Storage on this project's own test data (see that function's
    own docstring for the full verification) — when it matches, this
    shows the SAME origin/top_level_site/key fields
    chrome_local_storage.py's own report already shows, dash-joined,
    rather than the raw "_origin\\x00\\x01key" bytes; (2) plain UTF-8,
    but ONLY when _key_display_plausible accepts the decoded result
    (added 2026-09-19, a second real bug on the SAME real
    cellebrite.com IndexedDB store that first prompted fix 3 below,
    found by direct user report: a genuinely binary key like 5 raw NUL
    bytes decodes as "valid UTF-8" — each byte is its own valid
    codepoint — and used to be shown as-is, rendering as an invisible,
    blank-looking filename in the file browser; see
    _key_display_plausible's own docstring for why this needed a
    purpose-built check rather than reusing artifact_runner.
    text_plausible directly); (3) — the original fix for a related but
    distinct bug found 2026-09-19 (a raw hex dump of the WHOLE key,
    however long, made an IndexedDB-shaped key's own filename
    unreadable and looked broken) — a short, honest synthetic label
    instead of a long hex blob for a key this function genuinely can't
    make sense of, since a filename's job is to be a clear,
    non-misleading label, not to carry full fidelity (the record's own
    raw key bytes are still the real dict/lookup key everywhere else in
    this project's own convention, nothing is lost, just not crammed
    into what's shown here). Both (2)'s implausible-decode case and (3)'s
    UnicodeDecodeError case share the identical fallback label.

    A FOURTH thing is now tried too, before any of the above, for a
    Session Storage record specifically — see the
    session_storage_names parameter above and
    artifact_runner.resolve_chromium_session_storage_names' own
    docstring (added 2026-09-19, directly prompted by a user question
    about what Session Storage's own "namespace"/"map-id" keys mean).

    Still NOT attempted here: decoding IndexedDB's own key format
    specifically — real, structurally consistent across ten real
    origins checked (and independently confirmed against Chromium's own
    official `leveldb_coding_scheme.md`), but decoding it MEANINGFULLY
    needs a real per-record-type decoder (its own database/object-
    store/index metadata records, several distinct IndexedDBKey value
    types), a genuinely bigger feature than a filename tweak."""
    from artifact_runner import parse_chromium_dom_storage_key
    ss_resolved = session_storage_names.get(raw_key) if session_storage_names and raw_key else None
    parsed = ss_resolved or (parse_chromium_dom_storage_key(raw_key) if raw_key else None)
    if parsed:
        origin, top_level_site, key = parsed
        readable = f"{origin} - {top_level_site} - {key}" if top_level_site else f"{origin} - {key}"
    elif raw_key:
        try:
            candidate = raw_key.decode('utf-8')
        except UnicodeDecodeError:
            candidate = None
        readable = (candidate if candidate is not None and _key_display_plausible(candidate)
                    else f"record_{index:06d} ({len(raw_key)} bytes, binary key)")
    else:
        readable = '(empty key)'
    display_name = readable if len(readable) <= 120 else readable[:120] + '…'

    safe = _UNSAFE_SEGMENT_CHARS.sub('_', readable)[:60].strip('_') or 'record'
    vpath_segment = f"{index:06d}_{safe}"
    return vpath_segment, display_name


def _content_shape(text: str) -> str | None:
    """'json'/'plist'/'xml' if TEXT (already decoded — never raw bytes,
    see classify_leveldb_value's own docstring for why that distinction
    is exactly what a real bug turned on) genuinely parses as one, else
    None — real validation (json.loads / xml.dom.minidom), not just a
    leading-character sniff, so a "json"/"xml"/"plist" label a user
    filters by is trustworthy, not a false positive on something that
    merely starts with '{'/'<'. 'plist' specifically means an XML
    property list (`<plist` present — the same real check Crush's own
    plist_parser.py uses, `_is_plist_xml`) — a real, distinct, common
    enough shape to name separately from generic XML, not a guess at a
    new category."""
    stripped = text.lstrip()
    if stripped[:1] in ('{', '['):
        try:
            import json
            json.loads(text)
            return 'json'
        except Exception:
            return None
    if stripped[:1] == '<':
        try:
            import xml.dom.minidom
            xml.dom.minidom.parseString(text)
        except Exception:
            return None
        return 'plist' if '<plist' in text[:512] else 'xml'
    return None


def _protobuf_field_count(raw: bytes, *, min_fields: int = 2) -> int | None:
    """Field count if *raw* parses cleanly as a schema-less protobuf
    message end-to-end (walking `(tag varint, wire-type-appropriate
    payload)*` with ZERO leftover bytes), else None — the same
    structural technique `protoc --decode_raw`/PBTK use for schema-less
    protobuf inspection (no `.proto` needed, since a valid protobuf
    WIRE FORMAT is self-describing enough to walk without one). Added
    2026-09-19, prompted by a real gap found reviewing this project's
    own real non-Chrome LevelDB stores (Google Play Services' own
    CryptAuth/SafetyNet/semantic-location/usage-reporting databases,
    Chrome's own Sync Data and Local Storage META: records): every one
    of these stores real protobuf with NO tag-byte convention at all,
    and a short/simple protobuf message's bytes routinely happen to all
    be < 0x80 — trivially "valid UTF-8" — getting mislabeled 'text' by
    this module's own existing UTF-8-decode-success bar, the identical
    failure shape already fixed for IndexedDB elsewhere in this file
    (see `_decode_indexeddb_folder`), just for a real encoding a generic
    per-cell LevelDB view can't reach for with a real decoder the way
    IndexedDB's own on-disk format allows — protobuf has no fixed
    per-app schema to decode against generically, only a checkable WIRE
    SHAPE.

    Real, load-bearing tuning, not an arbitrary default: `min_fields=2`
    is the single biggest lever against false-positiving on genuinely
    random/unrelated binary — validated via a 20,000-trial-per-length
    false-positive sweep against real `random.randrange(256)` noise at
    lengths 1-100 bytes: `min_fields=1` lets real noise false-positive
    up to ~7%, `min_fields=2` cuts that to roughly 1% peak. The real,
    disclosed cost of that safety margin: a genuine but SINGLE-field
    short protobuf value (e.g. a real Chrome `shared_proto_db` value,
    `b'\\n\\x011'`, field 1 = the string "1") stays unclassified as
    protobuf (falls through to the ordinary text/bin fallback below) —
    not wrongly labeled 'text' either way, just not POSITIVELY
    identified as protobuf; a real, accepted trade-off, not silently
    glossed over.

    Wire types 3/4 (deprecated START_GROUP/END_GROUP, removed from
    proto3 entirely) are rejected outright — a real message using them
    is vanishingly rare in practice and accepting them would only widen
    the false-positive surface for no real corresponding gain."""
    pos = 0
    n = len(raw)
    fields = 0
    while pos < n:
        tag = 0
        shift = 0
        while True:
            if pos >= n or shift > 63:
                return None  # truncated or runaway varint
            b = raw[pos]
            pos += 1
            tag |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_no = tag >> 3
        wire_type = tag & 0x7
        if field_no < 1:
            return None
        if wire_type == 0:  # varint
            while True:
                if pos >= n:
                    return None
                b = raw[pos]
                pos += 1
                if not (b & 0x80):
                    break
        elif wire_type == 1:  # 64-bit (fixed64/double)
            if pos + 8 > n:
                return None
            pos += 8
        elif wire_type == 2:  # length-delimited (string/bytes/embedded message)
            length = 0
            shift2 = 0
            while True:
                if pos >= n or shift2 > 63:
                    return None
                b = raw[pos]
                pos += 1
                length |= (b & 0x7f) << shift2
                if not (b & 0x80):
                    break
                shift2 += 7
            if pos + length > n:
                return None
            pos += length
        elif wire_type == 5:  # 32-bit (fixed32/float)
            if pos + 4 > n:
                return None
            pos += 4
        else:
            return None  # wire type 3/4 (deprecated groups) or 6/7 (invalid)
        fields += 1
    return fields if fields >= min_fields else None


def _is_length_delimited_protobuf(raw: bytes) -> bool:
    """True if *raw* is a leading varint BYTE LENGTH followed by exactly
    that many bytes of a valid schema-less protobuf message — Java's
    real `Message.writeDelimitedTo()` convention (structurally different
    from bare protobuf: the outer varint is a LENGTH, not a field tag).
    Confirmed as a real, distinct on-disk convention in this project's
    own real casework: Google Play Services' CryptAuth device-sync data
    (key `DEVICE_METADATA_DeviceSync:BetterTogether@@<account>`) uses
    exactly this shape. Requiring the outer length to match the
    remaining buffer EXACTLY is itself a strong constraint — validated
    via the same false-positive sweep as `_protobuf_field_count`: at
    most 0.035% of random noise trials matched at any tested length,
    the safest of the detectors added this session — so the inner
    message only needs `min_fields=1` here, unlike the bare-protobuf
    case above."""
    if not raw:
        return False
    pos = 0
    n = len(raw)
    length = 0
    shift = 0
    while True:
        if pos >= n or shift > 63:
            return False
        b = raw[pos]
        pos += 1
        length |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    if pos + length != n:
        return False
    return _protobuf_field_count(raw[pos:pos + length], min_fields=1) is not None


def _abx_to_xml_text(raw: bytes) -> str | None:
    """Real, decoded XML text for an ABX-magic value, or None — shared
    by classify_leveldb_value (label only) and
    leveldb_value_preview_bytes (the actual text to render), so a
    record's own Type label and its preview pane can never disagree
    about whether its ABX content actually decoded."""
    try:
        import ccl_abx
        import xml.etree.ElementTree as _ET
        return _ET.tostring(ccl_abx.abx_bytes_to_xml_root(raw), encoding='unicode')
    except Exception:
        return None


def _leveldb_value_candidates(raw: bytes) -> list[tuple[str, bool]]:
    """[(decoded_text, is_definitely_real_text)] candidates for *raw*,
    in priority order — shared by classify_leveldb_value (label only)
    and leveldb_value_preview_bytes (the actual bytes to render), so
    the two can never disagree about which candidate is real content.

    Candidate 1: the plain UTF-8 decode of raw itself — covers an
    untagged value from ANY LevelDB store (most real stores; see
    CLAUDE.md's own 145-distinct-store survey, most of which have no
    tag-byte convention at all). is_definitely_real_text=True here,
    matching this project's existing bar for "is this text" everywhere
    else (header_scan.is_text) — a real file/record's own bytes
    decoding cleanly as UTF-8 is already the accepted signal.

    Candidate 2, ONLY for Chrome's own real DOM Storage type-tag byte
    (0x00=UTF-16LE, 0x01=Latin-1 — the same convention
    artifacts/android/chrome_local_storage.py's own _decode_value
    already handles): the tag-stripped body, decoded with the CORRECT
    tag-specific encoding, never a blind UTF-8 retry.
    is_definitely_real_text=False here — Latin-1 never raises on ANY
    byte 0-255, so successfully decoding it is, on its own, no evidence
    the result is genuinely text rather than an unrelated app's own
    real binary value that merely happens to start with 0x00/0x01 (a
    real risk for a GENERIC viewer covering every app's own LevelDB
    store, not just Chrome's — see this module's own docstring for why
    that distinction matters here). A caller wanting to treat this
    candidate as real text should additionally require either
    _content_shape (validated json/xml/plist) or
    artifact_runner.text_plausible — never accept it on decode success
    alone. Generated even for a bare 1-byte tag with an EMPTY payload
    (`b'\\x00'`/`b'\\x01'` alone — fixed 2026-09-19, found while adding
    the candidate below: the original `len(raw) > 1` guard silently
    skipped this, so a genuinely tagged-empty-string value never
    resolved as 'text' via this candidate at all) — `body` is simply
    b'', which both `bytes.decode('utf-16-le')`/`bytes.decode('latin-1')`
    happily decode to `''`.

    Candidate 3, added 2026-09-19: bare UTF-16LE with NO tag byte at
    all — Chromium's real Session Storage convention, confirmed against
    5 completely unrelated real apps' own real Session Storage data
    (Chrome, Edge, Opera, TikTok, GroupMe — e.g. a real value
    `b'A\\x00c\\x00c\\x00o\\x00u\\x00n\\x00t\\x00s...'` = "Accounts and
    settings"), genuinely DIFFERENT from Local Storage's own tag+text
    scheme this module was originally built around. Gated on: even
    length >= 2 (a UTF-16 code unit is always 2 bytes) AND at least 50%
    of odd-position bytes being 0x00 — 50%, not a stricter threshold,
    because a real UI string containing even one non-ASCII BMP
    character (an em-dash, an accented letter) would push a stricter
    ratio below what genuine content can realistically satisfy; 50%
    combined with the caller's OWN downstream plausibility check on the
    decoded text still keeps random-noise false positives low
    (validated: <=0.7%, mostly 0%, across lengths 2-40 in a dedicated
    false-positive sweep). is_definitely_real_text=False here too, same
    reasoning as candidate 2 — a caller must still apply
    text_plausible/_content_shape before trusting it."""
    candidates: list[tuple[str, bool]] = []
    try:
        candidates.append((raw.decode('utf-8', errors='strict'), True))
    except UnicodeDecodeError:
        pass
    if len(raw) >= 1:
        tag, body = raw[0], raw[1:]
        if tag == 0x00:
            try:
                candidates.append((body.decode('utf-16-le', errors='strict'), False))
            except UnicodeDecodeError:
                pass
        elif tag == 0x01:
            candidates.append((body.decode('latin-1'), False))   # never raises
    # Skipped when raw[0] is a recognized Chrome tag byte (0x00/0x01),
    # even if that tag-specific candidate above failed to decode — a
    # real bug found during this fix's own full-dataset regression
    # check, not theoretical: a real npr.org IndexedDB value
    # (tag=0x00, body = "application/vnd.blink-idb-value-wrapper" plus
    # trailing bytes that make the BODY's own UTF-16LE decode fail)
    # would otherwise ALSO try decoding the FULL raw bytes — tag byte
    # included — as UTF-16LE, misaligning every subsequent byte pair by
    # one byte and producing a plausible-looking but completely garbled
    # decode, which is worse than the honest 'bin' this value should
    # get when its one real, tag-specific interpretation doesn't work
    # out. A value starting with 0x00/0x01 already has its own dedicated
    # interpretation path above; guessing at a second, different
    # (untagged) interpretation of the SAME bytes when that one fails
    # has no real upside and a demonstrated real downside.
    if len(raw) >= 2 and len(raw) % 2 == 0 and raw[0] not in (0x00, 0x01):
        odd_total = len(raw) // 2
        odd_zero = sum(1 for i in range(1, len(raw), 2) if raw[i] == 0)
        if odd_zero / odd_total >= 0.5:
            try:
                candidates.append((raw.decode('utf-16-le', errors='strict'), False))
            except UnicodeDecodeError:
                pass
    return candidates


def classify_leveldb_value(raw: bytes) -> str:
    """Content-type label for one record's own raw value bytes — shown
    as the file browser's own "Type" column for a LevelDB record's
    virtual file (via _header_type_overrides, the exact same mechanism
    an extracted nested-archive entry's own file type already uses — no
    new column/filter machinery needed, the file browser's existing
    Type filter menu already builds itself from whatever distinct
    values are actually present). One of: 'empty', 'bplist' (binary
    plist), 'plist' (XML plist), 'json', 'xml', 'text', 'bin'.

    A REAL bug, found AND FIXED TWICE, 2026-09-19 (direct user report:
    "the json value... a dot at the beginning... prevent[s] them from
    being recognised as text so being shown as hex"):

    Fix #1 — every real Chrome Local/Session Storage value carries a
    genuine single-byte type-tag prefix (0x00=UTF-16LE, 0x01=Latin-1 —
    Chromium's own real DOM Storage convention) BEFORE the actual
    content — a real JSON value's true first byte is genuinely 0x01,
    not '{'.

    Fix #2 — a SECOND, more subtle bug found testing fix #1 against a
    synthetic tag-prefixed value, not caught by inspection alone: the
    first draft tried `raw.decode('utf-8', errors='strict')` on the
    FULL tag-prefixed bytes as a blanket "is this text at all" check —
    and that SUCCEEDS even with the tag byte still attached (a lone
    0x00 or 0x01 byte is itself a valid single-byte UTF-8 codepoint,
    and UTF-16LE-encoded pure-ASCII content is, byte-for-byte, ALSO
    valid UTF-8), so it returned plain 'text' before ever reaching the
    tag-aware JSON/XML check below. Confirmed directly with a synthetic
    tag-prefixed JSON value before trusting the fix. The real fix is
    ORDER: real content-shape detection (json/xml/plist) is tried, on
    every _leveldb_value_candidates() candidate, BEFORE any generic
    "is it text at all" classification.

    Fix #3 (2026-09-19, same day, the SEPARATE preview-pane bug a
    direct follow-up user report surfaced — see
    leveldb_value_preview_bytes for the fix to the actual rendering,
    this fix is narrower: the generic 'text' fallback below used to
    accept ANY tag-stripped candidate on decode success alone — but
    Latin-1 never raises, so that's no real evidence of real text; a
    genuinely binary value from an unrelated (non-Chrome) LevelDB store
    that happens to start with byte 0x00/0x01 could be mislabeled
    'text' instead of 'bin'. Now requires artifact_runner.text_plausible
    (control-character-fraction check, the same one this project's own
    SQL carving-confidence gate already uses) for a tag-stripped-only
    candidate — a plain untagged UTF-8 decode still needs no extra
    check, matching this project's existing "clean UTF-8 decode is
    already good enough evidence" bar elsewhere.

    ABX (Android Binary XML) is decoded and labeled 'xml' — once
    decoded it genuinely IS xml content, no separate 'abx' category
    needed for what's ultimately the same shape a user would filter
    for.

    Fix #4 (2026-09-19, same day, a much broader review beyond Chrome
    Local Storage specifically — direct user request: "this parser is
    meant for all leveldb ... the whole key and value need to be
    decoded correctly"). Surveying real non-Chrome LevelDB stores
    (Google Play Services' own CryptAuth/SafetyNet/semantic-location/
    usage-reporting databases, Chrome's own Sync Data and Local Storage
    META: records) found the SAME "short bytes trivially decode as
    UTF-8" trap fix #2/#3 already closed for Chrome's own tag+text
    convention recurring for real PROTOBUF values these other stores
    use instead, with no tag-byte convention at all — e.g. real GMS
    `shared_proto_db/metadata` values (`b'\\x08\\x00 \\x00'`, 105/105
    real records on this project's own test archive) were ALL
    mislabeled 'text'. Fixed with a new 'protobuf' label — see
    `_protobuf_field_count`/`_is_length_delimited_protobuf`'s own
    docstrings for the schema-less detection technique and its real
    false-positive-rate validation — checked BEFORE the generic
    text/bin fallback below, same "structural detection before a
    generic decode-success guess" ordering principle as the JSON/XML/
    plist content-shape check above.

    The SAME review also tightened the long-standing `is_definite`
    branch below: it used to accept ANY value whose FULL raw bytes
    happen to decode as UTF-8 as 'text' with NO further check at all
    (the one candidate this function ever gave a completely free pass —
    fix #3 above already added a plausibility gate for the
    tag-stripped-only candidate, but never for this one) — confirmed
    this is exactly how real short IndexedDB/protobuf-adjacent binary
    values (`b'\\x05'`, `b'\\x15\\x00\\x00\\x00\\x0f'`) got mislabeled
    'text' too: a short binary blob's own bytes are individually valid
    UTF-8 codepoints by simple coincidence. Now gated on
    `_key_display_plausible` (the SAME no-length-floor control-
    character-ratio check already built for the key-naming fix earlier
    today — reused rather than duplicated, since the same coincidental-
    short-decode risk applies to both a key AND a value) — deliberately
    NOT `artifact_runner.text_plausible`, whose 8-character floor would
    let exactly these short values right back through. The
    tag-stripped-only branch's own EXISTING `text_plausible` check is
    completely UNCHANGED — this fix only closes the one branch that
    previously had no plausibility check at all."""
    if not raw:
        return 'empty'
    if raw[:6] == b'bplist':
        return 'bplist'
    if raw[:4] == b'ABX\x00' and _abx_to_xml_text(raw) is not None:
        return 'xml'

    from artifact_runner import text_plausible

    candidates = _leveldb_value_candidates(raw)
    for text, _ in candidates:
        shape = _content_shape(text)
        if shape:
            return shape
    if _is_length_delimited_protobuf(raw) or _protobuf_field_count(raw) is not None:
        return 'protobuf'
    for text, is_definite in candidates:
        if is_definite:
            if _key_display_plausible(text):
                return 'text'
        elif text_plausible(text):
            return 'text'
    return 'bin'


def leveldb_value_preview_bytes(raw: bytes) -> bytes:
    """The bytes to hand to the generic file-preview renderer
    (FastZipBrowser._display_preview_bytes / _render_as_text) for one
    record's own raw value.

    Real, direct user bug report, 2026-09-19: a real Chrome Local
    Storage JSON value's preview pane showed hex, not formatted JSON,
    because of the same leading DOM Storage type-tag byte
    classify_leveldb_value already accounts for — the Type-column fix
    above never touched the SEPARATE code path
    (_load_leveldb_record_preview) that feeds the preview pane, which
    handed the still-tagged raw bytes straight to the generic renderer
    untouched.

    Returns raw UNCHANGED whenever it already renders correctly through
    the generic pipeline: empty, bplist, ABX, or already-valid-UTF-8 —
    the common case across this project's own 145-distinct-real-store
    survey, which has NO tag-byte convention at all outside Chrome's
    own DOM Storage. Only strips+re-encodes the tag-prefixed body when
    doing so is what makes the value's real content actually
    renderable — never a blind "any 0x00/0x01 prefix must be a tag"
    guess, which could corrupt an unrelated app's own genuinely binary
    value into garbled pseudo-text instead of the honest hex view it
    should get. Per direct user instruction: this view covers many
    different apps' own LevelDB stores, most of which will never have
    this tag byte at all — the fix has to stay scoped to cases it can
    actually justify, not applied blindly everywhere.

    A REAL bug was found and fixed in THIS function's own first draft,
    2026-09-19, testing against the exact real PubMatic JSON value that
    prompted this whole fix — the SAME ordering mistake fix #2 in
    classify_leveldb_value's own docstring already describes, freshly
    reintroduced here: the first draft tried `raw.decode('utf-8',
    errors='strict')` on the FULL tag-prefixed bytes as an early
    "already fine, nothing to fix" return — and that succeeds even with
    the tag byte still attached (b'\\x01{"a":1}' decodes as valid UTF-8
    just fine — the leading 0x01 is a valid single-byte codepoint), so
    it returned the STILL-TAGGED raw bytes completely unchanged,
    unverified against real data before this was caught. Confirmed
    directly: `leveldb_value_preview_bytes` on a real tag-prefixed
    PubMatic Local Storage value returned bytes still starting with
    `\\x01{`, which `json.loads` on the decoded text correctly still
    fails to parse — the exact symptom this function exists to fix,
    unfixed by the first draft. Rewritten to check real JSON/XML/plist
    SHAPE (via _content_shape, on every _leveldb_value_candidates()
    candidate) FIRST, before ever treating "raw already decodes as
    UTF-8" as a reason to return it unchanged — the identical ordering
    principle as classify_leveldb_value, now shared via the same
    _leveldb_value_candidates helper so the two functions can't drift
    apart on this again.

    Fix #4 (2026-09-19, direct follow-up user report: a tag-prefixed
    value with no JSON/XML/plist shape — a plain STRING — still rendered
    with its leading 0x01/0x00 tag byte intact, which made the generic
    preview renderer show hex instead of text). Same root cause as fix
    #2/#3, one level further down this function's own priority chain: a
    real Chrome DOM-Storage tag byte (0x00/0x01) is itself a valid
    single-byte UTF-8 codepoint, so `raw.decode('utf-8', errors='strict')`
    on the FULL still-tagged bytes can succeed even for plain text —
    `b'\\x01Hello'` decodes as valid UTF-8 with the 0x01 control
    character as its own first "character." The old Priority 2 treated
    that success as "already fine, return unchanged" without ever
    checking whether the TAG-STRIPPED candidate was ALSO plausible text
    — so only a tag-prefixed JSON/XML/plist value ever got stripped (via
    Priority 1's shape check), never a tag-prefixed plain string. Fixed
    by moving the tag-stripped candidate's text_plausible check ahead of
    the "raw already decodes as UTF-8" branch: a value that genuinely
    starts with a real tag byte and whose stripped body reads as
    plausible text is now shown stripped, matching the JSON/XML/plist
    case. A value with no tag byte at all has no such candidate to begin
    with, so it's unaffected by this reordering.

    Fix #5 (2026-09-19, same broader non-Chrome-store review that added
    `classify_leveldb_value`'s own Fix #4 — protobuf detection, and its
    `_key_display_plausible` gate on the is_definite candidate): Priority
    3 below used to return `raw` unchanged whenever it merely decoded as
    UTF-8, with NO plausibility check at all — harmless for the PREVIEW
    bytes specifically (a short binary blob like a real protobuf value
    still falls through to the unconditional `return raw` at the very
    end either way, so the actual bytes shown never changed), but kept
    for the same reason `classify_leveldb_value` was tightened: so this
    function's own notion of "is raw plausible text" never silently
    drifts from the Type label's — a record whose Type now correctly
    reads 'protobuf'/'bin' should never have its own preview logic
    quietly still believe Priority 3 accepted it as plain text."""
    if not raw or raw[:6] == b'bplist':
        return raw
    if raw[:4] == b'ABX\x00':
        xml_text = _abx_to_xml_text(raw)
        return xml_text.encode('utf-8') if xml_text is not None else raw

    from artifact_runner import text_plausible

    candidates = _leveldb_value_candidates(raw)

    # Priority 1: real, VALIDATED json/xml/plist shape on any candidate —
    # checked before anything else, so a tag-prefixed JSON value renders
    # as JSON even though the still-tagged raw bytes also "decode as
    # UTF-8" (see this function's own docstring for the real bug this
    # ordering fixes).
    for text, _ in candidates:
        if _content_shape(text):
            return text.encode('utf-8')

    # Priority 2: a genuine tag-stripped candidate whose body is
    # plausible real text — preferred over the full raw bytes even when
    # raw ALSO happens to decode as valid UTF-8 (see this function's own
    # Fix #4 above): a real tag byte is itself a valid single-byte UTF-8
    # codepoint, so "raw decodes as UTF-8" is no evidence on its own that
    # there's no tag byte to strip. text_plausible on the STRIPPED body
    # (never on raw decode success alone) is the actual signal this is
    # genuinely tag-prefixed text, not raw content that coincidentally
    # starts with a control byte with no tag meaning.
    for text, is_definite in candidates:
        if not is_definite and text_plausible(text):
            return text.encode('utf-8')

    # Priority 3: raw itself is already valid UTF-8 AND plausible —
    # shown completely unchanged. _key_display_plausible (no length
    # floor, see Fix #5 above and classify_leveldb_value's own Fix #4)
    # rather than a bare decode-success pass-through.
    for text, is_definite in candidates:
        if is_definite and _key_display_plausible(text):
            return raw

    return raw


def _jsonify(value):
    """Recursively convert a real deserialized IndexedDB value (which can
    genuinely be a Blink dataclass like CryptoKey/IndexedDBExternalObject/
    BlobIndex, an Enum field inside one, raw bytes, or an ordinary
    dict/list/str/int/datetime) into a plain JSON-safe structure BEFORE
    ever handing it to json.dumps — done by hand rather than via
    json.dumps' own `default=` callback, because a real bug was found
    doing it that way first: many of these vendored Blink enums are
    `enum.IntEnum` subclasses, and json's own encoder special-cases any
    `int` subclass (IntEnum passes `isinstance(x, int)`) BEFORE ever
    consulting `default` — so a CryptoKey's real `algorithm_type`/
    `key_usage` fields rendered as bare integers (9, 6) instead of their
    real names (AesGcmTag, kEncryptUsage|kDecryptUsage), confirmed
    directly against a real npr.org CryptoKey record before switching to
    this recursive pre-conversion, which handles an Enum BEFORE json's
    own encoder ever sees it. Anything left over (datetime.datetime, or a
    genuinely unexpected type) is handled by json.dumps' own `default=str`
    as a final, simple fallback.

    Module-level (moved out of _decode_indexeddb_folder 2026-09-19) so
    index_leveldb_folder_for_search's own lightweight search-indexing
    pass can reuse it too, via the shared _iterate_indexeddb_records
    below — one real implementation of "how to render a decoded
    IndexedDB value," not two that could drift apart."""
    import dataclasses as _dataclasses
    import enum as _enum
    if _dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonify(getattr(value, f.name))
                for f in _dataclasses.fields(value)}
    if isinstance(value, _enum.Enum):
        return value.name
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value


def _iterate_indexeddb_records(extract_dir: str, blob_extract_dir: str | None
                                ) -> tuple[list[tuple[str, bytes, str | None]], int]:
    """Opens extract_dir via the vendored ccl_chromium_indexeddb.WrappedIndexDB
    and returns ([(display_name, preview_bytes, content_type), ...], bad_count)
    for every real record found — the one shared core both
    _decode_indexeddb_folder (browsing — builds the virtual per-record file
    browser entries from this) and index_leveldb_folder_for_search
    (search-indexing — builds SQL rows from this, never touching
    folder_map/full_metadata at all) walk a real IndexedDB store through,
    added 2026-09-19 specifically so there's exactly one place that knows
    how to do this rather than two copies that could silently drift apart
    on the next real bug fix. Raises on a genuine open/walk failure —
    callers decide what "give up" means for their own context (fall back
    to the generic per-raw-cell view, for browsing; skip this folder
    entirely, for indexing)."""
    import ccl_chromium_indexeddb
    import json as _json

    wrapped = ccl_chromium_indexeddb.WrappedIndexDB(extract_dir, blob_extract_dir)
    results: list[tuple[str, bytes, str | None]] = []
    bad_count = 0

    def _on_bad_record(key, raw_data):
        nonlocal bad_count
        bad_count += 1

    for db_id in wrapped.database_ids:
        db = wrapped[db_id]
        for store_name in db.object_store_names:
            store = db[store_name]
            for rec in store.iterate_records(bad_deserializer_data_handler=_on_bad_record):
                key_label = _format_idb_key_value(rec.key.value)
                live_suffix = '' if rec.is_live else ' [deleted]'
                display_name = f"{db_id.name} - {store_name} - {key_label}{live_suffix}"
                if isinstance(rec.value, (bytes, bytearray)):
                    # Genuinely still binary — an unresolved/raw value
                    # (e.g. real encrypted content a CryptoKey elsewhere
                    # in this same store was used to produce) that ISN'T
                    # a decode failure on this project's own part, just
                    # honestly reported as bytes rather than fake-decoded
                    # into something it isn't.
                    preview_bytes = bytes(rec.value)
                    content_type = None
                elif rec.value is None:
                    preview_bytes = (
                        b'(record present but its value could not '
                        b'be deserialized)')
                    content_type = None
                else:
                    try:
                        rendered = _json.dumps(
                            _jsonify(rec.value), indent=2, ensure_ascii=False, default=str)
                    except Exception:
                        rendered = repr(rec.value)
                    preview_bytes = rendered.encode('utf-8')
                    content_type = 'indexeddb'
                results.append((display_name, preview_bytes, content_type))
    return results, bad_count


def leveldb_decode_logic_version() -> str:
    """Short content hash of this module's own source — the staleness key
    for the leveldb_search_index table (db_utils.py), same auto-derived
    technique app_intelligence.scan_logic_version() already uses for the
    identical problem (a cached scan silently going stale the moment the
    underlying logic improves, with no signal anything's wrong). Never
    hand-authored, so it can't drift from what's actually on disk — the
    NEXT classify_leveldb_value/decode fix automatically invalidates every
    already-indexed folder's cached text, rather than needing this bumped
    by hand and inevitably forgotten once."""
    import hashlib
    try:
        with open(__file__, 'rb') as f:
            return hashlib.blake2b(f.read(), digest_size=8).hexdigest()
    except OSError:
        return 'unknown'


class LevelDbViewerMixin:

    # ── Decoding ─────────────────────────────────────────────────────────

    def _extract_leveldb_children_flat(self, real_children: list, extract_dir: str) -> None:
        """Extract a flat LevelDB directory's own real children (no real
        subfolders — see _looks_like_leveldb_dir's own docstring) to
        extract_dir. Factored out 2026-09-19 so both the generic
        raw-record decode below AND _decode_indexeddb_folder's own real
        decode share the identical extraction logic rather than
        maintaining two copies."""
        for child_ui_path in real_children:
            if child_ui_path in self.folder_map:
                continue   # a subfolder — a real LevelDB directory is flat
            name = child_ui_path.rsplit('/', 1)[-1]
            dest = os.path.join(extract_dir, name)
            if os.path.exists(dest):
                continue   # already extracted from a prior open/session
            data = self._read_zip_bytes(child_ui_path)
            if data is None:
                continue
            with open(dest, 'wb') as f:
                f.write(data)

    def _extract_tree(self, ui_path: str, extract_dir: str) -> None:
        """Recursively extract ui_path's own real files/subfolders (via
        folder_map) to extract_dir — needed for IndexedDB's own external
        blob directory specifically, which (unlike an ordinary flat
        LevelDB directory _extract_leveldb_children_flat handles) genuinely
        nests: Chromium's own real on-disk layout is
        `<blob-dir>/<db_id>/<blob-number-high-byte-hex>/<blob-number-hex>`
        (confirmed against ccl_chromium_indexeddb.IndexedDb.get_blob's own
        `data_path` construction, and against a real blob file on this
        project's own npr.org IndexedDB store). A no-op if ui_path isn't
        actually a real folder in this archive."""
        children = self.folder_map.get(ui_path)
        if children is None:
            return
        os.makedirs(extract_dir, exist_ok=True)
        for child_ui_path in children:
            name = child_ui_path.rsplit('/', 1)[-1]
            dest = os.path.join(extract_dir, name)
            if child_ui_path in self.folder_map:
                self._extract_tree(child_ui_path, dest)
                continue
            if os.path.exists(dest):
                continue
            data = self._read_zip_bytes(child_ui_path)
            if data is None:
                continue
            with open(dest, 'wb') as f:
                f.write(data)

    def _decode_indexeddb_folder(self, ui_path: str, real_children: list) -> bool:
        """Real, schema-informed decode of a Chromium IndexedDB directory
        via the vendored ccl_chromium_indexeddb.WrappedIndexDB (added
        2026-09-19 — see that module's own docstring for the full "why a
        real decoder, not a better heuristic" reasoning, prompted
        directly by a user report that this project's own schema-less
        classify_leveldb_value guess badly mislabels real IndexedDB
        values). Tried FIRST, from _decode_leveldb_folder below, for any
        ui_path matching Chromium's real `<origin>.indexeddb.leveldb`
        naming convention (_is_indexeddb_leveldb_dir) — returns False on
        ANY failure (an exception opening/walking the store) so the
        caller falls back to the existing generic per-raw-cell view
        instead. Strictly additive: an IndexedDB store this real decoder
        can't handle behaves exactly as it did before this existed.

        One virtual record-file per REAL decoded IndexedDbRecord
        (database -> object store -> record), not per raw LevelDB cell
        the way the generic path works — a single logical IndexedDB
        record can legitimately span several raw LevelDB cells (an
        externally-wrapped blob is a separate real LevelDB entry the
        decoder resolves and merges in automatically), so mapping one raw
        cell to one virtual file would double-count/mis-split what an
        examiner actually wants to see: one row per real stored object.
        A real, CONFIRMED case where this produces genuinely ZERO record
        rows is not treated as a failure needing fallback — this
        project's own real cellebrite.com/adroll IndexedDB store has an
        object store containing ONLY database/object-store bookkeeping,
        no actual stored records at all (every one of its 44 raw LevelDB
        cells is Chromium's own metadata, confirmed by their real
        all-zero-prefixed keys — see CLAUDE.md); reporting that
        confidently (a real, decoded "0 records" answer) is more useful
        to an examiner than silently falling back to a confusing pile of
        44 ambiguous generic metadata pseudo-files.

        Delegates the actual open+walk to module-level
        _iterate_indexeddb_records (refactored out 2026-09-19 so
        index_leveldb_folder_for_search's own search-indexing pass can
        share the identical decode logic rather than a second copy)."""
        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', ui_path)
        extract_dir = os.path.join(self._case_dir, 'leveldb_browse', safe_name)
        os.makedirs(extract_dir, exist_ok=True)
        self._extract_leveldb_children_flat(real_children, extract_dir)

        blob_ui_path = _indexeddb_blob_dir_for(ui_path)
        blob_extract_dir = None
        if blob_ui_path in self.folder_map:
            blob_extract_dir = os.path.join(
                self._case_dir, 'leveldb_browse',
                re.sub(r'[^A-Za-z0-9_.-]', '_', blob_ui_path))
            self._extract_tree(blob_ui_path, blob_extract_dir)

        try:
            with open(os.path.join(extract_dir, _SOURCE_MARKER_NAME), 'w',
                      encoding='utf-8') as f:
                f.write(ui_path)
        except OSError:
            pass

        try:
            records_raw, bad_count = _iterate_indexeddb_records(extract_dir, blob_extract_dir)
        except Exception as exc:
            # Covers both an open failure AND a real decode failure
            # partway through (a genuinely malformed store this vendored
            # decoder can't fully walk) — fall back to the generic
            # per-raw-record view entirely rather than showing a
            # half-populated folder with no way to see the rest.
            self.status_bar.showMessage(
                f"Could not open as IndexedDB, showing generic view instead: {exc}")
            return False

        records: list = []
        virtual_children: list = []
        total_size = 0
        for idx, (display_name, preview_bytes, content_type) in enumerate(records_raw):
            segment = _UNSAFE_SEGMENT_CHARS.sub('_', display_name)[:60].strip('_') or 'record'
            vpath = f"{ui_path}/{idx:06d}_{segment}"
            value_size = len(preview_bytes)
            total_size += value_size
            self.full_metadata[vpath] = {
                'size':                  value_size,
                '_display_name':         (display_name if len(display_name) <= 120
                                           else display_name[:120] + '…'),
                '_leveldb_folder':       ui_path,
                '_leveldb_record_index': idx,
                'mtime':                 None,
            }
            virtual_children.append(vpath)
            records.append(_IdbPreviewRecord(preview_bytes, content_type))
            if content_type:
                self._header_type_overrides[vpath] = content_type

        self.folder_map[ui_path] = virtual_children
        self.full_metadata.setdefault(ui_path, {})['size'] = total_size
        self._nested_virtual_paths = self._nested_virtual_paths | frozenset(virtual_children)
        self._leveldb_folder_map[ui_path] = {
            'extract_dir':    extract_dir,
            'records':        records,
            'real_children':  real_children,
            'record_vpaths':  virtual_children,
            'kind':           'indexeddb',
        }
        message = f"{ui_path}  —  decoded {len(records)} real IndexedDB record(s)"
        if bad_count:
            message += f" ({bad_count} could not be deserialized and were skipped)"
        self.status_bar.showMessage(message)
        return True

    # ── Search indexing (Keyword Search coverage) ───────────────────────────

    def index_leveldb_folder_for_search(self, ui_path: str, real_children: list
                                         ) -> list[tuple[int, str, str, str]]:
        """Lightweight decode pass for Keyword Search coverage ONLY — added
        2026-09-19, directly prompted by confirming (by reading
        keyword_search.py directly) that Keyword Search has no visibility
        at all into decoded LevelDB/IndexedDB content, since it only ever
        scans the real archive's own physical bytes, and a real deserialized
        IndexedDB value (or a Chrome DOM-Storage value re-encoded from
        UTF-16LE for display) is frequently not a literal byte substring of
        the raw file at all.

        Extracts and decodes ui_path's own real records exactly like
        _decode_indexeddb_folder/_decode_leveldb_folder do (same shared
        _iterate_indexeddb_records/classify_leveldb_value/
        leveldb_value_preview_bytes this project already trusts for
        browsing), but returns rows to persist to db_utils'
        leveldb_search_index table instead of touching
        folder_map/full_metadata/_leveldb_folder_map/_header_type_overrides
        at all — measured necessary, not just cautious: a real 234-folder/
        257,804-record archive fully decodes in ~3.4s of CPU time, but
        turning every one of those records into a LIVE virtual file the way
        ordinary browsing does would be a real ~17x bloat of full_metadata
        for a single search. A folder the examiner later actually browses
        into still goes through the ordinary decode path unchanged and
        unaffected by this ever having run — this function's own
        extraction reuses the SAME case_dir/leveldb_browse/ directory
        _decode_leveldb_folder itself uses, so a later browse-in is never
        slowed by this having run first, only sped up (no re-extraction
        needed, same "the extracted file being there simply IS the
        finished-state signal" convention artifact_runner.run_artifact
        already uses elsewhere).

        Returns [(record_index, display_name, searchable_text,
        content_type)] for every record whose real decoded content is
        genuine TEXT — see leveldb_search_index's own CREATE TABLE comment
        (db_utils.py) for exactly which classify_leveldb_value labels get a
        row: 'bin'/'empty' records have no text at all, and 'protobuf'
        deliberately gets none either — it's a structural SHAPE label, not
        a real decode, so there is no genuinely NEW text here beyond what
        the main archive-wide search already covers from the record's own
        still-physically-present raw bytes."""
        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', ui_path)
        extract_dir = os.path.join(self._case_dir, 'leveldb_browse', safe_name)
        os.makedirs(extract_dir, exist_ok=True)
        self._extract_leveldb_children_flat(real_children, extract_dir)

        if _is_indexeddb_leveldb_dir(ui_path):
            blob_ui_path = _indexeddb_blob_dir_for(ui_path)
            blob_extract_dir = None
            if blob_ui_path in self.folder_map:
                blob_extract_dir = os.path.join(
                    self._case_dir, 'leveldb_browse',
                    re.sub(r'[^A-Za-z0-9_.-]', '_', blob_ui_path))
                self._extract_tree(blob_ui_path, blob_extract_dir)
            try:
                records_raw, _bad_count = _iterate_indexeddb_records(extract_dir, blob_extract_dir)
            except Exception:
                records_raw = None
            if records_raw is not None:
                rows = []
                for idx, (display_name, preview_bytes, content_type) in enumerate(records_raw):
                    if content_type != 'indexeddb':
                        continue   # genuinely binary/undeserializable — nothing new to index
                    try:
                        text = preview_bytes.decode('utf-8')
                    except UnicodeDecodeError:
                        continue
                    rows.append((idx, display_name, text, content_type))
                return rows
            # else: real decode failed — fall through to the generic path
            # below, the identical fallback _decode_leveldb_folder itself
            # uses for a store the real decoder can't handle.

        import ccl_leveldb
        try:
            db = ccl_leveldb.RawLevelDb(extract_dir)
        except Exception:
            return []
        try:
            records = list(db.iterate_records_raw())
        finally:
            db.close()

        from artifact_runner import resolve_chromium_session_storage_names, decode_plist_blob
        session_storage_names = resolve_chromium_session_storage_names(records)

        import json as _json
        rows = []
        for idx, rec in enumerate(records):
            content_type = classify_leveldb_value(rec.value)
            if content_type == 'bplist':
                # The generic preview pane decodes this via
                # decode_plist_blob (FastZipBrowser._display_preview_bytes),
                # NOT via leveldb_value_preview_bytes (which deliberately
                # returns bplist bytes UNCHANGED — see that function's own
                # docstring) — mirrored here so a record's real decoded
                # plist content is searchable the same way it's already
                # viewable.
                content = decode_plist_blob(rec.value)
                if content is None:
                    continue
                try:
                    text = _json.dumps(content, indent=2, ensure_ascii=False, default=str)
                except Exception:
                    continue
            elif content_type in ('text', 'json', 'xml', 'plist'):
                preview = leveldb_value_preview_bytes(rec.value or b'')
                try:
                    text = preview.decode('utf-8')
                except UnicodeDecodeError:
                    continue
            else:
                continue   # 'bin'/'empty'/'protobuf' — nothing new to index
            _, display_name = _sanitize_record_name(
                rec.user_key, idx, session_storage_names=session_storage_names)
            rows.append((idx, display_name, text, content_type))
        return rows

    def _decode_leveldb_folder(self, ui_path: str) -> bool:
        """Extract *ui_path*'s own real files to a local scratch dir under
        this case's own artifact-parser-files-style area, open via
        ccl_leveldb.RawLevelDb, and PERMANENTLY replace folder_map[ui_path]
        with one synthetic leaf per real record. Idempotent — a folder
        already in self._leveldb_folder_map returns True immediately
        without redoing any work, so this is safe to call speculatively
        (see _maybe_decode_leveldb_on_navigate) and from the reopen-
        rescan path (see _rescan_decoded_leveldb_folders) without a
        separate "already done?" check at every call site.

        A small, deliberately PARALLEL extraction routine to
        artifact_runner.open_leveldb, not a reuse of it — see that
        function's own CLAUDE.md Conventions entry for the exact
        reasoning (a running parser's own paths dict takes a PHYSICAL
        zip entry name for _read_zip_bytes; this live-GUI path's own
        self._read_zip_bytes takes a UI_PATH and resolves it internally
        instead — a real, already-documented distinction elsewhere in
        this project).

        For a folder matching Chromium's real IndexedDB naming
        convention, _decode_indexeddb_folder (above) is tried FIRST — a
        real, schema-informed decode rather than this function's own
        generic per-raw-cell guess. Everything below this point is the
        ORIGINAL generic path, unchanged, still used for Local
        Storage/Session Storage/every other real LevelDB store, and as
        the fallback for an IndexedDB store the real decoder can't
        handle."""
        if ui_path in self._leveldb_folder_map:
            return True
        if not self._case_dir:
            return False

        real_children = list(self.folder_map.get(ui_path) or [])
        if not _looks_like_leveldb_dir(self.folder_map, ui_path):
            return False

        if _is_indexeddb_leveldb_dir(ui_path):
            if self._decode_indexeddb_folder(ui_path, real_children):
                return True
            # Falls through to the generic decode below on any failure.

        import ccl_leveldb

        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', ui_path)
        extract_dir = os.path.join(self._case_dir, 'leveldb_browse', safe_name)
        os.makedirs(extract_dir, exist_ok=True)
        self._extract_leveldb_children_flat(real_children, extract_dir)
        # Reopen-persistence marker — see this module's own docstring and
        # _SOURCE_MARKER_NAME. Always (re)written, even on an already-
        # extracted folder, so a real ui_path change (a re-run against a
        # different archive sharing this case_dir, an edge case but a
        # real one) can't leave a stale marker behind.
        try:
            with open(os.path.join(extract_dir, _SOURCE_MARKER_NAME), 'w',
                      encoding='utf-8') as f:
                f.write(ui_path)
        except OSError:
            pass

        try:
            db = ccl_leveldb.RawLevelDb(extract_dir)
        except Exception as exc:
            self.status_bar.showMessage(f"Could not open as LevelDB: {exc}")
            return False
        try:
            records = list(db.iterate_records_raw())
        finally:
            db.close()

        # Chromium Session Storage needs a real two-record join to name
        # its own 'map-<id>-<key>' entries (see artifact_runner.
        # resolve_chromium_session_storage_names' own docstring for the
        # full mechanism and its real-data verification) — cheap to
        # always attempt: a folder that isn't Session Storage-shaped
        # simply has no 'namespace-'/'map-' keys at all, so this finds
        # nothing and costs one harmless pass over records already in
        # memory.
        from artifact_runner import resolve_chromium_session_storage_names
        session_storage_names = resolve_chromium_session_storage_names(records)

        virtual_children = []
        total_size = 0
        for idx, rec in enumerate(records):
            segment, display_name = _sanitize_record_name(
                rec.user_key, idx, session_storage_names=session_storage_names)
            vpath = f"{ui_path}/{segment}"
            value_size = len(rec.value or b'')
            total_size += value_size
            self.full_metadata[vpath] = {
                'size':                  value_size,
                '_display_name':         display_name,
                '_leveldb_folder':       ui_path,
                '_leveldb_record_index': idx,
                'mtime':                 None,   # no real timestamp at the LevelDB layer
            }
            virtual_children.append(vpath)
            # Populate the file browser's EXISTING Type column/filter for
            # this record — _header_type_overrides already drives both
            # (self.file_model.distinct_values('Type') builds the filter
            # menu dynamically from whatever's present), so a real record
            # value's content type is filterable with zero new UI. Skip
            # 'bin' — leaving it unset falls back to the ordinary 'Other'
            # label every other undetected file already gets, rather than
            # adding a redundant synonym for the same bucket.
            content_type = classify_leveldb_value(rec.value)
            if content_type != 'bin':
                self._header_type_overrides[vpath] = content_type

        self.folder_map[ui_path] = virtual_children
        self.full_metadata.setdefault(ui_path, {})['size'] = total_size
        self._nested_virtual_paths = self._nested_virtual_paths | frozenset(virtual_children)
        self._leveldb_folder_map[ui_path] = {
            'extract_dir':    extract_dir,
            'records':        records,
            'real_children':  real_children,
            'record_vpaths':  virtual_children,  # parallel to 'records', same order —
                                                  # lets _reapply_leveldb_type_overrides
                                                  # re-populate without re-deriving names
            'kind':           'raw',
        }
        return True

    def _reapply_leveldb_type_overrides(self) -> None:
        """Re-populate self._header_type_overrides for every already-
        decoded LevelDB folder's records — a real gap found and fixed
        2026-09-19: a manual header rescan (ProcessDialog._start_header_scan
        → FastZipBrowser._on_header_types_cleared, ffs-explorer.py)
        unconditionally clears the WHOLE _header_type_overrides dict
        (it has no way to know some of those entries came from a
        decoded LevelDB folder rather than a real file's own magic-byte
        scan), and _decode_leveldb_folder is deliberately idempotent —
        it returns True immediately for a folder already in
        self._leveldb_folder_map, so it never naturally re-runs to
        repopulate these. Left unfixed, every decoded LevelDB record's
        Type column would silently go back to 'Other' after any header
        rescan, with nothing to explain why. _on_header_types_cleared
        calls this right after clearing.

        Cheap even for a large store: every record is already held in
        memory (self._leveldb_folder_map[...]['records']), so this is
        just re-running the same lightweight classify_leveldb_value()
        per record — no re-extraction, no re-opening ccl_leveldb, no
        disk I/O at all.

        An 'indexeddb'-kind entry (_decode_indexeddb_folder, added
        2026-09-19) needs no re-classification at all — its own
        _IdbPreviewRecord.content_type was already computed ONCE, at
        real-decode time, from the actual deserialized value; this just
        re-reads that already-final field rather than re-deriving
        anything from raw bytes (an indexeddb-kind entry's 'records'
        aren't raw ccl_leveldb.Record objects with a raw .value to feed
        classify_leveldb_value at all)."""
        for entry in self._leveldb_folder_map.values():
            if entry.get('kind') == 'indexeddb':
                for vpath, rec in zip(entry.get('record_vpaths', ()), entry['records']):
                    if rec.content_type:
                        self._header_type_overrides[vpath] = rec.content_type
                continue
            for vpath, rec in zip(entry.get('record_vpaths', ()), entry['records']):
                content_type = classify_leveldb_value(rec.value)
                if content_type != 'bin':
                    self._header_type_overrides[vpath] = content_type

    def _maybe_decode_leveldb_on_navigate(self, ui_path: str) -> None:
        """Called from on_folder_selected — the one real interception
        point every navigation path into a folder already funnels
        through (tree click directly; double-click-in-table via
        navigate_tree_to_path, which itself ends by calling
        on_folder_selected). A no-op unless ui_path is a real, still-
        undecoded LevelDB-shaped folder."""
        if not ui_path or ui_path in self._leveldb_folder_map:
            return
        if not self._case_dir:
            return
        if _looks_like_leveldb_dir(self.folder_map, ui_path):
            self._decode_leveldb_folder(ui_path)

    def _rescan_decoded_leveldb_folders(self) -> None:
        """Re-establish every LevelDB folder decoded in an earlier
        session of this same case — called once at case load, right
        after _inject_nested_archives (the same real spot that already
        restores extracted-nested-archive state). Reads each local
        extraction's own _SOURCE_MARKER_NAME back to recover which real
        archive ui_path it came from (never guessed by reversing the
        sanitized folder name — lossy, not reliable), then re-runs
        _decode_leveldb_folder, which — since the real files already
        exist on disk — does no re-extraction at all, just reopens them
        via ccl_leveldb, cheap even for a large store."""
        if not self._case_dir:
            return
        base = os.path.join(self._case_dir, 'leveldb_browse')
        if not os.path.isdir(base):
            return
        try:
            entries = os.listdir(base)
        except OSError:
            return
        for name in entries:
            extract_dir = os.path.join(base, name)
            marker = os.path.join(extract_dir, _SOURCE_MARKER_NAME)
            if not os.path.isfile(marker):
                continue
            try:
                with open(marker, 'r', encoding='utf-8') as f:
                    ui_path = f.read().strip()
            except OSError:
                continue
            # The source folder must still exist, as a real folder, in
            # THIS archive's own metadata — a stale extraction left over
            # from a different case/archive sharing this case_dir (or a
            # genuinely removed path) is skipped rather than guessed at.
            if ui_path and ui_path in self.folder_map:
                self._decode_leveldb_folder(ui_path)

    # ── Raw-files peek ──────────────────────────────────────────────────

    def _peek_leveldb_raw_files(self, ui_path: str) -> None:
        """Show ui_path's own REAL physical files (CURRENT/MANIFEST-*/
        *.ldb/*.log) in the file table for one view — confirmed directly
        with the user this is a ONE-OFF PEEK, not a persistent toggle:
        folder_map[ui_path] is swapped back to the decoded record list
        immediately after this one render completes (synchronous, single-
        threaded Qt — nothing else can observe the swapped-out state in
        between), so navigating away and back shows decoded records
        again, same as before this was ever called."""
        if ui_path in self._leveldb_folder_map:
            real_children = self._leveldb_folder_map[ui_path]['real_children']
        elif _looks_like_leveldb_dir(self.folder_map, ui_path):
            real_children = list(self.folder_map.get(ui_path) or [])
        else:
            return

        saved = self.folder_map.get(ui_path)
        self.folder_map[ui_path] = real_children
        self._view_path = ui_path
        self._view_is_recursive = False
        try:
            self._refresh_folder_view()
        finally:
            if saved is not None:
                self.folder_map[ui_path] = saved
        self.status_bar.showMessage(
            f"{ui_path}  —  showing raw LevelDB files "
            f"(navigate away and back to see decoded records again)")

    # ── Record preview ───────────────────────────────────────────────────

    def _load_leveldb_record_preview(self, ui_path: str) -> None:
        """Preview one decoded record's own VALUE bytes — reusing
        FastZipBrowser._display_preview_bytes directly (self, since this
        mixin is composed into that same class), the exact same JSON/
        XML/bplist/ABX/plain-text/hex rendering a real file already
        gets. This is the whole reason this feature exists: a record
        whose value happens to be a binary plist decodes correctly here
        with zero LevelDB-specific special-casing — decode_plist_blob
        neither knows nor cares that these bytes came from a LevelDB
        record rather than a file on disk.

        The raw value is passed through leveldb_value_preview_bytes
        first (added 2026-09-19, a direct user bug report: a real
        tag-prefixed JSON value rendered as hex here even after
        classify_leveldb_value already correctly labeled it 'json' in
        the Type column — that fix only ever touched the LABEL, never
        what these bytes actually were; see that function's own
        docstring for why the fix stays narrowly scoped rather than
        stripping any 0x00/0x01-prefixed value blindly).

        An 'indexeddb'-kind entry (_decode_indexeddb_folder, added
        2026-09-19) skips all of the above — its own _IdbPreviewRecord
        already carries FINAL preview bytes computed once from the real
        deserialized value (pretty-printed JSON-ish text for genuinely
        decoded content, or the honest raw bytes for something still
        genuinely binary — e.g. real encrypted content), so there's no
        raw-bytes heuristic left to run."""
        meta = self.full_metadata.get(ui_path, {})
        ldb_path = meta.get('_leveldb_folder')
        idx = meta.get('_leveldb_record_index')
        entry = self._leveldb_folder_map.get(ldb_path) if ldb_path else None
        if not entry or idx is None or idx >= len(entry['records']):
            return
        record = entry['records'][idx]
        name = meta.get('_display_name') or ui_path.rsplit('/', 1)[-1]
        if entry.get('kind') == 'indexeddb':
            self._display_preview_bytes(ui_path, record.preview_bytes, name)
            return
        data = leveldb_value_preview_bytes(record.value or b'')
        self._display_preview_bytes(ui_path, data, name)
