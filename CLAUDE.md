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
2. `ZipMetadataWorker` (ffs-explorer.py:1132) → `app/ffs_metadata.py
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
| `hex_viewer.py` | Hex tab (`HexViewerMixin`, `HexLoadWorker`) |
| `media_viewer.py` | Thumbnail grid, ffmpeg video frames (`MediaViewerMixin`) |
| `sqlite_viewer.py` | Database tab: temp-copy extraction, table browser, **WAL net-change diff view** (`SqliteDiffModel`) |
| `segb_viewer.py` | SEGB/Biome tab: parses records via vendored `app/ccl_segb`, decodes protobuf with `blackboxprotobuf`; empty-record hiding + deleted-record toggle |
| `segb_schemas.py` | Built-in per-stream protobuf typedefs + field labels for known Biome streams; user-authored schemas persist to `caseresults.db` via `db_utils.save_segb_schema` and override the built-ins |
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | Plugin system: parser scripts in `artifacts/ios|android/` (e.g. `photos_metadata.py`, `sms_messages.py`, per-platform `whatsapp.py`) run against the archive, results into `casedata.db`, browsed in Artifacts tab. Third-party iOS apps declare `app_group` instead of `app_path` — their container is GUID-named per install, resolved via the case's `guid_to_bundle` map at run time (`artifact_runner._resolve_app_group_base`); see `artifacts/ios/whatsapp.py`. The `paths` dict `run()` receives also carries a reserved `_app_base_ui_path` key (the container's own ui_path) for a parser that needs to *reference* another file inside the container — e.g. an attachment path stored in a DB column — without extracting it itself; see `media_fields` below |
| `sqlite_carve.py` | Below-SQL-layer deleted-record recovery: freeblocks, freed/freelist pages, and full WAL frame history (not just the current valid chain `sqlite3.connect()` would replay) — decodes SQLite's on-disk record format directly, since a `DELETE`d row's bytes usually survive until something else reuses that space. Invoked automatically by `artifact_runner.py` for any table a parser names in `recoverable_tables`; no recovery code belongs in a parser script itself |
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
    `_start_art_media_thumbnails`. Double-clicking a media cell resolves
    the row's ui_path through the app's normal `_read_zip_bytes` and opens
    `artifact_media.MediaFullViewDialog` (full-size image, or video with
    play/pause + a seek slider via `QtMultimedia`). A row with no
    resolvable attachment (common — not every message type has media, and
    a `recoverable_tables` carved row never has one, since carving only
    reconstructs the parser's own primary table, not a join to a media
    table) just shows no thumbnail; this is expected, not an error — so is
    a *constructed* path that doesn't resolve to a real archive entry (the
    DB can reference an asset whose original was never cached locally,
    e.g. an iCloud Shared Album photo; confirmed on real data at ~98%
    resolvable, not 100%, and that's correct, not a bug to chase).
    Thumbnail/full-view decoding classifies by extension first, falling
    back to magic-byte sniffing (`media_viewer.sniff_media_kind`) for a
    generic filename that carries no real extension — needed for Google
    Messages' MMS cache files, always named `..._part_N_.bin` regardless
    of whether the real content is jpeg/png/mp4.
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
