# FFS Explorer — codebase map

Desktop forensics tool (PySide6) for browsing iOS/Android Full File System
extractions (Cellebrite / GrayKey zips) **without extracting them**: direct
seeks into the archive, plus a per-case folder for caches and results. It is
built around five things:
1. knowing which FFS format you're looking at;
2. scanning file headers so files are typed correctly even when the
   extension lies;
3. recovering media embedded *inside* other files (SQLite BLOBs, plist
   NSData);
4. double-clicking into native mobile formats (plist, SQLite, SEGB, XML,
   JSON, LevelDB/IndexedDB, ABX, raw protobuf) and just seeing the content;
5. a declarative scripting system for writing per-app parsers fast.

Everything else (bookmarks, Media Browser, keyword search, the tree UI) is
built on these.

**Always run with the project venv:** `venv/bin/python ffs-explorer.py`
(system Python lacks PySide6). `ffs-explorer.py` is ~8k lines; use the
section map below instead of reading it whole.

## Where things are documented — read this first

| File | Holds |
|---|---|
| **`ASSUMPTIONS.md`** | **Every fact about the data and every design decision**, with its evidence: the FFS formats and paths, what the zip reader relies on, scan scope, recovery limits, review-feature decisions, rejected ideas and why, Qt/build pitfalls. |
| `CLAUDE.md` (this file) | Code map and coding rules only. No facts or decision history. |
| `WRITING_ARTIFACT_PARSERS.md` | How to write a parser, with examples. |
| `VERIFICATION_STATUS.md` | Which parts a human has reviewed. |
| `IDEAS.md` | Ideas not yet scheduled, with evidence and sketches. Not decisions. |
| `FFS_processing.md` | Older processing walkthrough; `ASSUMPTIONS.md` §17 lists its known errors. |

**`ASSUMPTIONS.md` is read-only.**
- Read the relevant entries before changing code they name.
- Never edit it without first agreeing the change with the user: propose
  what to add or correct, and the evidence, in conversation.
- If a change would break or rely on an entry, say so before making it.
- Don't put facts or decision history in this file; they go in
  `ASSUMPTIONS.md`, by agreement.

## Architecture in one paragraph

`ffs-explorer.py` builds the main window (`FastZipBrowser`) and owns archive
loading, the file tree/table, filtering, bookmarks, recents, and worker
lifecycle. Heavy or specialised features live in `app/` as **mixins**
(`HexViewerMixin`, `MediaViewerMixin`, `KeywordSearchMixin`,
`ArtifactViewerMixin`, `SqliteViewerMixin`, `SegbViewerMixin`,
`LevelDbViewerMixin`, `TimestampDisplayMixin`) that `FastZipBrowser` inherits.
"The hex tab" means the mixin file, not the main file. Format *detection and
path mapping* (Cellebrite vs GrayKey, iOS vs Android) lives in
`app/adapters/`; other code branches on `ffs_adapter.format` where behaviour
genuinely differs, but raw format sniffing belongs only in the adapters. Long
work runs in `QThread` workers; first-open metadata parsing runs in a
separate **process** (`ffs_metadata.py`) since it is CPU-bound.

## Data flow (opening an archive)

1. `FastZipBrowser.start_loading()` → case dir chosen/created.
2. `ZipMetadataWorker` → `app/ffs_metadata.py parse_archive_metadata()` in a
   child process: central-directory parse, `ui_metadata` build, folder
   tree/sizes. A snapshot is saved to the case dir (msgpack) so re-opens are
   instant.
3. `FfsAdapter` (`app/adapters/ffs.py`) detects the format and maps `ui_path`
   to the physical zip path, and builds the GUID → bundle-id map for iOS
   containers. The five layouts and their path rules: `ASSUMPTIONS.md` §1–4.
4. Zip reading goes through `zip_cd_cache.py` (a local `.zcd`
   central-directory sidecar, so a network-hosted archive opens fast) plus
   `app/zip_entry.ZipEntry` (direct seek, no decompression). What this
   relies on: `ASSUMPTIONS.md` §5–6.

## Processing and features — where to look

Details and decisions for each are in `ASSUMPTIONS.md` (section in brackets).

- **Header scan** (§8): tiers 0–3 chosen in Process Case. Results go in
  `_header_type_overrides`, which every type-dependent feature reads. Scope
  comes from `scan_rules.py` + `config/scan_rules_{ios,android}.json`.
- **Embedded-media sweep** (§9): `embedded_media_scan.py`, with
  `embedded_media_skip_list.py` and `user_media_ignore_list.py` for noise.
- **Process Case stages** (§8, §10): LevelDB indexing → header scan → nested
  archives → embedded-media sweep → EXIF/video metadata → duplicate
  detection, run in that order by `ProcessDialog`.
- **Duplicates** (§10, A-69–A-71): `duplicate_scan.py` → `duplicate_files` table
  (`casecache.db`) → `_duplicate_files` / `_duplicate_groups`. The File
  Browser shows every copy (Duplicate Count/MD5 columns); the Media
  Browser's "Hide Duplicates" collapses each group via
  `_collapse_duplicate_paths`, also used by the "User Media (N · M to
  review)" count; bookmarking expands back to every copy
  (`_get_media_paths_for_bookmark`).
- **GrayKey acquisition hashes** (A-73): `graykey._parse_entry` reads the
  `S2`/`S1`/`MD` blocks into metadata `sha256`/`sha1`/`md5`;
  `_META_COL_HEADERS` + `self._meta_col_keys` add the "(GrayKey)" and "Capture" columns;
  `duplicate_scan` groups on the recorded SHA-256 (A-69).
- **Paths stored twice** (A-72): `zip_cd_cache._name_repeated_captures`
  renames every earlier copy via `capture_alias` ("sms [capture 1 of 2].db");
  `info.orig_filename` keeps the stored name for offsets; parsers skip
  `is_capture_alias` names.
- **Review features** (§10): Media Browser buckets and presets
  (`file_browser_presets.py`, shared with the File Browser), hover-scrub,
  "(EXIF)" columns, "Linked Report" column.
- **Format viewers** (§11): bplist, XML, SQLite, SEGB, JSON,
  LevelDB/IndexedDB, ABX, protobuf. All read-only toward the archive.
- **Import Bookmarks** (§14): `spreadsheet_import.py` (Qt-free) +
  `ImportBookmarksWorker`/`ImportBookmarksDialog` in `ffs-explorer.py`.

## Artifact parser scripting system

A parser is a script in `artifacts/ios/` or `artifacts/android/` with
`run(ctx, paths) -> list[dict]`. A new parser should be mostly
declarations, not plumbing. **Read `WRITING_ARTIFACT_PARSERS.md` first**; the
rules behind it are in `ASSUMPTIONS.md` §12.

Declarations a parser can set:
- `app_path` / `app_group`: where the app's container is.
- `files` / `optional_files`: files to extract.
- `media_fields`: attachment paths (thumbnails, Attachment hex mode).
- `timestamp_fields`: `{field: unit}`, unit one of `s`, `ms`, `cocoa_s`,
  `cocoa_ns`, `webkit_us`.
- `record_source` (with `source_match`/`presence_fields`): which raw cell(s)
  a row came from, for Record hex mode.
- `recoverable_tables`: tables to carve for deleted rows.
- `hidden_fields`, `core_fields`: column visibility.
- `device_wide = True`: scans the whole archive (given a `CaseContext`).
- `requires_nested_extraction`: embedded zips to extract before `run()`.
- `view_mode = "document"`: render as a markdown page.

Shared helpers in `app/artifact_runner.py` (import directly):
`open_db_readonly` (the only way to open an evidence SQLite file),
`log_note` (a line in the parser run log, e.g. why an optional table was
skipped), `embedded_media_path` (File Browser path of a BLOB image the sweep
extracts, for `link_fields`), `device_path_to_ui_path` / `resolve_mediastore_uri`
/ `archive_has` (Android paths and MediaStore URIs to archive paths), `open_leveldb`, `decode_plist_blob`, `text_plausible`,
`parse_chromium_dom_storage_key` / `resolve_chromium_session_storage_names`,
`decompress_http_body`, `first_nonempty`, `missing_ref_label`,
`resolve_path_after_marker`. Add a helper only when a second real parser
needs it.

## Case folder databases (`app/db_utils.py`)

- **`casecache.db`**: rebuildable cache, deleted and rebuilt on a schema
  mismatch.
- **`caseresults.db`**: examiner work and findings, never deleted
  automatically.

Which table belongs where and why: `ASSUMPTIONS.md` §13.

## app/ modules

| File | What it is |
|---|---|
| `adapters/ffs.py` | `FfsAdapter`: format/platform detection, path resolution, `container_parents()`, `is_android()`, `build_app_registry()`, scan-rule accessors. |
| `adapters/graykey.py` | GrayKey zip metadata (timestamps/xattrs from extra fields); UT/UX parsers also used for `zip_extras`. |
| `csstore.py` | Vendored (MIT) Apple LaunchServices csstore parser, the main source of iOS's `app_registry`. |
| `ffs_metadata.py` | Qt-free first-open parsing (child process); msgpack snapshot pack/unpack; `load_snapshot_from_case`. |
| `db_utils.py` | Case DB open/schema/save-load helpers. |
| `zip_entry.py` / `zip_reader.py` | `ZipEntry` (the archive-read handle every viewer uses), `ZipReader` (raw offset reads, thread pool), `read_nested_entry` (repacked nested archives, cached index). |
| `zip_cd_cache.py` | `.zcd` central-directory sidecar, memoized parse + name index, `CachedZipView`, `compute_data_offsets`. Tests: `tests/test_zip_offsets.py`, `tests/test_zip_readers.py`. |
| `header_scan.py` | Magic-byte/text file-type detection (`classify_magic`). |
| `scan_rules.py` | Shared folder-prefix ruleset behind `FfsAdapter.scan_folders()` and friends. |
| `embedded_media_scan.py` | The embedded-media sweep. |
| `duplicate_scan.py` | Process Case duplicate detection: size+CRC grouping, MD5 to confirm (inode shortcut where trusted). Qt-free. |
| `embedded_media_skip_list.py` | Known-noise database skip list for the sweep. |
| `media_metadata_scan.py` | EXIF/video container metadata extraction (`_LazyZipSlice`, `extract_image_exif`, `extract_video_metadata`). |
| `user_media_ignore_list.py` | Global exclusion list for the "User Media" filter. |
| `keyword_lists.py` | Grooming/drug keyword lists: shipped baseline (with sources) + the examiner's own terms and on/off choices (`keyword_lists_user.json`). Qt-free. |
| `keyword_lists_dialog.py` | Tools ▸ Keyword Lists window. |
| `chat_flags.py` | Red flags: ranks a messaging report's chats by a pattern (categories in one session, request→media, media-heavy, unexplained deletions; rare terms weigh more) with reasons and key messages. Qt-free. |
| `term_matcher.py` | Tolerant keyword matching (number/symbol swaps, stretched/spaced letters, text-speak, typos with a dictionary guard). Qt-free. |
| `config_seed.py` | Ships an examiner-editable config list with every install: seed in `_internal/config`, merged once per entry into the file next to the exe. Qt-free. |
| `file_browser_presets.py` | Shared filter+sort presets (File Browser dropdown, Media Browser buttons). |
| `dialog_helpers.py` | Shared Qt dialog-construction helpers; no business logic. |
| `startup_log.py` | Bounded startup/error log + crash/stall snapshot (`Tools → Open Log Folder`). Qt-free. |
| `timestamp_display.py` | `TimestampDisplayMixin`: timestamp-mode banner, `format_ts`, Timestamp Display dialog. |
| `device_timezone.py` | Best-effort handset/acquisition/system timezone detection. |
| `keyword_search.py` | Search workers (main archive, nested archives, LevelDB index), `KeywordSearchMixin`, `SqlHitInterpretWorker`. |
| `hex_viewer.py` | Hex tab; Record/Attachment toggle and record-source combo. |
| `media_viewer.py` | Media Browser grid (`MediaFileListModel`/`MediaGridDelegate`), PyAV video decode, `MediaFullViewDialog`, `_load_qimage`, seen-tracking. |
| `sqlite_viewer.py` | Database tab. |
| `segb_viewer.py` / `segb_schemas.py` | SEGB/Biome tab and built-in stream schemas. |
| `leveldb_viewer.py` | `LevelDbViewerMixin`: LevelDB/IndexedDB records as virtual files. |
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | Parser plugin system: running, storing, viewing. |
| `chrome_cache.py` / `chrome_shared.py` | Chrome HTTP-cache decode and helpers shared by the `chrome_*` parsers. |
| `ai_summary.py` / `ai_summary_store.py` / `local_llm.py` | Local-LLM report summarization. |
| `nested_archive.py` | Extracts one embedded archive into `case_dir/nested_archives/` (idempotent). |
| `sqlite_carve.py` | Deleted-record recovery; `locate_live_row`/`locate_offset`/`identify_structure`/`build_page_map`. |
| `artifact_media.py` | `MediaThumbnailDelegate`/`WebpageThumbnailRenderer` for report `media_fields` columns. |
| `research_store.py` | Global artifact research notes (row colouring). |
| `parser_versions.py` | Parser version tracking and changelog (update banner). |
| `report_columns_store.py` | Per-report column order (persisted) and visibility (session-only). |
| `app_intelligence.py` | Per-app scoring for `list_apps`; `build_app_registry_lookup`/`flatten_row` for App Report. |
| `ccl_abx.py` | Vendored Android Binary XML decoder (with local fixes). |
| `ccl_leveldb.py` / `ccl_simplesnappy.py` | Vendored LevelDB reader and Snappy decompressor. |
| `ccl_chromium_indexeddb.py`, `ccl_blink_value_deserializer.py`, `ccl_v8_value_deserializer.py`, `ccl_chromium_indexeddb_structures.py` | Vendored IndexedDB decoder (pinned upstream commit). |
| `ccl_chromium_pickle.py` / `ccl_chromium_snss.py` / `chrome_page_state.py` / `chrome_tabs.py` | Chrome tab/session decode; `chrome_tabs.py` is original code. |
| `validation_store.py` / `parser_validation.py` | Per-parser schema/folder baseline snapshots, compared on demand. |
| `mcp_server.py` | Read-only MCP server over processed case data. |
| `mcp_control.py` | Embedded MCP server lifecycle (localhost, per-start bearer token). |
| `highlight_delegate.py` | Highlights the active search term in views. |
| `spreadsheet_import.py` | Qt-free CSV/XLSX reading and path/hash normalization for Import Bookmarks. |

## ffs-explorer.py section map (no line numbers)

Line numbers drift; symbol names only go stale on a rename, which `grep`
catches. To find something:
```
grep -n "def the_symbol_name" ffs-explorer.py          # one symbol
grep -n "^class \|^def \|^    def " ffs-explorer.py     # whole file's structure
```

File order, top to bottom:
- Module-level helpers: prefs load/save, archive-entry formatting,
  device-info readers, photo-flag rules, export path sanitizers.
- `ExtractorWorker`: archive discovery/classification.
- `ZipMetadataWorker`: first-open orchestration.
- `FileTableModel` + its filter proxy.
- `ExportProgressDialog`.
- Settings/preferences dialogs, small scan workers, integrity check.
- `ProcessDialog`: extraction/processing hub, artifact parser runs.
- `ArchiveSelectionDialog`: open/recent UI.
- `FastZipBrowser`: the main window. Everything else is a method on it:
  - `__init__`/column config, filtering;
  - `_load_file_preview`, which routes a selection to the right mixin tab;
  - the tree/table/bookmark/selection system, jump menu;
  - `start_loading` → `on_metadata_ready` → `_start_case_meta_load`;
  - lazy tree expansion, recents, worker lifecycle, `closeEvent`.
- Module level again: Qt message handler, `__main__`.

## Config & resources

- `config/ffs_archives.json`: recent archives and device labels.
- `config/hardware_models.json`, `photo_flags.json`, `research_status.json`,
  `report_columns.json`, `parser_versions.json`, `parser_validation.json`,
  `embedded_media_skip_list_{android,ios}.json`, `user_media_ignore_list.json`,
  `ai_summary_settings.json`, `scan_rules_{ios,android}.json`. All follow one
  convention: in `config/` in dev, next to the executable in a frozen build,
  user-editable, cached by mtime+size.
- `user_media_ignore_list.json` and the presets file are checked into the
  repo deliberately: shared knowledge, not personal config.
- `artifacts/`: drop-in parser scripts. `resources/`: icons.

## Build / CI

- CI is `.github/workflows/build-windows-exe.yml` running
  `pyinstaller ffs_explorer.spec`; that spec is the one that matters.
- It must bundle `app/ccl_segb` (vendored) and the config seeds.
- New dependency or config file: read `ASSUMPTIONS.md` §16 (A-63) first
  (PyAV from source, the `excludes=` trap, `datas=` destinations).

## Keeping this map current (instruction to Claude)

After any change, update this file if the change invalidated it. Triggers:
- a file/module added, removed or renamed;
- responsibility moved between files;
- a case DB table added or changed;
- a new config file or frozen-exe location;
- a section-map symbol renamed, removed or moved.

Routine bugfixes need no update here; they belong in docstrings and commit
messages. A new fact or design decision goes in `ASSUMPTIONS.md`, by
agreement with the user (see the read-only rule above).

`scripts/check_claude_md.py` runs as a pre-commit hook. It auto-fixes
line-number drift in any `` `Symbol` (file.py:NNN) `` anchors and blocks the
commit if the module table is out of sync with `app/`.

## Human verification status (instruction to Claude)

`VERIFICATION_STATUS.md` tracks which parts of this AI-written codebase a
human has walked through and confirmed (🔴 not reviewed / 🟡 changed since /
🟢 reviewed).
- **Before editing a file or `ffs-explorer.py` section listed as 🟢**, name
  the row and its date first, so the user can decide whether to re-review.
- A real functional change to a 🟢 row should drop it to 🟡.

## Conventions (coding rules)

- **ui_path vs physical path:** `ui_path` is the display path; the physical
  zip name comes only via `FfsAdapter.resolve`. Never mix them.
- **Archive reading:** never read the main FFS archive with raw
  `zipfile.ZipFile(...)`/`.read(name)`. Use `zip_cd_cache.py` + `ZipEntry`,
  or `CachedZipView` for metadata-only reads.
- **Platform and paths:** never hand-roll an Android check
  (`FfsAdapter.is_android(folder_map)`) or a container list
  (`container_parents()`). Use `canonical_app_path()` for any path stored
  across cases (bundle id, never GUID).
- **File types:** one source for file type, `_header_type_overrides`. One
  description of header-scan tiers, `_HEADER_SCAN_TIER_TEXT`.
- **Workers:** create → connect → `_retire_worker` on replace;
  `_stop_all_workers` on close. Never block the GUI thread; batch model
  updates. A background worker owns its own archive reader.
- **Read-only evidence:** viewers never write to the archive. Open an
  evidence SQLite file only via `open_db_readonly` (an `ATTACH` needs its
  own `mode=ro` URI). Carve WAL/SHM before any connect.
- **Timestamps:** always UTC and labelled (`"...UTC"` or `+00:00`), never a
  bare `"YYYY-MM-DD HH:MM:SS"`. Never use SQLite `'localtime'` or a bare
  `datetime.fromtimestamp(x)` (no `tz=`) for an evidence timestamp.
  Reports store raw values plus `timestamp_fields`. Tool-provenance times
  (when the examiner did something) are local with an explicit offset.
- **Tab state:** preserved per `_on_center_tab_changed`; the shared preview
  panel needs a per-tab resync (`ASSUMPTIONS.md` A-57).
- **Escalate, never silently truncate or discard:** say when a list is
  capped; label low-confidence items with the reason.
- **Validation:** check against ground truth, not the tool's own citations.
  Replaying an answer the tool produced proves nothing.
- **Parser honesty:** a parser's `description`/`warning` states real
  limitations plainly.
- **Qt UI and headless tests:** read `ASSUMPTIONS.md` §16 first.
