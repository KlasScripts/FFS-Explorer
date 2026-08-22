# FFS Explorer — codebase map

Desktop forensics tool (PySide6) for browsing iOS/Android Full File System
extractions (Cellebrite / GrayKey zips) without extracting them. Single-window
app; one "case folder" per exhibit holds local caches and results.

**Always run with the project venv:** `venv/bin/python ffs-explorer.py`
(system Python lacks PySide6). Line counts ~23.5k total; `ffs-explorer.py`
alone is ~8.2k — use the section map below instead of reading it whole.

## Architecture in one paragraph

`ffs-explorer.py` builds the main window (`FastZipBrowser`) and owns archive
loading, the file tree/table, filtering, bookmarks, recents, and worker
lifecycle. All heavy or specialised features live in `app/` as **mixins**
(`HexViewerMixin`, `MediaViewerMixin`, `KeywordSearchMixin`,
`ArtifactViewerMixin`, `SqliteViewerMixin`, `SegbViewerMixin`) that
`FastZipBrowser` inherits — so "the hex tab" or "the SEGB tab" means the mixin
file, not the main file. Format *detection and path mapping* (Cellebrite vs GrayKey,
iOS vs Android) live in `app/adapters/`; callers still branch on
`ffs_adapter.format` where behaviour differs (archive classification ~line
842, metadata passes ~1287–1351, integrity sidecar ~2242) — that's expected,
but raw format sniffing belongs only in the adapters. Long work runs in `QThread` workers;
first-open metadata parsing runs in a separate *process* (`ffs_metadata.py`).

## Data flow (opening an archive)

1. `FastZipBrowser.start_loading()` → case dir chosen (`_get_or_ask_case_dir`).
2. `ZipMetadataWorker` (ffs-explorer.py:1142) → `app/ffs_metadata.py
   parse_archive_metadata()` in a child process: central-directory parse,
   `ui_metadata` build, folder tree/sizes; snapshot persisted to case dir
   (msgpack) so re-opens are instant.
3. `FfsAdapter` (`app/adapters/ffs.py`) detects format and maps `ui_path`
   (what the UI shows) ⇄ physical zip path; GrayKey specifics in
   `adapters/graykey.py`; GUID→bundle-id map for `/private/var/mobile/…`.
4. Zip reading: via `zipfile` + `zip_cd_cache.py` (.zcd sidecar so network
   archives open fast). A zip that fails to open (`zipfile.BadZipFile`) is
   treated as corrupted/truncated and surfaces as an error — there is no
   fallback path. Every viewer receives an `app/zip_entry.ZipEntry` and
   never cares whether the entry is stored (direct seek) or deflated
   (zipfile decompress).

## Case folder databases (`app/db_utils.py`)

- `casecache.db` — rebuildable cache: thumbnails, blobs (folder sizes, search
  entry index), header_types, guid_bundle, nested-archive index. On schema
  mismatch it is auto-deleted and rebuilt.
- `caseresults.db` — precious results (never auto-deleted): search_index /
  search_results, bookmarks, device_info, run_log, user SEGB schemas
  (`segb_schemas`), per-case settings (`case_settings`, e.g. AI backend
  choice), artifact parser output (`artifact_<name>` tables). On
  schema mismatch raises `OldSchemaError`.
- (Heads-up: `zip_cd_cache.py`'s docstring still says "casedata.db" — stale
  name, the real file is `caseresults.db`.)

## app/ modules

| File | What it is |
|---|---|
| `adapters/ffs.py` | `FfsAdapter` — single place for Cellebrite/GrayKey/iOS/Android differences; path resolution, plist candidate paths, prefix detection |
| `adapters/graykey.py` | GrayKey zip metadata (timestamps/xattrs from extra fields), based on gkls |
| `ffs_metadata.py` | Qt-free first-open parsing (runs in child process); msgpack snapshot pack/unpack |
| `db_utils.py` | Case DB open/schema/save-load helpers (see above) |
| `zip_entry.py` / `zip_reader.py` | Entry read primitives; `ZipEntry` is the universal handle passed to viewers |
| `zip_cd_cache.py` | .zcd central-directory sidecar cache + integrity hashes for network zips |
| `header_scan.py` | Magic-byte/text file-type detection by direct offset reads |
| `dialog_helpers.py` | Shared Qt dialog-construction helpers (2026-08-19, after a survey found 25+ hand-rebuilt Cancel/OK button rows, 24+ wordWrap note labels, and four different ad-hoc warning/error colors): `button_row()` (Cancel/OK, `on_ok`/`on_cancel` default to accept/reject — only fits a plain two-button row in that fixed order, a dialog with a third button or different order keeps its own hand-built row), `note_label()`, `error_label()`, and `WARNING_COLOR`/`ERROR_COLOR` reusing `research_store.py`'s existing `#b8860b`/`#c62828` rather than inventing new ones. No case/business logic — pure widget construction |
| `timestamp_display.py` | `TimestampDisplayMixin` (extracted from `ffs-explorer.py` 2026-08-19, same treatment as the other mixins below): the shared timestamp-mode banner, `format_ts` (the single entry point every view calls to display an evidence timestamp per the case's UTC/handset/acquisition/manual setting), and the Timestamp Display dialog. Module-level `_format_ts_cached`/`_format_ts_named_zone` do the actual formatting. Tool-provenance formatting (`_format_tool_ts_local`) is a different concern and stays in `ffs-explorer.py` |
| `device_timezone.py` | Best-effort timezone detection for the opt-in device-local timestamp display: `detect_handset_zone` (iOS `private/var/db/timezone/localtime`), `detect_acquisition_offset`/`guess_acquisition_zones` (the `.ufd`'s recorded UTC offset, Cellebrite-only), `detect_system_zone` (the analysis machine's own current zone — macOS/Linux via `/etc/localtime`, Windows via the registry + a bundled CLDR name mapping since Windows has no IANA-named equivalent). All best-effort, never raise, never applied silently — see the Conventions timestamp section |
| `keyword_search.py` | Search workers (live + nested archives), saved-search DB loaders, dialogs, `KeywordSearchMixin` |
| `hex_viewer.py` | Hex tab (`HexViewerMixin`, `HexLoadWorker`). Also builds the Record/Attachment toggle and the joined-record source combo shown beside "File Preview" — see Conventions — but has no artifact-report knowledge itself, just the checkable buttons, the combo widget, and `_hex_source_is_record()`; `ArtifactViewerMixin` owns what each mode/entry loads |
| `media_viewer.py` | Thumbnail grid, ffmpeg video frames (`MediaViewerMixin`). Selecting a thumbnail (`_on_thumb_clicked`) also loads that file into the shared bottom Hex panel — see "Per-tab state on switching" |
| `sqlite_viewer.py` | Database tab: temp-copy extraction, table browser, **WAL net-change diff view** (`SqliteDiffModel`) |
| `segb_viewer.py` | SEGB/Biome tab: parses records via vendored `app/ccl_segb`, decodes protobuf with `blackboxprotobuf`; empty-record hiding + deleted-record toggle |
| `segb_schemas.py` | Built-in per-stream protobuf typedefs + field labels for known Biome streams; user-authored schemas persist to `caseresults.db` via `db_utils.save_segb_schema` and override the built-ins |
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | Plugin system: parser scripts in `artifacts/ios|android/` (e.g. `photos_metadata.py`, `sms_messages.py`, per-platform `whatsapp.py`) run against the archive, results into `casedata.db`, browsed in Artifacts tab. Third-party iOS apps declare `app_group` instead of `app_path` — their container is GUID-named per install, resolved via the case's `guid_to_bundle` map at run time (`artifact_runner._resolve_app_group_base`); see `artifacts/ios/whatsapp.py`. The `paths` dict `run()` receives also carries a reserved `_app_base_ui_path` key (the container's own ui_path) for a parser that needs to *reference* another file inside the container — e.g. an attachment path stored in a DB column — without extracting it itself; see `media_fields` below |
| `sqlite_carve.py` | Below-SQL-layer deleted-record recovery: freeblocks, freed/freelist pages, and full WAL frame history (not just the current valid chain `sqlite3.connect()` would replay) — decodes SQLite's on-disk record format directly, since a `DELETE`d row's bytes usually survive until something else reuses that space. Invoked automatically by `artifact_runner.py` for any table a parser names in `recoverable_tables`; no recovery code belongs in a parser script itself. Also holds `locate_live_row` — the opposite case, finding a currently-LIVE row's on-disk cell by rowid for the Artifact Viewer's "Record" hex mode (see `record_source` in Conventions) |
| `artifact_media.py` | `MediaThumbnailDelegate` (per-column QTableView delegate painting a thumbnail instead of raw path text) and `MediaFullViewDialog` (full-size image / video playback with transport controls, opened on double-click) for Report table `media_fields` columns — see Conventions below. Reuses `media_viewer.ThumbnailWorker` for decoding, so results share the Media tab's own on-disk thumbnail cache |
| `research_store.py` | Global (cross-case) artifact research notes in `config/research_status.json`, keyed by stream/bundle identity, drives row colouring |
| `validation_store.py` / `parser_validation.py` | Parser validation baselines: a one-time snapshot of a parser's SQLite schema + a *generalized* folder-structure fingerprint (see Conventions below), recorded against the specific GTD-documented image a parser was built/checked against. `validation_store.py` is the cross-case JSON store (`config/parser_validation.json`, same dev/frozen-path convention as `research_store.py`), keyed `"{platform}:{script_name}"` since e.g. `ios:whatsapp`/`android:whatsapp` are different apps sharing a filename. `parser_validation.py` has the actual snapshot/diff/render logic; `ArtifactViewerMixin._art_show_validation` (`artifact_viewer.py`) is the "Validation" tree leaf per parser — diffs the current case against the recorded baseline, or offers to record one (an explicit action, never automatic) |
| `mcp_server.py` | Read-only MCP server (tools + prompts) over processed case data; Qt-free; audit-logs every tool call to `run_log` (run_type `mcp`). Tier 3 (opt-in, separate consent checkbox): `get_sqlite_schema`/`sample_sqlite_rows` extract any archive SQLite db to a locked-down read-only temp copy — no arbitrary raw SQL, no generic file-read tool. `build_artifact_parser(bundle_id)` prompt chains them into a drafted `artifacts/ios\|android/`-format parser for human review |
| `mcp_control.py` | Lifecycle for the embedded MCP server: uvicorn on a daemon thread, 127.0.0.1 + per-start bearer token; lazy-imports mcp/uvicorn (optional deps) |
| `highlight_delegate.py` | Yellow highlight of active search term in views |

## ffs-explorer.py section map (no line numbers — see why below)

Line numbers are dropped deliberately, not an oversight: this project can
go days between commits, and the only thing that ever re-checked numeric
drift was a pre-commit hook — no commits, no check, and a wrong number is
worse than no number (it actively misleads instead of just being absent).
Symbol names don't have that failure mode: they only go stale on a rename
(rare, deliberate, and `grep` catches it instantly), never on routine
insertions/deletions elsewhere in the file the way a line number does.

**To find where something actually is right now:**
```
grep -n "def the_symbol_name" ffs-explorer.py          # one symbol
grep -n "^class \|^def \|^    def " ffs-explorer.py     # whole file's structure
```
The second command is a complete, always-current index of every class and
top-level/method definition in the file — regenerate it instead of trusting
a stored copy of it.

File order, top to bottom:

- Module-level helpers: prefs load/save (`_load_prefs`/`_save_prefs`),
  archive-entry formatting, device-info readers (UFD/plist/build.prop),
  photo-flag rules + `_build_photo_index`, Windows-safe export path
  sanitizers (`_sanitize_export_rel`, `_fs_path`)
- `ExtractorWorker` — archive discovery/classification
- `ZipMetadataWorker` — first-open orchestration; hands off to
  `app/ffs_metadata.py` in a subprocess, snapshot fast-path for re-opens
- `FileTableModel` + its filter proxy
- `ExportProgressDialog`
- Settings/preferences dialogs, small scan workers, integrity check
- `ProcessDialog` — extraction/processing hub, artifact parser runs
- `ArchiveSelectionDialog` — open/recent UI
- `FastZipBrowser` — the main window (everything below is a method on it):
  - `__init__` + column config (incl. Tools menu / AI access)
  - filtering (text/date/type)
  - `_load_file_preview` — routes a selection to the right mixin tab
    (hex/text/sqlite/segb), then tree/table selection, entry rows, context
    menus, export
  - `_detect_android_user_data`, jump menu (`_show_jump_menu`/
    `_build_jump_menu`)
  - `start_loading` → `on_metadata_ready` → `_start_case_meta_load` (async
    DB extras: header overrides, photo index, timezone detection —
    `_load_or_detect_timezone_settings`/`_timestamp_display_dialog`/
    `format_ts` live in `app/timestamp_display.py`'s `TimestampDisplayMixin`
    now, not here); AI-access consent/toggle (`_toggle_mcp_server`,
    `_copy_mcp_config`); nested archives
  - `_refresh_folder_view`, `_populate_tree_children_batched`, bookmarks
  - `_research_status_dialog` (uses `research_store`)
  - Lazy tree expansion, recents/`config/ffs_archives.json`,
    `_retire_worker`/`_stop_all_workers`, `closeEvent`
- Module level again: Qt message handler (`_qt_message_handler`),
  `__main__` (incl. stall-detector timer)

## Config & resources

- `config/ffs_archives.json` — recent archives + device labels;
  `hardware_models.json` — hw id → model name; `photo_flags.json` — seeded
  photo classification rules; `research_status.json` — research store.
- `photo_flags.json` and `research_status.json` live in `config/` **in dev
  only** — in a frozen exe they sit next to the executable (user-editable,
  survive updates). See `_photo_flags_path()` / `research_store.store_path()`.
- `artifacts/` — drop-in parser scripts (two APIs, see `artifact_runner.py`
  docstring). `resources/` — icons + `make_icon.py`.

## Build / CI gotchas

- Windows exe: CI is **`.github/workflows/build-windows-exe.yml`**, which runs
  `pyinstaller ffs_explorer.spec` — that spec is the one that matters (past
  bugs came from applying bundling fixes to a spec CI didn't use).
- The spec must bundle `app/ccl_segb` (vendored) and config seeds.
- `requirements.txt`: msgpack, PySide6, blackboxprotobuf (+ pyobjc on macOS);
  mcp + uvicorn are optional at runtime (lazy-imported by the AI-access
  feature) but listed so frozen builds include them.

## Keeping this map current (instruction to Claude)

After completing any change, check whether it invalidated a claim in this
file, and if so update the relevant line(s) in the same session. Triggers:
- a file/module added, removed, or renamed (update the module table)
- responsibility moved between files, or a new mixin/tab added
- case DB tables/schema versions changed (update the databases section)
- new config file, or changed frozen-exe file locations
- a function named in the `ffs-explorer.py` section map gets renamed,
  removed, or moves to a different thematic group (the map has no line
  numbers to drift, but the prose can still describe something that no
  longer exists)

Routine bugfixes inside an existing method need **no** map update — do not
add method-level detail here; that belongs in docstrings.

**Automated backstop:** `scripts/check_claude_md.py` runs as a pre-commit
hook (install via `scripts/install-hooks.sh` after cloning) and auto-corrects
plain line-number drift in single-symbol anchors like `` `Foo` (file.py:NNN) ``
(a handful still exist elsewhere in this file, e.g. the Data flow section) —
and flags (blocks the commit on) anything needing judgment: a symbol that
moved files, or the module table going out of sync. The section map above no
longer carries line numbers specifically so there's nothing left for the hook
to check there — but only a hook that actually runs (i.e. you're
committing) catches any of this at all; go days without a commit and none of
it fires, however much drifts in the meantime. It only catches mechanical
drift either way; still update the map's *prose* yourself when the semantic
triggers above apply (a function renamed, moved between the thematic groups,
or removed).

## Human verification status (instruction to Claude)

`VERIFICATION_STATUS.md` tracks which parts of this AI-written codebase a
human has actually walked through and confirmed — separately from whether
the code merely runs. Most of it starts unverified (🔴); it moves to 🟢
only after the user has done that with Claude, section by section, and
says so.

**Before editing a file or `ffs-explorer.py` section, check whether it's
listed there as 🟢.** If it is, say so before making the change — name the
row and its verified date — so the user can decide whether to re-review
after, rather than silently trusting a verified-and-since-changed section.
No need to say anything for 🔴/🟡 rows. After a real functional change to a
🟢 row, tell the user it should probably drop back to 🟡 until re-checked —
don't silently leave it marked verified against code that's since changed
underneath that verification.

## Conventions

- `ui_path` = display path (adapter-normalised); physical zip name only via
  `FfsAdapter.resolve`. Never mix them.
- Workers: create → connect → `_retire_worker` on replace; `_stop_all_workers`
  on close. Don't block the GUI thread; batch model updates (see
  `_populate_tree_children_batched`).
- Viewers must be read-only towards the archive (evidence integrity).
- **Per-tab state on switching** (added 2026-08-22): the four center tabs
  (File Browser, Media Browser, Keyword Search, Artifact Viewer) should
  resume exactly where the examiner left them on switching back — same
  selection, scroll position, sort/filter, whatever was open — UNLESS the
  underlying data actually changed while they were away, in which case
  the tab should update to reflect that. `FastZipBrowser._on_center_tab_changed`
  is the one place all four tabs' switch-in behavior lives; the reference
  implementation is Media Browser's own pre-existing check: it rebuilds
  its thumbnail grid only when the shared folder tree's current media-file
  list (`_media_context`, the exact ordered tuple of visible media paths)
  differs from what was loaded last time — same folder/filter/sort means
  an untouched grid, re-applying only the pending selection; a genuinely
  different context reloads.

  Each tab's OWN widgets (the file table, the thumbnail grid, the report
  table, the search results tree) need no special handling — nothing on
  their switch-in path ever clears them, so Qt's own hide/show (not
  destroy) of a QTabWidget page preserves that state for free. The bottom
  preview panel (Hex/Text/Database/SEGB — `app/hex_viewer.py`'s
  `_setup_hex_panel`) is the one widget genuinely SHARED across all four
  tabs (File Browser, Media Browser, Keyword Search, Artifact Viewer all
  load into the same physical panel — Media Browser's own thumbnail
  selection loads the selected file's hex too, `MediaViewerMixin._on_thumb_clicked`),
  so it needs its own explicit per-tab memory — switching tabs doesn't
  clear it (whichever tab was active last just leaves its content
  sitting there), but switching INTO a tab actively RE-ASSERTS that tab's
  own last state over whatever's currently showing, since another tab may
  have overwritten it in the meantime. Four symmetric resyncs, each keyed
  off state that already had to exist for the feature itself (no new
  snapshot/restore machinery — cheap to just recompute fresh from the
  archive, same reasoning as everywhere else in this project that prefers
  re-deriving over caching fragile state):
  - **File Browser** (`_resync_file_browser_preview`, called entering
    index 0): reloads `_fb_last_preview_path` — set inside
    `_load_file_preview`/`_load_nested_entry_preview` themselves, so it's
    "the file actually double-clicked into the preview," not
    `_selected_file_path` (which updates on a plain single-click and can
    point at a row that was never actually previewed — using that instead
    would silently load a preview the examiner never asked to see).
  - **Media Browser** (`_resync_media_hex_preview`, called entering
    index 1 only on the "nothing changed" path — see below): reloads
    `_selected_media_path` if it's still a currently-loaded thumbnail.
    The "context changed, full grid reload" path needs no separate call —
    `_on_thumbnails_done` already re-selects the pending file via
    `_on_thumb_clicked`, which itself now loads that file's hex as part of
    ordinary thumbnail-click handling (not just the tab-switch case).
  - **Keyword Search** (calls its own pre-existing `_on_search_row_selected`,
    entering index 2): re-reads whatever's currently selected in the
    results tree and re-jumps the hex view there — needed no new state at
    all, since the method already worked entirely off the live selection.
  - **Artifact Viewer** (calls `_on_art_hex_source_toggled`, entering
    index 3): re-derives from the still-selected report row + the
    Record/Attachment toggle/combo state — see the Hex panel entry below.
  All four CLEAR the panel (`_clear_hex_preview()`) if that tab never had
  anything loaded yet, rather than leaving whatever the previously-active
  tab put there showing under a tab that's never touched it — an unused
  tab should look unused. Media Browser additionally clears on loading a
  folder with no media files at all (`_start_thumbnail_load`'s early
  return) — reachable while already on the tab (changing the folder
  selection), not just on a tab switch. Safe to call unconditionally on
  every switch-in either way, even when nothing changed (idempotent —
  reloads the same content, or re-clears an already-empty panel).

  The Artifact Viewer's TREE/REPORT (as opposed to the shared hex panel)
  was a separate, now-fixed issue: `_on_center_tab_changed` used to call
  `_refresh_artifact_tab()` on EVERY switch into that tab — rebuilding the
  tree and collapsing back to the tree-root placeholder page regardless of
  whether anything had actually changed, discarding whichever report was
  open. Fixed by removing that call entirely: `_refresh_artifact_tab()`
  already runs on both events that actually change the tab's data — case
  load (`ffs-explorer.py`'s archive-open completion handler) and a parser
  run finishing (`parsers_completed`, connected in `ArtifactViewerMixin`)
  — so merely switching tabs no longer needs its own refresh call there
  either. When adding a future tab, or a new widget shared across more
  than one tab the way the hex panel is, follow this same rule: refresh
  a tab's OWN widgets only on an event that actually changed their data,
  never unconditionally on tab-entry — but DO give any genuinely SHARED
  widget an explicit per-tab resync on entry, since Qt's hide/show only
  protects state that belongs to a single tab's own widgets.
- **Timestamps split into two categories, and every one of them must say
  which category it's in — never a bare `"YYYY-MM-DD HH:MM:SS"`.**
  Examiners work across timezones and DST; an unlabeled timestamp gets
  silently misread, in whichever direction is wrong for the reader.
  - **Evidence timestamps** (anything describing what happened *on the
    device* — message times, file mtimes/ctimes pulled from the archive,
    SEGB/Biome record timestamps, photo taken/added dates): always UTC,
    always labeled (`"...UTC"` suffix, or an ISO `+00:00` offset for
    structured export). Never converted to the analysis machine's zone —
    that machine has nothing to do with the evidence. Reference
    implementation: `_format_ts_cached` in `app/timestamp_display.py`.
  - **Opt-in device/acquisition-local evidence display** — UTC stays the
    default and the only mode needing no caveat; this is a per-case,
    user-chosen exception applied everywhere a timestamp is shown: the
    main file browser's Modified/Created columns, and every artifact
    Report table's declared `timestamp_fields` columns (see below). Two
    variants, both via `_format_ts_named_zone` in `app/timestamp_display.py` (one
    formatter, a named IANA `zoneinfo.ZoneInfo` zone either way — DST
    resolved per the specific timestamp's own date, never
    `datetime.now()`):
    - *Handset*: the zone read directly from the device
      (`device_timezone.detect_handset_zone` — iOS only, confirmed against
      a real extraction that `private/var/db/timezone/localtime` holds the
      IANA name as plain text; nothing equivalent found on Android).
    - *Acquisition computer*: the workstation that ran UFED and wrote the
      `.ufd` — **not** whoever is reviewing the case right now in
      ios-ffs-browser, which can be a different person, machine, and
      timezone entirely (verified as a real scenario: reviewing a
      US-Eastern acquisition from a UK-based machine). Its zone is never
      actually recorded anywhere (checked the `.ufd`, a Cellebrite
      case-export `DeviceInfo.txt`, and the `.ufdx` on two real cases —
      every one only ever has a flat UTC-offset-in-hours, no IANA name). So
      this mode guesses: `device_timezone.guess_acquisition_zones` matches
      every zone sharing that offset at the acquisition moment (dozens can
      tie) and the timestamp-display dialog shows the guess in an editable
      dropdown, each entry labeled with its UTC offset alongside the zone
      name — a starting suggestion, never applied silently. The offset
      itself only means anything for timestamps near the acquisition date;
      it is not a claim about the device.
    - *Manually selected*: a third, always-available option — the other two
      need a handset file (iOS only) or a `.ufd` (Cellebrite only), so a
      GrayKey Android extraction has neither and manual selection can be
      the only non-UTC choice at all. Full IANA zone list
      (`zoneinfo.available_timezones()`), starts on an unselected
      placeholder (never auto-picks a real zone) unless the examiner
      already confirmed one in an earlier session of this same case.
      `device_timezone.detect_system_zone()` — reads `/etc/localtime`'s
      symlink target on macOS/Linux, `None` on Windows (no IANA-named
      equivalent there) — surfaces the ANALYSIS MACHINE's own current zone
      as one convenience entry in that list, explicitly labeled as such;
      never pre-selected, and never conflated with the device's or the
      acquisition workstation's zone (same distinction as the acquisition
      note above, restated in that entry's own tooltip text since it's an
      easy mix-up otherwise). Persisted as `manual_timezone_name`.
    - Detection is staged by cost: the cheap part (handset zone; the raw
      acquisition offset + its own timestamp) runs once per case, in the
      background, at first case-load (`_start_case_meta_load`), and is
      persisted (`case_settings`: `handset_timezone_name`,
      `acquisition_offset_hours`, `acquisition_dt`) so it never silently
      changes between sessions. Matching dozens of zones against the offset
      (`guess_acquisition_zones`) only runs when the timestamp-display
      dialog is actually opened — not on every load.
    - The dialog (`_timestamp_display_dialog`) is shown proactively the
      first time a case is ever opened, not left undiscovered behind a
      menu item — and stays reachable afterward via Tools ▸ Timestamp
      Display. Its warning text names the specific risk, not a generic
      reminder: both zones are a snapshot at/near seizure only (this
      project's own ground-truth test images document devices changing
      timezone multiple times over their test period — a detected zone
      does not retroactively apply to older evidence), and the acquisition
      zone additionally isn't read from the device — or the current
      reviewer — at all.
    - **Proactive first-load trigger race (found + fixed 2026-08-22):**
      `_start_case_meta_load()`'s background poll used to fire the
      proactive dialog via `QTimer.singleShot(200, self._timestamp_display_dialog)`
      — a fixed 200ms GUESS that other, entirely unrelated case-load
      pre-render (`reload_tree_entirely()`'s `_populate_tree_children_batched`
      chain, `_refresh_artifact_tab()`, the device-label fetch, folder-
      count precompute) would have settled by then. None of those have
      any actual bearing on this dialog — it never reads tree/artifact
      state — so the 200ms was never a real dependency, just a
      coincidence that happened to hold on a fast dev machine. Reported
      as an apparent hang specifically on a Windows compiled (frozen)
      build: a slower cold start (exe unpacking, antivirus scanning,
      slower disk I/O for the tree's own zip reads) routinely pushed
      those unrelated stages past 200ms, so the modal dialog popped up
      while they were still visibly running underneath it — a nested Qt
      event loop from `.exec()` still processes their queued
      `QTimer.singleShot` callbacks, so the UI kept visibly changing
      *after* a modal had already taken over input, reading as a hang
      right when everything else looked done. A narrower, related race:
      if the archive also has `missing_plist_paths`, `_warn_and_select_missing`'s
      synchronous `QMessageBox.exec()` (called right after
      `_start_case_meta_load()` returns, in `on_metadata_ready`) could
      have the delayed timestamp-dialog timer fire *inside* its own
      nested loop, stacking a second unrelated modal on top without
      warning. Fixed by `FastZipBrowser._show_timestamp_dialog_when_ready`:
      shows the dialog immediately once background detection genuinely
      completes (no guessed delay — nothing else needs to finish first),
      deferring only if `QApplication.activeModalWidget()` is currently
      non-`None` (e.g. that same integrity warning), re-checking every
      50ms until clear rather than stacking blindly — and re-checking
      `_case_meta_seq` (the same staleness guard `_poll()` itself uses) on
      every deferred retry, not just the initial call, since a long wait
      for another modal to clear could in principle span the user opening
      a DIFFERENT case; bails quietly rather than showing a stale case's
      dialog against whatever case is current by the time a modal clears.
      That specific interaction with `_warn_and_select_missing` is fully
      deterministic, not just lower-probability: that warning is shown
      synchronously, in the same call stack as `_start_case_meta_load()`,
      before the event loop regains control — so if it's going to appear
      at all, it's always already open by the time any deferred check can
      run, and is reliably detected. Related, same day:
      `_BG_POOL` (`ffs-explorer.py`, shared I/O-bound job pool) raised from
      2 to 4 workers — at case-load, the zip-reopen (`_zip_open_future`)
      and this same background job (which also gates the dialog) can both
      be in flight within moments of each other, with folder-count
      precompute following shortly after; with only 2 workers a slow (or
      network-hosted) zip-reopen could occupy a slot long enough to queue
      the detection job behind it, delaying the dialog further still.
    - Needs `tzdata` on Windows (`requirements.txt`) — Windows doesn't ship
      IANA zone data the way macOS/Linux do; harmless no-op elsewhere.
    - The active mode is shown in exactly one place: a shared orange
      "Time setting: ..." banner above every tab (File Browser, Media,
      Search, Artifacts), built by
      `FastZipBrowser._refresh_timestamp_mode_indicator` from
      `_timestamp_mode_text`. The OS window title deliberately stays
      plain ("FFS Explorer") — the banner is the only place the mode is
      stated, not duplicated into the title too. Also deliberately not a
      per-tab label or a per-column header suffix (both were tried and
      removed) — one always-visible indicator beats several easy-to-miss
      ones repeated across every view/report.
  - **Artifact Report tables store raw, unformatted timestamp values**
    (Unix epoch seconds/milliseconds, or Cocoa/Mac epoch seconds/
    nanoseconds), never a baked-in `"...UTC"` string — formatted at
    display time by `ArtifactTableModel` per the case's UTC/handset/
    acquisition setting (same mechanism as the file browser, above; the
    active mode isn't repeated per column, just the shared banner). Each
    parser module declares which of its output fields are timestamps via
    a module-level `timestamp_fields: dict[str, str]` (field name → unit
    code: `"s"`, `"ms"`, `"cocoa_s"`, `"cocoa_ns"`), matching this
    project's existing declarative conventions
    (`recoverable_tables`/`recovery_field_notes`/`description`). Must
    cover both the module's own live-row field names AND the *raw*
    column name(s) recovered/carved rows carry instead (a carved row uses
    the source table's own column name verbatim, never the parser's
    renamed field — see `artifacts/ios/sms_messages.py` and
    `artifacts/android/whatsapp.py` for examples with both). Wired in
    `ArtifactViewerMixin._art_show_report`
    (`app/artifact_viewer.py`), which looks up the report's module,
    reads `timestamp_fields`, and calls
    `ArtifactTableModel.set_timestamp_formatting(units, formatter)` right
    after `load_from_db` (which resets it on every load, so a report
    with no declaration never inherits a previous report's formatting).
    `photos_metadata.py`'s `Taken`/`Added` are a deliberate exception —
    they merge into the *file browser's* table via a different mechanism
    (`_build_photo_index`), not a Report table, and still bake in a
    formatted `"...UTC"` string; revisit only if specifically asked.
  - **Artifact Report tables can declare media columns** the same
    declarative way: a module-level `media_fields: list[str]` names output
    fields holding an archive **ui_path** (never a filesystem path, and
    never bytes copied by the parser itself) to an attachment/media file.
    `ArtifactViewerMixin._art_show_report` reads it, calls
    `ArtifactTableModel.set_media_columns(names)`, and installs
    `artifact_media.MediaThumbnailDelegate` on just those columns via
    `setItemDelegateForColumn` (other columns keep the normal
    `HighlightDelegate`) — a thumbnail is decoded asynchronously per
    distinct path via a `media_viewer.ThumbnailWorker` kicked off in
    `_start_art_media_thumbnails`. That on-open decode is also
    *pre-warmed* right when the parser runs: `ArtifactRunnerWorker.run()`
    (`app/artifact_viewer.py`) calls `_precache_media_thumbnails()` after
    each parser's rows are written, which drives the same
    `ThumbnailWorker` synchronously (`.run()`, not `.start()` — safe reuse
    since the runner itself is already on a background `QThread`, and
    `ThumbnailWorker` touches no GUI state) over that parser's own
    `media_fields` paths. Both call sites share the same on-disk cache
    (keyed `ui_path`+`file_size`+`thumb_size` in `casecache.db`), so this
    is purely a latency fix — added 2026-08-20 after opening a report with
    hundreds/thousands of media rows (WhatsApp) was visibly slow to fill
    in thumbnails on first open; a report opened after its parser has
    already run reads a warm cache instead. Best-effort and never allowed
    to fail the parser run itself. Simply selecting a report row — single
    click, or arrow-key navigation, via `_on_art_report_row_selected`
    (`ArtifactViewerMixin`) — resolves the first non-empty `media_fields`
    value on that row through the app's normal `_read_zip_bytes` and loads
    those bytes into the bottom Hex panel via `_load_hex_preview_from_bytes`
    (that panel lives in `outer_splitter`, shared across every center tab
    — see `ffs-explorer.py`'s layout section — so it's already visible
    under the Artifact Viewer tab without switching tabs). Double-clicking
    the cell additionally opens `artifact_media.MediaFullViewDialog`
    (full-size image, or video with play/pause + a seek slider via
    `QtMultimedia`) on top. A row with no resolvable attachment clears the
    Hex panel rather than leaving the previous row's bytes on screen, and
    the panel is likewise cleared (`ArtifactViewerMixin._clear_art_hex`,
    guarded by an `_art_hex_active` flag so it's a no-op when nothing
    artifact-sourced is showing) on any navigation away from that row's
    context: a different tree node (`_on_art_tree_clicked`), a parser
    re-run rebuilding the tree (`_refresh_artifact_tab`), or leaving the
    Artifact Viewer center tab entirely (`FastZipBrowser._on_center_tab_changed`
    in `ffs-explorer.py`) — so stale evidence bytes never linger somewhere
    unrelated to what's currently selected. This is also, separately, why
    a row with no resolvable attachment (common — not every message type has media, and
    a `recoverable_tables` carved row never has one, since carving only
    reconstructs the parser's own primary table, not a join to a media
    table) just shows no thumbnail; this is expected, not an error — so is
    a *constructed* path that doesn't resolve to a real archive entry (the
    DB can reference an asset whose original was never cached locally,
    e.g. an iCloud Shared Album photo; confirmed on real data at ~98%
    resolvable, not 100%, and that's correct, not a bug to chase).
    Thumbnail/full-view decoding classifies by extension first, falling
    back to magic-byte/text sniffing (`media_viewer.sniff_media_kind`) for
    a generic filename that carries no real extension — needed for Google
    Messages' MMS cache files, always named `..._part_N_.bin` regardless
    of whether the real content is jpeg/png/mp4/pdf/vcf. Returns
    `'image'|'video'|'pdf'|'text'|None`; the magic-byte fallback reuses
    `header_scan.classify_magic`/`header_scan.is_text` (added 2026-08-21
    after a real test device turned up PDF/vCard/text attachments) rather
    than a second signature table, so an attachment is typed the same way
    whether it's shown in the main file browser's Type column or here.
    Only `'image'`/`'video'` ever get an actual thumbnail (`ThumbnailWorker`
    skips the rest, same generic gray-box-plus-filename fallback as an
    unresolvable path); `MediaFullViewDialog` renders `'text'` (decoded
    UTF-8, monospace) directly, and shows a plain "no in-app preview" panel
    for `'pdf'` or an unrecognized kind rather than a false "could not
    decode as image" error — deliberately not rendering PDF pages
    in-app (would need a new rendering dependency in a forensic tool's
    chain of custody; revisit only if specifically asked).
    Five parsers use this convention so far, each confirmed end-to-end
    against real archive bytes, not just plausible-looking code:
    `artifacts/ios/whatsapp.py` (`attachment_path` — first user; the
    non-obvious `Message/` path segment `ZWAMEDIAITEM.ZMEDIALOCALPATH`
    needs prepended to the container base that a bare extension-based join
    would miss), `artifacts/ios/photos_metadata.py` (`attachment_path` —
    note this is a SEPARATE field from `asset_path`, which stays in its
    existing short relative form because `ffs-explorer.py`'s
    `_build_photo_index`/`_photo_key()` already use it as a join key;
    renaming/reshaping `asset_path` itself would silently break that),
    `artifacts/ios/sms_messages.py` (`attachment_path`, from
    `attachment.filename`'s `~/Library/SMS/...` form), `artifacts/android/
    whatsapp.py` (`attachment_path`, built from the existing `media_path`
    field plus a hardcoded external-storage base — WhatsApp media lives
    outside its own app-data container, a fixed OS/app convention rather
    than a per-install GUID), and `artifacts/android/google_messages.py`
    (`attachment_path`, from `parts.local_cache_path` — NOT `parts.uri`,
    which is a `content://` reference with no meaning inside an archive).
    Apps checked and deliberately left un-wired, so as not to be
    "rediscovered" as an oversight later: GroupMe/Burner store media at a
    remote CDN/S3 URL with no local copy anywhere in the archive (checked
    by searching for the media id, zero hits); Viber's body sometimes
    holds a `content://` provider URI with no corresponding literal file
    either; LINE's `attachement_local_uri` column exists but is NULL on
    every row of the test data available, so there was nothing real to
    verify a path convention against.
    **When writing or reviewing ANY artifact parser going forward** —
    same standing checklist item as the timestamp grep in the paragraph
    below — check whether the app has attachments/photos/videos and
    whether the underlying column is a real local path (not a remote URL
    or a `content://`/similar runtime-only reference); if it is, declare
    `media_fields` and build the ui_path the same way `_app_base_ui_path`
    (see `artifact_runner.py`) is used for the five parsers above — and
    verify the constructed path actually resolves against real archive
    bytes before calling it done, the same rigor the rest of this
    checklist already requires for every other field. `mcp_server.py`'s
    `build_artifact_parser` prompt includes this step for exactly this
    reason — see its own text before re-deriving the process by hand.
  - **`hidden_fields`** (a module-level list of output-field names,
    added 2026-08-22 alongside `record_source` below): a generic
    `ArtifactTableModel` feature, not specific to record_source, for a
    field a parser needs in its own output row for internal purposes but
    that has no value as report content — currently that's exactly the
    join-target rowids `record_source` needs to look up (see below), but
    any future declarative convention needing its own hidden plumbing
    field could reuse it the same way. `ArtifactViewerMixin._art_show_report`
    reads it and calls `ArtifactTableModel.set_hidden_columns(names)`
    right after `load_from_db()` — the model still stores/queries every
    column (so `row_dict()`, used by record_source lookups, always sees
    hidden fields too), it's only the QAbstractTableModel-facing surface
    (`columnCount`/`data`/`headerData`/`sort`, and by extension every
    QTableView column index — delegates, resize, the filter dropdown)
    that's narrowed to the visible subset via a `_visible_idx` index
    translation layer. A hidden field is still queryable by name for
    filtering via "All Columns" search, just not offered as a specific
    column to filter by or shown in the grid. Verified end-to-end against
    a synthetic multi-column table: column count/headers/cell data/
    media-column mapping/sort all correctly reflect only the visible
    subset while `row_dict()` still returns every column, hidden or not.
  - **The Hex panel's Record/Attachment toggle** (added 2026-08-22): a
    two-button `QButtonGroup` (`HexViewerMixin._setup_hex_panel`) shown
    beside the "File Preview" label, but ONLY while the Artifact Viewer
    tab is active (`FastZipBrowser._on_center_tab_changed` toggles
    `_hex_source_toggle.setVisible`) — on every other tab it's hidden and
    irrelevant. Selecting a report row (single click or arrow-key
    navigation, `ArtifactViewerMixin._on_art_report_row_selected`) loads
    the bottom Hex panel per whichever button is currently checked:
    - **Record** (the default) — jumps to and highlights the row's OWN
      on-disk database cell in its source db file, via the new
      `record_source` module declaration (below) and
      `sqlite_carve.locate_live_row`. This is deliberately a different
      question from "what does this attachment file look like" — it lets
      an examiner see the actual raw bytes a parsed field was decoded
      from, for citation/verification, the same motivation as this
      project's carved-row page/offset citations but for ordinary live
      rows too.
    - **Attachment** — the pre-existing behavior: the row's `media_fields`
      value(s), same as `_on_art_report_double_clicked` uses for the
      exact cell double-clicked (which additionally still opens
      `MediaFullViewDialog`, and force-switches the toggle to Attachment
      first — see its own docstring — so the hex shown always matches
      what the dialog just opened).
    Whichever mode is checked is sticky as the user clicks through
    different rows (nothing resets it), and re-fires immediately for the
    currently-selected row when the toggle itself is clicked
    (`_on_art_hex_source_toggled`) rather than waiting for the next row
    click. A row with nothing resolvable in the active mode clears the
    panel with a SPECIFIC explanatory message
    (`ArtifactViewerMixin._show_art_hex_message`) — e.g. "Record location
    not available for this parser yet", "No attachment on this row",
    "Record not found at its expected location in X.sqlite (may be
    WAL-only, or deleted)" — never silently blank, and never a guess
    dressed up as a citation. The panel is also reset
    (`ArtifactViewerMixin._clear_art_hex`) on navigating to a different
    tree node or a parser re-run — genuinely different content, so stale
    evidence bytes from the PREVIOUS report/node never linger — but
    deliberately NOT merely on leaving the Artifact Viewer tab for another
    center tab: per "Per-tab state on switching" above, the row selection
    (and the toggle/combo mode) survives a tab switch untouched, and
    `_on_center_tab_changed` RE-SYNCS the panel from that still-selected
    row on the way back in (reusing `_on_art_hex_source_toggled`, the same
    path a manual toggle/combo change uses) rather than clearing — so
    switching away to inspect something in the File Browser's own hex
    preview and back correctly restores the Artifact row's hex, even
    though the two features share one physical panel. See `_art_hex_active`,
    the flag `_clear_art_hex`/`_show_art_hex_message` share to track
    whether the panel is currently artifact-owned content. On a
    successful load, the main status bar shows the source file plus (in
    Record mode) the exact offset jumped to — `f"{ui_path}  —  offset:
    {abs_offset:,}"` — same message shape `keyword_search.py`'s
    `_on_search_row_selected` already uses for a search hit's hex jump,
    so the two features read consistently; Attachment mode shows just the
    file (no single offset to cite, since it opens at the top of the
    attachment rather than jumping to a position within a larger file).
    A parser opts into Record mode with a module-level `record_source` —
    a LIST of entries, one per DB row a report's own rows are actually
    built from. A single-table parser declares one entry; a JOINed report
    (most of them) declares one per joined table too, since an examiner
    citing "where did this come from" for a message also wants to see the
    chat/contact/media row it was joined against, not just the message
    row (added 2026-08-22, same day as the toggle itself — the first cut
    only resolved the main table, which undersold a report whose entire
    point is combining several tables' rows into one). Example
    (`artifacts/ios/whatsapp.py`):
    ```python
    record_source = [
        {
            "label":        "Message",
            "file_key":     "chatstorage",       # a files/optional_files key
            "table_field":  "source_table",      # row field naming the source table (varies per row)
            "rowid_fields": ["raw_message_id", "raw_rowid"],  # tried in order; first non-None wins
        },
        {
            "label":        "Chat Session",
            "file_key":     "chatstorage",
            "table":        "ZWACHATSESSION",    # fixed — this join target never varies per row
            "rowid_fields": ["raw_chat_id"],
        },
        # ... Group Member, Media Item — same file, same shape
    ]
    ```
    A single-entry parser may declare a bare dict instead of a
    list-of-one (`ArtifactViewerMixin._art_show_report` normalizes either
    form) — Android WhatsApp uses the list form directly (Message + Chat)
    since it already needed more than one entry. When there's more than
    one entry, a combo box next to the Record/Attachment toggle
    (`HexViewerMixin._setup_hex_panel`'s `_hex_record_source_combo`,
    populated per-report in `_art_show_report`) lets the examiner pick
    which joined table's cell to jump to — hidden entirely for zero or one
    entries, since there's nothing to choose. Like the Record/Attachment
    toggle itself, the selection is sticky across row navigation and
    re-fires immediately when changed (`_on_art_hex_source_toggled`,
    connected to both the toggle's `buttonClicked` and the combo's
    `currentIndexChanged`).

    Per entry: `file_key` is resolved back to a CURRENT archive ui_path at
    click time via `artifact_runner.resolve_module_file_ui_path` (mirrors
    `run_artifact`'s own app_base/ui_path join) — deliberately re-reading
    fresh archive bytes through `_read_zip_bytes` every time rather than
    reusing the cached copy under `artifact_parser_files/`, since that
    copy was opened by the parser's own (non-read-only) `sqlite3.connect()`
    and could have been checkpointed since; the archive entry itself never
    changes. Different entries may name different `file_key`s pointing at
    ENTIRELY different db files (verified with a synthetic two-file
    fixture: two `files`/`optional_files`-style entries with different
    keys correctly resolved to two different ui_paths, both independently
    readable) — needed for an app like Android WhatsApp whose joins span
    two files (`msgstore.db` main + `wa.db` contacts, ATTACHed in the
    parser's own SQL). `table`/`table_field` give the source table two
    ways: a literal `table` string for a join target that never varies
    per row (every entry except the main one, so far), or `table_field`
    naming a row field that DOES vary (the main entry, via the existing
    `source_table` field also used by `recoverable_tables` rows).
    `rowid_fields` name OUTPUT fields the parser already returns per row —
    no parser code changes needed if the value is already there (Android's
    Chat entry reused the existing `chat_id`/`c._id` output field
    unchanged); iOS's Group Member/Media Item entries needed two new
    `SELECT` columns added (`m.ZGROUPMEMBER`, `med.Z_PK`) since neither
    joined table's own rowid was being selected at all before. A rowid
    field kept ONLY to feed `record_source` — never independently useful
    as report content, since the examiner already sees the resolved
    name/path columns it enabled — is named in the module's own
    `hidden_fields` list (below) so it doesn't appear as a Report column;
    a rowid that's *also* independently meaningful (the main entry's own
    id, or a fallback citation already embedded in another visible
    field's string) stays visible, unchanged from before this feature.
    `rowid_fields` can list more than one name because a LIVE row and a
    `recoverable_tables`-CARVED row from the same report use different
    field names for the same concept (`raw_message_id` vs `raw_rowid` on
    the iOS Message entry) — trying both in order means one entry covers
    both row kinds, though in practice a carved row always resolves to
    "not found" today since `locate_live_row` only walks the table's
    CURRENT live b-tree, never freed space (that's `recover_deleted_rows`'
    job) — expected, not a bug.

    `sqlite_carve.locate_live_row(raw_bytes, table, rowid)` does the actual
    lookup: reads `sqlite_master.rootpage` for `table` via one throwaway
    read-only (`mode=ro`) connection against a temp copy of `raw_bytes`
    (same reasoning as `record_column_names`/`rowid_alias_column`
    elsewhere in that module — a real SQLite reader correctly follows
    overflow pages for a long `CREATE TABLE` statement, which the raw
    cell-decoder deliberately doesn't attempt), then walks that table's
    live leaf pages (`walk_table_leaf_pages` + `decode_leaf_page_cells`,
    both pre-existing) for the matching rowid's cell — returning its exact
    page, in-page offset, absolute file offset, and cell byte length (used
    to highlight the whole record, not just its first byte). Verified
    against synthetic multi-table SQLite fixtures modeled on each parser's
    real schema (not just plausible-looking code): every one of iOS
    WhatsApp's four entries and Android WhatsApp's two resolved to a cell
    whose bytes contained that exact row's own text, and a missing
    rowid/table both correctly returned `None` rather than a wrong answer.
    Only two parsers declare `record_source` so far
    (`artifacts/ios/whatsapp.py`, `artifacts/android/whatsapp.py`) — same
    incremental-rollout pattern as `media_fields` above; every other
    parser's rows show the "not available for this parser yet" message in
    Record mode until it's declared for them too. Two known, deliberate
    gaps, both left as explicit follow-ups rather than guessed at:
    - A row that exists only in an un-checkpointed WAL frame (never merged
      into the main db file) won't be found, since `locate_live_row` only
      reads the base file's own b-tree — surfaced as "may be WAL-only, or
      deleted" rather than a wrong offset or a silent miss.
    - Android WhatsApp's `wa_contacts` table (in `wa.db`, joined for
      sender/chat display-name lookups) has NO `record_source` entry: its
      declared primary key is `jid TEXT`, and whether that's actually the
      table's rowid alias (vs. a separate `WITHOUT ROWID`-style key
      `locate_live_row` couldn't address by integer rowid at all) hasn't
      been confirmed against a real extraction's schema. Declaring it
      without that check would risk silently citing the WRONG cell — a
      forensic tool showing a confident but incorrect "this is where it
      came from" is worse than the current honest gap. Same standing rule
      as every other record_source/media_fields entry in this project:
      verify the schema assumption against real data before declaring, not
      after.
  - **Parser validation baselines** (`validation_store.py` /
    `parser_validation.py`, per-parser "Validation" tree leaf) exist so a
    parser's schema/folder-structure assumptions can be checked against
    real casework, not just trusted forever from whatever GTD image it was
    built against. The folder-structure side deliberately does NOT record a
    flat file listing — confirmed against real WhatsApp iOS/Android and
    Google Messages containers that most of the volume is per-device
    content (JID-sharded media directories, hash-named cache files) that
    would show 100% different on every real case even with an unchanged,
    correctly-parsed app version. Instead every path segment/filename is
    generalized (`parser_validation.generalize_segment` — UUIDs, long hex,
    long digit runs, `@`-containing JIDs all collapse to placeholders) and
    grouped into shapes; a shape whose real instances all generalize to
    themselves (nothing collapsed) is shown with its literal filenames
    since that IS the meaningful case — a new file appearing in `databases/`
    or `shared_prefs/` is real signal, unlike a new UUID-named media file.
    `diff_snapshot` compares table/column presence-and-type and filename
    *patterns* within each shape (not raw counts, which vary by design
    between a GTD image and real casework) — recording a baseline is
    always an explicit, human-triggered action against a case the examiner
    knows is GTD-validated, never automatic.
  - **Tool-provenance timestamps** (when the *examiner* did something with
    this app on *this* machine — an artifact parser run, a bookmark, an
    integrity check): local time, always labeled with the explicit UTC
    offset (`"YYYY-MM-DD HH:MM:SS (UTC+HH:MM)"`), via
    `_format_tool_ts_local` in `ffs-explorer.py` (`_local_date` in
    `artifact_viewer.py` for the date-only case). Local is what a user
    expects here — this is not evidence, it's "when did I do X." Storage
    stays UTC in the DB either way (sortable, unambiguous, no migration
    needed for existing rows); only the display layer converts, using
    `datetime.astimezone()` so DST is resolved correctly for the specific
    date being converted, not just the machine's current offset.
  - Never use SQLite's `'localtime'` modifier or Python's bare
    `datetime.fromtimestamp(x)` (no `tz=`) to get evidence-side UTC — both
    convert using the *analysis machine's* OS timezone, and can look
    correct by pure coincidence (e.g. a UTC+0-in-winter analysis machine
    matching UTC for a January timestamp, then silently wrong in summer or
    from a different machine). Two real, previously-shipped bugs of this
    exact shape were found and fixed in `artifacts/android/whatsapp.py`
    and `artifacts/android/google_messages.py`. (A bare `fromtimestamp()`
    interpreted as local-on-purpose is fine for the tool-provenance case
    above — the bug is using it where UTC was actually wanted.)
  - Even when the underlying arithmetic is already timezone-independent
    and correct (raw epoch/Cocoa-timestamp addition is always UTC-valued),
    the string still needs the label — the bug class to guard against is
    display ambiguity, not just miscomputation. Fixed on this basis in
    `artifacts/ios/photos_metadata.py` and `app/segb_viewer.py` (neither
    was actually computing the wrong value, both were displaying a
    correct one unlabeled).
  - Consistency matters more than any one field's stakes: a mix of
    labeled and unlabeled columns in the same view is itself the failure
    mode, since it teaches the examiner nothing is reliably labeled.
  - When adding a new artifact parser or any other timestamp conversion,
    grep the diff for `fromtimestamp(` and `'localtime'` before calling it
    done — neither announces itself as wrong in casual review.
