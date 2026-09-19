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


def _sanitize_record_name(raw_key: bytes, index: int) -> tuple[str, str]:
    """(vpath_segment, display_name) for one real record's own key bytes.

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
    nothing is lost, just not crammed into what's shown here). NOT
    attempted here: decoding IndexedDB's own key format specifically —
    real, structurally consistent across ten real origins checked, but
    decoding it MEANINGFULLY needs a real per-record-type decoder (its
    own database/object-store/index metadata records, several different
    IndexedDBKey value types), a genuinely bigger feature than a
    filename tweak, not attempted here."""
    from artifact_runner import parse_chromium_dom_storage_key
    parsed = parse_chromium_dom_storage_key(raw_key) if raw_key else None
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

        virtual_children = []
        total_size = 0
        for idx, rec in enumerate(records):
            segment, display_name = _sanitize_record_name(rec.user_key, idx)
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

        self.folder_map[ui_path] = virtual_children
        self.full_metadata.setdefault(ui_path, {})['size'] = total_size
        self._nested_virtual_paths = self._nested_virtual_paths | frozenset(virtual_children)
        self._leveldb_folder_map[ui_path] = {
            'extract_dir':   extract_dir,
            'records':       records,
            'real_children': real_children,
        }
        return True

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
        record rather than a file on disk."""
        meta = self.full_metadata.get(ui_path, {})
        ldb_path = meta.get('_leveldb_folder')
        idx = meta.get('_leveldb_record_index')
        entry = self._leveldb_folder_map.get(ldb_path) if ldb_path else None
        if not entry or idx is None or idx >= len(entry['records']):
            return
        record = entry['records'][idx]
        data = record.value or b''
        name = meta.get('_display_name') or ui_path.rsplit('/', 1)[-1]
        self._display_preview_bytes(ui_path, data, name)
