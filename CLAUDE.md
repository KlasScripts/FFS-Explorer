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
2. `ZipMetadataWorker` (ffs-explorer.py:1146) → `app/ffs_metadata.py
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
  tables). On schema mismatch raises `OldSchemaError`.
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
helpers" below, the standard place to add the next one |
| `sqlite_carve.py` | Below-SQL-layer deleted-record recovery: freeblocks, freed/freelist pages, and full WAL frame history (not just the current valid chain `sqlite3.connect()` would replay) — decodes SQLite's on-disk record format directly, since a `DELETE`d row's bytes usually survive until something else reuses that space. Invoked automatically by `artifact_runner.py` for any table a parser names in `recoverable_tables`; no recovery code belongs in a parser script itself. Every recovered row also carries its own exact `raw_file`/`raw_offset`/`raw_length` (see `record_source` in Conventions) for the same Hex-panel Record-mode jump a live row gets. Also holds `locate_live_row` — the opposite case, finding a currently-LIVE row's on-disk cell by rowid for the Artifact Viewer's "Record" hex mode |
| `artifact_media.py` | `MediaThumbnailDelegate` (per-column QTableView delegate painting a thumbnail instead of raw path text) and `MediaFullViewDialog` (full-size image / video playback with transport controls, opened on double-click) for Report table `media_fields` columns — see Conventions below. Reuses `media_viewer.ThumbnailWorker` for decoding, so results share the Media tab's own on-disk thumbnail cache |
| `research_store.py` | Global (cross-case) artifact research notes in `config/research_status.json`, keyed by stream/bundle identity, drives row colouring |
| `parser_versions.py` | Global (cross-case) parser version tracking in `config/parser_versions.json`, same dev/frozen-path convention as `research_store.py` — a hash-derived version number per parser script plus an optional human-authored changelog; drives the Artifact Viewer's "newer parser version available" banner. See Conventions |
| `app_intelligence.py` | Per-app coverage/category/permission intelligence + a deterministic 0-10 "interest score" for apps with no artifact parser yet (added 2026-08-23, driven by the MCP `list_apps` tool — see `mcp_server.py`). Qt-free. Cross-checked against iLEAPP's `appItunesmeta.py` and ALEAPP's `packageInfo.py` (both by their respective creators) before/during building this — caught two real bugs a plan-only design missed (see below), not just a style comparison. `resolve_parser_coverage(platform)` maps each parser's declared `app_path`/`app_group` to the same app id `list_app_containers` reports (Android: last segment of `data/data/<pkg>`; iOS: `app_group` used verbatim — an iOS `app_path` pointing at an OS path like `mobile/Library/SMS` correctly resolves to no app). **iOS**: `find_bundle_container_parent` locates the archive's Bundle/Application container parent (a DIFFERENT GUID per app than its Data container, and confirmed NOT even the same path-prefix convention as the Data-container parent in real casework, so it's discovered by scanning, not hardcoded); `scan_ios_bundle_containers` then reads each Bundle container's `iTunesMetadata.plist` + `Info.plist` ONCE each — identity (`softwareVersionBundleId` / `CFBundleIdentifier`), category (`genre` / `LSApplicationCategoryType`), and permissions (`NS*UsageDescription` keys) all come from those same two reads; no separate identity lookup needed (an earlier version of this tried reverse-searching `guid_to_bundle` for the Bundle container's own GUID — confirmed that map covers ONLY `container_parents()`, i.e. Data/PluginKitPlugin/Shared-AppGroup, never Bundle containers, so it silently produced zero results for every app until this was found and fixed). **Android**: permissions do NOT live in `packages.xml` (confirmed against real casework — its `<package>` elements carry signing/install metadata only) — real per-user grants are in the separate `data/misc_de/0/apexdata/com.android.permission/runtime-permissions.xml` (`read_android_permissions`, via `_load_runtime_permissions_xml`), flat `<package><permission name=.. granted=../></package>`, no wrapper tag. Category, reversing this project's own earlier "no Android category source" conclusion, DOES exist on-device: `packages.xml`'s `<package categoryHint="N">` maps to the public, stable `ApplicationInfo.CATEGORY_*` Android SDK constants (`read_android_category`, `_ANDROID_CATEGORY_HINTS` — confirmed `categoryHint="4"` on `com.whatsapp` decodes to `CATEGORY_SOCIAL`, matching a known comms app). Both `packages.xml` and `runtime-permissions.xml` can be **binary ABX-encoded** rather than plain XML depending on device/build — confirmed via real Android 14 casework (`packages.xml` opened with `ABX\x00` magic bytes, not `<?xml`) — decoded via vendored `app/ccl_abx.py` (see its own docstring/NOTICE-equivalent header for provenance — MIT, CCL Forensics via ALEAPP, same vendoring convention as `ccl_segb`), checked by magic bytes rather than assumed from OS version. All raw-content reads only run when `ctx.raw_content_enabled`; without it, `category`/`permissions_declared` stay `None` and are scored as "unknown," never as confirmed-absent. Deliberate remaining v1 gap: `AndroidManifest.xml` (binary AXML nested inside each `base.apk` zip) is not read — `packages.xml`/`runtime-permissions.xml` already cover category+permissions at far lower cost (flat files, no zip-within-zip). `compute_interest_score` and `scan_apps` are pure/deterministic — never call out to any LLM — see Conventions for the score formula. **App-Group scoring fix** (2026-08-23, the concrete bug that motivated the LaunchServices work below): an App-Group container (e.g. `group.ch.threema`) has no `CFBundleIdentifier` of its own, so it used to score from nothing — `_load_group_owner_index` (built from the now-precomputed `app_registry` table, not re-derived) maps `{app_group_id: owning_bundle_id}`; `scan_apps` looks an App-Group row's id up there and borrows the owning bundle's category/permissions/`has_parser` for scoring instead of treating it as an identity-less app. **`find_evidence_databases`** (renamed from `find_evidence_database`, 2026-08-24) returns multiple ranked candidates rather than one guessed winner — see the "find_evidence_database → find_evidence_databases redesign" paragraph below for why and how. **Vault/cloud-storage scoring fix** (2026-08-24, same day, prompted by a user question: "why is this only focused on messaging apps?"). The `comms_signal` breakdown component — renamed `high_interest_category` — was actively counter-productive for vault (hide-and-lock) apps: its `category_noncomms` branch scored a declared `Utilities`/`Calculator` category with no sensitive permission as a flat 0, treating "looks boring" as confirmed-uninteresting — exactly the profile a vault app is built to present. Changed to score 2 (undetermined, same as the true-unknown case) instead of 0. Added `_KNOWN_VAULT_SUBSTRINGS` (galleryVault, vaulty, calculatorLockVault, NQ_Vault, playgroundVault — Android package names confirmed via ALEAPP; no iOS vault bundle id listed, since iLEAPP's own `nsVault.py` notes its identity is inferred from a filename, "not bundle-specific") and `_KNOWN_CLOUD_STORAGE_SUBSTRINGS` (Dropbox, OneDrive/`microsoft.skydrive`, Google Drive/`google.android.apps.docs`, ProtonDrive — confirmed via iLEAPP/ALEAPP), both scored the same +4 as a comms/social match. **`find_hidden_vault_storage`** (added 2026-08-24, same day) — presence-only detection (same pattern as `find_webview_storage`) of a vault app's raw hidden-media folder by PATH-substring signature (`.Calculator_Lock`, `.galleryvault_`, `applocker/vault`, `folderlockadvanced` — confirmed via ALEAPP's `calculatorLockVault.py`/`galleryVault.py`/`playgroundVault.py`), since several vault apps dump renamed/extensionless media into such a folder with NO database at all — invisible to `find_evidence_databases` by design, not just outranked. `scan_apps`'s tie-break ranks a hit here (or a `known_real_store`-cited `evidence_databases` candidate) at the very top, above a merely-clean unconfirmed hit. **Bare-filename fix, since SUPERSEDED** (2026-08-24, found while adding Chrome to `known_evidence_patterns.py`, replaced the same day): `find_evidence_databases` only matched by extension (`_DB_EXTENSIONS`), so Chromium-family browsers' real stores (`History`, `Cookies`, `Web Data`, etc. — confirmed no extension at all, per ALEAPP's `chromeCookies.py`/`chromeLoginData.py`/etc.) were invisible as CANDIDATES, not just outranked. First fix was `_DB_BARE_FILENAMES`, a hardcoded exact-name allowlist — removed later the same day once the magic-byte fallback below made it redundant AND it was recognized as the same whack-a-mole pattern this project moved away from for noise-filtering (one more specific name to maintain, rather than a general fix). See "Row-merge + magic-byte fallback" below for what replaced it. **Row-merge + magic-byte fallback** (2026-08-24, same day, prompted by two user questions after a non-circular validation test — see below). `scan_apps` now groups Data/Application + Shared/AppGroup containers belonging to the SAME app into ONE row (via `group_owner`, already built for the scoring/citation identity fixes above) instead of one row per physical container — `containers` on each row lists every physical folder merged (`{app_id, path, kind}`), and `evidence_databases`/`webview_storage`/`hidden_vault_storage` are pooled and re-ranked across all of them together. PluginKitPlugin (extension) containers deliberately stay separate — self-describing dotted-suffix ids, not opaque group ids. Confirmed necessary and sufficient by a proper BLIND test (independent ground truth, not a citation replayed through the tool): Telegram (`ph.telegra.Telegraph`), never added to `known_evidence_patterns.py` beforehand, had ALL its real data in the App-Group container while its Data container (the one an LLM would check first, matching the bundle id it already knows) was empty — merging fixed that half. The other half: `find_evidence_databases` gained an optional `read_bytes` parameter (Tier 3, `raw_content_enabled`-gated) — for a file with NO extension at all that survived exclusion, its header is checked via `header_scan.classify_magic()` rather than dropped. Telegram's real store turned out to be literally named `db_sqlite` (underscore, not `.sqlite`) — invisible to the extension check, and would have been invisible to the earlier bare-filename allowlist too (a per-name list can never anticipate every app's naming convention) — this content-based fallback is what actually generalizes, which is why `_DB_BARE_FILENAMES` was removed once this existed rather than kept alongside it. Scoped to dot-less filenames only (bounded cost; most real files carry a self-describing extension) and capped at `_MAGIC_CHECK_MAX_BYTES` (64MB). Verified end-to-end against the real case: found `db_sqlite` (9.4MB, now ranked #1 and since cited) plus 3 more real Telegram Postbox files, and incidentally also caught Apple's own extensionless `CloudKit/cloudd_db/db` cache. Full-case `scan_apps` with this enabled: 3.9s, no performance concern. **`known_real_store` REMOVED from the live tool** (2026-08-25, one day later, per a further user design question): citing iLEAPP/ALEAPP inline in `evidence_databases`' live output was itself reconsidered — it risks testing whether a hand-fed answer key is right rather than whether the general ranking mechanism (size/WAL/noise-filtering/magic-byte detection) actually works on its own, and iLEAPP/ALEAPP are themselves live GitHub projects that keep changing, so an embedded snapshot would silently go stale. `find_evidence_databases` no longer takes `platform`/`app_id` params or imports any pattern-matching module; `evidence_databases` candidates no longer carry `known_real_store` at all. The mined cross-reference data moved to `scripts/leapp_evidence_fixtures.py` (validation-only, not imported by `app/`) and is now consumed only by `scripts/validate_evidence_ranking.py`, which loads a real case and checks whether the unaided mechanism still surfaces each fixture's known-real file in its own top-N — confirmed genuinely informative, not just theoretical: re-running it after the citation's removal immediately caught a real regression — TikTok's real message stores (`AwemeIM.db`, `ChatFiles/db.sqlite`) no longer rank in the top 5 unaided, buried under `tracker_v3.sqlite`/`tttracker_custom_event.sqlite`/`passportStorage/manifest.sqlite`/a second `tracker.sqlite`/`feature_engineering.db`, all uncited telemetry. `scripts/leapp_coverage_report.py` (same day) generates a LIVE comparison of this project's real parser coverage (`artifacts/ios|android/*.py`) against iLEAPP/ALEAPP's own declared categories, scanned fresh from the local checkouts every run rather than a hardcoded list — the "which apps do they support that we don't yet" roadmap view, deliberately regenerated rather than committed as a snapshot that would go stale the same way an embedded citation would have. **Escalation over silent truncation, and richer known-app info** (2026-08-25, same day, per the direct user objection: "if it silently discards stuff then it is a problem" — a per-app noise-list patch for the TikTok gap above was explicitly rejected in favor of a general fix). `find_evidence_databases` now returns `(candidates, total_found)` — the row's own `evidence_databases_total` field exposes when more candidates exist than the 5 shown, instead of silently cutting them. New MCP tool `list_evidence_candidates(app_id, limit=50)` reuses list_apps' cached container list to page arbitrarily deep into the SAME pool — confirmed closing the TikTok gap exactly as designed: `AwemeIM.db`/`ChatFiles/db.sqlite` rank #7/#12 among 34 real candidates, invisible in the default top-5 but immediately visible via this tool, and independently confirmed message-shaped by schema (`contactName`/`latestChatTimestamp`) against the top-5's telemetry shape (`track_id`/`entire_log`). `list_apps`' own docstring now instructs checking `evidence_databases_total` and escalating rather than concluding "no evidence" when the top 5 all read as telemetry. `scripts/validate_evidence_ranking.py` updated to report an ESCALATE status distinct from a true FAIL for exactly this case. Separately, prompted by "what are the most relevant apps, last used date, what is the app used for" — three fields were missing entirely, not just hard to reach: `display_name` (the real app name, e.g. 'TikTok' — sourced from `app_registry`, already computed but never surfaced; a bare bundle id isn't clear about "what app is this"), `last_activity_utc` (the existing raw `last_activity` nanosecond-epoch integer, now ALSO returned formatted and UTC-labeled via a new Qt-free `_format_last_activity` — the unlabeled-timestamp shape this project's own Conventions section explicitly warns against), and `known_location` (`{app_path_or_group, has_media_fields}` for any `has_parser=true` app, via new `resolve_parser_locations()` — this project's OWN parser already declares exactly where an app's database is and whether it tracks media/attachments, but a parsed row previously surfaced none of that, going quiet exactly where it should have been most informative). Schema bumped v9→v11 across this and the row-merge/escalation work. **`last_activity_data`/`last_activity_shared`** (2026-08-25, schema v11→v12, for the GUI Apps-node table below): the SAME per-member walk that already computes the merged `last_activity` now also buckets each container's own last-touched time by `kind` ('data' vs 'app_group') into two additional running maxes — zero extra file-walk cost, just not collapsing the per-kind values before returning. The pre-existing merged `last_activity`/`last_activity_utc` fields are UNCHANGED (kept for backward compatibility with existing `list_apps` callers) — these are additive, not a replacement. |

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
| `mcp_server.py` | Read-only MCP server (tools + prompts) over processed case data; Qt-free; audit-logs every tool call to `run_log` (run_type `mcp`). Tier 2: `list_apps` (added 2026-08-23) wraps `app_intelligence.scan_apps` — cached in `casecache.db`'s `app_intelligence` table, recomputed when the archive's indexed file count OR raw_content_enabled state has changed since the last scan (`app_intelligence_scan_key`, a `blobs` entry — not a new schema concept); works with or without raw content access, degrading gracefully rather than erroring. Note: the first scan of a large case walks every file under every app container in pure Python (~830k entries took roughly a minute in the case this was built against) — acceptable as a one-time cached cost, same tradeoff this project already made for media-thumbnail pre-warming (see `artifact_media.py` above), but worth knowing before assuming a slow first call is a hang. Tier 3 (opt-in, separate consent checkbox): `get_sqlite_schema`/`sample_sqlite_rows` extract any archive SQLite db to a locked-down read-only temp copy — no arbitrary raw SQL, no generic file-read tool. `build_artifact_parser(bundle_id)` prompt chains them into a drafted `artifacts/ios\|android/`-format parser for human review. `get_app_data_locations(bundle_id)` (added 2026-08-23) is a thin direct read of the already-built `app_registry` table — Bundle container, Data container, every App Group path, every PluginKit-extension bundle id, in one call, no fresh parsing at call time; the direct answer to "I don't care which folder holds it, I want everywhere this app's data could be" |
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
