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
    unchanged from before; (3) — the real fix for a genuine bug found
    2026-09-19 (a raw hex dump of the WHOLE key, however long, made an
    IndexedDB-shaped key's own filename unreadable and looked broken) —
    a short, honest synthetic label instead of a long hex blob for a
    key this function genuinely can't make sense of, since a filename's
    job is to be a clear, non-misleading label, not to carry full
    fidelity (the record's own raw key bytes are still the real
    dict/lookup key everywhere else in this project's own convention,
    nothing is lost, just not crammed into what's shown here).

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
            readable = raw_key.decode('utf-8')
        except UnicodeDecodeError:
            readable = f"record_{index:06d} ({len(raw_key)} bytes, binary key)"
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
    alone."""
    candidates: list[tuple[str, bool]] = []
    try:
        candidates.append((raw.decode('utf-8', errors='strict'), True))
    except UnicodeDecodeError:
        pass
    if len(raw) > 1:
        tag, body = raw[0], raw[1:]
        if tag == 0x00:
            try:
                candidates.append((body.decode('utf-16-le', errors='strict'), False))
            except UnicodeDecodeError:
                pass
        elif tag == 0x01:
            candidates.append((body.decode('latin-1'), False))   # never raises
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
    for."""
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
    for text, is_definite in candidates:
        if is_definite or text_plausible(text):
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
    apart on this again."""
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

    # Priority 2: raw itself is already valid UTF-8 (is_definite=True,
    # always candidates[0] when present) — shown completely unchanged,
    # even if it happens to start with a literal 0x00/0x01 byte with no
    # real tag meaning, rather than guessing it should be stripped.
    for text, is_definite in candidates:
        if is_definite:
            return raw

    # Priority 3: raw itself did NOT decode as UTF-8 at all — the only
    # remaining candidate is the tag-stripped one, used only if
    # text_plausible accepts it (never on decode success alone; see
    # _leveldb_value_candidates' own docstring for why Latin-1's
    # never-raises behavior isn't real evidence of real text on its
    # own).
    for text, is_definite in candidates:
        if text_plausible(text):
            return text.encode('utf-8')

    return raw


class LevelDbViewerMixin:

    # ── Decoding ─────────────────────────────────────────────────────────

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
        this project)."""
        if ui_path in self._leveldb_folder_map:
            return True
        if not self._case_dir:
            return False

        real_children = list(self.folder_map.get(ui_path) or [])
        if not _looks_like_leveldb_dir(self.folder_map, ui_path):
            return False

        import ccl_leveldb

        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', ui_path)
        extract_dir = os.path.join(self._case_dir, 'leveldb_browse', safe_name)
        os.makedirs(extract_dir, exist_ok=True)
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
        disk I/O at all."""
        for entry in self._leveldb_folder_map.values():
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
        stripping any 0x00/0x01-prefixed value blindly)."""
        meta = self.full_metadata.get(ui_path, {})
        ldb_path = meta.get('_leveldb_folder')
        idx = meta.get('_leveldb_record_index')
        entry = self._leveldb_folder_map.get(ldb_path) if ldb_path else None
        if not entry or idx is None or idx >= len(entry['records']):
            return
        record = entry['records'][idx]
        data = leveldb_value_preview_bytes(record.value or b'')
        name = meta.get('_display_name') or ui_path.rsplit('/', 1)[-1]
        self._display_preview_bytes(ui_path, data, name)
