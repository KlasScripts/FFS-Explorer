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
| **Issue log** (ffs-notes repo) | **Open bugs, features, and ideas.** GitHub issues on `KlasScripts/ffs-notes`, plus the `ISSUES.md` inbox there. See "Issue log" below. |
| `IDEAS.md` | Older ideas with evidence and sketches. Read for evidence; open work is tracked in the issue log. |
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
`ArtifactViewerMixin`, `TimelineViewerMixin`, `SqliteViewerMixin`, `SegbViewerMixin`,
`LevelDbViewerMixin`, `TimestampDisplayMixin`) that `FastZipBrowser` inherits.
"The hex tab" means the mixin file, not the main file. Format *detection and
path mapping* (Cellebrite vs GrayKey, iOS vs Android) lives in
`app/adapters/`; other code branches on `ffs_adapter.format` where behaviour
genuinely differs, but raw format sniffing belongs only in the adapters. Long
work runs in `QThread` workers; first-open metadata parsing runs in a
separate **process** (`ffs_metadata.py`) and the embedded-media sweep in a
**process pool** (`embedded_media_scan.scan_job`), since both are CPU-bound.

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
   central-directory sidecar, so a network-hosted archive opens fast, and
   `zip_index.py`'s mapped index built from it once per case) plus
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
  A row with more than one picture opens the full-size viewer in row mode: a bar names each picture, a caption names the one shown, and the chosen column is kept across rows. `media_compare = True`: open side by side (rows that are a picture comparison). Put media columns in `core_fields` if the pictures are the evidence.
- `timestamp_fields`: `{field: unit}`, unit one of `s`, `ms`, `cocoa_s`,
  `cocoa_ns`, `webkit_us`.
- `timeline_fields` / `timeline_skip`: what each time means (and the
  source's caution) for the Timeline tab; a time with neither is listed as
  "meaning not written yet", never guessed.
- `record_source` (with `source_match`/`presence_fields`): which raw cell(s)
  a row came from, for Record hex mode.
- `recoverable_tables`: retired (A-95, 2026-11-21): a main report holds live
  rows only; deleted records go in a `recovered_records` report (below).
- `hidden_fields`, `core_fields`: column visibility.
- `device_wide = True`: scans the whole archive (given a `CaseContext`).
- `requires_nested_extraction`: embedded zips to extract before `run()`.
- `view_mode = "document"`: render as a markdown page.
- `report_type = "recovered_records"` with `recovered_records = [...]`: a
  deleted / changed records report with no `run()` (the loader attaches
  `artifact_runner.recovered_records_report`): per table the columns that
  matter, `version_fields`, optional `labels` / `derived` / `not_found_in`;
  never joined; `deleted_of` names the main report, whose app location and
  files it takes (`artifact_runner._take_from_main_report`); identical
  copies are one row with `copies`; `orphan_of` also lists a media table's live
  rows whose message is gone (status Orphaned). Optional `json_fields` (a JSON-object column split into
  `<field>.<key>`) and `field_notes` (a checked caveat per row). One per
  messaging app (message + media tables): `*_deleted*.py`, `line_fts.py`,
  `burner_messageentity.py`. Example: `artifacts/ios/line_deleted_messages.py`.

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
| `adapters/ffs.py` | `FfsAdapter`: format/platform detection, path resolution (`resolve`, and back with `ui_path_from_physical`), `container_parents()`, `is_android()`, `is_ios_system_entry()` (the iOS system partition is not included, A-82), `build_app_registry()`, scan-rule accessors. |
| `adapters/graykey.py` | GrayKey zip metadata (timestamps/xattrs from extra fields); UT/UX parsers also used for `zip_extras`. An entry without a UT block gets no time and `no_ut` (never its DOS time, which is local), counted in the warning banner (`missing_ut_warning`, `FastZipBrowser._missing_ut_count`). |
| `csstore.py` | Vendored (MIT) Apple LaunchServices csstore parser, the main source of iOS's `app_registry`. |
| `ffs_metadata.py` | Qt-free first-open parsing (child process); msgpack snapshot pack/unpack; `load_snapshot_from_case`. |
| `db_utils.py` | Case DB open/schema/save-load helpers. |
| `zip_entry.py` / `zip_reader.py` | `ZipEntry` (the archive-read handle every viewer uses), `ZipReader` (raw offset reads, thread pool), `read_nested_entry` (repacked nested archives, cached index). |
| `zip_cd_cache.py` | `.zcd` central-directory sidecar, `load()` (entries from `zip_index`), `CachedZipView`, `compute_data_offsets`. Tests: `tests/test_zip_offsets.py`, `tests/test_zip_readers.py`. |
| `zip_index.py` | The archive's file directory as one compact, memory-mapped index (`<zip>.ffsidx` in the case folder), built once from the `.zcd`; entries read as zipfile reads them, except the name rule of ASSUMPTIONS.md A-80 (UTF-8 names without the flag; a backslash is kept), with each changed name's old form recorded. Qt-free. Tests: `tests/test_zip_index.py`. |
| `case_names.py` | Updates a case made by an older version to the A-80 names, once per case (`case_settings.zip_name_rule`): path columns in both case DBs, saved folder tree dropped for rebuild, one `run_log` row. Called by `ZipMetadataWorker._update_case_names` before the snapshot is used. Qt-free. Tests: `tests/test_case_names.py`. |
| `ios_system_check.py` | Once per iOS case: signs of jailbreaking or sideloading (a fixed path list: OWASP MASTG, rootless jailbreak folders, package managers, TrollStore; plus `/etc/hosts` lines a stock phone lacks) and how much of the system partition the extraction holds (A-82, A-83). Run by `ZipMetadataWorker._run_system_check`, stored as a `run_log` row (`ios_system_check`); `FastZipBrowser._show_system_check_dialog` shows it at case open only when signs are found (once per case, after the tree, before Process Case on a new case: `_after_case_ready`), leading with the signs and then what is out of scope; always from Tools ▸ iOS System Check. Qt-free. Tests: `tests/test_ios_system_check.py`. |
| `header_scan.py` | Magic-byte/text file-type detection (`classify_magic`). |
| `file_types.py` | File type by name: the extension sets, the one extension → type table (`_EXT_TO_TYPE`) and `_get_file_type`, imported by `ffs-explorer.py` under the same names. Qt-free (the parse process uses it). |
| `header_counts.py` | How many files each header-scan tier reads (`_header_tier_counts`, `_header_candidate_matches`), saved per case in `casecache.db` (`_header_tier_counts_cached`); the first-open parse process counts and saves (`ffs_metadata._save_header_tier_counts`), so case open only reads it. Qt-free. Tests: `tests/test_first_open_speed.py`. |
| `scan_rules.py` | Shared folder-prefix ruleset behind `FfsAdapter.scan_folders()` and friends. |
| `evidence_shortcuts.py` | The "Jump to ▾" menu's locations, each with its per-OS-version candidates (newest first; a `*` pattern opens the folder of its first match, e.g. Notes' `NoteStore.sqlite`); `resolve()` picks the first one the extraction has, and the menu greys out an entry with none. Android users are read from the extraction (`android_users`: numeric folders under `data/user`, `data/user_de`, `data/media`, `data/system_ce`, ...); `android_shortcuts(user=u)` gives that user's entries (never a hardcoded 0), shown as a submenu per user other than 0, typed from `data/system/users/<u>.xml` (`user_type_label`, never the name). Paths are Cellebrite-form (no `private/var/`; `prefix_shortcut` adds it on GrayKey). Sources and version notes: `EVIDENCE_LOCATIONS.md`. Qt-free. Tests: `tests/test_evidence_locations.py` (every jump target must be inside a `scan_rules` include). |
| `os_coverage.py` | The OS versions whose evidence locations were checked on real images (`CHECKED`) and documented by sources (`DOCUMENTED`); `warning()` gives the banner text shown under the time banner when an extraction is newer, older or of unreadable version. Update `CHECKED` only after checking a real extraction against `EVIDENCE_LOCATIONS.md`. Qt-free. |
| `embedded_media_scan.py` | The embedded-media sweep. |
| `duplicate_scan.py` | Process Case duplicate detection: size+CRC grouping, MD5 to confirm (inode shortcut where trusted); with `confirm_group` (Process Case's default: groups holding a picture or video) only accepted groups are read, the rest saved as *possible* duplicates (`POSSIBLE` marker in the md5 field): `load_duplicate_files` returns confirmed rows only, `load_possible_duplicates` the rest, shown as "possible N" in the File Browser's Duplicate Count only. Also `proven_copies()`: files proven identical with no read (same size+CRC plus the same GrayKey SHA-256 or trusted inode), used by Header Scan (`_header_copy_map` / `_scan_headers_once_per_copy` in `ffs-explorer.py`) to read each set once and give every copy the result; size+CRC alone is never proof. Qt-free. Tests: `tests/test_header_scan_copies.py`. |
| `embedded_media_skip_list.py` | Known-noise database skip list for the sweep. |
| `media_metadata_scan.py` | EXIF/video container metadata extraction (`_LazyZipSlice`, `extract_image_exif`, `extract_video_metadata`). 'taken' is EXIF `DateTimeOriginal` (Exif sub-IFD, never IFD0 `DateTime`) with `taken_offset` from `OffsetTimeOriginal`, or the video container's UTC time; `taken_display` / `taken_utc_seconds` (None without an offset: local time is never used as UTC). `TAKEN_RULE` is stamped on the EXIF run log; an older run makes Process Case offer the stage again. Tests: `tests/test_exif_taken.py`. |
| `user_media_ignore_list.py` | Global exclusion list for the "User Media" filter. |
| `keyword_lists.py` | Grooming/drug keyword lists: shipped baseline (with sources) + the examiner's own terms and on/off choices (`keyword_lists_user.json`). Qt-free. |
| `keyword_lists_dialog.py` | Tools ▸ Keyword Lists window. |
| `chat_flags.py` | Red flags: ranks a messaging report's chats by a pattern (categories in one session, request→media, media-heavy, unexplained deletions; rare terms weigh more) with reasons and key messages. Scored in a worker process (`compute_in_process`, run by the viewer's `RedFlagsWorker`) and kept in `casecache.db` `red_flags` under `fingerprint` (report rows, keyword lists, meaning-check vectors, `FLAGS_VERSION`). Qt-free. |
| `meaning_check.py` | Meaning check (Roblox Sentinel-style, re-implemented): messages vs a harmful example bank and the phone's own messages, via a local embedding model; vectors cached in `casecache.db` `text_embeddings`. Qt-free. |
| `term_matcher.py` | Tolerant keyword matching (number/symbol swaps, stretched/spaced letters, text-speak, typos with a dictionary guard). Qt-free. |
| `child_priority.py` | The background child processes (first-open parse, zip index build, iOS system check): `lower_this_process()` (below normal priority, so the window is served first) and `end_now()` (end at once when the work is saved; Python's normal exit freed millions of objects while the window waited). The parse child also runs with garbage collection off. Qt-free. |
| `config_seed.py` | Ships an examiner-editable config list with every install: seed in `_internal/config`, merged once per entry into the file next to the exe. Qt-free. |
| `files_by_google_glide.py` | Files by Google Glide disk-cache linker: rebuild ResourceCacheKey hashes (scan/Recents/grid/list/viewer), link via index → media store → Safe folder → absent ids → carved external.db size/mtime; temp cleanup. Used by the Google Files thumbnail reports. Qt-free. |
| `file_browser_presets.py` | Shared filter+sort presets (File Browser dropdown, Media Browser buttons). |
| `dialog_helpers.py` | Shared Qt dialog-construction helpers; no business logic. |
| `startup_log.py` | Bounded startup/error log + crash/stall snapshot (`Tools → Open Log Folder`). Qt-free. |
| `freeze_log.py` | GUI-freeze logger: a QTimer beat from `main()`; a watcher thread logs each freeze over 0.5 s to the startup log with the GUI thread's sampled stack, the call it is in (read from the bytecode; `exec()` = inside Qt) and the other busy threads (code locations only, no values); when the watcher itself was shut out it names the thread that used the CPU meanwhile (per-thread CPU time: the lock holder), or says no CPU was free; `beat()` notes a gap the watcher missed; faulthandler dump to crash.log at 15 s; exit watchdog ends a process still alive 20 s after "exiting". Qt-free. |
| `file_table.py` | The case's file list (ui_metadata) and folder contents (folder_map) as one memory-mapped sidecar in the case folder (`files-<token>.ffsfiles`, named in the load snapshot, SNAPSHOT_VERSION 6): number columns + null bits, one name pool, CRC-32 hash table, same design as `zip_index.py`. `FileList`/`FolderMap` are MutableMappings with a dict on top for runtime additions; entries read from the file are fresh dicts (assign back or use setdefault); `copy()` is cheap. Loops over every file use `fields_of`/`any_set` (columns), not `.items()`. `write()` refuses anything that wouldn't read back exactly, and the snapshot then keeps the dicts. zip_ui_paths is an IN_ZIP bit per file (`ZipPaths`, a read-only Set); the snapshot names the `.ffsidx` instead of storing zip_names when its name set is exactly theirs (checked at pack time, cd_sha256 checked at load). Also in the snapshot, worked out once at parse: the empty-folder list; header-scan counts for the table's files are saved in casecache.db (`_header_tier_counts_cached`). Qt-free. |
| `version.py` | The running release (e.g. `v0.50.21`) for the window title and startup log line: `version.txt` in a frozen build (written by the spec), `git describe` in a source run. Qt-free. Tests: `tests/test_version.py`. |
| `timestamp_display.py` | `TimestampDisplayMixin`: timestamp-mode banner, `format_ts`, Timestamp Display dialog. |
| `archive_offset.py` | The device's likely UTC offset from the zip's DOS time (local wall clock) vs its UT mtime (UTC), sampled from the cached index (`detect_from_archive`): a fixed offset, a daylight-saving pattern (two bins an hour apart: no fixed offset offered), or none; plus the IANA zones that give the zip's offset for at least 97% of the sampled files at their own dates, never one picked. Display only (Timestamp Display's "Archive offset (DOS vs UT)" mode, stored in `case_settings` `archive_offset` / `archive_zone_choice`); evidence times stay UT/UTC. Qt-free. Tests: `tests/test_archive_offset.py`. |
| `device_timezone.py` | Best-effort handset/acquisition/system timezone detection. |
| `keyword_search.py` | Search workers (main archive, nested archives, LevelDB index), `KeywordSearchMixin`, `SqlHitInterpretWorker`. Results tree builds a file's hit rows when it is expanded (`_search_show_hits`); results-DB writes go through one writer thread (`_write_results_db`). |
| `keyword_scan.py` | Keyword Search byte matching, run in worker processes (`search_job`: a list of stored entries read by offset, hits back), including the keyword inside base64 (`make_base64_patterns`, each candidate decoded to confirm) and, for a non-ASCII keyword, its case, decomposed (NFD) and Windows-1252 forms (`keyword_forms`), and, for a phone-number keyword, its formatted, no-country-code and reversed forms (`make_phone_patterns`). Qt-free. Tests: `tests/test_keyword_scan.py`. |
| `hex_viewer.py` | Hex tab; Record/Attachment toggle and record-source combo. |
| `media_viewer.py` | Media Browser grid (`MediaFileListModel`/`MediaGridDelegate`), `MediaFullViewDialog`, seen-tracking; the "User Media" and "Web Cache Media" (`_web_cache_media_paths`, `_show_web_cache_media`) review lists. Decoding (A-64): `_load_qimage` (full size), `_image_thumbnail`/`_video_thumbnail` (thumbnails, made on `ThumbnailWorker`'s decode pool, cached in `casecache.db` `thumbnails`). Tests: `tests/test_media_decode.py`. |
| `sqlite_viewer.py` | Database tab: committed view by default (`prepare_database`, via `sqlite_reader`), WAL before/after, and recovered versions from `sqlite_recovery` (WAL history, journal, unfinished write/delete, carved), carved in the background (`SqliteRecoveryWorker`, via the worker process and `casecache.db`) and kept per table; date-column guesses from one pass without BLOBs (`_sql_column_samples`); prepared databases kept per session (`_PREPARED`, path + size + CRC) so returning is instant. |
| `epoch_guess.py` | Database tab: guesses which epoch format a number column holds (a quick look, not a parser). Qt-free. |
| `epoch_store.py` | Database tab: which columns are times — the examiner's choices, then built-in `config/epoch_formats.json`, then the guess. Qt-free. |
| `segb_viewer.py` / `segb_schemas.py` | SEGB/Biome tab and built-in stream schemas. |
| `leveldb_viewer.py` | `LevelDbViewerMixin`: LevelDB/IndexedDB records as virtual files. |
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | Parser plugin system: running, storing, viewing. |
| `artifact_categories.py` | Category → App → Report arrangement for Run Artifact Parsers and the Artifact Viewer, from `config/artifact_categories.json` (kept out of the parsers so their hash-derived versions don't change); a parser's own `category` / `app_group_label` wins; anything unset is listed under "Other (category not set)", never hidden. Qt-free. Tests: `tests/test_artifact_categories.py` (every shipped parser has a category). |
| `chrome_cache.py` / `chrome_shared.py` | Chrome HTTP-cache decode and helpers shared by the `chrome_*` parsers. |
| `ai_summary.py` / `ai_summary_store.py` / `local_llm.py` | Local-LLM report summarization. |
| `nested_archive.py` | Extracts one embedded archive into `case_dir/nested_archives/` (idempotent). An entry's stored `mdate` says its clock (`entry_mdate`/`parse_mdate`): UT and gzip MTIME are UTC; DOS-only is "(local, zone unknown)", never a UTC mtime, shown orange with a tooltip and a note above the table (`_local_time_tip_for_row`, `_update_nested_time_note`). `recover_missing_records` re-records unpacked files whose `casecache.db` rows were lost at a cache rebuild (matched by `stored_name_for`, also under an archive's pre-A-80 path; a gzip's time read from the original `.gz` header by offset), run at case open (`_recover_nested_records`, logged in `run_log`). |
| `sqlite_carve.py` | Byte-level SQLite decoding and carving primitives; `recover_deleted_rows` (reports' `recoverable_tables`, now via `sqlite_recovery`); `locate_live_row`/`locate_offset`/`identify_structure`/`build_page_map`; `rowid_alias_column` (SQLite's exact INTEGER PRIMARY KEY rule). |
| `sqlite_reader.py` | The one way an evidence SQLite file is opened: committed view (WAL applied, hot journal rolled back on a temp copy), checkpoint view (main file alone), `immutable=1` only with no `-wal`/`-journal` beside the file; `stage`, `committed_image`, `journal_pages`; notes for the caller to show. Used by `open_db_readonly`, the Database tab, the carver. Qt-free. Tests: `tests/test_sqlite_reader.py`, fixtures `tests/sqlite_fixtures.py`. |
| `sqlite_recovery.py` | The one deleted/older-record carver for reports and the Database tab: per file once (freed pages, WAL frames, journal pages, overflow, free space; memo by page image), per table cheap attribution; methods checkpoint_deleted/older, wal_frame, journal, unfinished_write/delete, freeblock/page_gap, freed_page, overflow, header_signature, header_rebuilt, header_schema; shape/garbage checks; `describe()` labels; `RECOVERY_VERSION`; the Database tab carves in one spawned worker process (`records_in_process`: a CPU-bound QThread starved the GUI of the GIL) and keeps results in `casecache.db` `recovered_records` (`cache_get`/`cache_put`, JSON not pickle, keyed by path+size+CRC and `RECOVERY_VERSION`); the embedded-media sweep encodes in its worker processes (`encode_many`) and the window only writes the blobs (`cache_put_encoded`): JSON-encoding in the window's process froze it; `locate_committed_row` (where a live row's committed bytes are, main or WAL: Record mode). Qt-free. Tests: `tests/test_sqlite_recovery.py`; scored on the SQLite Forensic Corpus (scratch harness). |
| `timeline.py` | Timeline events: the declared time columns of every artifact report (`timeline_fields`), built from `caseresults.db` into `casecache.db` (`timeline_events`, `timeline_sources`, `timeline_meta`; rebuilt when its `fingerprint` changes); `to_unix_seconds` is the one unit conversion, shared with report time columns. `file_events` adds every User Media file's own times (Created/Changed/Modified, never Accessed; the times one file shares are one event) and its EXIF/video taken time when that has an offset; proven copies (duplicate scan MD5) with the same time are one event; `script` is `FILES_SCRIPT` and `path` the file. Qt-free. Two more file sources: web cache media (`CACHE_SCRIPT`, the record's times) and one event per unpacked archive (`ARCHIVE_SCRIPT`, its own times; files inside contribute no file-system times); detailed mode (`DETAIL_SCRIPT`, off by default, per-case `case_settings` `timeline_detailed`, the Timeline's "Detailed" checkbox): every other real file under `scan_folders()` minus `scan_excludes()` (`_timeline_detail_paths`). Tests: `tests/test_timeline.py`, `tests/test_timeline_files.py`. |
| `timeline_viewer.py` | `TimelineViewerMixin`: the Timeline centre tab (index 4), its build worker, filters, Sources list, "Show activity around this time" (`_timeline_show_around`), "Go to" (`_timeline_go_to`), and the jump back to the report row. `_timeline_files_spec` gives the build the User Media list (the Media Browser's locations and types, minus the ignore list; seen files stay in), collected on the worker; a file event's double-click opens it in the File Browser. `_timeline_add_time_menu` is the right-click "🕒 Timeline" submenu (Go to / ±1 day per time) used by the File Browser, Media Browser, Keyword Search, report rows, the Database tab and the Timeline; file times come from the archive metadata (`_timeline_file_times`), never from displayed text. |
| `artifact_media.py` | `MediaThumbnailDelegate`/`WebpageThumbnailRenderer` for report `media_fields` columns. |
| `research_store.py` | Global artifact research notes (row colouring). |
| `parser_versions.py` | Parser version tracking and changelog (update banner). |
| `report_columns_store.py` | Per-report column order (persisted) and visibility (session-only); `filled_columns_scan` (which columns have values, whole report and per chat). |
| `report_extras.py` | What opening a report needs beyond its rows, built when the report is made (`build_all`, called by `ArtifactRunnerWorker`) and kept in `casecache.db` `report_extras` under `chat_flags.report_stamp`: row count (row ids are 1..N, never read), filled columns per scope, distinct media paths, the Chat Groups index (report tree) and summary (`build_chat_groups`; bookmark counts added at display, `apply_bookmarks`). A report made before that gets them once on a background thread when opened (`_ReportExtrasWorker`). Qt-free. Tests: `tests/test_report_extras.py`. |
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
  `ai_summary_settings.json`, `scan_rules_{ios,android}.json`, `artifact_categories.json`,
  `epoch_formats.json` (examiner choices in `epoch_formats_user.json`). All follow one
  convention: in `config/` in dev, next to the executable in a frozen build,
  user-editable, cached by mtime+size.
- `user_media_ignore_list.json` and the presets file are checked into the
  repo deliberately: shared knowledge, not personal config.
- `artifacts/`: drop-in parser scripts. `resources/`: icons.

## Build / CI

- CI is `.github/workflows/build-windows-exe.yml` running
  `pyinstaller ffs_explorer.spec`; that spec is the one that matters.
  It builds only for a pushed `v*` tag (or a manual run), passing the tag as
  `FFS_VERSION`: the release zip is named with it
  (`ffs-explorer-windows-v0.50.21.zip`; the exe names stay fixed) and it
  ships as `_internal/version.txt` for the window title (`app/version.py`).
- It must bundle `app/ccl_segb` (vendored) and the config seeds.
- Every `app/` module and everything an `artifacts/` script imports is
  added to `hiddenimports` from the source (`scripts/frozen_imports.py`):
  report scripts load at run time, so PyInstaller can't see their imports.
  Tests: `tests/test_frozen_imports.py`.
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

## Issue log (instruction to Claude, on the Mac and the PC)

Open work is tracked as GitHub issues on `KlasScripts/ffs-notes`. Each title
starts with its id, for example "B-01 Opening a case hangs the window".
`ISSUES.md` in that repo is an inbox: Cursor adds new entries there, and they
are then turned into GitHub issues and deleted from the inbox. Use `gh`.
`ffs-notes/CLAUDE.md` has the conversion steps.

- **Start of a session on FFS-Explorer:** check the `ISSUES.md` inbox, for
  example with `gh api repos/KlasScripts/ffs-notes/contents/ISSUES.md --jq
  .content | base64 -d`. If entries are waiting under `## New`, tell Klas and
  offer to move them to GitHub issues. Read GitHub, not a local clone: Klas
  may add entries from Cursor on the phone, which pushes straight to GitHub,
  sometimes on its own branch or as a pull request
  (`gh pr list -R KlasScripts/ffs-notes`).
- **Before starting a task:** look for an open issue that covers it
  (`gh issue list -R KlasScripts/ffs-notes --search "<words>"`) and name it.
- **When the session fixes an issue:** say "We fixed B-xx (#N). Should I
  close it?" Close it only on a yes, with one line on what shipped and the
  commit or version.
- **When something comes up that will not be done now:** say "Shall I add
  this as an issue?" On a yes, create it with the next free id and raise the
  "Next free ids" line in `ISSUES.md`.
- **When an idea is rejected:** ask before closing its issue as not planned,
  with one line on why.
- Do not keep a second list of open work in this repo.

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
