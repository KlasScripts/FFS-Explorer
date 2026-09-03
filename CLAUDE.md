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
2. `ZipMetadataWorker` (ffs-explorer.py:1145) → `app/ffs_metadata.py
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
  entry index, `app_intelligence_scan_key` staleness marker — files_indexed
  count + raw_content_enabled state, so toggling raw content on triggers a
  rescan too, not just an archive change),
  header_types, guid_bundle, nested-archive index, `app_intelligence` (see
  `app/app_intelligence.py` below — `evidence_databases_json` since schema
  v7, 2026-08-24, replacing a single `evidence_db_path/bytes/note` triplet;
  see that row's `find_evidence_databases` entry), `app_registry` (schema
  v6, added 2026-08-23 — one row per iOS bundle id: display name, Team ID,
  Bundle/Data container paths, App-Group paths, `has_parser`; see "iOS app
  registry (LaunchServices)" below). On schema mismatch it is auto-deleted
  and rebuilt.
- `caseresults.db` — precious results (never auto-deleted): search_index /
  search_results, bookmarks, device_info, run_log (an `artifact_<script_name>`
  row's `parser_version` column records the parser's version — see
  `app/parser_versions.py` in Conventions — at the moment that run happened),
  user SEGB schemas (`segb_schemas`), per-case settings (`case_settings`,
  e.g. AI backend choice), artifact parser output (`artifact_<name>`
  tables), `ai_summaries` (added 2026-08-29 — `script_name` primary key,
  one row per report holding the LAST generated AI Summary: `text`,
  `total_rows`, `chunk_count`, `generated_at`; written by `ai_summary.
  save_summary`, overwritten on each new generate, same as a normal
  `artifact_<name>` table — see the "AI Summary GUI dialog" Conventions
  entry and the app-group-root paragraph right after it). On schema
  mismatch raises `OldSchemaError`.
- (Heads-up: `zip_cd_cache.py`'s docstring still says "casedata.db" — stale
  name, the real file is `caseresults.db`.)

## app/ modules

| File | What it is |
|---|---|
| `adapters/ffs.py` | `FfsAdapter` — single place for Cellebrite/GrayKey/iOS/Android differences; path resolution, plist candidate paths, prefix detection. `build_app_registry()` (added 2026-08-23) is the primary source of iOS's `app_registry` table — see "iOS app registry (LaunchServices)" below; a no-op returning `([], {})` on Android |
| `csstore.py` | Vendored parser (added 2026-08-23) for Apple's undocumented **LaunchServices csstore** binary format (`bdsl` magic) — MIT, from `github.com/JJTech0130/launchservices`; verbatim except removing a debug `print()` in `hashmap_from_stream()` that would spam stdout on every case load (documented in the file's own provenance header). Only the low-level `CSStore`/`CSTable`/string-table classes are used — `lsdatabase.py`'s higher-level wrapper was deliberately NOT vendored (confirmed crashing on real data decoding `icon_files`); the `Bundle`/`PropertyList` table field offsets `adapters/ffs.py` actually reads were reverse-engineered fresh against real casework instead — see below |
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
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | Plugin system: parser scripts in `artifacts/ios|android/` (e.g. `photos_metadata.py`, `sms_messages.py`, per-platform `whatsapp.py`) run against the archive, results into `casedata.db`, browsed in Artifacts tab. Third-party iOS apps declare `app_group` instead of `app_path` — their container is GUID-named per install, resolved via the case's `guid_to_bundle` map at run time (`artifact_runner._resolve_app_group_base`); see `artifacts/ios/whatsapp.py`. The `paths` dict `run()` receives also carries a reserved `_app_base_ui_path` key (the container's own ui_path) for a parser that needs to *reference* another file inside the container — e.g. an attachment path stored in a DB column — without extracting it itself; see `media_fields` below. `artifact_runner.py`
also exposes small importable helpers a parser's own `run()` can reach for
directly (`from artifact_runner import first_nonempty`, etc.) — see "Parser
helpers" below, the standard place to add the next one. The `paths` dict
also carries reserved `_nested_archives`/`_nested_archive_errors` keys
when a parser declares `requires_nested_extraction` — see Conventions
and `nested_archive.py` above |
| `chrome_cache.py` | Qt-free core for Chrome's HTTP disk cache (Simple Cache format) — entry parsing, HTML reference scanning, and synthetic `.mhtml` reconstruction, plus `parse_all_entries(paths)`, the shared full-directory pass both `artifacts/android/chrome_cache_media.py` and `chrome_cache_pages.py` filter/project down to their own content-type rather than each re-implementing the decode loop. See its own module docstring for the reverse-engineered on-disk format and the Conventions entries on the Chrome Cache report split |
| `chrome_shared.py` | Small helpers shared across the `artifacts/android/chrome_*.py` parser family (Login Data, Cookies, Network Action Predictor, Top Sites, Shortcuts, Favicons, Autofill, ...) — `query_rows` (the connect/row_factory/close boilerplate every simple single-table Chrome parser needs), `url_set` (distinct values of one column, with a real None-vs-empty-set "couldn't check" distinction for an optional cross-reference file), and `history_visits` (Chrome History's own visits/urls joined and webkit_us-converted once). Same "one Qt-free core module, imported by name" pattern as `chrome_cache.py` above — each consuming parser script stays a thin declaration (its own SQL + per-row shaping), not a place to re-derive this plumbing. See Conventions for the gap-sweep entry this was factored out of |
| `ai_summary.py` | The AI Summary feature's core logic (added 2026-08-29): reads an already-completed report's rows straight from `caseresults.db` (the same source `query_artifact` reads, so this only ever summarizes what's already been reviewed as a normal Report, never a fresh unreviewed parse), splits them into time-gap-bounded chunks (`_chunk_by_time_gap` — natural session boundaries in the real timestamps, not fixed row counts, so a redirect/sign-in chain never gets cut in half), sends each chunk to a local LLM (`local_llm.py`) for a mini-summary, then combines the mini-summaries into one final narrative via a size-bounded hierarchical reduce (`_reduce_hierarchically`). That last step exists because a flat single reduce call was confirmed by direct testing to hit the exact same context-length ceiling an unchunked report does, once there are enough chunks — a real 75-row/2.5-month case produced 24 chunks whose concatenated mini-summaries (37,505 chars) failed the same way the original unchunked 61-row case did at 32,575 chars; `_reduce_hierarchically` batches under a safe character budget and recurses until one narrative remains. Qt-free; used by both `mcp_server.py`'s AI Summary tools and `artifact_viewer.py`'s `AISummaryDialog`, so there is exactly one implementation of "what gets sent and how" |
| `ai_summary_store.py` | Global (cross-case) settings for AI Summary (added 2026-08-29): the local LLM connection (endpoint/API key/model — LM Studio by default) plus, per report (keyed by bare `script_name`, matching `query_artifact`'s own `name` parameter), which columns get sent, chunk size, max time-gap-per-chunk, and the editable prompt template (must contain a `{data}` placeholder). `config/ai_summary_settings.json`, same dev/frozen-path convention and mtime+size load cache as `research_store.py` |
| `local_llm.py` | Thin stdlib-only (`urllib`) HTTP client for a local OpenAI-compatible chat server (added 2026-08-29, LM Studio by default): `call_chat` (one chat-completion request) and `list_models` (which model id is actually loaded right now). Never raises — a local model server being unreachable, misconfigured, or slow is an ordinary, expected condition for this optional feature, not a bug to propagate as an exception. `call_chat`'s `timeout` is enforced as a genuine WALL-CLOCK deadline (a helper thread + `future.result(timeout=...)`), not just passed to `urlopen()`'s own `timeout=` — confirmed necessary the same day by direct testing: a real reduce call once ran 5,217 SECONDS (87 minutes) despite `urlopen`'s timeout being set to 240s, because LM Studio's server sends occasional keep-alive bytes during a long generation that reset urllib's per-read idle timer indefinitely without the response ever completing. A `max_tokens` cap (2048 default) is a second, independent safeguard against the same failure mode server-side |
| `nested_archive.py` | Extracts one embedded/nested archive (ZIP or gzip) from an FFS zip's raw bytes into `case_dir/nested_archives/`, recording it in `casecache.db` — Qt-free (added 2026-08-30, factored out of `ffs-explorer.py`'s own `NestedArchiveWorker._process_one`, since `app/` modules never import from the top-level script) so there is exactly ONE implementation shared by the examiner's manual "Extract as Nested Archive" / batch action AND `artifact_runner.py`'s own `requires_nested_extraction` (a parser declaring it needs one specific embedded archive extracted first — see `WRITING_ARTIFACT_PARSERS.md` and the Conventions entry below). `already_extracted`/`extracted_path` give a caller the same idempotency check `NestedArchiveWorker` already used, so a prior manual extraction and a parser-triggered one never redo each other's work |
| `sqlite_carve.py` | Below-SQL-layer deleted-record recovery: freeblocks, freed/freelist pages, and full WAL frame history (not just the current valid chain `sqlite3.connect()` would replay) — decodes SQLite's on-disk record format directly, since a `DELETE`d row's bytes usually survive until something else reuses that space. Invoked automatically by `artifact_runner.py` for any table a parser names in `recoverable_tables`; no recovery code belongs in a parser script itself. Every recovered row also carries its own exact `raw_file`/`raw_offset`/`raw_length` (see `record_source` in Conventions) for the same Hex-panel Record-mode jump a live row gets. Also holds `locate_live_row` — the opposite case, finding a currently-LIVE row's on-disk cell by rowid for the Artifact Viewer's "Record" hex mode |
| `artifact_media.py` | `MediaThumbnailDelegate` (per-column QTableView delegate painting a thumbnail instead of raw path text) and `MediaFullViewDialog` (full-size image / video playback with transport controls, opened on double-click) for Report table `media_fields` columns — see Conventions below. Reuses `media_viewer.ThumbnailWorker` for decoding, so results share the Media tab's own on-disk thumbnail cache |
| `research_store.py` | Global (cross-case) artifact research notes in `config/research_status.json`, keyed by stream/bundle identity, drives row colouring |
| `parser_versions.py` | Global (cross-case) parser version tracking in `config/parser_versions.json`, same dev/frozen-path convention as `research_store.py` — a hash-derived version number per parser script plus an optional human-authored changelog; drives the Artifact Viewer's "newer parser version available" banner. See Conventions |
| `report_columns_store.py` | Per-report column ORDER and VISIBILITY state for the Artifact Viewer's Report table (added 2026-08-30), keyed by bare `script_name`. The two halves are deliberately persisted DIFFERENTLY, per direct design instruction: display ORDER (`get_column_order`/`set_column_order`) is a genuine permanent, global (cross-case) preference — `config/report_columns.json`, same dev/frozen-path convention as `research_store.py`, survives every future run of the app. VISIBILITY (`get_visible_columns`/`set_visible_columns`, which columns are ticked/shown) is deliberately SESSION-ONLY — held in a plain in-memory dict, never written to disk — so reopening the same report later in the same run restores exactly what the examiner had, but restarting the app always resets to Core columns (or every column if the parser declares no `core_fields`); a hidden-column choice is exactly the kind of thing that should never be able to silently persist forever and leave material evidence permanently out of sight. `None` and `[]` are deliberately different states for visibility ("never customized this session" vs. "examiner explicitly chose to show nothing"). Drives the Report table's own "Columns" dialog — see Conventions and `core_fields` in `WRITING_ARTIFACT_PARSERS.md` |
| `app_intelligence.py` | Per-app coverage/category/permission intelligence + a deterministic 0-10 "interest score" for apps with no artifact parser yet (added 2026-08-23, driven by the MCP `list_apps` tool — see `mcp_server.py`). Qt-free. Cross-checked against iLEAPP's `appItunesmeta.py` and ALEAPP's `packageInfo.py` (both by their respective creators) before/during building this — caught two real bugs a plan-only design missed (see below), not just a style comparison. `resolve_parser_coverage(platform)` maps each parser's declared `app_path`/`app_group` to the same app id `list_app_containers` reports (Android: last segment of `data/data/<pkg>`; iOS: `app_group` used verbatim — an iOS `app_path` pointing at an OS path like `mobile/Library/SMS` correctly resolves to no app). **iOS**: `find_bundle_container_parent` locates the archive's Bundle/Application container parent (a DIFFERENT GUID per app than its Data container, and confirmed NOT even the same path-prefix convention as the Data-container parent in real casework, so it's discovered by scanning, not hardcoded); `scan_ios_bundle_containers` then reads each Bundle container's `iTunesMetadata.plist` + `Info.plist` ONCE each — identity (`softwareVersionBundleId` / `CFBundleIdentifier`), category (`genre` / `LSApplicationCategoryType`), and permissions (`NS*UsageDescription` keys) all come from those same two reads; no separate identity lookup needed (an earlier version of this tried reverse-searching `guid_to_bundle` for the Bundle container's own GUID — confirmed that map covers ONLY `container_parents()`, i.e. Data/PluginKitPlugin/Shared-AppGroup, never Bundle containers, so it silently produced zero results for every app until this was found and fixed). **Android**: permissions do NOT live in `packages.xml` (confirmed against real casework — its `<package>` elements carry signing/install metadata only) — real per-user grants are in the separate `data/misc_de/0/apexdata/com.android.permission/runtime-permissions.xml` (`read_android_permissions`, via `_load_runtime_permissions_xml`), flat `<package><permission name=.. granted=../></package>`, no wrapper tag. Category, reversing this project's own earlier "no Android category source" conclusion, DOES exist on-device: `packages.xml`'s `<package categoryHint="N">` maps to the public, stable `ApplicationInfo.CATEGORY_*` Android SDK constants (`read_android_category`, `_ANDROID_CATEGORY_HINTS` — confirmed `categoryHint="4"` on `com.whatsapp` decodes to `CATEGORY_SOCIAL`, matching a known comms app). Both `packages.xml` and `runtime-permissions.xml` can be **binary ABX-encoded** rather than plain XML depending on device/build — confirmed via real Android 14 casework (`packages.xml` opened with `ABX\x00` magic bytes, not `<?xml`) — decoded via vendored `app/ccl_abx.py` (see its own docstring/NOTICE-equivalent header for provenance — MIT, CCL Forensics via ALEAPP, same vendoring convention as `ccl_segb`), checked by magic bytes rather than assumed from OS version. All raw-content reads only run when `ctx.raw_content_enabled`; without it, `category`/`permissions_declared` stay `None` and are scored as "unknown," never as confirmed-absent. Deliberate remaining v1 gap: `AndroidManifest.xml` (binary AXML nested inside each `base.apk` zip) is not read — `packages.xml`/`runtime-permissions.xml` already cover category+permissions at far lower cost (flat files, no zip-within-zip). `compute_interest_score` and `scan_apps` are pure/deterministic — never call out to any LLM — see Conventions for the score formula. **App-Group scoring fix** (2026-08-23, the concrete bug that motivated the LaunchServices work below): an App-Group container (e.g. `group.ch.threema`) has no `CFBundleIdentifier` of its own, so it used to score from nothing — `_load_group_owner_index` (built from the now-precomputed `app_registry` table, not re-derived) maps `{app_group_id: owning_bundle_id}`; `scan_apps` looks an App-Group row's id up there and borrows the owning bundle's category/permissions/`has_parser` for scoring instead of treating it as an identity-less app. **`find_evidence_databases`** (renamed from `find_evidence_database`, 2026-08-24) returns multiple ranked candidates rather than one guessed winner — see the "find_evidence_database → find_evidence_databases redesign" paragraph below for why and how. **Vault/cloud-storage scoring fix** (2026-08-24, same day, prompted by a user question: "why is this only focused on messaging apps?"). The `comms_signal` breakdown component — renamed `high_interest_category` — was actively counter-productive for vault (hide-and-lock) apps: its `category_noncomms` branch scored a declared `Utilities`/`Calculator` category with no sensitive permission as a flat 0, treating "looks boring" as confirmed-uninteresting — exactly the profile a vault app is built to present. Changed to score 2 (undetermined, same as the true-unknown case) instead of 0. Added `_KNOWN_VAULT_SUBSTRINGS` (galleryVault, vaulty, calculatorLockVault, NQ_Vault, playgroundVault — Android package names confirmed via ALEAPP; no iOS vault bundle id listed, since iLEAPP's own `nsVault.py` notes its identity is inferred from a filename, "not bundle-specific") and `_KNOWN_CLOUD_STORAGE_SUBSTRINGS` (Dropbox, OneDrive/`microsoft.skydrive`, Google Drive/`google.android.apps.docs`, ProtonDrive — confirmed via iLEAPP/ALEAPP), both scored the same +4 as a comms/social match. **`find_hidden_vault_storage`** (added 2026-08-24, same day) — presence-only detection (same pattern as `find_webview_storage`) of a vault app's raw hidden-media folder by PATH-substring signature (`.Calculator_Lock`, `.galleryvault_`, `applocker/vault`, `folderlockadvanced` — confirmed via ALEAPP's `calculatorLockVault.py`/`galleryVault.py`/`playgroundVault.py`), since several vault apps dump renamed/extensionless media into such a folder with NO database at all — invisible to `find_evidence_databases` by design, not just outranked. `scan_apps`'s tie-break ranks a hit here (or a `known_real_store`-cited `evidence_databases` candidate) at the very top, above a merely-clean unconfirmed hit. **Bare-filename fix, since SUPERSEDED** (2026-08-24, found while adding Chrome to `known_evidence_patterns.py`, replaced the same day): `find_evidence_databases` only matched by extension (`_DB_EXTENSIONS`), so Chromium-family browsers' real stores (`History`, `Cookies`, `Web Data`, etc. — confirmed no extension at all, per ALEAPP's `chromeCookies.py`/`chromeLoginData.py`/etc.) were invisible as CANDIDATES, not just outranked. First fix was `_DB_BARE_FILENAMES`, a hardcoded exact-name allowlist — removed later the same day once the magic-byte fallback below made it redundant AND it was recognized as the same whack-a-mole pattern this project moved away from for noise-filtering (one more specific name to maintain, rather than a general fix). See "Row-merge + magic-byte fallback" below for what replaced it. **Row-merge + magic-byte fallback** (2026-08-24, same day, prompted by two user questions after a non-circular validation test — see below). `scan_apps` now groups Data/Application + Shared/AppGroup containers belonging to the SAME app into ONE row (via `group_owner`, already built for the scoring/citation identity fixes above) instead of one row per physical container — `containers` on each row lists every physical folder merged (`{app_id, path, kind}`), and `evidence_databases`/`webview_storage`/`hidden_vault_storage` are pooled and re-ranked across all of them together. PluginKitPlugin (extension) containers deliberately stay separate — self-describing dotted-suffix ids, not opaque group ids. Confirmed necessary and sufficient by a proper BLIND test (independent ground truth, not a citation replayed through the tool): Telegram (`ph.telegra.Telegraph`), never added to `known_evidence_patterns.py` beforehand, had ALL its real data in the App-Group container while its Data container (the one an LLM would check first, matching the bundle id it already knows) was empty — merging fixed that half. The other half: `find_evidence_databases` gained an optional `read_bytes` parameter (Tier 3, `raw_content_enabled`-gated) — for a file with NO extension at all that survived exclusion, its header is checked via `header_scan.classify_magic()` rather than dropped. Telegram's real store turned out to be literally named `db_sqlite` (underscore, not `.sqlite`) — invisible to the extension check, and would have been invisible to the earlier bare-filename allowlist too (a per-name list can never anticipate every app's naming convention) — this content-based fallback is what actually generalizes, which is why `_DB_BARE_FILENAMES` was removed once this existed rather than kept alongside it. Scoped to dot-less filenames only (bounded cost; most real files carry a self-describing extension) and capped at `_MAGIC_CHECK_MAX_BYTES` (64MB). Verified end-to-end against the real case: found `db_sqlite` (9.4MB, now ranked #1 and since cited) plus 3 more real Telegram Postbox files, and incidentally also caught Apple's own extensionless `CloudKit/cloudd_db/db` cache. Full-case `scan_apps` with this enabled: 3.9s, no performance concern. **`known_real_store` REMOVED from the live tool** (2026-08-25, one day later, per a further user design question): citing iLEAPP/ALEAPP inline in `evidence_databases`' live output was itself reconsidered — it risks testing whether a hand-fed answer key is right rather than whether the general ranking mechanism (size/WAL/noise-filtering/magic-byte detection) actually works on its own, and iLEAPP/ALEAPP are themselves live GitHub projects that keep changing, so an embedded snapshot would silently go stale. `find_evidence_databases` no longer takes `platform`/`app_id` params or imports any pattern-matching module; `evidence_databases` candidates no longer carry `known_real_store` at all. The mined cross-reference data moved to `scripts/leapp_evidence_fixtures.py` (validation-only, not imported by `app/`) and is now consumed only by `scripts/validate_evidence_ranking.py`, which loads a real case and checks whether the unaided mechanism still surfaces each fixture's known-real file in its own top-N — confirmed genuinely informative, not just theoretical: re-running it after the citation's removal immediately caught a real regression — TikTok's real message stores (`AwemeIM.db`, `ChatFiles/db.sqlite`) no longer rank in the top 5 unaided, buried under `tracker_v3.sqlite`/`tttracker_custom_event.sqlite`/`passportStorage/manifest.sqlite`/a second `tracker.sqlite`/`feature_engineering.db`, all uncited telemetry. `scripts/leapp_coverage_report.py` (same day) generates a LIVE comparison of this project's real parser coverage (`artifacts/ios|android/*.py`) against iLEAPP/ALEAPP's own declared categories, scanned fresh from the local checkouts every run rather than a hardcoded list — the "which apps do they support that we don't yet" roadmap view, deliberately regenerated rather than committed as a snapshot that would go stale the same way an embedded citation would have. **Escalation over silent truncation, and richer known-app info** (2026-08-25, same day, per the direct user objection: "if it silently discards stuff then it is a problem" — a per-app noise-list patch for the TikTok gap above was explicitly rejected in favor of a general fix). `find_evidence_databases` now returns `(candidates, total_found)` — the row's own `evidence_databases_total` field exposes when more candidates exist than the 5 shown, instead of silently cutting them. New MCP tool `list_evidence_candidates(app_id, limit=50)` reuses list_apps' cached container list to page arbitrarily deep into the SAME pool — confirmed closing the TikTok gap exactly as designed: `AwemeIM.db`/`ChatFiles/db.sqlite` rank #7/#12 among 34 real candidates, invisible in the default top-5 but immediately visible via this tool, and independently confirmed message-shaped by schema (`contactName`/`latestChatTimestamp`) against the top-5's telemetry shape (`track_id`/`entire_log`). `list_apps`' own docstring now instructs checking `evidence_databases_total` and escalating rather than concluding "no evidence" when the top 5 all read as telemetry. `scripts/validate_evidence_ranking.py` updated to report an ESCALATE status distinct from a true FAIL for exactly this case. Separately, prompted by "what are the most relevant apps, last used date, what is the app used for" — three fields were missing entirely, not just hard to reach: `display_name` (the real app name, e.g. 'TikTok' — sourced from `app_registry`, already computed but never surfaced; a bare bundle id isn't clear about "what app is this"), `last_activity_utc` (the existing raw `last_activity` nanosecond-epoch integer, now ALSO returned formatted and UTC-labeled via a new Qt-free `_format_last_activity` — the unlabeled-timestamp shape this project's own Conventions section explicitly warns against), and `known_location` (`{app_path_or_group, has_media_fields}` for any `has_parser=true` app, via new `resolve_parser_locations()` — this project's OWN parser already declares exactly where an app's database is and whether it tracks media/attachments, but a parsed row previously surfaced none of that, going quiet exactly where it should have been most informative). Schema bumped v9→v11 across this and the row-merge/escalation work. **`last_activity_data`/`last_activity_shared`** (2026-08-25, schema v11→v12, for the GUI Apps-node table below): the SAME per-member walk that already computes the merged `last_activity` now also buckets each container's own last-touched time by `kind` ('data' vs 'app_group') into two additional running maxes — zero extra file-walk cost, just not collapsing the per-kind values before returning. The pre-existing merged `last_activity`/`last_activity_utc` fields are UNCHANGED (kept for backward compatibility with existing `list_apps` callers) — these are additive, not a replacement. **`embedded_archives` — a real doc/code mismatch found and fixed** (2026-08-30, schema v14→v15, prompted by a direct user design question about whether nested/embedded zip archives could be silently hiding evidence from both existing parsers AND this project's own app-scanning). `find_evidence_databases`'s extensionless-file fallback (see "Row-merge + magic-byte fallback" above) turned out to NEVER actually call `header_scan.classify_magic()` despite its own docstring already claiming it did — it hand-rolled a narrower inline check for JUST the SQLite signature (`header[:16] == b'SQLite format 3\x00'`). Fixed to call the real, general `classify_magic()`: a `'Database'` result behaves exactly as before, but a `'Archive'` result (the file is genuinely a ZIP by its own magic bytes) is now returned as a NEW third value, `embedded_archives` — `find_evidence_databases` is now `(candidates, total_found, embedded_archives)`, a real signature change updated at all three call sites (`scan_apps` here, `list_evidence_candidates` in `mcp_server.py`, `scripts/validate_evidence_ranking.py`). Confirmed both the original gap and the fix directly, not assumed: a synthetic three-file test (SQLite / ZIP / neither, all extensionless) showed the SQLite file still correctly detected, the ZIP file previously silently dropped and now correctly surfaced, and the neither-file still correctly ignored in both lists. Checked against real data too, not just synthetic: all three real archives on hand (Android 14 JoshHickman, Android 15 CTF25 Cellebrite, Android 14 CTF26 Magnet) have 27-90 extensionless embedded archives EACH, confirmed by reading their actual magic bytes — genuinely common, not the "rare" an earlier draft of this fix's own comment first assumed before checking. Every one actually found in these three cases, though, looks like app-internal SDK/ML-model cache (WhatsApp's `wa_bwe_pl_classifier_mobile` bandwidth models, Instagram's `igsignals`/`rtc_automos`/`bwe_mobile_congestion` telemetry, Google Play services' `datadownloadfile_*` delivery cache, LINE's sticker packs) — NOT a confirmed case of real user-facing evidence actually being hidden this way, an important distinction from "the mechanism works" to "this specific gap has cost real casework a real finding so far." Surfaced unfiltered by name anyway, matching this same function's own established stance for `evidence_databases` above (telling real content apart from bundled-SDK noise by filename alone is a guess this function deliberately declines to make); each entry carries `{path, bytes, note}`, the note always pointing at the fix ("extract it — nested-archive extraction — to see what's inside"). This closes the SAME blind spot for whoever is building a NEW parser from GTD + iLEAPP/ALEAPP cross-reference (an AI or an examiner) as it does for the app-scanning tool itself — an app whose real evidence sits inside an unextracted embedded archive would previously read as "no evidence found" with zero signal either way; `list_apps`/`list_evidence_candidates` (which now also surfaces the SAME cached `embedded_archives` list per app, no re-scan needed) both flag it now instead. Deliberately NOT auto-extracted or auto-read here — nested-archive extraction stays the same separate, manual, examiner-triggered step it already was (see the File Browser's own "Extract as Nested Archive" / `ProcessDialog`'s batch extraction); this only makes the PRECONDITION visible where it used to be invisible, the same minimal, non-silent scope the equivalent `artifact_runner.py`-side question (should a PARSER itself be able to declare "I need a nested archive extracted first") was deliberately left at the discussion stage for, pending a real parser that needs it. |

**Evidence-database + encryption-caveat fields** (added 2026-08-23, same day, after live-testing an offline LLM against `list_apps` showed it redundantly looping across tool calls and once fabricating a package name never in the real data — the fix was to have the app pre-compute enough that an AI client's job narrows to reading a short, pre-ranked list, per the design goal in the Conventions score-formula section). `find_evidence_database(container_path, folder_map, ui_metadata)` — Tier 2, name/size-based, no raw content needed — finds the largest `.db`/`.sqlite`/`.sqlite3` file ANYWHERE under a container (deliberately not scoped to a `databases/` subfolder: confirmed Telegram's real store, `files/account1/cache4.db`, sits outside it entirely), filtered against `_DB_NOISE_EXACT`/`_DB_NOISE_SUFFIXES`/`_DB_NOISE_SUBSTRINGS` (filename-based) and `_DB_TRUE_EXCLUDE_PATH_SUBSTRINGS` (path-based) — cross-app telemetry/library/system-cache noise confirmed by hand against real casework this session: Firebase Data Transport, ExoPlayer, App Center, Mixpanel, Firebase Remote Config, Google's WorkManager/notification tables, Kik's own A/B-testing tables, Mapbox's offline map-tile cache, any emoji-picker library, Apple's `Library/HTTPStorages`, and TipKit's `.tipkit/` folder. This list is NOT exhaustive by construction — real false positives were caught and fixed by testing during the same session it was built: Threema's real `threema4.db` initially lost to Mapbox's `mbgl-offline.db` on raw size, MeWe's real `app_v3.db` lost to an `emoji.db` cache. **WebKit is deliberately NOT a true exclude** (`_DB_NOISE_PATH_SUBSTRINGS` was retired in favor of splitting it): a design discussion the same day raised a real risk that a blanket WebKit exclusion would silently hide real content for an app actually BUILT as a wrapped web view (a Cordova/Ionic-style "simple web app"), where WebKit's own LocalStorage genuinely IS the app's data — a false negative, worse than the false positives this filtering exists to catch. Only the genuinely OS-owned `com.apple.SafariViewService` silo (confirmed as where Discord's own WebKit noise actually lived — a SEPARATE container from the host app's own WebKit storage) is a true exclude; the app's-own `/webkit/` paths are instead DEPRIORITIZED (`_DB_DEPRIORITIZED_PATH_SUBSTRINGS`) — used only as a last-resort fallback, returned with a non-null `note` explaining the lower confidence, never silently hidden. Confirmed against real data that this is necessary, not theoretical: Discord's lack of any real local database was masked by FOUR successive layers of iOS system/WebKit cache files each winning on size once the previous one was excluded, before this split existed. As a further partial mitigation, `list_apps`' docstring tells the caller to weigh a very small (<50KB) `evidence_database` hit with more skepticism than a large one — every confirmed-noise pick found this session was small, every confirmed-real message store was substantially larger. `find_webview_storage(container_path, folder_map, ui_metadata)` — added the same day, a SEPARATE detection (not a read) for Chromium-style WebView local storage (IndexedDB/LevelDB — a folder of `.ldb`/`.log`/`MANIFEST-*`/`CURRENT` files, a completely different format `find_evidence_database`'s extension scan can't see at all), confirmed present in real Android casework under `app_webview/Default/Local Storage/leveldb/`. Presence-only by design: actually reading LevelDB's raw format would need vendoring a real reader (iLEAPP/ALEAPP both carry `ccl_leveldb.py`, same CCL Forensics lineage as `ccl_abx.py` — a credible future addition), and making Chromium's IndexedDB encoding on top of that meaningful needs a much larger, separately-maintained tool (`mister_skinnylegs`) — both explicitly out of scope for this pass. `find_encryption_caveat(app_id)` — a short, explicitly-sourced list (`_KNOWN_ENCRYPTED_APPS`) flagging apps whose local store is confirmed (Signal — SQLCipher, well documented) or reasonably inferred (Session, same codebase lineage) to be encrypted at rest; a real `evidence_database` hit on one of these is genuine evidence, just not a quick parser win. `scan_apps`'s final sort tie-breaks on these beyond raw score — same score, a CLEAN (no-note) `evidence_database` outranks a note'd/WebKit-fallback hit or a `webview_storage`-only hit, which both outrank neither; a `encryption_caveat` is pushed down; then `recently_used`; then size — so the front of `list_apps`' returned list is already the answer, not just a ranked list needing further investigation.

**`find_evidence_database` → `find_evidence_databases` redesign** (2026-08-24, same day as the LaunchServices work above, prompted directly by a user design question: "is [noise-filtering by name] something the code should not do — is this not something the LLM would be better at?"). Confirmed on the iOS 16.5 CTF23 Cellebrite image that the single-winner design above is an UNBOUNDED whack-a-mole, not an almost-finished list: even after adding four more confirmed-noise entries the same day (`heimdallr.db` — ByteDance's own crash/perf SDK, shadowing TikTok's real `AwemeIM.db`; `cache_controller.db`, shadowing Snapchat's real `arroyo.db`; `unifystorage.sqlite`; a `logstoreprovider` path exclude, shadowing Instagram's real `DirectSQLiteDatabase/*.db`), TWO MORE unrelated SDK-telemetry files (`tracker_v3.sqlite`, `time_in_app_*.db`) immediately took their place on the same two apps. Telling real app content apart from a bundled SDK's own analytics file by filename alone needs either a citable cross-reference or a content peek — not a filename guess — so the function's job was narrowed instead of extended further: `find_evidence_databases` (`app_intelligence.py`) now returns the **top 5 candidates**, not one winner, each carrying `bytes`, `wal_bytes` (reported SEPARATELY, not merged — a real hit can be a near-empty base file with all its content in an active WAL, confirmed on Instagram's real store: 4KB base + 1.4MB `-wal`), `wal_present`, `shm_present`, and, until it was removed 2026-08-25 (see the `app_intelligence.py` entry above), `known_real_store`. Deciding WHICH candidate is real is deliberately left to the caller (an LLM, optionally verifying via `get_sqlite_schema`, or an examiner) — code no longer tries to settle that by itself beyond the same zero-ambiguity true-exclude list as before (platform-level noise like `cache.db`/TipKit/WebKit `HTTPStorages`, confirmed never content for ANY app, kept as hard excludes — these aren't guesses, so removing them would only re-clutter the list). `evidence_database` (singular) is gone from `app_intelligence`'s stored rows and `list_apps`' output; callers now read `evidence_databases` (plural, a list, casecache.db schema v7). |

**`known_evidence_patterns.py`** (added 2026-08-24, alongside the redesign above; REMOVED 2026-08-25 — see the "`known_real_store` REMOVED from the live tool" note in the `app_intelligence.py` entry above; kept here for historical context on why the cross-reference existed and what replaced it). A small, explicitly-sourced cross-reference — NOT vendored code, just glob PATTERNS extracted as plain data — mined by hand from two local, MIT-licensed checkouts of public forensic tools' own `scripts/artifacts/*.py`: iLEAPP (iOS) and ALEAPP (Android), both by Alexis Brignoni. `match_known_pattern(platform, app_id, ui_path)` returns a citing source string (e.g. `'iLEAPP:instagramThreads.py:instagram_threads'`) when a path matches a known-real pattern for that app, else `None`. Deliberately covers only apps with an INDEPENDENTLY CONFIRMED bundle id/package name (either matched against this project's own real case data, or a literal match in the source tool's own `sample_data` comments) — never guessed; a couple of apps checked this session (Kik, MeWe) were left OUT specifically because their exact iOS bundle id couldn't be confirmed this way. `find_evidence_databases` ranks a `known_real_store` match ABOVE raw size — a small but cross-referenced hit beats a large but unidentified one. Extend by hand as more apps get worked, same convention as `app_intelligence.py`'s noise-exclusion lists — this table is deliberately NOT exhaustive by construction.
**iOS app registry (LaunchServices)** (added 2026-08-23). Every iOS
container-GUID-to-bundle-id link used across this project (`guid_to_bundle`,
consumed by `ffs-explorer.py`'s UUID column, `artifact_runner._resolve_app_group_base`,
and `app_intelligence.py`'s App-Group scoring above) previously had exactly
one source: each container's own
`.com.apple.mobile_container_manager.metadata.plist`
(`_build_guid_bundle_map`, `adapters/ffs.py`) — confirmed, from real
casework, to sometimes be **missing on GrayKey extractions**, silently
leaving those containers unresolved. Separately, that per-container plist
can only ever produce a bare GUID→bundle-id pair — it has no display name,
Team ID, App-Group membership, or PluginKit-extension linkage to offer.

Both gaps are solved by a single richer source: **`com.apple.LaunchServices-<version>-v2.csstore`**
(version number varies per build — 5019 on the iOS 17 JoshHickman image,
6012 on iOS 18 CTF25 Magnet — so it's glob-matched, never hardcoded),
found at `.../Containers/Data/InternalDaemon/<GUID>/Library/Caches/`. A
`SystemDataOnly-`-prefixed sibling (a smaller subset) exists alongside it;
the full, non-prefixed file is preferred when both are present. This is Apple's own central,
per-device app registry — one file instead of one per container — parsed
via vendored `app/csstore.py` (see its own table row above). Three record
shapes matter, all reverse-engineered fresh this session against real data
(WhatsApp/Discord/Signal/Threema), not carried over from any third-party
higher-level wrapper:

- **`Bundle` table**: one fixed-layout record per installed app/extension.
  Verified field offsets: `0` = Bundle-container `Alias` key (→ full path,
  GUID included), `8` = display name (`<string>` table), `12` = bundle id,
  `16` = Team ID, `35×4` = entitlements `PropertyList` key, `36×4` = this
  bundle's own App-Group-path `PropertyList` key, `96` = Data-container
  `Alias` key. Only these seven fields of the ~550-byte record are decoded
  — the rest is unmapped and out of scope. `_extract_app_registry_from_launchservices`
  (`adapters/ffs.py`) walks this table and **deduplicates by bundle id**,
  preferring whichever entry has a non-empty `data_container_path`: Apple's
  own built-in apps (Weather, Calculator, Health, Maps, …) are registered
  TWICE — once for real, once as an empty `/System/Library/AppPlaceholders/<Name>.app/`
  stub — a real, 100%-consistent pattern confirmed by inspecting all 33
  duplicate pairs on the iOS 17 image before fixing it, not papered over
  with `INSERT OR REPLACE`. 306 raw Bundle records → 273 clean rows on that
  image; 258 clean rows on iOS 18 CTF25 Magnet with zero duplicates at all
  (both platforms' placeholder patterns confirmed, not assumed identical).
- **`PropertyList` table, App-Group-path records**: plain (non-NSKeyedArchiver)
  `bplist00` blobs whose keys are *all* `"group."`-prefixed strings, mapping
  directly to each group's own container path (GUID included) —
  `_resolve_app_group_paths` scans the whole table for this shape.
- **`PropertyList` table, entitlements records**: a cached copy of an app's
  own code-signing entitlements (Team ID, App-Group list), reachable via a
  Bundle record's offset-35×4 key, as a plain bplist with one quirk — a
  literal 4-byte `"lnch"` prefix before the real `bplist00` magic
  (`_resolve_app_group_entitlements` strips it before `plistlib.loads`).
  Some decoded key names come out as bplist string-sharing artifacts
  (garbled, not the real entitlement key) — the extraction deliberately
  scans list **values** for a `"group."` prefix rather than trusting any
  key name. Cross-validated byte-for-byte against WhatsApp's independently
  known 5-group membership (already hardcoded in `artifacts/ios/whatsapp.py`)
  — confirming this LaunchServices-only path is sufficient on its own, with
  **no Mach-O binary reading and no NSKeyedArchiver decoding anywhere in
  the final design** (an earlier Mach-O-tail-read approach was verified
  working but dropped once this turned out to reach the identical data
  through the one file already being opened).

**Sequencing**: `ffs_metadata.py`/`ffs-explorer.py` now run
`FfsAdapter.build_app_registry()` FIRST during first-open metadata parsing
— one parse of one file yields bundle id, both container GUIDs, and
App-Group/extension links for the large majority of apps in a single pass,
merged into both the new `app_registry` table and `guid_to_bundle`. The
original per-container-plist method (`_build_guid_bundle_map`) still runs
afterward as a narrower top-up, scoped to whatever's still unresolved — it
can only ever contribute a bare `guid_to_bundle` entry, never an
`app_registry` row, since that's structurally all a per-container plist
ever contained. Persisted via `db_utils.save_app_registry`/`load_app_registry`
(schema v6). Android takes neither path — no GUID-container indirection
exists there — `build_app_registry` returns `([], {})` immediately.

| `ccl_abx.py` | Vendored Android Binary XML (ABX) decoder (added 2026-08-23) — MIT, CCL Forensics, lifted from ALEAPP's `ilapfuncs.py` `abxread()`, same vendoring convention as `app/ccl_segb/` (original license header kept verbatim in-file). `is_abx`/`abx_bytes_to_xml_root` are this project's own thin additions (not vendored) — the original takes a file path and opens it itself; callers here already have the bytes via `ctx.read_bytes`. Used by `app_intelligence.py` for `packages.xml`/`runtime-permissions.xml`, which are ABX on some Android devices/builds and plain XML on others — see that row for how this was discovered. |
| `validation_store.py` / `parser_validation.py` | Parser validation baselines: a one-time snapshot of a parser's SQLite schema + a *generalized* folder-structure fingerprint (see Conventions below), recorded against the specific GTD-documented image a parser was built/checked against. `validation_store.py` is the cross-case JSON store (`config/parser_validation.json`, same dev/frozen-path convention as `research_store.py`), keyed `"{platform}:{script_name}"` since e.g. `ios:whatsapp`/`android:whatsapp` are different apps sharing a filename. `parser_validation.py` has the actual snapshot/diff/render logic; `ArtifactViewerMixin._art_show_validation` (`artifact_viewer.py`) is the "Validation" tree leaf per parser — diffs the current case against the recorded baseline, or offers to record one (an explicit action, never automatic) |
| `mcp_server.py` | Read-only MCP server (tools + prompts) over processed case data; Qt-free; audit-logs every tool call to `run_log` (run_type `mcp`). Tier 2: `list_apps` (added 2026-08-23) wraps `app_intelligence.scan_apps` — cached in `casecache.db`'s `app_intelligence` table, recomputed when the archive's indexed file count OR raw_content_enabled state has changed since the last scan (`app_intelligence_scan_key`, a `blobs` entry — not a new schema concept); works with or without raw content access, degrading gracefully rather than erroring. Note: the first scan of a large case walks every file under every app container in pure Python (~830k entries took roughly a minute in the case this was built against) — acceptable as a one-time cached cost, same tradeoff this project already made for media-thumbnail pre-warming (see `artifact_media.py` above), but worth knowing before assuming a slow first call is a hang. Tier 3 (opt-in, separate consent checkbox): `get_sqlite_schema`/`sample_sqlite_rows` extract any archive SQLite db to a locked-down read-only temp copy — no arbitrary raw SQL, no generic file-read tool. `build_artifact_parser(bundle_id)` prompt chains them into a drafted `artifacts/ios\|android/`-format parser for human review. `get_app_data_locations(bundle_id)` (added 2026-08-23) is a thin direct read of the already-built `app_registry` table — Bundle container, Data container, every App Group path, every PluginKit-extension bundle id, in one call, no fresh parsing at call time; the direct answer to "I don't care which folder holds it, I want everywhere this app's data could be". `get_ai_summary_settings(name)`/`set_ai_summary_settings(name, ...)`/`run_ai_summary(name)` (added 2026-08-29) expose `ai_summary.py`'s report-summarization pipeline over MCP — an AI client can read/tune a report's column selection, chunk size, time-gap threshold, and prompt template, then trigger a run; these are the SAME settings (`ai_summary_store.py`) the GUI's `AISummaryDialog` (`artifact_viewer.py`) edits, so a change from either surface is visible to the other |
| `mcp_control.py` | Lifecycle for the embedded MCP server: uvicorn on a daemon thread, 127.0.0.1 + per-start bearer token (regenerated every start by default); lazy-imports mcp/uvicorn (optional deps). Opt-in **dev mode** (Preferences ▸ AI Access ▸ "Developer mode", off by default, own warning text) passes `persist_dev=True` into `start()`, which instead reuses a port+token saved plaintext in `config/dev_mcp_credentials.json` (gitignored) — so an external client's `claude mcp add`/mcp.json only needs entering once instead of after every app restart. Never enable outside a machine you control |
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
- **`WRITING_ARTIFACT_PARSERS.md`** (repo root) — the example-driven,
  examiner-facing how-to for writing a new parser script; check this file
  before re-deriving "how do I add a new artifact parser" from the
  `artifact_runner.py` docstring or CLAUDE.md's Conventions section alone
  — it's the one meant to be read first, and both of those exist to go
  deeper once you already know a field exists. Keep it in sync with the
  Conventions section below whenever a parser-facing convention changes —
  it drifted out of date once already (missing `hidden_fields`/
  `record_source` for a few hours on 2026-08-22) specifically because
  nothing pointed back at it from here.

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
    code: `"s"`, `"ms"`, `"cocoa_s"`, `"cocoa_ns"`, `"webkit_us"` — the
    last added 2026-08-27 for Chrome's own SQLite stores, see the Chrome
    artifacts entry below), matching this
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

    **`'webpage'` kind added 2026-09-01**, a genuinely new rendering
    dependency taken on directly (unlike the PDF non-decision just above)
    — prompted by the user asking specifically for Chrome Offline Pages'
    `.mhtml`/`.mht` archives (`archive_ui_path`, `chrome_offline_pages.py`)
    to open as an actual rendered page on double-click instead of raw MIME
    source. Split out of the `.mhtml`-as-`'text'` classification added
    2026-08-27 (`header_scan.WEBPAGE_ARCHIVE_EXTENSIONS`, now separate from
    `TEXT_ATTACHMENT_EXTENSIONS`) into its own kind;
    `MediaFullViewDialog._build_webpage` renders it via `QWebEngineView`,
    same read-only-scratch-copy-then-cleanup pattern `_build_video` already
    uses for `QMediaPlayer` (WebEngine needs a real `file://` URL to parse
    MHTML's multipart/related structure — `setHtml()` only understands
    plain HTML, not the archive format itself). JavaScript and remote/
    local-file network access are explicitly disabled on the view
    (`QWebEngineSettings` — `JavascriptEnabled`/`LocalContentCanAccess
    RemoteUrls`/`LocalContentCanAccessFileUrls` all `False`) — a forensic
    snapshot should never execute embedded script from evidence, and an
    MHTML archive is self-contained by construction (every real resource
    already inline), so this costs no legitimate rendering while stopping
    a beacon/tracking request from silently firing the moment an examiner
    opens one. Verified against a real archive from this project's own
    JoshHickman casework, not a synthetic file: a real 152KB snapshot
    (Wickr's OAuth sign-in page, cited in the parser's own description)
    loaded successfully with JS off (`loadFinished: True`) and the page
    title read back correctly as "Signin" — proof the static render
    actually works under the lockdown settings, not just that the dialog
    opens without crashing. **Frozen-build risk flagged, not yet
    verified**: unlike `QtMultimedia` (already shipping in this same
    dialog, confirmed working in the frozen exe without any special
    listing), `QtWebEngine` bundles a genuinely separate helper process
    plus its own resource/locale `.pak` files — a real, previously-
    reported PyInstaller packaging gap class for this specific Qt module.
    `PySide6.QtWebEngineWidgets`/`QtWebEngineCore` added to
    `ffs_explorer.spec`'s `hiddenimports` out of caution, but this has
    only been confirmed in dev/venv mode — check the next Windows CI
    build actually renders an `.mhtml` archive, not just that the exe
    launches, before trusting it.

    **The bottom "File Preview" Text tab was completely unwired, found
    the same day**: asked to make the Text tab show the full `.mhtml`
    source for Chrome Offline Pages specifically, investigation found
    the gap was much bigger — `text_view`/`_load_text_preview`
    (`hex_viewer.py`) had ZERO callers anywhere in the codebase. The tab
    existed, with its own placeholder text promising "Decoded text
    content of the selected file appears here," but nothing had ever
    actually populated it, for ANY file type, not just MHTML — dead UI,
    not a Chrome-specific bug. Fixed at the shared root rather than
    narrowly for one parser: new `_sync_text_preview(data, label)`
    keeps the Text tab in step with whatever `_load_hex_preview_from_
    bytes`/`_load_hex_preview_from_bytes_at` just loaded (both call
    sites — every Artifact Viewer Attachment/Record-mode hex load goes
    through one of these two), classifying via the same `sniff_media_
    kind` the double-click dialog above already uses. `'text'`/`'webpage'`
    decode the FULL buffer with NO size cap — deliberately not reusing
    `is_text()`'s own `TEXT_SIZE_LIMIT`-gated path directly, since a
    multi-megabyte `.mhtml` archive is exactly the case that gate exists
    to protect the OTHER (magic-byte-fallback) classification path from,
    not something to truncate here. Never switches to the Text tab
    itself — populated so it's ready the moment the examiner switches
    there themselves, matching how they already described the existing
    workflow, rather than yanking focus off Hex. Anything non-text
    (image/video/pdf/a binary database file) clears the tab instead of
    decoding replacement-character noise — cheap even for a large binary
    file, since `sniff_media_kind`'s magic-byte/`is_text` fallback only
    ever runs below `TEXT_SIZE_LIMIT`, short-circuiting a multi-megabyte
    database to `None` without scanning it. `_clear_hex_preview` now
    clears the Text tab too, so the two never fall out of step — one
    showing a previous file's stale content after the other has already
    moved on. Verified against real data: the same 1.8MB real MHTML
    archive decoded to its full 1,811,308 characters, ending exactly at
    the archive's own closing MIME boundary marker (not silently cut
    short); a real 229KB SQLite database (`History`) correctly classified
    as `None` rather than being decoded as garbage text.
    Five parsers use this convention so far, each confirmed end-to-end
    against real archive bytes, not just plausible-looking code:
    `artifacts/ios/whatsapp.py` (`attachment_path` — first user; the
    non-obvious `Message/` path segment `ZWAMEDIAITEM.ZMEDIALOCALPATH`
    needs prepended to the container base that a bare extension-based join
    would miss. Falls back to `ZWAMEDIAITEM.ZXMPPTHUMBPATH` — a separate,
    smaller pre-generated preview column — when `ZMEDIALOCALPATH` is NULL
    (the full-size media was never/no-longer cached locally, e.g. an
    iCloud-synced photo never opened on-device); confirmed against a real
    extraction where exactly that happened — a `.thumb`-extensioned file
    whose magic bytes are a complete, ordinary JPEG (`ff d8 ff e0 ...`
    `JFIF`/`Exif` markers), not a different format or a broken/partial
    file, under the same `Message/` + App Group base convention as the
    full-size case. `.thumb` was also added to `media_viewer.MEDIA_EXTENSIONS`
    for this same reason — the Media Browser's own folder-listing filter is
    extension-gated for performance and never falls back to sniffing magic
    bytes the way the Artifact Report thumbnail pipeline already does),
    `artifacts/ios/photos_metadata.py` (`attachment_path` —
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
  - **Artifact tree "Apps" node** (`artifact_viewer.py`, added
    2026-08-25): `app_intelligence.scan_apps()` — the per-app inventory
    built for the MCP `list_apps` tool — was AI-only until this change; an
    examiner not using AI access had no GUI surface for it at all. The
    tree's top level is now a single bold "Apps" node
    (`_ART_APPS`/`_art_show_apps`), whose own click loads every gathered
    app (parsed or not) into the Report table widget via list-mode
    (`ArtifactTableModel.load_rows`, the same mechanism `_art_show_files`
    already used for a non-DB-backed table) — reusing, not duplicating,
    `mcp_server.CaseContext` and the SAME `casecache.db` `app_intelligence`
    cache table/staleness key `list_apps` reads and writes, so a scan
    triggered from the GUI and one triggered later by an AI client share
    one result instead of each re-walking the archive (up to ~1 minute on
    a large case — see the `mcp_server.py` row above). Runs on a
    background `QThread` (`AppIntelligenceWorker`) for that reason; a
    cache hit still returns synchronously, same as `list_apps`' own
    cache-hit branch. Deliberately `raw_content_enabled=True`
    unconditionally for this path (unlike the AI-access consent toggle,
    which defaults to whatever the examiner chose) — there's no
    AI-consent boundary to protect for the GUI's own use of its own
    opened archive, and the cache key already includes
    `raw_content_enabled`, so a GUI scan and a later consent-restricted
    MCP scan never silently share a result across that boundary.

    Every completed-parser group (formerly a flat top-level tree item)
    moved to be a CHILD of "Apps" instead — clicking the app's own name
    now shows its Report directly (`_ART_GROUP`'s click handler calls
    `_art_show_report`), replacing the old separate "Report" child row;
    the remaining children (Report Notes/Warning, Script, Source in ZIP,
    Exported Files, Validation) stay, but only the Apps node itself starts
    expanded (`expandAll()` removed) — the per-app detail children stay
    collapsed until deliberately opened, so the tree reads as "Apps → the
    ones with a report" at a glance rather than always sprawled open.

    **Redesigned around `app_registry` as the starting point, not
    `app_intelligence`'s own merged fields** (2026-08-25, same day, per
    direct user design guidance after seeing the first version: "did we
    not use [the LaunchServices csstore] as the original source of the
    app data? ... app name, bundle id, shared data folder, data folder,
    plugin"). `_flatten_app_intelligence_row` turns one `scan_apps()` row
    into one flat table row — deliberately never a raw list/dict cell,
    but a cell with genuinely more than one real value (an App-Group
    folder, a PluginKit extension) is comma-joined rather than truncated
    to the first, per direct instruction — confirmed on real casework that
    truncating would be a real loss: WhatsApp alone has 5 real App-Group
    folders and 6 real PluginKit extensions. Columns, in the requested
    order: App, Bundle ID, Shared Data Folder, Data Folder, Plugin(s),
    Total Size, Last Activity (Data Folder), Last Activity (Shared
    Folder), Score, Has Parser, Category. The old Location/Evidence DBs
    (Total)/Recently Used/Webview Storage/Hidden Vault Storage/Encryption
    Caveat columns were dropped from this GUI table per the same
    instruction ("the rest can be kept for AI only") — still returned by
    the MCP `list_apps` tool, untouched, just not duplicated here.

    Bundle ID/Shared Data Folder/Data Folder/Plugin(s) are sourced from
    `app_registry` (`db_utils.load_app_registry`, built once per case load
    from the device's own LaunchServices csstore — see "iOS app registry
    (LaunchServices)" below) via a new `_build_app_registry_lookup()`,
    NOT from `app_intelligence`'s own `containers`/`known_location`
    fields — falling back to `containers` only when no app_registry row
    exists for that identity (Android, always, since app_registry is
    iOS-only; or an unlinked App-Group on iOS). `data_container_path`/
    `app_group_paths` (`{group_id: guid}`) come straight off the registry
    row; `_container_path_to_ui_path()` normalizes the registry's raw
    on-device absolute path (`/private/var/mobile/Containers/Data/
    Application/<GUID>/`) into this project's own ui_path convention —
    confirmed byte-for-byte against real Cellebrite casework that
    stripping a leading `/private/var` and trailing slash exactly matches
    the same container's path as it already appears in `containers`; NOT
    independently verified against GrayKey (mirrors the same
    unconditional convention `artifact_runner._resolve_app_group_base`
    already uses for an App-Group path, so any GrayKey gap here is
    pre-existing, not new). App-Group container ui_paths are built
    directly from each entry's own GUID (`mobile/Containers/Shared/
    AppGroup/<GUID>`, same literal convention as
    `_resolve_app_group_base`) — confirmed on real data these can be a
    SUPERSET of app_intelligence's own merged `containers` (WhatsApp: 5
    declared entitlements in app_registry vs 4 physically-merged
    containers) since the registry reflects DECLARED entitlements, not
    just ones with an actual folder on this specific device; shown as-is,
    not filtered down to only what's physically present, since a declared
    App-Group with no folder is itself real signal, not noise.

    **Plugin(s) is deliberately NOT sourced from app_registry**, despite
    everything else being registry-first — confirmed by direct testing
    that app_registry does NOT reliably carry PluginKit extension bundle
    ids at all: WhatsApp's 6 real extensions (ServiceExtension,
    NotificationExtension, ShareExtension, TodayExtension, IntentsUI,
    Intents) are entirely absent from app_registry on real casework, since
    the csstore's own Bundle table simply has no row for them. Built
    instead from the CURRENT `scan_apps()` result being displayed (the
    same dotted-suffix convention — `net.whatsapp.WhatsApp.ShareExtension`
    under host `net.whatsapp.WhatsApp` — just checked against the right
    source, since app_intelligence already gives every PluginKitPlugin
    container its own row independent of the csstore — see the
    `app_intelligence.py` row's own note on why an extension is never
    merged into its host's identity).

    **Total Size/Last Activity (Data Folder)/Last Activity (Shared
    Folder)** still come from `app_intelligence.scan_apps()` (`total_bytes`,
    and two NEW fields — `last_activity_data`/`last_activity_shared`,
    schema v12, `casecache.db`'s `app_intelligence` table — added
    alongside the pre-existing merged `last_activity` rather than
    replacing it, so an existing MCP `list_apps` caller reading the merged
    field is unaffected). Computed in the SAME per-container walk
    `scan_apps()` already does for the merged value — each member
    container's own `_walk_container()` result is bucketed by its `kind`
    ('data' vs 'app_group') into a second running max, alongside the
    existing merged one, at zero extra file-walk cost. Confirmed on real
    data these genuinely differ per app (WhatsApp: Data folder last
    touched 2023-06-28 00:05:39 UTC, Shared folder last touched
    2023-06-28 00:06:00 UTC — 21 seconds apart, a real distinction the
    single merged value would have hidden).

    **Total Size display, since revised** (2026-08-25, same day, per
    direct user feedback: "some byes come megabt or kilo can you make it
    all megabytes"): the first version formatted with
    `keyword_search.py`'s own auto-switching KB/MB/GB/TB convention
    (`_fmt_size`), which meant two rows' sizes weren't directly comparable
    without a unit conversion in the examiner's head. Fixed unit — always
    MB — instead. This also surfaced a second, more subtle bug the same
    day: `_flatten_app_intelligence_row` used to bake the formatted string
    ('51.80 MB') into the stored cell value, which `ArtifactTableModel
    .sort()`'s freshly-fixed list-mode branch (see this same Apps-node
    entry's own earlier "Two bugs found" paragraph) would then sort
    LEXICOGRAPHICALLY, not numerically —
    '10.00 MB' sorting before '9.00 MB'. Fixed the same way this project
    already handles timestamp columns: `_flatten_app_intelligence_row` now
    stores the RAW byte count (int); `ArtifactTableModel` gained a new
    `set_byte_columns(names)` (mirrors `set_timestamp_formatting`'s exact
    raw-value/format-at-`data()`-time split, same reset-per-load
    lifecycle) that MB-formats only for display, keeping the underlying
    value numerically sortable. `_populate_apps_table` calls
    `set_byte_columns(["Total Size"])` right after `load_rows()`. Verified
    directly against real data: sorting Total Size descending now produces
    a genuinely descending byte sequence (1,058 MB → ... → 232 MB in the
    top 10 on the real CTF23 case), not a string-sorted one.

    Two bugs found and fixed by direct testing shortly after shipping the
    above, 2026-08-25 same day: (1) `_art_show_apps`'s slow (fresh-scan)
    path updated `_art_row_label`/status bar text but never switched
    `_art_stack` off the index-0 blank placeholder page until the scan
    finished — since `_art_row_label` itself lives on the report page
    (index 1), this meant "Scanning apps…" was set but never actually
    visible, and on any case needing a real scan (first time, or a stale
    cache) clicking Apps looked like it did nothing for up to the ~1
    minute the scan takes. Fixed by switching the stack immediately when
    the scan starts, not just on completion. (2) `ArtifactTableModel.sort()`
    silently no-op'd for List mode (`if not self._conn: return`) — it was
    only ever implemented for DB mode's SQL `ORDER BY`, so clicking a
    column header did nothing for any list-mode table, not just Apps
    (Exported Files too, just never noticed there). Fixed by adding a
    Python-side `sorted()` branch for list mode, keyed `(0, '')` for
    blank/None vs `(1, value)` for a real value so type-mixed columns
    (a blank cell alongside real ints/strings) sort correctly without
    a raw comparison ever touching an empty string against a number.

    **Default view on a fresh case load** (same day): nothing is selected
    in the tree the moment a case first opens, so `ffs-explorer.py`'s
    archive-open completion handler now calls
    `ArtifactViewerMixin._art_select_and_show_apps()` right after
    `_refresh_artifact_tab()` — selects the Apps tree node and shows its
    report, replacing the blank "Select a Report or Script from the tree"
    placeholder as the first thing an examiner sees. Deliberately NOT done
    unconditionally inside `_refresh_artifact_tab()` itself, even though
    that method also reruns after a parser finishes
    (`parsers_completed` → `_refresh_artifact_tab`): forcing a switch to
    Apps on every rebuild would discard whatever report the examiner had
    open mid-session — the exact bug class this project already fixed
    once before (`_on_center_tab_changed` used to call
    `_refresh_artifact_tab()` unconditionally on every tab switch too,
    "discarding whichever report was open" — see the Per-tab state
    section above). Confirmed by direct testing that a second refresh
    with no explicit re-select (simulating a parser finishing while
    something else is open) leaves the stack on its existing reset state
    rather than forcing Apps back to the front.
  - **AI Summary GUI dialog** (`AISummaryDialog`/`AISummaryWorker`/
    `ModelListWorker` in `artifact_viewer.py`, added 2026-08-29): the
    Artifact tree gained a second always-present top-level sibling next to
    "Apps" — "AI Summary" (`_ART_AI_SUMMARY`). Unlike every other tree
    node, clicking it opens a modal dialog directly (`_on_art_tree_clicked`
    → `ArtifactViewerMixin._open_ai_summary_dialog`) rather than showing a
    page in `_art_stack` — it has no per-report children of its own the
    way a parser group does, so there's nothing for the stack to hold.
    `_art_select_and_show_apps`'s "Apps is always child(0)" assumption
    still holds — AI Summary is always appended right after Apps as a
    second sibling, never before it.

    The dialog lets an examiner pick which already-completed report to
    summarize (a combo box built the same way `_refresh_artifact_tab`
    enumerates completed parsers — never duplicated tree-walking logic),
    check/uncheck which of its columns get sent, set the chunk-size/
    time-gap thresholds that drive `ai_summary._chunk_by_time_gap`'s
    splitting, edit the prompt template (validated for a `{data}`
    placeholder before saving or running, same rule `ai_summary.run_summary`
    itself enforces), and configure the local LLM connection (endpoint/API
    key/model, with a "Refresh Models" button hitting `local_llm.list_models`
    on its own `ModelListWorker` thread rather than the GUI thread). Reads/
    writes settings through `ai_summary.py`/`ai_summary_store.py` directly
    — the SAME modules `mcp_server.py`'s AI Summary tools use — so a
    setting saved from the GUI and one saved by an AI client via MCP are
    the same setting, never two independent copies, and a run triggered
    from either surface sends exactly the same data the same way.

    "Generate Summary" runs `ai_summary.run_summary` on a background
    `AISummaryWorker` `QThread`, never the GUI thread — a real run is
    several sequential local-LLM calls (confirmed by direct testing to
    take anywhere from under a minute to several minutes depending on how
    fragmented the data is), so this follows the same long-work-off-the-
    GUI-thread rule as every other worker in this file. The report picker
    and action buttons disable while running, and `closeEvent` refuses to
    close the dialog mid-run (identical guard to `ArtifactRunnerDialog`
    above — the run keeps going in the background either way; this only
    stops the examiner from losing track of it or double-triggering a
    second run). On success, shows the final narrative plus an optional
    "Show per-chunk detail" toggle (the individual mini-summaries, for the
    same transparency/verification reason `run_summary` itself returns
    them); on a reduce-step failure, shows whichever per-chunk mini-
    summaries DID complete instead of losing them, mirroring
    `run_summary`'s own `completed_chunks` fallback in its error return.
    Verified end-to-end against real case data (not just constructed
    plausibly): settings load/save round-trips correctly (an unchecked
    column is confirmed excluded from what's persisted), and a real
    "Generate Summary" run against a live LM Studio server completed and
    displayed the correct narrative for the exact row in the case's own
    `caseresults.db`.
  - **AI Summary as an app-group's root view, and intent-focused prompts**
    (2026-08-29, same day, per direct feedback that the pre-existing
    `chrome_overview.py` one-row counts dashboard "does not really add
    anything"). That parser was REMOVED entirely — `is_group_overview`
    moved to `chrome_web_history.py` instead, alongside a new module-level
    `group_overview_mode = "ai_summary"`: clicking the bold "Chrome" parent
    tree node now shows that report's persisted AI Summary (rendered as
    markdown/rich text via a new `QTextBrowser.setMarkdown()` page,
    `_art_stack` index 5) rather than a raw stats table. `_on_art_tree_clicked`'s
    `_ART_APP_GROUP` branch checks the designated overview member's
    `group_overview_mode` attribute and dispatches to
    `ArtifactViewerMixin._art_show_ai_summary_panel` instead of
    `_art_show_report` when it's set to `"ai_summary"` — a generic
    mechanism any future multi-report app-group can opt into the same way,
    not a Chrome-specific hardcode. A report with no summary generated
    yet shows an explanatory message ("Open AI Summary from the tree...")
    rather than a blank page. New persistence in `ai_summary.py`
    (`save_summary`/`load_summary`, `caseresults.db`'s `ai_summaries`
    table — see the databases section above) means a summary generated
    through EITHER surface — the GUI's `AISummaryDialog` or the MCP
    `run_ai_summary` tool — is immediately visible to the other, and
    survives closing/reopening the case.

    Separately, prompted by a direct design question — "is [a summary
    that conveys apparent intent/activity, not just a literal row
    restating] possible without hallucinating?" — both `ai_summary_store.
    DEFAULT_PROMPT` (per-chunk) and `ai_summary._REDUCE_PROMPT` (final
    narrative) were rewritten around one specific, narrow permission: the
    model MAY characterize what something is ABOUT (its subject/topic)
    when the row's OWN content (a title, URL, search term, or message
    text) directly supports that — e.g. a news article's own title states
    its subject — but MUST NOT guess at WHY it happened, mood, or motive
    beyond what's shown, and every timestamp/URL/name still must be
    copied verbatim (the original anti-fabrication rules, unchanged). Both
    prompts also now explicitly ask for flowing prose instead of a
    markdown table restating the input, and to avoid repeating "the user"
    in every sentence — matching direct feedback that literal, "the user
    did X, the user did Y" phrasing read as a data dump rather than a
    written report. Verified genuinely non-hallucinatory by direct testing
    against real data, not assumed from the prompt wording alone: re-ran
    the full pipeline against Joshua Hickman's documented Android 14
    image's `chrome_web_history` (61 rows, 7 chunks) and checked the
    resulting narrative's specific claims against the real database
    row-by-row. Every checked claim held, including two non-obvious,
    genuinely-grounded inferences that could easily have looked like
    fabrication if not checked: "a Gmail-derived link" (the real URL
    literally contains `source=gmail`) and "Google's login and OSID
    endpoints" (the real redirect chain literally hits a `SetOSID`
    endpoint) — both are the model reading real query-string/URL content,
    not inventing plausible-sounding detail. One minor imprecision found:
    "two hours later" for a 16:33→18:51 gap (actually ~2h18m) — a narrative
    rounding of elapsed time, not an invented fact/timestamp (the rule
    only forbids fabricating a specific timestamp/URL/name value, which
    "two hours later" isn't), but worth knowing the model will round
    elapsed-time phrases loosely rather than always computing them exactly.

    **A real hallucination WAS found on the very next test, and the first
    prompt draft above was NOT the final version** — logged here in full
    rather than quietly rewritten, since the honest answer to "is this
    possible without hallucinating" turned out to be "mostly, with a real
    caveat, not an unqualified yes." Testing against a second, different
    case (Android 15 CTF25 Cellebrite, `chrome_web_history`, 75 rows, 24
    chunks) found the model characterizing a 6 July 2025 entry — real
    title "Chris Patrick believes Spencer Carbery will start next season
    with Connor McMichael playing center", on `russianmachineneverbreaks.com`
    (a real Washington Capitals/NHL fan site; Carbery is the Capitals'
    real head coach, McMichael a real Capitals player) — as "a Philadelphia
    76ers analysis article". Nothing about "76ers" (an NBA team) appears
    anywhere in the real title/URL; the model inferred a specific team AND
    sport from partial pattern-matching ("roster", "center") and got both
    wrong, while still quoting the real title/URL/timestamp correctly
    alongside the wrong label. The SAME run's reduce step independently
    invented a "Sports-related content" section header and folded a real
    Crystal Rogers murder-investigation article and a Supreme Court story
    into it — neither is sports content; they were only chronologically
    adjacent to genuine sports items in the input. Two distinct failure
    points, both in the topic-CHARACTERIZATION layer specifically, never
    in literal fact grounding (every timestamp/URL/quoted title across
    both runs stayed accurate).

    Fixed by narrowing the permission rather than removing it: the model
    may now only describe a subject using words that literally appear in
    the title/URL/message text itself (quoting or close paraphrase) —
    inferring an unstated specific (a team, sport, or category) is
    explicitly forbidden, and the instruction says outright that this
    exact kind of guess is "a common source of mischaracterization even
    when every literal fact stays accurate". The reduce prompt separately
    forbids inventing a broad category label to group unlike subjects
    together, citing the exact murder/Supreme-Court-as-"sports" failure
    as the reason, and only allows two sessions to be called related when
    their stated subjects are genuinely the same specific thing. Verified
    the fix two ways before trusting it: (1) re-sent the exact failing
    single-row chunk through the tightened prompt in isolation — it now
    quotes the real title and stops, no team/sport guess at all; (2)
    re-ran the FULL CTF25 pipeline end-to-end (24 chunks, real LLM calls,
    not reusing old output) — the 76ers claim was gone (now the safely
    generic "a sports-related article", which doesn't name a wrong team),
    no invented category headers appeared, and spot-checking several
    elapsed-time claims against the real data still held ("fourteen
    seconds later" for an exact 14-second gap; "six minutes" for an exact
    6-minute-1-second span). A third real case (Android 14 CTF26 Magnet,
    detected as a GrayKey-format Android extraction despite the "Magnet"
    tool name — confirmed via `FfsAdapter.detect()`, not assumed; Chrome
    had 96 web-history rows but had never been parsed on this case before
    this session) was run through chrome_web_history + chrome_search +
    chrome_autofill + chrome_offline_pages for the same reason: checking
    the tightened prompt generalizes to a genuinely new device, not just
    re-passing the two cases it was tuned against.

    Existing settings previously saved with the OLD literal-restate
    default prompt (`chrome_search`, `chrome_web_history`, `whatsapp` —
    plus the now-deleted `chrome_overview` entry, removed) were migrated
    TWICE via one-time scripts, once per prompt revision above, so
    already-configured reports pick up each improvement without needing
    manual reconfiguration through the GUI. Standing takeaway for anyone
    tuning this prompt further: a rule permitting the model to
    "characterize" or "interpret" anything, however narrowly worded,
    needs to be tested against real output and checked against real-world
    facts (not just the source row) before being trusted — grounding a
    literal fact and correctly interpreting what that fact MEANS are two
    different claims with two different failure modes, and this project's
    own anti-fabrication rules only ever guarded the first one until this
    pass added guards for the second.

    **A THIRD failure mode was found the same day, on the third real case**
    (Android 14 CTF26 Magnet — detected as GrayKey format despite the tool
    name "Magnet", confirmed via `FfsAdapter.detect()` rather than assumed;
    Chrome parsers had never been run on this case before this session, so
    it was run fresh: `chrome_web_history`/`chrome_search`/`chrome_autofill`/
    `chrome_offline_pages`, 96/16/6/4 rows respectively). This one is
    neither a fabricated fact nor a mischaracterized topic — it's a
    TIMESTAMP-MERGING error, the specific thing rule 1 already forbids,
    happening anyway under one condition: two rows sharing a near-identical
    title ("99 Restaurants Williston — Google Local" at 20:33:37 UTC vs.
    "Bar Restaurant in Williston, VT | 99 Restaurants" at 20:36:04 UTC,
    same real-world place, genuinely different rows/timestamps/URLs) got
    collapsed into one sentence attributing the LATER row's specific URL
    to the EARLIER row's timestamp ("by 20:33:37 UTC ... finally reaching
    https://www.99restaurants.com/locations/vermont/williston/" — the real
    arrival at that exact URL is stamped 20:36:04). Every OTHER claim in
    the same narrative — including a Nike OAuth chain with two genuinely
    different `code=` parameters correctly distinguished, and a
    `FORWARD_BACK` qualifier claim independently confirmed against the raw
    qualifiers column — held up exactly, so this isn't a general
    reliability problem, but a specific, reproducible gap: similar-looking
    repeated titles are where the model is most likely to blur two
    distinct rows into one. Fixed with an explicit addendum to rule 1
    naming this exact scenario (same/similar title, different timestamp
    and URL, still separate events, never attribute a later row's action
    to an earlier row's timestamp) — verified by re-sending exactly the
    28 real rows that produced the error through the tightened prompt in
    isolation (now correctly says the click happened at 20:33:37 and the
    resulting page opened at 20:36:04, matching the real data), then
    re-ran the FULL pipeline for both CTF26 Magnet and (for consistency)
    CTF25 Cellebrite end-to-end again and re-persisted both. Three
    distinct, real failure modes found and fixed in one session (wrong
    category/team inference, wrong topic bucketing, timestamp-merging
    across similar titles) is itself the honest data point: this
    feature's output is a strong orientation tool grounded well enough to
    trust for literal facts checked so far, but each round of real testing
    against a genuinely new case surfaced a NEW failure shape rather than
    re-confirming the same one — there is no basis to claim the prompt is
    now exhaustively safe against every possible mischaracterization, only
    that these three specific, observed ones are fixed. The GUI's AI
    Summary panel's own on-screen caveat ("It's an AI-generated narrative,
    not raw evidence — verify anything material...") is load-bearing, not
    boilerplate, precisely because of this.
  - **Parser helpers** (`artifact_runner.py`, "Parser helpers" section,
    added 2026-08-22 with `first_nonempty`): small, generic utilities a
    parser's own `run()` imports directly (`from artifact_runner import
    first_nonempty`) — the same way it already reaches for a stdlib
    module — for a pattern more than one parser needs, so the logic lives
    once instead of being copy-pasted into every script that needs it.
    Deliberately NOT a place to pre-build abstractions for a pattern only
    one parser has needed so far (matching this project's general
    anti-premature-abstraction stance) — `first_nonempty` itself only
    exists because a second real need for "prefer A, fall back to B" was
    anticipated for the exact kind of full-media/cached-preview split
    `artifacts/ios/whatsapp.py`'s `ZMEDIALOCALPATH`/`ZXMPPTHUMBPATH`
    fallback introduced (see `media_fields` above) — add the NEXT helper
    here when a second parser genuinely needs the same small building
    block, not preemptively. Keeps parser scripts themselves short and
    declarative, which is the whole point of this project's parser
    convention system (`media_fields`/`timestamp_fields`/`record_source`/
    `hidden_fields`/`recoverable_tables` are the declarative side of the
    same idea — say WHAT, not HOW, and let shared code do the HOW).
    `decode_plist_blob` (added 2026-08-25, see the Instagram entry in the
    module table above) is the NSKeyedArchiver-aware plist decoder — same
    section, same import convention, deliberately built generic (not
    Instagram-specific) so a future parser with its own archived-plist
    BLOB column reaches for this instead of reinventing it.

    **First full-project sweep** (2026-08-25, this project's first
    deliberate pass reviewing every existing parser — not just adding a
    helper as a second need happened to come up — specifically to catch
    logic that had already been copy-pasted more than once before anyone
    noticed): three more helpers, each pulled from genuine near-verbatim
    duplication found across the full `artifacts/ios|android/*.py` set,
    not speculative:
    - `missing_ref_label(noun, field_name, value)` → `"[no {noun} — raw
      {field_name}={value}]"`. Found hand-built, separately, in SEVEN
      parsers (`burner.py`, `groupme.py`, `line.py`, `viber.py`,
      `android/whatsapp.py`, `ios/sms_messages.py`, `ios/whatsapp.py`) —
      each already using the identical shape for "this foreign-key lookup
      came back empty," just with a different noun (`'chat record'`,
      `'burner record'`, `'participant records'`, ...). Standardizing the
      shape (not the vocabulary — `noun` stays a free string, there's no
      fixed enum to validate against) means an examiner reading two
      different apps' reports sees one consistent citation convention for
      "couldn't resolve this," not several independently-invented ones.
    - `first_nonempty` gained an optional `default=''` parameter (was
      hardcoded to `''`) so it can also replace a coalesce-several-optional-
      name-columns helper — found duplicated, nearly identically, as a
      private `_display_name` function in TWO parsers
      (`android/google_messages.py`: `full_name or display_destination or
      None`; `android/viber.py`: `contact_name or viber_name or number or
      member_id or None`) — both now call `first_nonempty(..., default=None)`
      directly instead of keeping their own copy.
    - `resolve_path_after_marker(base, raw_path, marker)` → `base` plus
      everything in `raw_path` after the first occurrence of `marker`.
      Generalizes two call sites that looked different but did the exact
      same thing: `android/google_messages.py`'s old `_resolve_cache_path`
      (marker = the package name, found mid-string in an absolute
      on-device path) and `ios/sms_messages.py`'s old inline
      `if filename.startswith('~/'): 'mobile/' + filename[2:]` (marker =
      `'~/'`, found at the very start — a plain substring search still
      finds a true prefix at index 0, so one helper covers both shapes).
    Every replacement was checked for exact byte-for-byte output
    equivalence against the code it replaced (not just "looks
    equivalent") before landing — see the helpers' own docstrings in
    `artifact_runner.py` for the full per-call-site justification.
    Deliberately NOT extracted in this same pass, despite surface-level
    repetition, because the repetition wasn't real duplication: the
    `{key: val for r in conn.execute(sql)}` lookup-dict idiom (present in
    nearly every parser, but it's a bare language idiom applied to
    different SQL/columns each time, not shared logic — wrapping it would
    only hide the SQL, not shorten anything meaningful); `photos_metadata.py`'s
    hand-rolled NSKeyedArchiver `$objects`/`UID`-walking reverse-geocode
    decoder (`_decode_reverse_location`) — a real candidate for
    `decode_plist_blob` above, since it's the same underlying format, but
    rewriting it needs verifying the resulting object shape against real
    GPS/reverse-geocode data from a live case, which wasn't available
    when this sweep was done; flagged for a future pass rather than
    changed on an unverified guess, per this project's own
    verify-against-real-data rule.
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
  - **Report table "Columns" dialog** (`_show_art_columns_dialog` /
    `_apply_art_column_visibility` / `_apply_art_column_order` /
    `report_columns_store.py` in `artifact_viewer.py`; added 2026-08-30 as
    a checkable `QMenu`, then REBUILT the same day into a `QDialog` per
    direct follow-up feedback asking for it to actually match
    `ffs-explorer.py`'s own File Browser "Columns…" behavior rather than
    just its underlying idea): a `QPushButton` in its OWN row ABOVE the
    Filter row (not beside it — same layering as the File Browser's own
    `sel_bar`/`filter_bar` stack) opens a `QDialog` with a `QListWidget`:
    tick a row to show/hide it, drag rows — or the "▲ Move Up"/"▼ Move
    Down" buttons — to reorder them, IDENTICAL mechanism to the File
    Browser's own `_show_columns_dialog` (`QAbstractItemView.DragDropMode.
    InternalMove`, `header.moveSection()` to apply the resulting order to
    the real `QTableView` header). Above the list, an "All"/"None"/
    ("Core" only when the report declares `core_fields` — see
    `WRITING_ARTIFACT_PARSERS.md`) preset row — the one addition beyond a
    literal File Browser mirror, since a plain file listing has no
    "core columns" concept to preset toward. The button's own text
    doubles as an at-a-glance indicator exactly like the File Browser's:
    plain "Columns…" when everything is shown, "Columns ⊘N" (tooltip
    listing the N hidden names) once anything is hidden — same shape as
    `ffs-explorer.py`'s `_update_columns_indicator`, now mirrored here as
    `_update_art_columns_indicator`.

    Visibility is a SECOND, independent, user-toggleable layer applied at
    the `QTableView` level (`setColumnHidden` on `_art_report_view`
    directly) on top of `ArtifactTableModel`'s existing `_visible_idx`
    (which stays reserved for a parser's PERMANENT `hidden_fields` —
    never user-toggleable, never offered as a choice here); reordering is
    a second, equally independent header-section rearrangement
    (`header.moveSection`) — logical column index in the view equals
    position in the model's own visible-column space either way, since
    order is a pure view-level concept layered on top, same relationship
    the File Browser has between its own model and header. Both are
    persisted together per bare `script_name` (`report_columns_store.py`
    — `get_visible_columns`/`set_visible_columns` and the parallel
    `get_column_order`/`set_column_order`, global/cross-case — a display
    preference, not case evidence): never customized means "show
    everything in the parser's own natural order" (same default-to-
    everything convention `ai_summary_store.get_report_settings` already
    uses for its own column selection); a saved order/visibility survives
    closing and reopening the same report, with any newly-added column
    (a parser version bump) appended at the end rather than silently
    dropped.

    Only shown for a real per-parser report — `_setup_report_filter_ui`
    (shared by `_art_show_report`, `_art_show_files`, and
    `_populate_apps_table`) takes optional `script_name`/`core_fields`
    params that only `_art_show_report` passes; the other two callers
    omit them and correctly get the button hidden, the same show-only-
    for-the-right-view pattern the Apps-only Category/Date filters
    already use (just inverted — those hide for a normal report, this
    hides for Apps/Exported Files). Six Chrome parsers
    (`chrome_web_history`/`chrome_search`/`chrome_downloads`/
    `chrome_autofill`/`chrome_bookmarks`/`chrome_offline_pages`) declare
    `core_fields` as the initial concrete example set, each picked from
    that parser's own real schema (e.g. `chrome_search`'s
    `["last_visit_time", "search_term"]` — when and what was searched
    for, matching the exact "time and term is the only thing that are
    really needed" framing that originally motivated the AI Summary
    column selector this reuses the same instinct for). Verified
    end-to-end against real case data, including simulated dialog
    interaction (not just the underlying helpers called directly):
    clicking "Core" in the actual dialog narrows the checked set to
    exactly the declared subset; selecting a row and clicking "Move Up"
    twice moves it to the front of both the live view AND the persisted
    order; the button's own indicator text/tooltip update correctly; and
    both visibility and order survive a simulated close-and-reopen of the
    same report.

    **Layout + quick-access buttons + default-to-Core, same day, per
    direct follow-up feedback**: the row-count label (`_art_row_label`)
    moved from the end of the Filter row onto the SAME row as the Columns
    controls, on the far LEFT, with "All"/"Core"/"Columns…" grouped on
    the far RIGHT — the identical left/right split as `ffs-explorer.py`'s
    own File Browser `sel_bar` (`table_status_label` ... stretch ...
    `columns_btn`). "All" and "Core" (`_art_columns_all_btn`/
    `_art_columns_core_btn` — Core only visible when `core_fields` is
    declared, same condition as the dialog's own Core preset) are
    one-click shortcuts to jump straight to either state without opening
    the dialog — "Columns…" itself still opens the full dialog for
    individual toggles/reordering/"None". Both call a shared
    `_set_art_visible_columns(names)` (also used by the dialog's own
    preset buttons now, replacing duplicated logic).

    A report NEVER customized (nothing ever saved either way) now
    defaults its DISPLAY to Core rather than every column, when the
    parser declares `core_fields` — direct feedback that a report
    showing every field by default undercuts the whole point of this
    feature. Applied only in `_setup_report_filter_ui` at display time;
    deliberately NOT written to `report_columns_store` at that point, so
    the store still correctly reports "never customized" and a future
    change to a parser's own `core_fields` list still takes effect for a
    report nobody has touched yet. This changed what "All" has to mean
    when explicitly chosen: `_set_art_visible_columns` (and the dialog's
    `_apply_and_persist`) now ALWAYS persists a concrete list, even one
    that happens to equal every column — never collapsed back to `None`
    the way an earlier version of this code did — because collapsing
    "all checked" to `None` would have silently turned a deliberate "show
    everything" choice back into "never customized", which would then
    incorrectly re-default to Core on the next open. Verified end-to-end:
    a report's first-ever open shows Core with the store still reporting
    uncustomized; clicking the quick "All" button shows everything AND
    persists that as an explicit list; reopening after that keeps
    showing everything (does NOT fall back to Core); clicking quick
    "Core" afterward correctly narrows back down and that also survives
    a reopen.

    **Visibility persistence scope narrowed to session-only, same day,
    per further direct instruction**: everything in the paragraph above
    about visibility "surviving a reopen" was true only within a single
    run of the app when first built — `report_columns_store.py`'s
    `get_visible_columns`/`set_visible_columns` wrote to
    `report_columns.json` right alongside `get_column_order`/
    `set_column_order`. Split apart the same day into two genuinely
    different persistence lifetimes: ORDER stays on disk exactly as
    before (a real, permanent, "for all future uses of the app"
    preference — reordering carries no risk, every column is still
    there either way), but VISIBILITY moved to a plain in-memory dict
    (`_session_visible`) that is never written to `report_columns.json`
    at all. A hidden column is exactly the kind of choice that
    shouldn't be able to silently persist forever: an examiner who hides
    a column, forgets they did, and doesn't reopen that report for weeks
    could otherwise miss material evidence in a future case without ever
    realizing a column was hidden. Reopening the SAME report later in
    the SAME run of the app still correctly restores whatever was
    chosen (All/Core/None/custom) — the in-memory dict is keyed by
    `script_name` exactly like the disk-backed order store, so nothing
    about `_setup_report_filter_ui`'s own calling code needed to change,
    only what backs the two `report_columns_store` functions it calls.
    Restarting the app always resets visibility to Core (or every column
    if the parser declares no `core_fields`) — verified with a genuinely
    fresh Python process (not just re-running the same one): after
    setting visibility to "All" and a custom column order in one
    process, a second, separate process correctly showed Core-only
    columns while the reordering (title moved first) was still there,
    confirmed by directly inspecting that `_session_visible` starts
    empty on a fresh import while `report_columns.json` still had the
    saved order on disk.

    **"All"/"Core" active-state highlighting, same day, per further
    direct feedback**: the two quick-access buttons now highlight blue
    (`dialog_helpers.ACTIVE_BUTTON_STYLE`, a new `ACTIVE_COLOR = "#1a73e8"`
    alongside that file's existing `WARNING_COLOR`/`ERROR_COLOR`) when
    what's CURRENTLY shown exactly matches that preset — "All" lights up
    only when every column is visible, "Core" only when the visible set
    is exactly the declared `core_fields`, and NEITHER lights up for any
    other (custom) subset — a second, independent-of-the-tooltip signal
    for "is anything hidden, and is it a preset or a custom choice."
    Computed in `_update_art_columns_indicator` (already the one place
    that recomputes the Columns button's own "⊘N" text/tooltip, now also
    doing this), so every call site that changes visibility — the quick
    buttons themselves, the dialog's own presets, and an individual
    checkbox toggle in the dialog — picks it up automatically without
    needing its own separate update call. Verified via all four reachable
    states directly: freshly opened (Core lit), after clicking "All"
    (All lit, Core not), after removing one column from the full set
    (custom subset — neither lit), and after clicking "Core" again (Core
    lit again) — confirmed via `styleSheet()` containing/not containing
    `background-color`, not just visually.
  - **`requires_nested_extraction`** (a module-level list of subpaths,
    added 2026-08-30, alongside the `nested_archive.py` refactor and the
    `app_intelligence.py` `embedded_archives` fix above — all three grew
    out of one direct question: can a nested/embedded archive silently
    hide real evidence from a parser, or from the process of BUILDING a
    new parser from GTD + iLEAPP/ALEAPP cross-reference?). Restates this
    project's own core design principle directly: nothing here EVER
    decompresses or header-scans anything automatically or in bulk — the
    examiner can process everything, do it selectively via a dialog, or
    do nothing at all, all three deliberately valid (see ProcessDialog)
    — so a parser whose target app packs real content inside a
    compressed blob has to say so ITSELF; nobody else can know that for
    it. `artifact_runner.run_artifact()` reads this list right after
    setting `paths['_app_base_ui_path']` and before its normal `files`/
    `optional_files` resolution loop (same "checked directly inside
    run_artifact, not artifact_viewer.py" placement `recoverable_tables`
    already established, since this is about finding source bytes, not
    display) — each declared subpath (same glob-capable convention as
    `files`/`optional_files`) is resolved, extracted via
    `nested_archive.extract_one` if not already done
    (`nested_archive.already_extracted` — reuses either a PRIOR manual
    "Extract as Nested Archive" or an earlier parser run's own
    extraction, whichever happened first; never re-extracts), and the
    on-disk result exposed via a reserved `paths['_nested_archives']`
    dict (`{ui_path: extracted-zip-path}`) for the parser's own `run()`
    to open with plain `zipfile` calls — deliberately NOT an attempt to
    make `files`/`optional_files` transparently reach inside an
    arbitrary embedded archive's own internal layout, which no generic
    resolution step could guess at. A declared archive that can't be
    found or fails to extract is recorded in
    `paths['_nested_archive_errors']` (`{ui_path: message}`) rather than
    aborting the whole parser run, matching `optional_files`' own
    "missing is fine, `run()` decides" shape rather than `files`' hard-
    fail one. No current parser declares this yet — built ahead of a
    concrete need per direct instruction, since (unlike most of this
    project's other "wait for a second real need" deferrals) the app's
    own deliberate no-automatic-preprocessing design means this ISN'T
    speculative: it's a real question every future parser author has to
    actively answer one way or the other, not a hypothetical convenience.
    Verified against a synthetic FFS archive (a nested zip containing a
    real SQLite-signed file, built the same way a real Cellebrite
    extraction's `filesystem2/` layout works) end-to-end: first run
    extracts and the parser's own `run()` successfully reads the real
    bytes back out of the resulting on-disk zip; a second run confirms
    `already_extracted` skips re-extraction; a parser that declares
    NOTHING here is completely unaffected (regression check); and a
    parser declaring a genuinely-missing nested archive gets
    `_nested_archive_errors` instead of a hard failure, with its other,
    unrelated `files` still resolving normally.

    **When writing or reviewing ANY artifact parser going forward** —
    same standing checklist item as the `media_fields`/timestamp checks
    above — check for BOTH of these before concluding an app has no real
    data, or before writing a `files`/`optional_files` path that will
    never resolve: (1) could the real content be an EXTENSIONLESS file a
    plain name/extension filter would miss — check `list_apps`'
    `evidence_databases` (already magic-byte-aware, not extension-only)
    rather than a raw `find_paths` extension search; and (2) could the
    real content be sitting inside an EMBEDDED ARCHIVE that's never been
    extracted — check `list_apps`' `embedded_archives` field for that
    app, and if it's non-empty while `evidence_databases` looks thin or
    noise-only, that's where the real data likely is, not evidence the
    app has nothing. Declare `requires_nested_extraction` when (2)
    applies rather than assuming an examiner already ran (or will
    remember to run) manual "Extract as Nested Archive" first — see this
    entry's own note above on why that step is deliberately never
    automatic. `mcp_server.py`'s `build_artifact_parser` prompt (step 1)
    enforces this exact check for anyone using that tool to draft a new
    parser — see its own text before re-deriving the discovery process by
    hand; found and fixed the same day this convention was added, since
    the prompt's OWN step 1 was still a plain `find_paths` extension
    filter — a real, live example of the same silent-miss failure mode
    this whole feature exists to close, in the very tool meant to guard
    against it.
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
    Four parsers declare `record_source` so far (`artifacts/ios/whatsapp.py`,
    `artifacts/android/whatsapp.py`, `artifacts/android/burner.py`,
    `artifacts/ios/photos_metadata.py` — the last one needed `table_field`
    rather than a fixed `table` string, since the asset table's own NAME
    varies by iOS version, ZASSET vs ZGENERICASSET; verified against both
    variants directly, not just the one this parser happened to be built
    against) — same
    incremental-rollout pattern as `media_fields` above; every other
    parser's rows show the "not available for this parser yet" message in
    Record mode until it's declared for them too. A standing TODO list of
    which parsers still need it (and which never will) lives in
    conversation history as of 2026-08-22 — worth re-deriving with the
    same live-query-vs-recovery-only classification described there rather
    than assuming it's stale, since it isn't tracked as a file anywhere in
    the repo itself. Two known, deliberate gaps, both left as explicit
    follow-ups rather than guessed at:
    - A LIVE row that exists only in an un-checkpointed WAL frame (not yet
      merged into the main db file, but already visible to a normal SQLite
      reader — so it's counted as "live" and not something
      `recover_deleted_rows` would ever surface either) won't be found:
      `locate_live_row` only walks the base file's own b-tree. Surfaced as
      "may be WAL-only, or deleted" rather than a wrong offset or a silent
      miss. (Not the same case as the recovered-row WAL support below,
      which is specifically for a DELETED row that used to exist in some
      historical WAL frame.)
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
    - **Recovered/carved rows get the exact same Record-mode hex jump**
      (added 2026-08-22, same day the user pointed out citing a location is
      arguably MORE important for a deleted row than a live one, since
      unlike a live row it can't be independently re-verified by just
      opening the db normally): `sqlite_carve.recover_deleted_rows` now
      attaches `raw_file` ('main' or 'wal'), `raw_offset`, and `raw_length`
      to every row it returns — the carving pass already knows precisely
      where it found each candidate, so no b-tree search is needed (or
      possible — a carved row is by definition not in the live b-tree).
      `ArtifactViewerMixin._art_load_record_hex` checks `row['recovered']`
      first and, if `raw_offset` is present, jumps straight there —
      resolving `raw_file == 'wal'` to that record_source entry's
      `f"{file_key}_wal"` sidecar via the SAME `resolve_module_file_ui_path`
      (matching the existing `_wal`-suffixed key naming convention every
      parser's `optional_files` already uses), or the entry's own
      `file_key` for `'main'`. The underlying byte-offset math for all
      four carving sources — `_brute_force_records` (freeblock/page-gap),
      `decode_leaf_page_cells` reused by both the freed-page and WAL-frame
      paths, and a length reconstructed from header + serial-type sizes for
      `carve_by_header_signature` (which has no payload-length varint to
      read one from directly — see its own docstring) — was verified
      against hand-built byte buffers with a known-exact record layout,
      not a real DELETE (a live SQLite DELETE's own freeblock-overwrite
      behavior turned out to make a small test record fully unrecoverable
      on the first attempt — a real, expected forensic limitation for a
      tiny row, not a bug, but a bad vehicle for isolating whether THIS
      new offset math specifically was correct).

      **Bug found and fixed 2026-08-31**, nine days after the paragraph
      above, prompted directly by the user reporting Record-mode hex
      simply didn't work for a carved OR a WAL-recovered row (both, not
      one) — a real gap the 2026-08-22 verification's own scope explains:
      it checked `sqlite_carve.py`'s offset math in isolation, never the
      actual click-through a real report row takes. Root cause, confirmed
      by direct reproduction before writing any fix (not assumed):
      `artifact_db.write_artifact_results` stores every artifact table
      column as `TEXT` (its own docstring already says so, for an
      unrelated reason — a union-of-keys schema across live and carved
      rows with different shapes). So by the time a click reaches
      `_art_load_record_hex`, `row['raw_offset']`/`row['raw_length']` are
      the STRINGS `"683"`/`"1592"`, not the ints `sqlite_carve` originally
      produced — passed unconverted into
      `hex_viewer._load_hex_preview_from_bytes_at`'s own arithmetic
      (`(offset - 10) // 32`, `max(length, 0)`), which raises an uncaught
      `TypeError` on a plain string. Reproduced directly:
      `("683" - 10)` throws the exact same error. This hit BOTH the
      carved and WAL cases identically, since `raw_file == 'wal'` only
      changes which file is opened — the offset-arithmetic crash happens
      either way, on the same line, before the file distinction even
      matters — matching exactly what the user reported (neither worked,
      not just one). Silent from the user's side: nothing in the row-
      selection signal chain catches the exception, so clicking a
      recovered row just... did nothing, no error shown in the GUI.

      Fixed at the one place that matters — `_art_load_record_hex` now
      wraps `int(row['raw_offset'])`/`int(row.get('raw_length') or 0)` in
      their own try/except (mirroring the live-row rowid path just below
      it in the same function, which already did `int(rowid)` correctly —
      the recovered-row shortcut was the one branch that skipped this),
      falling back to the same "No record-location data on this row"
      message the live-row path already uses for an equivalent failure,
      rather than crashing. Verified directly against the real row shape,
      not just the abstract bug: built a dict matching exactly what
      `row_dict()` returns for a genuine recovered row from this same
      Android 14 JoshHickman case (`raw_offset: "683"`, `raw_length:
      "1592"`, from the same header_signature carve the confidence-gate
      fix above was verified against) — old code reproduces the
      TypeError, patched code returns `offset=683, length=1592` (correct
      ints) with no crash.

      **Second, independent bug found the same day, once the user actually
      restarted the app and clicked through**: the TypeError fix let the
      jump run, but it landed on the wrong bytes — the user reported the
      highlighted selection was all `00` and asked directly "so where did
      the url value come from?", exactly the right question to ask rather
      than trusting the fix on faith. Root cause: `c['offset']` (the
      rowid-based carving paths) and `c['header_offset']`
      (`carve_by_header_signature`) are PAGE-relative — same convention as
      every other page-scoped value this module works with internally
      (`decode_leaf_page_cells`'s own `cell['offset']` is page-relative
      too, by design, since most of this module's internal work is
      naturally scoped to one page at a time) — but `recover_deleted_rows`
      was handing that page-relative number straight out as `raw_offset`,
      which every consumer (the Hex panel) reasonably treats as a
      file-absolute seek position. `locate_live_row` (the LIVE-row
      equivalent, a few functions above in this same file) already gets
      this right — it explicitly computes `abs_offset = (page_no - 1) *
      page_size + cell['offset']` — so the bug was narrower than "offsets
      are broken": only the CARVED-row path skipped a conversion the
      LIVE-row path already had. The WAL case was never affected — its
      own `wal_offset` is built from the WAL frame's own absolute header
      position, not a page number, so it was already correct; this is
      also, not coincidentally, why the WAL case was the one already
      believed to be closer to working. Confirmed on the real data before
      writing the fix, not assumed: `raw` file bytes at the OLD (bare)
      offset 683 are 30 bytes of literal `\x00` — coincidentally landing
      in page 1's own unused space, nowhere near the real record — while
      bytes at page 9's true absolute offset (`(9-1)*4096 + 683 = 33451`)
      are exactly the record header followed by the readable
      `https://m.facebook.com/login.php?...` text this row actually
      decoded from. Fixed in `recover_deleted_rows` (the one place that
      assembles the output row, not the internal carving helpers
      themselves, which correctly keep their own page-relative
      convention for their own internal use) — both the rowid-based loop
      and the header_signature loop now compute `(c['page'] - 1) *
      header['page_size'] + c['offset']` (or `c['header_offset']`) before
      storing it as `raw_offset`; `c['wal_offset']` is passed through
      unchanged, since it needed no fix. Re-verified through the FULL
      real chain end-to-end, not just the arithmetic in isolation: real
      archive, real `FfsAdapter.detect()`, real `chrome_web_history.py`
      module, `resolve_module_file_ui_path` → `adapter.resolve` → real
      `ZipEntry.read()` → the corrected offset — produced `raw_offset:
      33451` and the actual bytes at that position in the live-read
      archive are the real record header plus readable URL text, byte for
      byte. `visits` table regression-checked again after this change
      too (still 0 rows, no exception). Both bugs together explain the
      user's original report precisely: bug 1 meant nothing happened at
      all; fixing bug 1 alone would have shown wrong (all-zero) bytes
      without bug 2 also being fixed — consistent with why testing after
      only the first fix still looked broken. Live-GUI click-through
      still not directly observed by Claude (no way to drive this
      session's own PySide6 window) — everything short of that step is
      now verified against the real archive.

      **`record_source` gained per-query scoping (`source_match`), same
      day**, prompted by the user pointing out a live Chrome History
      row's Record-mode jump no longer showed the URL text they expected
      and restating the original intent directly: select a row, see its
      OWN main table by default; pick a join from the dropdown
      deliberately, not automatically. Investigating that surfaced the
      real, pre-existing gap: `chrome_web_history.py`'s `record_source`
      only ever declared `visits` (Chrome History's main table, an
      INTEGER FK for `url` — never text, confirmed against the real
      schema) and `ukm_db`'s `urls` — Chrome History's OWN `urls` table,
      the actual join target holding the URL/title TEXT this report
      already displays via `run()`'s own `urls_by_id` join, was never
      declared as an entry AT ALL. So a live Chrome-History row's Record
      mode could only ever show the `visits` row's own binary fields —
      never the text — regardless of anything from earlier today; this
      was never a working feature to regress.

      Fixed two ways together: (1) `chrome_web_history.py` now populates
      `raw_url_id` for Chrome History rows too (`r["url"]`, the real FK
      into History's own `urls` table — previously hardcoded `None`,
      since nothing consumed it) and declares a third `record_source`
      entry, `"Chrome History URL"` (`file_key: "history", table:
      "urls", rowid_fields: ["raw_url_id"]`). (2) A NEW `source_match`
      convention (documented in `artifact_runner.py`'s own docstring)
      scopes each entry to the `source` value(s) it actually applies to
      — `["Chrome History"]` on the two History-side entries, `["Segmentation
      Platform (UKM)"]` on the UKM one — since a report merging two live
      queries (this one) has a genuinely DIFFERENT main-table-plus-joins
      per query, and offering every parser-wide entry regardless of which
      query built the selected row would let an examiner pick a join
      that structurally can't apply to it. `ArtifactViewerMixin` gained
      `_art_record_sources_for_row(row)` (filters by `source_match`,
      entries with none apply to every row — every OTHER parser's
      existing `record_source` declaration is completely unaffected) and
      the combo is now rebuilt PER ROW SELECTION from that scoped
      subset, reset to entry 0 (the row's own main table) — but ONLY on
      a genuinely NEW row (`_on_art_report_row_selected` now
      distinguishes this from `_on_art_hex_source_toggled` re-invoking it
      for the SAME row after the examiner manually changes the combo/
      toggle, via `current.row() != previous.row()`), so manually picking
      a join for one row is never silently reset by the by re-fire that
      same toggle change already causes.

      This reintroduced, and then had to solve, the EXACT table-name
      collision `recoverable_tables`' own `file_key` pinning already
      exists for (see the `recovery_source_labels` entry below): two
      `record_source` entries now share the bare name `"urls"` (History's
      own, and `ukm_db`'s). A CARVED row's OWN matching logic (unaffected
      by `source_match` — it already knows its real source table via
      `source_table`, never guesses from `source`) previously matched by
      table name alone, which would have silently picked whichever
      same-named entry happens to be declared first — now genuinely wrong
      for a real carved UKM row, not just theoretically. Fixed by having
      `sqlite_carve.recover_deleted_rows` carry the SAME `file_key` it
      was already given (or `None` for the common unpinned case) onto the
      row as `source_file_key`, and having the carved-row matching logic
      narrow by it ONLY when more than one table-name match exists — a
      single match (every parser without this collision) is completely
      unaffected.

      Verified against real data at every step, not assumed: re-ran
      `chrome_web_history.run()` fresh (the on-disk case DB predated the
      `raw_url_id` change) — the real Slack-invite row's `source_match`
      scoping correctly offered `["Chrome History Visit", "Chrome History
      URL"]`; resolving the new join entry via `locate_live_row` against
      the real History file found the row at page 42/offset 170797, whose
      actual bytes ARE the real `https://www.google.com/url?q=https://join.slack.com/...`
      URL and `"Redirecting... | Slack"` title, byte for byte — the exact
      gap this session opened with. The UKM live row correctly scoped to
      exactly one entry (combo hidden, no ambiguity). The real carved UKM
      row (the false-positive-flagged one from earlier today) was
      confirmed to still resolve to the CORRECT `"Segmentation Platform
      URL"` entry via `source_file_key="ukm_db"` disambiguation, not the
      new `"Chrome History URL"` entry that now shares its bare table
      name — proving the collision fix actually holds, not just that it
      compiles. `WRITING_ARTIFACT_PARSERS.md`'s `record_source` section
      updated in sync with a `source_match` writeup, same as every other
      convention change today.

      **Default entry + sticky combo choice, same day, per direct
      instruction**: `chrome_web_history.py`'s `record_source` reordered
      so `"Chrome History URL"` is first (the URL/title text an examiner
      wants to verify by default), `"Chrome History Visit"` second — order
      alone controls the default, no new declaration mechanism needed.
      New `ArtifactViewerMixin._art_record_source_sticky` (a `{source:
      last_picked_label}` dict, reset only on report (re)load —
      `_art_show_report`/`_refresh_artifact_tab`/`_populate_apps_table`,
      same reset points `_art_current_record_sources` already had) makes
      a manually-picked entry persist across every OTHER row sharing that
      row's own `source` value, instead of resetting on each new row —
      `_art_load_record_hex` now consults it before falling back to
      `scoped[0]` when rebuilding the combo for a genuinely new row,
      and records the combo's current choice back into it whenever
      `len(scoped) > 1`. Verified with a full simulation (no Qt needed —
      pure dict/list logic): pick a non-default entry on one Chrome
      History row → the next Chrome History row inherits it → an
      interleaved UKM row (single entry, unaffected) → a simulated report
      reload correctly resets back to the declared default.

      **A THIRD, more significant bug found immediately after, testing
      the UKM side specifically**: the user reported UKM rows' Record
      mode looked like it was "looking at history as well" — investigated
      directly rather than guessed at. Root cause: `sqlite_carve.
      read_varint`'s accumulator is built for this format's otherwise-
      always-UNSIGNED varints (payload length, header length, every
      serial type) — but a ROWID varint is the one place SQLite actually
      stores a SIGNED 64-bit value (an `INTEGER PRIMARY KEY` column can be
      negative), and Chrome's own UKM `url_id` is a signed 64-bit hash
      that genuinely lands in negative range on real data (confirmed:
      `-2797302551615810181` on the real Slack-adjacent UKM row from
      earlier today). Read via the unsigned path, that decoded as a huge
      POSITIVE number (`15649441522093741435`) instead — `locate_live_row`
      compares against the ACTUAL signed rowid a live SQL query returns
      (which `chrome_web_history.py`'s own `raw_url_id` field already
      correctly carries), so the comparison silently never matched and
      returned `None` for a row that was genuinely present and live —
      not a corrupted/deleted-row edge case, an ordinary live lookup
      failing. `_art_load_record_hex`'s existing "not found" branch then
      called `_show_art_hex_message`, which (correctly, by its own
      design) leaves the Hex panel showing an explanatory placeholder —
      but from the user's side, on a row selected right after a Chrome
      History row, this READ as "still showing History", not as an error
      message being displayed; the real bug was upstream of that message
      ever being reached at all.

      Fixed with a new `_to_signed_rowid(value)` (two's-complement
      correction, `value - 2**64` when `value >= 2**63`) applied ONLY at
      the two places this module actually decodes a rowid specifically —
      `decode_leaf_page_cells` (covers live lookups, freed-page carving,
      AND WAL-history carving, since the WAL path reuses this same
      function) and `_brute_force_records` (freeblock/page-gap carving) —
      never inside `read_varint` itself, which every OTHER call site in
      this file (payload length, header length, serial types) still
      needs unsigned. This is NOT scoped to Record-mode hex or even to
      chrome_web_history — every one of this module's four carving paths
      decodes rowids through one of these same two functions, so ANY
      parser's `recoverable_tables`-carved row with a negative rowid was
      previously either mismatched against `live_rowids` (risking a
      still-live row being wrongly treated as deleted, since the
      comparison uses the same unsigned-vs-signed mismatch) or reported
      under the wrong (positive) `raw_rowid` value — this fixes both,
      genuinely below the surface of what was directly reported. Verified
      directly, not assumed: confirmed the row exists in the real
      b-tree under the WRONG (unsigned) rowid bit-pattern before writing
      the fix (ruling out "row genuinely absent" as the explanation), then
      confirmed `locate_live_row` finds it under the CORRECT signed value
      afterward, byte-for-byte the same URL the report already displays
      (`https://adclick.g.doubleclick.net/aclk?...`). Full regression
      pass after: the small-positive-rowid live lookups (`visits`
      rowid=31, `urls` rowid=26 — well under 2^63, identical under either
      interpretation) came back byte-identical to before this fix; carved-
      row output (`visits`: 0 rows, no exception; the UKM `header_signature`
      row's offset/label) unchanged.

      **Combo UX polish + a full parser audit, same day**: (1) the combo
      now stays visible whenever a parser declares ANY `record_source`,
      never toggling hidden/shown as the examiner moves between a 1-entry
      and a multi-entry row (previously reflowed the panel layout on
      every such transition); (2) a multi-entry row's items read `"N of M
      - label"` (a bare label, no prefix, when there's only one — a
      `"1 of 1"` prefix would be noise, not clarity); (3) a carved row —
      which never had ANY combo update before, since its branch returned
      early — now shows a bare `"Carved Record"` item, same
      no-prefix-for-one convention. (4) Auditing every OTHER parser's
      `record_source` for this exposed a real side effect: a single-entry
      declaration never NEEDED a `label` before (the combo stayed hidden
      for count==1), so 6 of this project's 10 `record_source`-declaring
      parsers (`chrome_autofill`/`burner`/`chrome_downloads`/
      `chrome_search`/`chrome_offline_pages`/iOS `instagram`) had none —
      now always visible, they'd have shown a bare `"?"` fallback. Given
      real labels instead (`"Autofill Entry"`, `"Download"`, etc.); the
      other 4 (WhatsApp iOS/Android, Photos Metadata) already had one on
      every entry and needed nothing. `artifact_runner.py`'s docstring
      updated — no longer says label is "only needed with >1 entry".

      **Android WhatsApp's `record_source` expanded from 2 entries to
      12, same day**, prompted by the user reading the parser's own real
      SQL query and asking directly whether the 2-entry declaration was a
      deliberate simplification given how many tables (8, across 2 files)
      the query actually joins. It wasn't — investigated rather than
      assumed: `jid._id`, `message_media.message_row_id`,
      `message_location.message_row_id`, and `wa_contacts._id` (checked
      via `PRAGMA table_info` against this project's own real WhatsApp
      casework, both `msgstore.db` and `wa.db`) are ALL genuine
      single-column `INTEGER PRIMARY KEY` rowid aliases — including
      `wa_contacts`, which the old comment here specifically flagged as
      unconfirmed; it turned out to just need checking. `message_media`/
      `message_location` needed no new SQL at all — their own rowid is
      provably identical to `m._id` (their own JOIN condition), already
      captured as `message_id`. The four `jid` joins and four
      `wa_contacts` joins DID need one more `SELECT` column each (their
      own `_id`, computed and joined against already but never carried
      into the row's own output dict) — added, then surfaced via
      `hidden_fields` (plumbing only, already resolved into the visible
      `chat_jid`/`remote_party_jid`/`remote_name`/`media_path`/
      `latitude`+`longitude` columns). New entries: Chat JID, Sender JID,
      Chat/Sender Mapped JID (LID-privacy variants — `None` on an
      ordinary chat, correctly falling back to "no record-location data"
      rather than silently citing the wrong, non-mapped jid), Media,
      Location, Chat Contact, Sender Contact, Chat/Sender Mapped Contact.
      No `source_match` needed — unlike `chrome_web_history`, every row
      here comes from ONE query, so every entry applies to every row.
      Verified against this same case's real WhatsApp data (230 rows,
      row count unchanged before/after): `locate_live_row` resolved real
      bytes for Chat JID, Sender JID, Media, Location, and Chat Contact —
      a phone number, a WhatsApp-format jid string, and a real contact
      status ("Hey there! I am") all read back correctly; the Mapped
      variants correctly returned `None` throughout (no LID contacts
      exist in this case, an honest "not tested positive" rather than a
      false "verified" claim); `recoverable_tables` carving and the
      pre-existing Message/Chat entries confirmed unchanged (one apparent
      "Chat entry failed" during testing turned out to be an unrelated,
      already-documented dangling-FK row, not a regression — checked
      before concluding either way).

      **`presence_fields` — per-row, not just per-query, combo scoping,
      same day**: the user asked directly whether the combo could be
      narrowed to only entries whose LEFT JOIN actually matched THIS row
      (not just this row's query, which `source_match` already handled).
      `_art_record_sources_for_row` gained a second filter: an entry's
      `rowid_fields` (or, absent that, a new optional `presence_fields`)
      must have a real value on the row, or the entry is dropped —
      genuinely per-row, since two rows from the identical query can
      differ in which of that query's own joins matched. `rowid_fields`
      alone sufficed for every entry EXCEPT WhatsApp's Media/Location,
      caught by direct testing, not assumed: their rowid is `message_id`,
      BORROWED from the always-present row PK rather than re-selected
      from their own table (see the entry above), so it's non-null
      whether or not the join matched — the presence check needed the
      actual joined value (`media_path`/`latitude`) instead, which
      `presence_fields` now provides. Verified against real WhatsApp
      data: a plain-text row now lists `[Message, Chat, Chat JID, Chat
      Contact]` (no Media/Location); a real media row adds Media; a real
      location row adds Location; a row with a real `sender_jid_id`
      correctly adds Sender JID. `chrome_web_history.py` needed no
      `presence_fields` at all — confirmed unchanged, its two entries'
      `rowid_fields` already answer the presence question correctly on
      their own.

      **"Hide likely false positives" report filter, same day**: the
      user asked to stop showing carved rows the confidence gate above
      already flags as unlikely to be real — reasoning directly that a
      candidate whose only real content is a garbage URL fragment "is
      not web history" and is actively misleading sitting in the table,
      not just noise. Deliberately NOT implemented as dropping the row
      anywhere in the pipeline (sqlite_carve/artifact_runner/the DB) —
      only as a new Report-table FILTER, checked by default, next to the
      existing text Filter box, visible only for a report whose parser
      declares `recoverable_tables` (`_setup_report_filter_ui`'s new
      `has_recoverable` param). `total_rows` still counts the hidden ones
      (`"41 of 42 rows"`, the SAME "N of M" pattern the text filter
      already used) and unchecking the box brings them straight back —
      nothing is ever actually gone, matching this project's standing
      escalate-don't-discard rule (see the `app_intelligence.py`
      `embedded_archives`/`known_real_store`-removal precedents) even
      though the DEFAULT view now matches what the user asked for.
      `ArtifactFilterWorker` gained an `exclude_low_confidence` flag,
      ANDed onto whatever text-filter WHERE clause already applies (both
      can be active together) via `("source" IS NULL OR "source" NOT
      LIKE '%(likely false positive%')` — NULL-safe by construction,
      though `source` is never actually NULL in practice (artifact_db.py
      stores '' instead). `_art_show_report` now calls `_apply_art_filter()`
      itself right after loading, so the checked-by-default state takes
      effect immediately on open, not just after the examiner interacts
      with the filter row. Verified directly against the real case DB,
      not simulated: hiding alone took `chrome_web_history` from 42→41
      rows, correctly identifying the exact flagged row from the
      confidence-gate work above; combined with a text search
      ("facebook", which the garbage row's own URL fragment also
      contains) still correctly excluded it (3→2 rows) while a real
      match stayed; unchecking brought it back in both cases.

      **A THIRD escalation the same day, this one an actual rejection —
      the first place in this whole project's carving pipeline that
      drops a candidate rather than surfacing it**: the user pushed back
      further, twice, on the filter above — "it really is rubbish why
      would be leave it," "only a url... definitely not a valid record."
      Re-examined the specific row: of the UKM `urls` table's 6 real
      columns, only 2 (`url`, `profile_id`) decoded non-null, and BOTH
      are dominated by literal NUL bytes (`url`: 1,425 of 1,566
      characters; `profile_id`: 10 of 10) — not merely an implausible
      VALUE (which the timestamp check already covered), but bytes that
      are not text at all. The distinction that makes this safe to treat
      as an outright rejection, unlike the content-meaning judgment calls
      this project otherwise deliberately avoids making on a candidate's
      behalf (see `app_intelligence.py`'s removed `known_real_store`
      entry): whether a byte sequence is printable text is a mechanical
      fact, not a guess about what the text MEANS.

      New `sqlite_carve._text_plausible(value)`: False when a string is
      at least 8 characters long and more than 15% of its characters are
      control characters (NUL and friends — a lone tab/newline/CR
      doesn't count, real text can hold those). Applied only inside
      `recover_deleted_rows`'s `header_signature` loop (the one carving
      path where this failure mode is structurally possible at all — see
      the `carve_by_header_signature` docstring) — any candidate with an
      implausible-text field is `continue`d past entirely, never reaching
      `out`, so it's absent from the database from the moment the parser
      runs, not merely filtered at display time. Logged, not silent, even
      though it never reaches the examiner-facing Report table: a
      `print(f"[sqlite_carve] rejected header_signature candidate...")`
      names the table/page/offset/field, the same "log to console, don't
      surface in the GUI" precedent `artifact_runner.load_artifacts()`
      already set for a parser load failure — a real anomaly here still
      leaves SOME trace for whoever reads the console, even though
      hiding it from the report (not just flagging it) was the explicit
      point. Verified against real data both directions: the exact
      row that prompted this now returns 0 rows (previously 1, labeled
      "likely false positive"), logged with the specific rejected
      fields; a battery of genuine real strings (a real URL, a real
      WhatsApp contact status, a real jid, `""`, `"ok"`) all correctly
      still pass `_text_plausible` — this is a real check with real
      discriminating power, not a rule that happens to reject everything.
      `WhatsApp message`/`Chrome visits` carving (both 0 real recoverable
      rows already, per earlier verification) re-confirmed unchanged, no
      exception. This sits BELOW the softer confidence-gate label from
      earlier today, not instead of it: a header_signature candidate
      whose text is genuinely printable but merely fails the NOT-NULL or
      timestamp check (a real, if unconfirmed, structural anomaly rather
      than obvious garbage) still gets surfaced with the "(likely false
      positive — ...)" label and the "Hide likely false positives"
      toggle above — this new check only removes the candidates with
      nothing left to argue for even that much visibility.

      **New parser, same day: `artifacts/android/chrome_cache.py` +
      `app/chrome_cache.py`** — Chrome's HTTP disk cache (Simple Cache
      format, `data/data/com.android.chrome/cache/Cache/Cache_Data/` —
      NOT `app_chrome/Default`, a genuinely different location from
      every other Chrome parser here), prompted by the user asking
      whether cached page resources could be reconstructed "as the user
      would have seen it," similarly to `chrome_offline_pages.py`'s real
      `.mhtml` snapshots. The entry FORMAT itself was reverse-engineered
      directly against this project's own real casework, not any
      third-party tool or documentation trusted at face value: the
      24-byte `SimpleFileHeader` (magic/version/key_length/key_hash/
      reserved) is confirmed one `uint32` field longer than what this
      project could find independently documented (20 bytes,
      magic/version/key_length/key_hash) — found by noticing a real
      decoded cache key was consistently truncated by exactly 4 bytes at
      the 20-byte offset, and exactly correct at 24, on TWO independent
      real entries (`...against-ti` / `...against-tigers`,
      `...overview-and-sche` / `...overview-and-schedule`) before
      trusting the fix. EOF-record boundaries are located by searching
      for Simple Cache's own EOF magic number rather than computed
      offsets, deliberately — a reliable anchor regardless of any
      further (real, observed) header quirks, the same lesson the
      24-vs-20-byte discovery itself already taught.

      Body decompression: gzip/deflate free (stdlib); `brotli`/`zstd`
      needed real new dependencies (`requirements.txt`/`ffs_explorer.spec`
      hiddenimports) — NOT hypothetical, confirmed by direct measurement
      on this project's own real cache: 496 of 3,041 entries (16%) were
      `br` and 56 (2%) were `zstd` before adding them, dropping to 19
      residual failures (13 genuinely truncated gzip streams, 6
      structurally truncated entries — both correctly reported per-row
      via `body_error`/`header_parse_error`, never silently dropped)
      after. HTML-content-type entries additionally get a synthesized
      `.mhtml` reconstruction: `chrome_cache.find_referenced_urls` scans
      the page's own markup (img/script/link src|href, CSS `url()` — a
      lightweight regex scan, not a real HTML parser, deliberately, to
      avoid a new parsing dependency for this alone) for locally-fetchable
      resources, resolves each against every OTHER entry in the SAME
      cache run (built as one url→entry index up front), and
      `build_synthetic_mhtml` embeds whatever's found via each part's own
      `Content-Location` header — the IDENTICAL mechanism a real
      Chrome-generated `.mhtml` archive already uses to link its own
      parts (confirmed directly against a real Chrome Offline Pages
      archive when `_build_webpage` was built) — rather than rewriting
      the HTML's own attribute values, which would risk corrupting
      anything JS-driven in the markup. Opens in the exact same
      `MediaFullViewDialog._build_webpage` viewer a real snapshot does —
      no new UI code needed there at all.

      Genuinely new framework capability, not just a leaf parser script:
      every OTHER parser here reads a small fixed/globbed set of files,
      but this needs to enumerate ~3,000 arbitrarily-named files in one
      directory. `run_artifact` (`artifact_runner.py`) gained two new
      reserved `paths` keys, same convention as `_app_base_ui_path` —
      `_zip_names` (the full archive namelist, already computed there for
      other reasons) and `_read_zip_bytes(name)` (reads one entry
      directly, no disk write, via the same `ZipEntry` path
      `_extract_candidate` already uses) — plus `_parser_files_dir` (this
      parser's own `case_dir/artifact_parser_files/<name>/` folder,
      already created for normal extraction) for writing the synthesized
      `.mhtml` files, which are DERIVED, not copied evidence, so
      `media_fields`' "never bytes copied by the parser itself" rule
      (written for extracted-evidence paths) doesn't apply to them the
      same way. Reading that path back needed no new field convention
      either: `hex_viewer._read_zip_bytes` now checks `os.path.isabs()`
      first and reads straight from disk when true — an archive ui_path
      is never absolute, so every EXISTING `media_fields`/`record_source`
      caller across every other parser is unaffected, confirmed by
      inspection, not just assumed.

      Verified end to end against real data at every stage, not
      simulated: parsed two independently-known real pages (Chrome
      History already recorded both titles) — both decompress correctly
      and their own `<title>` matches exactly. The FULL parser `run()`
      produced 3,041 rows in ~1 second against this project's real
      archive; 141 of 309 HTML-content-type entries reconstructed
      successfully. Both the smaller article reconstruction (188
      references found) and the much larger real homepage reconstruction
      (422 references found, 115 resolved) were loaded in an actual
      headless `QWebEngineView` (JavaScript disabled, same lockdown as
      real `.mhtml` viewing) and BOTH correctly rendered — `loadFinished:
      True` and the page `<title>` read back exactly matching Chrome
      History's own recorded title for that URL in both cases, not just
      "the dialog didn't crash."

      Known, honest limitations stated directly in the parser's own
      `description`/`warning` fields, not glossed over: this reflects
      only STATIC references in the page's own markup, never anything a
      real browser's JavaScript would have fetched at runtime; a
      resource this page referenced but the cache no longer holds
      (evicted, blocked, never cached) is silently absent from the
      reconstruction the same way it would be from a browser with no
      network access — `references_found`/`references_resolved` on each
      row states exactly how much of the page's own reference list could
      actually be recovered, so this is never overstated as complete.
  - **`recovery_source_labels` — carved rows self-label their own source
    and confidence** (`app/artifact_runner.py`, added 2026-08-30, prompted
    directly by a user question after reviewing a carved
    `chrome_web_history` row: "there is no source... is it a real History
    or UKM result or a false positive?"). Root cause: a carved row is
    produced entirely by the shared `sqlite_carve`/`recoverable_tables`
    pipeline, never by the parser's own `run()` (per the standing "no
    recovery code in a parser" rule above) — so it never gets a `source`
    value the way a live row does, and for a report whose `core_fields`
    includes `"source"` (as `chrome_web_history.py`'s does, deliberately,
    since it merges two independently-tagged live sources) that showed as
    a blank cell on exactly the rows where provenance matters most, not
    merely as expected raggedness. Fixed generically, not as a Chrome-only
    patch: `artifact_runner.py`'s `recoverable_tables` loop now sets
    `source` on every recovered row from every parser, using an optional
    per-table `recovery_source_labels = {table_name: friendly_label}`
    declaration (falls back to the bare SQL table name when a parser
    doesn't declare one) — `f"Carved — {label}"`, with `"
    (unverified match)"` appended automatically when
    `recovery_method == "header_signature"`. That specific tier is
    load-bearing, not decorative: the other three carving paths
    (freeblock/freed-page/WAL-frame) are pre-filtered inside
    `sqlite_carve.recover_deleted_rows` to an exact column-count match
    against the table's live schema before a candidate ever reaches this
    loop, while `header_signature` has no rowid at all to cross-check
    against and can be genuinely truncated — the one carving path where a
    false-positive match is structurally possible, not just hypothetical
    (see `sqlite_carve.py`'s own `carve_by_header_signature` docstring).
    `chrome_web_history.py` declares real names for both its tables
    (`"visits"` → "Chrome History (visits table)", `"urls"` → "Segmentation
    Platform (UKM, urls table)") plus an in-file note on what a GENUINE
    carved row of each table should and shouldn't be missing — checked
    against each table's real Chromium schema (`visits`:
    id/url/visit_time/from_visit/transition/segment_id/visit_duration/...,
    confirmed via the public Chromium source, not assumed; `urls` in
    `ukm_db`: `url_id INTEGER PRIMARY KEY NOT NULL, url TEXT NOT NULL,
    last_timestamp INTEGER NOT NULL, counter INTEGER, title TEXT,
    profile_id TEXT`, same). Two concrete findings from that check, stated
    directly in the parser file so an examiner doesn't have to re-derive
    them under time pressure: `visits` has NO `title` column at all
    (title only ever lives in the separate, live-row-only `urls` join) —
    a carved `visits` row missing a title is expected, not a red flag —
    while `ukm_db`'s `urls` table DOES have `title`, but it's nullable
    there too (a real anti-bot/redirect page genuinely has none, per this
    same parser's own already-recovered ground-truth row, described
    above); the one field a genuine carved `visits` row should NOT be
    missing is `transition` itself (`NOT NULL` in the real schema) — an
    absent/zero-length `transition` alongside an `"(unverified match)"`
    source is the actual signal worth distrusting, not a missing title.
    `record_source` itself was NOT the gap here and needed no change —
    already correctly declared for both tables since 2026-08-22, so the
    Hex panel's Record-mode jump for a carved `chrome_web_history` row was
    *believed* to already work before this fix (the 2026-08-22 entry below
    only verified `sqlite_carve.py`'s own offset math against hand-built
    buffers, never the actual GUI click-through — see the correction dated
    2026-08-31 right after that entry, which found it did NOT); only the
    textual "what am I looking at" provenance was the thing THIS fix
    addressed. `WRITING_ARTIFACT_PARSERS.md`'s `recoverable_tables` section
    updated in sync per this file's own consistency-checking instruction.

    **`header_signature` confidence gate — "(unverified match)" upgraded
    to a named reason, or left alone** (`app/sqlite_carve.py`/
    `app/artifact_runner.py`, added 2026-08-31, one day later, prompted by
    the user directly examining the one real `header_signature` row this
    feature had just surfaced — page 9, offset 683 of the real `ukm_db` on
    this same Android 14 JoshHickman case — and asking whether "(unverified
    match)" was doing enough: "my problem is that it is more than likely a
    false positive... could we record what the expected data types are...
    use the timestamp... must be an integer and less than tomorrow." Root
    cause of why the bare row looked suspicious once actually inspected:
    of the real `ukm_db` `urls` table's 6 columns, only `url` and
    `profile_id` were populated on this candidate — both garbage
    (`url` is `"...next=https"` followed by 1,425 literal null bytes out of
    1,566 total; `profile_id` is 10 null bytes) — `url_id`/`last_timestamp`/
    `counter`/`title` all blank. That's a genuinely different, sharper
    signal than "looks sparse": `url_id` and `last_timestamp` are `NOT
    NULL` in the real schema (restated from the entry above), so a GENUINE
    row — carved or live — cannot have them blank; a plain fill-rate
    heuristic would have missed that this is a schema violation, not
    stylistic thinness.

    Two mechanical, no-per-parser-guessing checks added to
    `sqlite_carve.py`, applied ONLY to `header_signature` candidates (the
    other three carving paths stay untouched — already pre-filtered to an
    exact column-count match against the live schema before a candidate
    ever reaches `recover_deleted_rows`' output loop, a stronger check
    already than either of these): (1) `notnull_columns(conn, table)` reads
    `PRAGMA table_info`'s own `notnull` flag directly — no hardcoded
    per-table knowledge — and `recover_deleted_rows` flags any of those
    columns decoding blank on a header_signature row, EXCLUDING the
    table's rowid-alias column (`id_col`) first: a header_signature match
    structurally can never populate that column regardless of whether the
    match is real (the value only ever exists as the cell's own rowid,
    which this carving path doesn't have — see `carve_by_header_signature`'s
    own docstring) — checking it unexcluded would have flagged every
    genuine header_signature carve of this table as a false positive, not
    just a real one; caught and fixed before shipping, not after. (2) a
    small, self-contained `_epoch_seconds`/`_timestamp_plausible` pair
    (duplicating the same unit-code conversion `artifact_viewer.py`/
    `ai_summary.py` already each have their own copy of — same
    zero-cross-dependency reasoning `blob_safe`'s own docstring already
    gives) checks any RAW-column-named entry in the parser's own
    `timestamp_fields` against `[2000-01-01 UTC, now + 1 day slack]` — the
    exact "must be an integer and less than tomorrow" check requested,
    generalized to any of this project's five unit codes rather than just
    Chrome's. `recover_deleted_rows` gained an optional `timestamp_fields`
    parameter (passed straight through from the parser's own module-level
    declaration by `artifact_runner.py`'s `recoverable_tables` loop —
    zero new parser-facing surface for a parser that already declares
    `timestamp_fields` for ordinary display formatting, which
    `chrome_web_history.py` already did) so this check has the same raw
    values a header_signature row's own fields dict already carries — no
    parser code changes needed to opt in.

    Both checks land on the row as `notnull_violations`/`timestamp_issues`
    (lists, possibly empty) — **the row itself is never withheld either
    way**, per this project's standing escalate-don't-silently-discard
    rule (the same principle `app_intelligence.py`'s `embedded_archives`
    fix and `list_evidence_candidates`'s escalation both already
    established) — only `artifact_runner.py`'s confidence suffix changes,
    from the generic `"(unverified match)"` to
    `"(likely false positive — {reason})"` naming exactly which check
    fired and on which column(s), e.g. `"(likely false positive —
    implausible timestamp: last_timestamp)"`.

    Verified against the exact real row that prompted this, through the
    actual code path (not a hand-simulated copy): extracted the real
    `History`/`ukm_db` files fresh from this case's own
    `EXTRACTION_FFS.zip`, called `sqlite_carve.recover_deleted_rows` and
    `artifact_runner._recover_deleted_rows` directly. Result confirmed —
    and one assumption corrected by actually testing rather than
    predicting it: `notnull_violations` came back EMPTY (`last_timestamp`
    decoded as the real integer `0`, not `NULL` — `url`/`profile_id` are
    non-NULL too, just garbage content), so the NOT-NULL check alone would
    have missed this specific row; it's `timestamp_issues: ['last_timestamp']`
    that correctly caught it — `0` under `webkit_us` decodes to
    1600-ish, far below the 2000 floor. Final label produced by the real
    `artifact_runner.py` loop code (diffed against the file on disk to
    confirm no copy drift, not just eyeballed): `"Carved — Segmentation
    Platform (UKM, urls table) (likely false positive — implausible
    timestamp: last_timestamp)"`. Regression-checked the same way against
    this case's `visits` table (0 rows, matching the pre-existing
    "confirmed negative — Chrome rebuilds/vacuums on delete" note above;
    ran clean, no exception). Not yet confirmed against a case with a
    genuine WAL-recovered POSITIVE `header_signature` row (none available
    in this session) that it stays silent (empty violations, plain
    `"(unverified match)"`) rather than false-flagging a real one — the
    6-row GTLAB-run positive cited above was a WAL-frame recovery, not
    `header_signature`, so it was never in scope for this specific check
    either way. `WRITING_ARTIFACT_PARSERS.md`'s `recoverable_tables`
    section updated in sync, same as the entry above.
  - **`chrome_cache.py` follow-ups, 2026-09-02: visibility bug fixed, then
    the custom tree view built and reverted same day, per direct user
    instruction each time.** (1) User reported the parser didn't appear
    in the "select parsers to run" dialog for the real JoshHickman FFS.
    Root cause: `ArtifactRunnerDialog._mod_matches` (`artifact_viewer.py`)
    only shows a parser when at least one of its own `files.values()`
    exists in the archive — `chrome_cache.py` deliberately declares
    `files = {}` (it enumerates the whole `Cache_Data/` directory itself
    via `_zip_names`/`_read_zip_bytes`, not a fixed file set), so that
    check was unconditionally `False` regardless of whether the cache
    genuinely existed. Fixed with a new, generic fallback convention: a
    module-level `existence_check_paths` list, checked only when
    `files.values()` is empty; `chrome_cache.py` declares
    `["cache/Cache/Cache_Data/index"]` (Simple Cache's own lookup index,
    present whenever the directory is real). Confirmed both against the
    real archive: `chrome_cache` now matches (was `False`, now `True`);
    `chrome_web_history.py` (an ordinary `files`-based parser) still
    matches unchanged — no regression to the existing convention. (2) Per
    a direct, iteratively-refined design request the same day, this
    report's own tree node was given real nested `QTreeWidget` children —
    two flat filtered tables (All Media / Orphaned Media) plus a genuine
    nested tree (one item per reconstructed page, its own resolved image/
    video children beneath it, double-click dispatching to a dedicated
    `_on_art_cache_tree_double_clicked` handler) — via a new, intentionally
    generic `tree_children` module attribute (`[(label, view_key), ...]`)
    read by `_build_report_item` and a new `_ART_CUSTOM_VIEW` role
    dispatched in `_on_art_tree_clicked`, a real, reusable framework
    extension (any future parser can opt in the same way), not a
    Chrome-only special case. The nested-tree half of it was then
    explicitly reverted the same day ("i have change my mind about the
    tree... just use the table"): all `QTreeWidget`/`QTreeWidgetItem` UI
    code, the `chrome_cache_page` stack widget, `_format_epoch_for_tree`,
    and `_on_art_cache_tree_double_clicked` were deleted outright, not
    kept dormant. `tree_children` itself survived the revert — it now
    drives THREE views that are all ordinary flat Report tables (list-
    mode `load_rows`, so Filter/Columns/sort all work normally), sharing
    one new engine method, `_art_show_chrome_cache_filtered(script_name,
    predicate, use_media_fields, row_label)`, parameterized only by a
    per-view row predicate — `_art_show_chrome_cache_media` (All Media /
    Orphaned Media, `orphaned_only` toggling whether a row's own URL is
    excluded via the same `child_asset_urls`-derived reference set
    `chrome_cache.py`'s own `run()` already computes once) and
    `_art_show_chrome_cache_reconstructed` (HTML rows with a non-empty
    `reconstructed_mhtml_path`) are now both thin predicate closures over
    it. The third view's own label was deliberately changed from "Web
    Pages" to **"Reconstructed Web Pages"**, in the `tree_children` list
    AND the on-screen label together — direct instruction, so it reads
    unambiguously as a synthesized `.mhtml` this tool built from cache
    fragments, never a file that existed on the device as such. Unlike
    the other two views, Reconstructed Web Pages DOES wire `media_fields`
    (`["reconstructed_mhtml_path"]`) — that column already means exactly
    "an openable attachment" everywhere else in this project, so the
    existing thumbnail/double-click machinery
    (`_on_art_report_double_clicked`, `MediaThumbnailDelegate`) just
    works unmodified; no bespoke double-click handler was needed once the
    tree (and its own separate dispatch path) was gone. Re-verified
    against this project's real `caseresults.db` (Android 14 JoshHickman,
    3,041 stored rows) after the revert, not just compiled: 1,095 all-
    media rows, 974 orphaned (a proper subset, consistent with 121
    distinct referenced URLs across all pages), 141 reconstructed pages —
    and confirmed the first one's `reconstructed_mhtml_path` genuinely
    exists on disk, not just present as a string.
  - **`WebpageThumbnailRenderer` — real rendered thumbnails for
    Reconstructed Web Pages** (`app/artifact_media.py`, added
    2026-09-02, prompted directly: "can we thumbnail to show in the
    reconstructed web results so it is possible to visually see which
    result it is worth checking"). `MediaThumbnailDelegate`'s existing
    thumbnail path (`ThumbnailWorker`, `_start_art_media_thumbnails`) is
    unusable for this column unmodified, for two independent reasons,
    not one: it decodes bytes straight out of the ARCHIVE zip via
    `adapter.resolve()`, but `reconstructed_mhtml_path` is a local
    filesystem path the parser itself wrote (never an archive entry —
    see the `chrome_cache.py` entry above), so that resolution step was
    always wrong for this column; and even given the right bytes, it
    only ever handles `kind == 'image'`/`'video'` (`media_viewer.py`'s
    own `ThumbnailWorker.run()`), silently `continue`-ing past anything
    else — an `.mhtml` was never going to get a thumbnail through it
    regardless. Given the explicit goal ("visually see which result is
    worth checking"), an embedded-image stand-in (cheap, fully off-
    thread, symmetric with the existing image/video path) was considered
    and explicitly rejected in favor of an ACTUAL page render — offered
    to the user as a real cost/accuracy tradeoff, not decided unilaterally
    — because a page with no distinctive embedded image (or one
    dominated by ad/tracking images) would show a misleading or blank
    stand-in for exactly the pages an examiner most needs to judge.

    `WebpageThumbnailRenderer` loads each `.mhtml` in an actual headless
    `QWebEngineView` — same lockdown as `MediaFullViewDialog._build_webpage`
    (JS off, no remote/local-file access beyond the archive itself) — and
    grabs a real pixmap, rather than decoding bytes. `QWebEngineView` has
    no way to run off the main thread (a real Qt/Chromium limitation, not
    a design choice), so unlike `ThumbnailWorker` this is NOT a
    `QThread`: one `QObject` on the main thread, processing its queue
    sequentially via signal-driven advance (`loadFinished` → grab →
    next), reusing a single view across pages rather than one per page.
    A real, previously-hit gotcha during verification: grabbing
    immediately on `loadFinished` can still catch a blank frame — the
    signal fires on load completion, not on the next compositor paint —
    confirmed directly (an offscreen-platform repro consistently
    returned an all-white 400×300 grab even 3 seconds after
    `loadFinished`, while a REAL windowing platform did paint real
    content by ~200ms); worked around with a short (150ms) settle timer
    via `QTimer.singleShot` between `loadFinished` and the actual
    `grab()`, not a guess — this was iterated on with a standalone
    headless repro script against a real reconstructed page from this
    case (`app/artifact_media.py`'s own `WebpageThumbnailRenderer`, not
    the offscreen QPA platform, is what runs in the shipped app; the
    offscreen platform was only ever this verification's own harness).
    Disk-cached in the exact SAME `casecache.db` `thumbnails` table every
    other media thumbnail already uses (`ui_path`/`file_size`/
    `thumb_size`/`data`) — `ui_path` here is a local path string rather
    than an archive one, which the table accepts fine (an opaque TEXT
    key everywhere else in this project too), so a report reopened later
    re-renders nothing, just reads back cached JPEG bytes.

    Verified end to end with a standalone headless script (`QApplication`
    + real windowing platform, not simulated), not just compiled: first
    run against a real reconstructed page from this case (the same
    Shohei Ohtani MLB article `.mhtml` cited in the entry above) produced
    a genuinely non-blank 64×48 thumbnail (108 sampled non-white pixels,
    not 0); a second run against the SAME path emitted an equally
    non-blank thumbnail (154 sampled non-white pixels) with ZERO
    `QWebEngineView` page loads (confirmed via the absence of the page's
    own "Blocked script execution" console warnings that appear on every
    real render) — the disk cache hit and returned the stored JPEG
    without re-rendering, not just "ran without crashing."
  - **`MediaFullViewDialog` follows row selection while open**
    (`app/artifact_media.py` + `app/artifact_viewer.py`, added
    2026-09-02, prompted directly: "can we make it the media viewer is
    open when you change row the content in the viewer will change to
    that rows attachment/mhtml"). Previously the dialog was opened via
    `.exec()` — application-modal, so the report table was frozen (no
    row navigation at all) for as long as it stayed open; every row
    meant close, reselect, re-double-click. Two changes, not one:
    (1) `MediaFullViewDialog.__init__`'s content-building logic was
    extracted into a new `load_content(ui_path, data)` method (a
    recursive `_clear_layout` static helper tears down the PREVIOUS
    content first — including nested layouts like `_build_video`'s own
    transport-controls row, a plain single-level `takeAt(0)` loop
    wouldn't reach those — and stops any running `QMediaPlayer`/deletes
    any temp-file copy before rebuilding), so the same dialog instance
    can be redirected to new content rather than only ever built once.
    (2) `_on_art_report_double_clicked` now calls `.show()` instead of
    `.exec()` and keeps the open dialog on `self._art_media_dialog`
    (cleared via the `finished` signal when the examiner closes it); a
    second double-click while one is already open reuses that same
    window (`load_content` + raise) instead of stacking another.
    `_on_art_report_row_selected` (already the hook that keeps the Hex
    panel following the selected row) gained one more line,
    `_art_sync_open_media_dialog(row)`, calling `load_content` on the
    open dialog for whatever row is now selected — silently a no-op if
    no dialog is open, or the newly-selected row has no attachment
    (stays on its last real content rather than closing or erroring on
    the gaps). Row→attachment resolution (first non-empty
    `media_fields` value) was ALREADY implemented once for Hex-panel
    Attachment mode; factored into a shared `_art_resolve_row_attachment`
    rather than left duplicated a second time for this feature, so
    there's exactly one definition of "this row's own attachment."
    Framework-level, not Chrome-cache-specific, despite the prompt's own
    wording — `MediaFullViewDialog` has exactly one call site in the
    whole project, so this benefits every `media_fields` report
    (WhatsApp/iMessage attachments, Chrome Offline Pages, Reconstructed
    Web Pages, ...) the same way, not just this one.

    Verified end to end with a standalone headless script, not just
    compiled: opened the dialog on a real reconstructed page from this
    case (the Ohtani MLB article), read its live DOM text back
    (`page().toPlainText()`) and confirmed it matched — then called
    `load_content` with a SECOND, unrelated real reconstructed page (a
    Fox News article, same case) and confirmed both the window title AND
    the live DOM text genuinely changed to the second page's own content
    ("Fox News ☰ LIFESTYLE Best friends break world record..."), not
    just that the call didn't raise.
  - **New parser: `artifacts/android/chrome_favicons.py`** (added
    2026-09-02, prompted directly: "can we extract the favicons artifact
    are there not sometime google search capture with this artifact").
    Chrome's Favicons database (`app_chrome/Default/Favicons`) is a
    genuinely SEPARATE SQLite file from History, not a table inside it —
    `icon_mapping.page_url` records every page Chrome fetched/associated
    a favicon for, joined through `favicons` (the icon's own url +
    `icon_type`, decoded from Chromium's real bitmask — FAVICON=1,
    TOUCH_ICON=2, TOUCH_PRECOMPOSED_ICON=4, WEB_MANIFEST_ICON=8) to
    `favicon_bitmaps` (the actual PNG bytes, written out per row as a
    real `.png` via `media_fields`, plus `last_updated`/`last_requested`).
    The user's own hunch was checked directly against this case's real
    `Favicons` file (extracted via `zip_cd_cache`/`ZipEntry`, never raw
    `zipfile`, per this project's own standing convention) before writing
    a single line of parser code: confirmed BOTH of Joshua Hickman's
    documented real Google searches ("mobile phone forensics", "shelley
    duvall") really do appear in `icon_mapping.page_url` — Chrome fetches
    the search-results page's own favicon like any other page. Then
    cross-checked against `chrome_web_history`/`chrome_search`'s own
    already-parsed output for the SAME case (not assumed): both searches
    were already fully recoverable there too, so on THIS specific case
    Favicons adds no NET-NEW recovery — stated plainly in the parser's
    own `description` rather than oversold. The reason to still ship it
    is structural: Favicons being a separate file from History means
    "Clear Browsing Data" clearing one doesn't guarantee an identical
    code path/schedule clears the other — a well-established general
    mobile-forensics rationale, not invented here, but explicitly marked
    in the description as NOT field-tested in this project the way
    `chrome_web_history.py`'s own UKM-survives-a-clear claim was (that
    one has a real GTLAB clear-history run behind it; this one doesn't
    yet) — an honest distinction between a claim that's been tested here
    and one that's merely the standard literature rationale. Also states
    a real observed oddity plainly rather than passing it through
    silently: every `favicon_bitmaps` row in this case's real data has
    `last_requested = 0` — only `last_updated` (actual network fetch
    time) is reliably populated in this Chrome build.

    Generalized `ThumbnailWorker` itself, not the third bespoke renderer
    class one might expect (`media_viewer.py`'s `run()`): favicon PNGs
    are plain still images, so the EXISTING image-decode thumbnail path
    is exactly right — it just couldn't reach a parser-generated LOCAL
    file, the identical `reconstructed_mhtml_path` gap from the
    Reconstructed Web Pages entry above, but for images/video generally
    rather than one Chrome-cache-specific column. Fixed once, at the
    root, the same `os.path.isabs(ui_path)` convention
    `hex_viewer._read_zip_bytes` already established: `ui_path` local ->
    read bytes straight off disk (size via `os.path.getsize`, no
    `adapter.resolve()`/archive read at all) rather than only ever
    resolving into the zip's own namelist. Benefits every current and
    future `media_fields` column pointing at a parser-generated local
    image/video, not just this one parser.

    Verified end to end against this case's real `Favicons` file (a
    locked-down local copy, not simulated), not just compiled: `run()`
    produced 40 rows (matching `icon_mapping`'s own real row count
    exactly), both Google-search `page_url` rows present with correctly
    decoded `icon_type_label` ("FAVICON"), correct 144×144 dimensions,
    and a written `.png` independently confirmed by the system `file`
    command to be a real "PNG image data, 144 x 144, 8-bit/color RGBA"
    image, not just a byte count.

    **`history_coverage` column, same day, direct follow-up.** Asked
    first whether Favicons should be folded into `chrome_web_history.py`
    the same way Segmentation Platform (UKM) is, with a matching row
    suppressed. Answered no, not the same pattern: UKM and History are
    both genuine PER-VISIT logs with comparable timestamps, which is
    what makes "same URL within 1 second" a safe same-event dedup;
    `icon_mapping` is one row per (page_url, icon), upserted rather than
    appended, with no comparably reliable per-row visit timestamp to
    match against (`last_requested` is unreliable here — see the entry
    above) — folding it in and suppressing matches would mean losing the
    report's own point (Favicons surviving when History/UKM don't)
    exactly when it matters least to lose it. Direct follow-up landed on
    a materially different design instead: keep every row, but add a
    column showing whether `page_url` is ALSO visible via Chrome History/
    UKM, so the examiner filters instead of the parser hiding rows.
    Implemented as `_history_coverage(page_url, history_urls, ukm_urls)`
    in `chrome_favicons.py` itself, reading `history`/`ukm_db` as two
    new `optional_files` entries used ONLY for this comparison (this
    parser still never re-reports a History/UKM row itself — that stays
    `chrome_web_history.py`'s job). Three real outcomes, not two: "Chrome
    History" / "Segmentation Platform (UKM)" / "Chrome History + UKM"
    when found, "Not in History/UKM" when genuinely checked and absent
    (the actual filterable signal requested), and "History/UKM
    unavailable" when the comparison files themselves couldn't be read —
    deliberately never conflated with "not in History/UKM": a case where
    History AND UKM are both gone and only Favicons survived is the
    single most forensically interesting state this report can be in,
    not a gap to paper over as a plain negative. Chrome Search needed no
    separate check — its own rows are already a subset of History's
    `urls` table (keyword-search join or a `search?q=` URL match — see
    `chrome_search.py`), so checking `urls` alone covers "or searches"
    from the original request too.

    Re-verified against the same real case after adding this, not just
    compiled: of 40 rows, 25 are `"Chrome History + UKM"`, 5 are
    `"Chrome History"`, and a real 10 are genuinely `"Not in
    History/UKM"` — both Google-search rows correctly read `"Chrome
    History + UKM"` (matching the entry above), and the 10 "not in"
    rows are exactly what a real explanation predicts rather than noise:
    `m.facebook.com/`, `m.youtube.com/`, `amazon.com/`,
    `en.m.wikipedia.org/`, `espn.com/`, `yahoo.com/`, `m.ebay.com/`,
    `instagram.com/`, and two AMP article pages — consistent with
    Chrome's New Tab page prefetching favicons for its own suggested-
    sites tiles without a matching History visit ever being logged, the
    exact prefetch scenario this report's own `warning` field already
    calls out — not a sign the cross-reference logic is wrong.
  - **Chrome Cache split into two top-level reports; `tree_children`/
    `_ART_CUSTOM_VIEW` fully removed** (2026-09-02, prompted directly:
    "changing chrome cache to two report rather than nested i want
    chrome cache - media and chrome cache - pages"). The single merged
    `chrome_cache.py` (one parser, one node, three custom filtered
    views under it via `tree_children` — see the two entries above for
    that mechanism's own history, including the earlier tree-to-flat-
    table revert) is now genuinely two separate top-level parsers,
    `artifacts/android/chrome_cache_media.py` ("Chrome Cache - Media")
    and `chrome_cache_pages.py` ("Chrome Cache - Pages"), sitting in the
    Chrome group exactly like every other Chrome report — no nesting, no
    bespoke tree-node handling. Since `chrome_cache.py` was the ONLY
    module ever declaring `tree_children`, the whole mechanism it
    required is now genuinely dead, not just unused by this one parser —
    removed outright rather than left as unused framework code (per this
    project's own standing convention against speculative abstractions):
    the `_ART_CUSTOM_VIEW` role constant, the `tree_children` branch in
    `_build_report_item`, its dispatch in `_on_art_tree_clicked`, and the
    three custom view methods (`_art_show_chrome_cache_filtered`/
    `_art_show_chrome_cache_media`/`_art_show_chrome_cache_reconstructed`
    plus their shared `_art_load_chrome_cache_rows` loader) are all gone
    from `artifact_viewer.py`. Both new parsers use the ordinary,
    unmodified `_art_show_report` path every ordinary SQL-backed parser
    already uses — Filter/Columns/sort/thumbnails/double-click-open all
    work exactly as they do for any other report, for free.

    The one real piece of framework surface that DID need to survive
    (Reconstructed Web Pages' real-page-render thumbnail, previously
    reached only via the custom view's own `webpage_render` flag) was
    generalized into `_art_show_report` itself as a new, plainly-named
    optional module attribute: `webpage_thumbnail_fields` — a parser
    declaring it routes its `media_fields` thumbnails through
    `WebpageThumbnailRenderer` instead of the ordinary `ThumbnailWorker`.
    `chrome_cache_pages.py` is the only current user
    (`webpage_thumbnail_fields = ["reconstructed_mhtml_path"]`), but any
    future parser reconstructing pages the same way can opt in with one
    line, no artifact_viewer.py changes required — the declarative
    surface this project's own `media_fields`/`timestamp_fields`/
    `core_fields` conventions already establish, rather than a special
    case hardcoded to Chrome Cache specifically.

    Both new parsers share ONE underlying entry-decode pass rather than
    duplicating it: `app/chrome_cache.py` gained a new
    `parse_all_entries(paths)` (the full former `run()` loop, moved
    here), returning one row per Cache_Data entry with every field
    either report needs; each script's own `run()` just filters that
    same full parse down to its own content-type (image/video for Media,
    text/html-with-a-successful-reconstruction for Pages) rather than
    re-deriving any of it. Two independent parser RUNS still each pay the
    full ~3,000-entry decode cost once (measured ~1s against this
    project's real archive, see the original chrome_cache.py entry above
    for that measurement) — duplicated I/O, not duplicated logic;
    considered and rejected a shared-cache mechanism as unwarranted
    complexity for that cost.

    Two genuinely new pieces of data, not just a reshuffle, both prompted
    directly: (1) `referenced_by_pages` on Chrome Cache - Media — the
    INVERSE of the existing page->asset mapping (`asset_referenced_by`,
    built in the same pass as the forward `page_asset_urls` map so both
    directions share one definition of "linked to a page"), listing
    every page URL that references a given media file, specifically "so
    that if there is an image that is of interest, the examiner can
    search on the pages table for it." (2) `decoded_media_path` — the
    same idea as `reconstructed_mhtml_path` but for a raw image/video
    body: the RAW cache entry (`raw_ui_path`) is still wrapped in Simple
    Cache's own container plus whatever Content-Encoding Chrome applied,
    not directly image/video-decodable, so the already-decompressed body
    is written out as its own real local file (extension guessed via
    stdlib `mimetypes.guess_extension` off the real Content-Type) and
    wired as `media_fields` — meaning Chrome Cache - Media now has real
    thumbnails at all, a genuine gap in the ORIGINAL merged report (its
    All Media/Orphaned Media views never wired media_fields, so an
    image/video row was never openable there, only in the Attachment hex
    panel) fixed as a direct consequence of this restructuring, not a
    separately-requested feature. Reuses the SAME `os.path.isabs()`
    local-file support added to `media_viewer.ThumbnailWorker` for
    `chrome_favicons.py`'s own icon PNGs (see that entry above) — no
    further framework change needed for this to just work.

    Verified end to end against this project's real archive (via
    `zip_cd_cache`/`ZipEntry`, never raw `zipfile`, and a real
    `_read_zip_bytes`-shaped reader — not simulated), not just compiled:
    `chrome_cache_media.py` produced exactly 1,095 rows and
    `chrome_cache_pages.py` exactly 141 — both matching the ORIGINAL
    merged report's own previously-verified All Media / Reconstructed
    Web Pages counts exactly, confirming the split changed structure,
    not results. 974 of the 1,095 media rows have an empty
    `referenced_by_pages` (matching the prior "974 orphaned" count
    exactly) and 121 have a non-empty one (matching "121 distinct
    referenced URLs" exactly). Picked one specific real asset (the MLB
    logo `apple-touch-icons-180x180/mlb.png`, referenced by TWO different
    real articles) and confirmed `referenced_by_pages` correctly lists
    BOTH page URLs, not just the last one processed — multi-page linkage
    genuinely works, not just single-parent tracking. Confirmed
    `decoded_media_path` files are real, valid images independently via
    the system `file` command across a sample (a real 83×42 PNG, a real
    239×37 PNG, and a real 1×1 tracking-pixel PNG — not placeholder/empty
    files), not just present-on-disk.
  - **`referenced_by_page_titles`** (`app/chrome_cache.py` +
    `chrome_cache_media.py`, 2026-09-03, direct request: "give the page
    title of the page that the cache belongs to"). `referenced_by_pages`
    already listed which page URL(s) reference a given media file;
    added the SAME list's own page `<title>`s, positionally parallel
    line-for-line — a separate column, not combined into one string,
    matching this project's own established title/url-as-separate-
    columns convention (`chrome_cache_pages.py`'s own `url`/`title`).
    Titles are captured in the SAME pass that already builds the
    page->asset/asset->page maps (`page_title_by_url`, keyed off the
    same `_extract_title` every page row already uses for its own
    `title` field), not a second lookup pass. Verified against the real
    Android 14 JoshHickman case: the shared MLB logo asset (already used
    to verify multi-page linkage in the entry above) correctly lists
    both real titles ("Shohei Ohtani hits home run No. 200 against
    Tigers", "MLB Draft overview and schedule") in the same order as
    their own URLs in `referenced_by_pages`.
  - **`render_note` — "most of the mhtml do not work" investigated,
    NOT a reconstruction bug** (`app/chrome_cache.py` +
    `chrome_cache_pages.py`, 2026-09-03, prompted directly). Investigated
    by actually rendering all 141 of this case's own real reconstructions
    in a real headless `QWebEngineView` with the exact JS-disabled
    lockdown the real viewer uses, then cross-checking against each
    page's own decoded HTML — not guessed at. Result: exactly 23 of 141
    have any real static (non-`<script>`) body content, and ALL 23
    render with real matching text; the other 118 have NOTHING outside a
    `<script>` tag — ad-auction payloads (Chrome's own Protected
    Audience/FLEDGE API interest-group JSON), tracking-sync beacons, and
    React/SPA app shells (Discord, Calendly's booking widget) whose
    entire body is one empty `<div id="root">`. With JavaScript
    deliberately disabled for forensic safety (same reasoning as
    `MediaFullViewDialog._build_webpage`'s own lockdown), those 118
    correctly render blank — that's not a broken reconstruction, it's
    Chrome having genuinely cached a JS-only shell as "the page." Real
    root cause of the user-visible symptom: nothing surfaced this
    distinction anywhere, so opening one of the 118 looked identical to
    a genuine failure.

    Fixed by surfacing the distinction, not by trying to "fix" pages that
    were never broken: `_visible_static_text_length(html)` (new,
    `app/chrome_cache.py`) strips `<head>` (title/meta — never rendered
    body text either), `<script>`, and `<style>` before counting —
    the same three things a real JS-disabled render never displays as
    page text. Below a 40-char threshold, `parse_all_entries` sets a new
    `render_note` field ("No visible static content -- renders blank
    with JavaScript disabled (script/ad-tech shell)"), added to
    `chrome_cache_pages.py`'s own `core_fields` so it's visible by
    default rather than buried in Columns. This heuristic's exact 40-char
    threshold was cross-checked against the real render of all 141 real
    pages in this case, not assumed accurate: flagged/clean counts
    (118/23) matched the real browser render's blank/rendered counts
    EXACTLY, zero discrepancy either direction.

    One real self-caught mistake during this investigation, worth
    recording: an early re-verification pass appeared to show a
    regression (96 reconstructed instead of the real 141) — traced
    directly to the test script itself, not the parser: it was run
    against the system `python3` rather than this project's own `venv`
    (missing the real `brotli`/`zstandard` dependencies — see the
    original `chrome_cache.py` entry above), so every brotli-compressed
    page failed to decompress in the TEST, not in the actual parser.
    Re-ran with `venv` correctly activated and got 141 back, matching
    the real stored case data exactly — caught before being reported to
    the user as a real regression, per this project's own standing
    "verify before claiming, especially a regression" discipline.
  - **Second `render_note` case — CSS-in-JS sites render unstyled, found
    against a real GTD ground-truth pair** (`app/chrome_cache.py`,
    2026-09-03, same-day follow-up, prompted directly: "can you look at
    this gtd since you have some screenshots to compare to the output" —
    `androidVmGTD/packages/chrome-bbc-google-001/...`, a real BBC-
    browse-then-Google-search session with real device screenshots per
    step). Used as an actual validation exercise, not just a curiosity:
    ran `chrome_web_history.py` against the GTD's own real History.db
    and diffed against `ground_truth.json`'s own `chrome_history_after`
    — all 16 ground-truth URLs recovered exactly, 0 missing. Ran the
    split Chrome Cache reports against the same GTD zip (via
    `zip_cd_cache`/`ZipEntry`, confirmed deterministic across 3 clean
    re-runs after an initial run produced a different count that could
    not be reproduced again — logged as an unresolved one-off harness
    anomaly, not chased further since it never recurred) — 10 of 96
    HTML cache entries reconstructed, 9 correctly real (matching BBC/
    Google/Reddit URLs from the plan) and 1 correctly flagged by the
    existing `render_note` (a Flourish embedded-chart widget, JS-only).

    Then went further than counting rows: rendered the reconstructed BBC
    "story-1" page (the exact page in `04_story-1_done.png`) in a real
    headless `QWebEngineView` and grabbed a screenshot to compare against
    the real one directly. Text content matched almost exactly (same
    headline, same live-viewer-count ballpark, confirmed via
    `toPlainText()` — 16,315 real chars) — but the RENDERED IMAGE was
    solid black top to bottom, nothing like the real screenshot's white/
    red BBC layout. Root cause, found by inspecting the reconstruction's
    own MIME parts: zero `text/css` parts among ~100 embedded resources,
    despite ~70 JavaScript bundles — BBC's real site ships NO
    `<link rel="stylesheet">` in its static markup at all; its actual
    styling is CSS-in-JS, injected by the same JavaScript this viewer
    deliberately disables. Confirmed this wasn't a scanning bug (
    `find_referenced_urls` already matches any `href`, stylesheet or
    not) by checking a real MLB article that DOES render correctly
    styled — it has exactly one resolved `text/css` part. This is a
    genuinely different failure mode from the `render_note` entry above
    (real content, but not real STYLING) — not discovered by counting
    rows or checking `toPlainText()` length alone, only by actually
    rendering a screenshot and comparing it side by side with real GTD
    ground truth.

    Fixed the same way as the first `render_note` case — surfaced, not
    silently left for the examiner to discover: `render_note` now also
    fires ("has real text content but may render unstyled/plain") when
    zero resolved resources have a `text/css` content-type, checked only
    for pages that already passed the visible-content check above (a
    strictly separate, second condition, not conflated with it).
    Explicitly caveated as imprecise, not claimed certain: a page styled
    entirely via inline `<style>` with no external stylesheet at all
    (AMP pages forbid external CSS by spec, so this is a REAL, not
    theoretical, false-positive risk) can trip this check while still
    rendering fine — stated directly in both the code comment and the
    parser's own `description`, not glossed over. Re-verified against
    BOTH real datasets after adding this: the real MLB article still
    reads `CLEAN` (no false positive on the case already used to derive
    the heuristic), and all 5 of the GTD's own BBC pages correctly
    flag — matching the actual solid-black render exactly.
  - **`chrome_autofill.py`: `inferred_site_title`/`inferred_site_url` —
    unverified nearest-prior-History inference** (2026-09-03, direct
    request: "can we have a column that shows the history item before
    the data was entered ... it is important to say something like not
    verified"). Classic autofill genuinely has no site/URL column at all
    in its real schema (already documented in this parser's own
    `description` before this change) — this fills that real gap with an
    explicit INFERENCE, not a claim of fact: for each row,
    `_nearest_prior_visit` (`bisect` over History's own visits/urls,
    `visit_time` converted from webkit_us to the same plain Unix-seconds
    epoch autofill's own `date_created` already uses) finds the LATEST
    History visit at or before that row's `date_created` — whatever page
    was open right before the value was first saved. History read as a
    new, purely-comparison-only `optional_files` entry (same pattern as
    `chrome_favicons.py`'s own History cross-reference) — this parser
    still never re-reports a History row itself. `inferred_site_seconds_before`
    states the real gap in seconds on every row with any match
    at all, deliberately un-gated by any assumed "too far to count"
    cutoff — per direct instruction, that threshold would be this tool's
    own guess, not the examiner's; a large gap is real information for
    the examiner to weigh, not noise to hide by omission. A new `warning`
    field states the UNVERIFIED framing explicitly (word used directly),
    plus the concrete real reason it can be wrong: classic autofill
    values are reusable by field-name on a DIFFERENT site than where they
    were first entered (the exact behavior this parser's own description
    already established), so a later autofill (not retype) could pair
    with an unrelated History visit.

    Verified against this project's real Android 14 JoshHickman case,
    not just compiled: 4 real rows (same as this parser's own previously
    field-tested count). Row 1 (an April 2024 email entry, predating this
    History file's own coverage) correctly shows an empty inference — no
    match, not a wrong guess. The other 3 rows (`username`,
    `requiredAttributes[given_name]`="Liz",
    `requiredAttributes[family_name]`="Dehner", all sharing one
    `date_created`, i.e. one form submission) all inferred the SAME real
    page, a Wickr Cognito signup URL, only ~118 seconds before — a
    genuinely corroborated match, not just temporal proximity: the field
    names themselves (`requiredAttributes[given_name]`/`[family_name]`)
    are literal AWS Cognito/OIDC attribute names, independently
    consistent with a Cognito-backed signup form, the exact kind of
    cross-check this project's own standing "verify against ground truth,
    not just the tool's own citations" discipline calls for.
  - **Chrome gap sweep — seven new parsers in one pass** (2026-09-03,
    prompted directly: "we have done a lot of messing about with the
    chrome artifacts do you think there is anything else ... that could
    hide some evidence of user web browser," then "go for login data
    but i want it all"). Investigated by actually listing every real
    file/directory under this project's own real `app_chrome/Default/`
    (via `zip_cd_cache`, not guessed from memory of Chromium's schema)
    and checking each real candidate's real row count/content before
    deciding whether to build it — several genuinely surprising findings
    came directly from that check, not from assumption:
    - `chrome_login_data.py` (`Login Data`, `logins` table) — real site
      column (origin_url/signon_realm), the exact gap classic Autofill
      doesn't have. All 3 real rows on this case turned out to be a
      DIFFERENT shape than "3 saved passwords" once actually read: 2 are
      `blacklisted_by_user=1` ("never save" declines, not credentials at
      all) and the third is a FEDERATED login (Pinterest,
      `federation_url`, real `ldehner505@gmail.com` username, zero-byte
      password by design) — `login_type` decodes this distinction
      explicitly rather than leaving three structurally different rows
      looking alike. `password_value`, when non-empty, is reported ONLY
      as `has_password` — never decrypted or shown; it's Android-
      Keystore-backed OS encryption, key material outside a filesystem
      FFS, the same "never fabricate a decryption" rule as everywhere
      else in this project.
    - `chrome_cookies.py` (`Cookies`, 703 real rows) — checked directly,
      NOT assumed from desktop Chrome's usual behavior: on this real
      Android build, cookie `value` is genuinely PLAIN TEXT
      (`encrypted_value` empty on every row checked), unlike Login
      Data's real OS-encrypted passwords — reported as-is, with the
      encrypted-blob-length fallback path still there in case a
      different platform/version populates `encrypted_value` instead.
    - `chrome_network_action_predictor.py` (`Network Action Predictor`,
      7 real rows) — real typed-omnibox-prefix evidence, confirmed on
      real data: `user_text` "mlb."/"mlb.c"/"mlb.co"/"mlb.com" were four
      separate real HIT rows, all resolving to `https://www.mlb.com/`.
      Same-day follow-up, direct instruction ("i do want to not waist
      people time so can you only have relevent ..."): `run()` now
      drops a hit row when its own `user_text` is a strict prefix of
      ANOTHER hit row for the SAME url (keeping only "mlb.com", the
      longest of the four) — a deliberate, narrow exception to this
      project's usual raw-table-as-is convention, safe specifically
      because a shorter hit prefix is ENTIRELY implied by a longer one,
      never a distinct fact by itself. MISS rows are explicitly EXEMPT
      from this collapsing, on purpose: a miss records the predictor
      guessing wrong for that exact prefix, a genuinely separate fact
      each time, not a redundant fragment — real data confirms this
      matters, not just theory: "m"/"mo"/"s" all miss against
      `mlb.com` on this same case and all three stay. Verified the
      collapsing logic against a real prefix-chain (mlb.*, 4→1 rows)
      AND a synthetic non-prefix case (`"nf"`/`"net"`/`"netflix"`, all
      hits for the same url) to confirm it only drops a TRUE prefix
      relationship, not just "shorter user_text for the same url" —
      "net" correctly collapsed into "netflix", but "nf" (not a prefix
      of "netflix" at all — "n","f" vs "n","e") correctly kept as its
      own distinct row, not wrongly dropped by a naive shortest-wins
      rule.
    - `chrome_top_sites.py` (`Top Sites`, 1 row) and
      `chrome_omnibox_shortcuts.py` (`Shortcuts`, 0 rows) — both real,
      both far sparser than History on this case; `Shortcuts` in
      particular is a real checked-EMPTY table, so this parser's own
      extraction logic is schema-verified only, not yet exercised
      against a populated row — stated plainly in its own description
      rather than implied equivalent to the others.
    - `chrome_indexeddb_origins.py` — directory-NAME-only (real origins
      are literally encoded in each subdirectory's own name under
      `IndexedDB/`, e.g. `https_www.mlb.com_0.indexeddb.leveldb`);
      deliberately does NOT parse the LevelDB content inside each
      directory — no LevelDB reader in this project's dependencies yet
      (checked: no `plyvel`, nothing hand-rolled either). 3 real origins
      on this case (cellebrite.com, www.mlb.com, www.npr.org). A first
      draft of this parser's own description claimed cellebrite.com
      appeared NOWHERE else in this case — checked directly before
      shipping and found that claim was simply wrong (1 real row already
      in `chrome_web_history`) — caught and corrected before the false
      claim went out, not after.
    - `chrome_site_settings.py` (`Preferences`, the big per-profile JSON)
      — GENERIC across all of Chromium's ~60 possible content-setting
      categories (site_engagement/media_engagement/geolocation/camera/
      mic/notifications/...) rather than one hand-built schema per
      category, since Chromium adds new ones over time and a fixed
      column set would go stale; each category's own real payload kept
      as JSON text (`setting_json`), not flattened. Real, checked
      against this case: only 8 of ~60 categories are populated at all
      (most, including every permission-prompt category, are real
      checked-empty negatives). Of the 8, `site_engagement` (real
      per-origin usage scores from actual visits) and `fedcm_idp_signin`
      (confirms `accounts.google.com` used as a federated identity
      provider) are directly meaningful — the latter independently
      corroborates `chrome_login_data.py`'s own federated Pinterest row,
      two genuinely different files agreeing with each other, not
      re-deriving the same fact from the same source twice.

    Checked and explicitly NOT built, for real, stated reasons rather
    than silently skipped: `TransportSecurity` (HSTS) stores each
    hostname as a raw SHA-256 hash, not plaintext — real content, but
    only usable via cross-referencing against hashes of URLs already
    known from elsewhere in the case, a materially different (and
    separately worth deciding on) feature from a normal parser.
    `Visited Links` is a fixed 128KB salted-fingerprint table for CSS
    `:visited` styling only — the salt itself is per-profile and not
    straightforwardly recoverable, making even hash cross-referencing
    impractical; checked its real magic bytes (`VLnk`) and size
    directly rather than assumed unusable. `Local Storage`/`Session
    Storage`/`Service Worker/CacheStorage` all need actual LevelDB
    content parsing (not just directory names, unlike IndexedDB) — real
    files confirmed present on this case, deferred as a genuine new
    reverse-engineering project, not attempted half-built. Also checked,
    per direct follow-up question about home-screen web-app shortcuts:
    no `org.chromium.webapk.*` packages exist on this device at all, and
    the Pixel Launcher's own `favorites` table has zero web-URL intents
    (only the Chrome/Brave app icons themselves) — a real checked
    negative on this specific device, and a different app's data
    (Nexus Launcher, not Chrome) regardless.

    `app_tabs/`/`Sessions/` (Android Chrome's own per-tab binary state,
    confirmed real and populated on this case — a crude string-scan of
    one real tab file already recovered real embedded URLs directly) is
    the single largest remaining gap, explicitly deferred to LAST per
    direct instruction ("leave the tab to last") — a genuine binary-
    format reverse-engineering project on the same scale as
    `chrome_cache.py`'s own Simple Cache work, not a quick add.
  - **`app/chrome_shared.py` — the gap-sweep batch's own boilerplate
    factored out, same day** (2026-09-03, direct instruction: "remember
    the idea that each artifact script is meant to be as simple as
    possible and the complexity should be in the shared script that
    stops repeated code"). Reviewed the seven parsers from the entry
    above (plus `chrome_favicons.py`/`chrome_autofill.py` from earlier
    the same day) for exactly this: every simple single-table parser was
    hand-writing the identical `sqlite3.connect`/`row_factory`/`close`
    ceremony, and `chrome_favicons.py`/`chrome_autofill.py` had each
    independently written their OWN small "read a url column"/"read
    History's own visits for cross-reference" helper rather than sharing
    one. New shared module — same "one Qt-free core module, imported by
    name" pattern `app/chrome_cache.py` already established for the
    Chrome Cache split — provides exactly three functions, each used by
    more than one real caller, not spec'd out speculatively: `query_rows`
    (the connect/row_factory/close boilerplate, now used by SEVEN parser
    files), `url_set` (distinct values of one column — generalizes
    `chrome_favicons.py`'s own former `_load_url_set`, including its
    real None-vs-empty-set "couldn't check" distinction, which moved
    into the shared function rather than staying a per-caller wrapper
    around it), and `history_visits` (History's own visits/urls joined
    and webkit_us-converted once — generalizes `chrome_autofill.py`'s
    own former `_history_events_by_time`). Each of the seven consuming
    parser scripts is now materially thinner — its own SQL plus its own
    per-row shaping, nothing else. Also fixed a real, if minor,
    inconsistency surfaced by this same review: `chrome_autofill.py`'s
    own two sqlite reads had been using a `file:...?mode=ro` URI connect,
    the ONLY parser in this whole batch still doing so — every sibling
    parser (chrome_login_data.py, chrome_cookies.py, ...) already uses a
    plain connect on the pre-extracted local temp copy, the established
    convention (see chrome_favicons.py's own earlier "match the primary-
    file convention" note) — `chrome_shared.query_rows` uses the plain
    form, so switching `chrome_autofill.py` onto it silently fixed that
    inconsistency as a side effect of sharing the code, not a separate
    change. Also cleaned up `chrome_network_action_predictor.py`'s own
    redundant-hit-prefix filter while reviewing it in the same pass:
    was keying a dedup set on Python's own `id()` builtin (works, but
    names an unrelated concept — object identity, not anything about the
    data — and shadows this project's own frequent use of `id` as a real
    column name); switched to keying on each row's own `raw_rowid`
    instead, a real, already-present, meaningful key.

    Re-verified all seven consuming parsers against this project's real
    Android 14 JoshHickman case AFTER the refactor, not assumed
    behavior-preserving from the diff alone: every one produced the
    EXACT same row counts and content as before — Login Data 3, Cookies
    703, Network Action Predictor 4 (post-dedup, same four rows: m/
    mlb.com/mo/s), Top Sites 1, Shortcuts 0, Favicons 40 (including the
    same `history_coverage` result on the same real search-query row),
    Autofill 4 (including the same real Wickr Signin inference at
    ~117.96 seconds) — confirming the refactor changed structure, not
    results, the same standard this project held the Chrome Cache report
    split to.
  - **Parser version tracking** (`app/parser_versions.py`, added
    2026-08-22): every artifact parser script has a version number, auto-
    derived from a fast content hash of its own .py source — never hand-
    authored, so it can't drift from what's actually on disk. Global
    (cross-case) JSON store, `config/parser_versions.json`, same dev/
    frozen-path convention and mtime+size load cache as
    `research_store.py`, keyed `"{platform}:{script_name}"` (same reason
    as `validation_store.py`: `ios:whatsapp`/`android:whatsapp` are
    different parsers sharing a filename). `artifact_runner.load_artifacts()`
    calls `check_version()` once per script as it imports it — a hash
    mismatch against the last-seen value (or no prior record at all) bumps
    the stored version; unchanged content is a no-op. Guarded by an
    in-memory `_checked_this_session` set so it only ever hashes a given
    script once per process, regardless of how many times
    `list_artifacts()`/`load_artifacts()` gets called afterward (every
    report open, every tree refresh) — measured at ~0.02ms per script file
    or negligible, so this never meaningfully affects startup or any
    other call site, by design (a "low-effect hash" was an explicit
    requirement, not just a nice-to-have).

    `ArtifactRunnerWorker.run()` (`app/artifact_viewer.py`) records the
    CURRENT version into `run_log.parser_version` (a new column,
    migrated the same way `completed_at` was) at the moment a parser
    actually runs — `start_run_log()` takes it as an optional kwarg.
    `ArtifactViewerMixin._update_art_version_banner`, called from
    `_art_show_report` right after `list_artifacts()` (which has already
    refreshed the store's version for every script it just loaded),
    compares that recorded version against the current one via
    `load_last_run()`. A mismatch shows a non-blocking amber banner above
    the report table — never a modal, and never blocks viewing the
    existing (older) results — with an **Update** button
    (`_on_art_update_parser_version`) that re-runs just that one parser
    via a fresh `ArtifactRunnerWorker([(script_name, mod)], ...)` and
    reloads the report on completion, mirroring what a normal multi-
    parser run already does via `parsers_completed` → `_refresh_artifact_tab`.
    `used_version is None` (a report run before this feature existed, or
    `start_run_log`'s best-effort recording failed) is treated as "nothing
    to honestly compare against" and stays quiet, rather than claiming an
    update is available when there's no real basis to say so.

    The version NUMBER is purely mechanical; a **changelog** entry is a
    separate, optional, human-authored note about WHY a specific version
    changed (`parser_versions.record_changelog(platform, script_name,
    description)` — attaches to whatever the CURRENT version is at the
    time it's called). The banner shows that note when one exists, or
    "No changelog has been recorded for this update." when the hash
    changed but nobody said why — which is the honest, expected result
    for a version bump nobody deliberately logged (e.g. an external hand-
    edit to a parser script), not a bug to chase. **Standing instruction:
    whenever an artifact parser script (`artifacts/ios|android/*.py`) is
    intentionally modified, call `parser_versions.record_changelog()`
    with a one-line description of what changed, in the same session as
    the edit** — same spirit as the CLAUDE.md-consistency-checking
    instruction elsewhere in this file: an easy thing to forget that
    quietly degrades a feature (every future version bump would show "no
    changelog recorded" instead of a real reason) rather than failing
    loudly, so it needs to be a habit, not something remembered only when
    convenient.
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
