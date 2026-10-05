# FFS Explorer — codebase map

Desktop forensics tool (PySide6) for browsing iOS/Android Full File System
extractions (Cellebrite / GrayKey zips) **without extracting them** — direct
seeks into the archive, a per-case folder for caches/results. The tool exists
around five things: knowing which of the two real FFS formats you're looking
at; scanning file headers so content is typed correctly even when the
extension lies; recovering media embedded *inside* other files (SQLite BLOBs,
plist NSData); letting an examiner double-click into native mobile formats
(plist, SQLite, SEGB, XML, JSON, LevelDB/IndexedDB, ABX, raw protobuf) and
just see the content; and a declarative scripting system for writing new
per-app parsers fast. Everything else in the app (bookmarks, the Media
Browser, keyword search, the tree UI) is built on top of those five.

**Always run with the project venv:** `venv/bin/python ffs-explorer.py`
(system Python lacks PySide6). `ffs-explorer.py` is ~8k lines — use the
section map below instead of reading it whole.

## Architecture in one paragraph

`ffs-explorer.py` builds the main window (`FastZipBrowser`) and owns archive
loading, the file tree/table, filtering, bookmarks, recents, and worker
lifecycle. Heavy/specialised features live in `app/` as **mixins**
(`HexViewerMixin`, `MediaViewerMixin`, `KeywordSearchMixin`,
`ArtifactViewerMixin`, `SqliteViewerMixin`, `SegbViewerMixin`,
`LevelDbViewerMixin`, `TimestampDisplayMixin`) that `FastZipBrowser` inherits
— "the hex tab" means the mixin file, not the main file. Format *detection
and path mapping* (Cellebrite vs GrayKey, iOS vs Android) lives in
`app/adapters/`; other code branches on `ffs_adapter.format` where behaviour
genuinely differs, but raw format sniffing belongs only in the adapters.
Long work runs in `QThread` workers; first-open metadata parsing runs in a
separate **process** (`ffs_metadata.py`) since it's CPU-bound and must not
block the GUI.

## Data flow (opening an archive)

1. `FastZipBrowser.start_loading()` → case dir chosen/created.
2. `ZipMetadataWorker` → `app/ffs_metadata.py parse_archive_metadata()` in a
   child process: central-directory parse, `ui_metadata` build, folder
   tree/sizes; snapshot persisted to the case dir (msgpack) so re-opens are
   instant.
3. `FfsAdapter` (`app/adapters/ffs.py`) detects format and maps `ui_path`
   (what the UI shows) ⇄ physical zip path; GUID→bundle-id map built for
   `/private/var/mobile/…` containers.
4. Zip reading goes through `zip_cd_cache.py` (a `.zcd` central-directory
   sidecar, so a network-hosted archive opens fast) plus
   `app/zip_entry.ZipEntry` (direct offset seek for a STORED entry, no
   decompression). Every viewer receives a `ZipEntry` and never cares
   whether the entry is stored or deflated.

---

## 1. The two FFS formats & platform detection

`FfsAdapter.format` is one of three values:

- `FORMAT_CELLEBRITE` — the classic Cellebrite FFS layout. Has an
  `old_layout` sub-flag (older Cellebrite extractions keyed every path with a
  literal `private/var/` prefix that newer ones don't) — resolved once at
  detection time from the archive's own namelist, never guessed per-call.
- `FORMAT_GRAYKEY` — GrayKey's own layout, which (on iOS) *always* needs a
  `private/var/` prefix prepended to a ui_path before it resolves to a real
  zip entry.
- `FORMAT_ZIP_EXTRAS` — a plain Android zip carrying UT/UX extra fields
  instead of a Cellebrite/GrayKey wrapper (no msgpack manifest). Always
  Android, unambiguously, by format alone.

**Format is a layout, not a tool identification.** `FORMAT_GRAYKEY`/
`FORMAT_CELLEBRITE` are each genuinely iOS/Android-ambiguous — a real
Magnet-acquired Android device can detect as `FORMAT_GRAYKEY`, and a real
Android device can detect as plain `FORMAT_CELLEBRITE`. Never label a report
"Cellebrite acquisition" / "GrayKey acquisition" from `.format` alone — it
describes the archive's own internal path conventions, nothing about which
physical tool pulled it.

**`FfsAdapter.is_android(folder_map=None)`** is the one canonical "is this
archive Android" check — `FORMAT_ZIP_EXTRAS` is unambiguous; the other two
formats check for `'data/data'` in the real folder_map (confirmed absent
from every real iOS archive). Never hand-roll a second Android check —
several call sites used to, independently, each missing a different case.

**`FfsAdapter.container_parents(folder_map=None)`** returns the real
per-app-container parent paths for the current platform — the single
canonical list reused by app-intelligence scanning, the User Media filter,
and the "Selected Only" tree's own landmark-folding (auto-expands down to
an app's own identifying folder or a recognized media folder, no
further), so none of
them can silently drift onto a different idea of "where an app's data
lives":
- iOS: `mobile/Containers/Data/Application`, `mobile/Containers/Data/
  PluginKitPlugin`, `mobile/Containers/Shared/AppGroup` (each with a
  `private/var/` prefix on GrayKey).
- Android: `data/data`.

**iOS app registry.** Per-container GUID→bundle-id resolution has two
sources, layered: `FfsAdapter.build_app_registry()` parses Apple's own
LaunchServices csstore (`com.apple.LaunchServices-<version>-v2.csstore`,
vendored parser in `app/csstore.py`) once at first-open — one file yields
bundle id, Team ID, display name, both container GUIDs, and App-Group/
PluginKit links for most apps in one pass. The older per-container
`.com.apple.mobile_container_manager.metadata.plist` read
(`_build_guid_bundle_map`) still runs afterward as a narrower top-up for
whatever the csstore didn't resolve (it's sometimes absent on GrayKey
extractions, and can only ever yield a bare GUID→bundle-id pair anyway).
Android has no GUID indirection to resolve — `build_app_registry` is a
no-op there.

## 2. Header-scan tiers

Extension-based typing is the default and is sometimes wrong (a renamed
database, a generic `.bin` MMS part that's really a JPEG). The header scan
reads real magic bytes to correct the `Type` column, and every other feature
that depends on "is this file really a database/image/archive" — Keyword
Search's coverage reminder, the embedded-media sweep's candidate list, the
raw file-browser preview — reads from the SAME `_header_type_overrides` dict
this scan populates, never a second guess.

Four levels, persisted per case (`header_scan_tier`/`header_scan_complete_tier`
in `case_settings`):

| Tier | Scope |
|---|---|
| 0 | Off — extension-based typing only |
| 1 | Unknown-extension files, app/user-accessible areas |
| 2 | All files, app/user-accessible areas |
| 3 | All files, everywhere |

A case always starts at tier 0 — nothing runs automatically at case
creation; the examiner picks a tier (and other processing options) from
**Process Case**, which defaults to Tier 1 pre-ticked. The Search Coverage
reminder (shown before a keyword search if coverage looks incomplete) is
tier-aware: it states which tier is current, what that tier does and
doesn't check, and what a low tier risks missing (a renamed archive or
database that header-scanning alone would have caught).

A real, confirmed trap: `_HEADER_SCAN_TIER_TEXT`'s own wording is the single
source of truth for what each tier covers — any other UI surface describing
tier coverage (the Search Coverage dialog, Process Case's own status label)
reads the SAME shared text/coverage dict rather than keeping an independent
copy that could drift.

## 3. Recovering media embedded inside other files

The embedded-media sweep (`app/embedded_media_scan.py`) finds real images/
video that live **inside** a SQLite BLOB cell or a plist NSData value —
content invisible to an ordinary file listing because it was never its own
file. Schema-agnostic by design (unlike the per-app `artifacts/` parsers):
SQLite's own record format is self-describing per field (a BLOB's serial
type encodes its own byte length), so a real photo can be found and
extracted without knowing which table or app it belongs to.

- **Live content**: a genuine SQL query per table (`SELECT rowid, * FROM
  table`, via `artifact_runner.open_db_readonly`) so SQLite's own engine
  transparently follows a BLOB's overflow-page chain — recovers a full-size
  photo regardless of how many pages it spans. Every column of every row is
  checked via `isinstance(value, bytes)` — no sampling, no per-table
  assumption.
- **Deleted content**: reuses `sqlite_carve.py`'s existing carving
  primitives unmodified (freed/freelist pages, WAL frame history) —
  genuinely recovers a deleted thumbnail/sticker small enough to have never
  needed an overflow page. A disclosed, real limitation: a *deleted* photo
  large enough to have needed an overflow page is not recovered (the
  carving primitives don't follow a dangling overflow pointer); an ordinary
  single-row `DELETE` on an otherwise-live page is also not recovered
  without a known table schema to derive a carving signature from (the
  per-app `recoverable_tables` parsers cover that case instead).
- **plist/NSData**: the same sweep walks a plist's own value tree looking
  for real image/video magic bytes inside any NSData, including a NESTED
  bplist embedded inside an NSData value (confirmed real in iOS
  `_atsContext`-style fields).
- **HTTP-wrapped bodies**: a blob that's really a captured HTTP response
  (status line + headers + a compressed body) is unwrapped via
  `artifact_runner.decompress_http_body` before the magic-byte check.

**Noise control, two layers, both examiner-curated, neither silent:**
- `embedded_media_skip_list.py` — "don't even scan this known-noise
  database" (e.g. a bundled SDK's own icon cache), checked *before* reading
  a candidate file at all. Per-platform, basename/path-fragment/wildcard
  matching, app-scoped by default from a right-click so excluding one app's
  copy of a shared cache filename never silently excludes a different app's
  copy of the same name.
- `user_media_ignore_list.py` — a GLOBAL, examiner-editable exclusion layer
  on the separate "User Media" filter (below), not on the sweep itself.

**Results are real, precious findings**, not a rebuildable cache —
`caseresults.db`'s `embedded_media_hits` table, `UNIQUE(source_ui_path,
location, sha256)` so a re-run never duplicates. A hit surfaces two ways:
the standalone "Embedded Media" review button in the Media Browser, and as a
REAL virtual child of its source file in the File Browser tree (so browsing
into `msgstore.db` shows its own recovered images as files inside it,
named `<dbname>-<column>-<rowid>` for a live hit or `<dbname>-carved` for a
recovered one — deliberately no column/rowid on a carved hit, since its
identity is inherently less certain than a live row's).

A `processing_registry` table (`casecache.db`) tracks which files have
already been swept under which version of the sweep's own decode logic (a
blake2b hash of the module's own source, so the NEXT improvement to the
sweep automatically re-offers every previously-scanned file) — avoids
re-reading a huge database on every bulk run while still letting a single
file be manually re-swept on demand.

## 4. In-app format viewers

Every one of these is reachable by double-clicking the file in the ordinary
File Browser — no separate "open as…" step, and the raw bytes are never
modified (all viewers are read-only toward the archive).

| Format | How it's decoded |
|---|---|
| **Binary plist** (`bplist00`) | `plistlib`, wrapped by `artifact_runner.decode_plist_blob` for the NSKeyedArchiver-wrapped case (detects `$archiver == 'NSKeyedArchiver'`, re-decodes via `nska_deserialize`). A real stdlib defect — a single CFDate field outside Python's representable range raises and loses the WHOLE file — is worked around via `_read_binary_plist_tolerantly` (ported from iLEAPP), catching the error per-field instead of per-file. |
| **XML plist / generic XML** | Pretty-printed via `minidom`. |
| **SQLite** | Dedicated Database tab: temp-copy extraction, table browser, a WAL net-change diff view. The raw-carve machinery (`sqlite_carve.py`) also drives deleted-record recovery for `recoverable_tables`-declaring parsers and the "Interpret as SQL Record" Keyword Search feature (jump from a raw byte hit straight to the live/carved row it belongs to). |
| **SEGB / Biome** | `app/segb_viewer.py`, via vendored `ccl_segb` + `blackboxprotobuf` for the embedded protobuf payloads. Built-in per-stream typedefs in `segb_schemas.py`; an examiner can author/override a schema, persisted in `caseresults.db`. |
| **JSON** | Pretty-printed directly. |
| **LevelDB / IndexedDB** | `app/leveldb_viewer.py`. A real LevelDB-shaped folder decodes automatically the first time it's navigated into — every record becomes a synthetic virtual "file" (key as filename, value as content) injected into the same `folder_map`/`full_metadata` machinery an extracted nested archive uses, so it's browsable through the ordinary file browser with no bespoke UI. IndexedDB gets a REAL schema-aware decoder (vendored `ccl_chromium_indexeddb.py`, pinned to a specific upstream commit) — genuine Blink/V8 value deserialization, not a byte-guess. Every other LevelDB store (Chrome Local/Session Storage, GMS CryptAuth, raw protobuf stores, …) goes through a generic, schema-less classifier (`classify_leveldb_value`) that tries real JSON/XML/plist/protobuf structural validation before falling back to a labeled hex dump — deliberately never guesses past what it can structurally confirm. A record's own Type is written into the SAME `_header_type_overrides` dict the header scan uses, so the File Browser's Type-column filter works on decoded LevelDB content for free. |
| **ABX** (Android Binary XML) | Vendored `app/ccl_abx.py` (from ALEAPP), with two real bugs found and fixed in the vendored reader itself before trusting it: an unsigned/signed 16-bit length mixup that silently truncated the rest of a document past one long value, and a missing `multi_root` retry for a real, common multi-root-document shape. Both fixed with a zero-regression re-run across hundreds of real ABX files. Routed through the same renderer as plain XML once decoded. |
| **Protocol Buffers** | No universal protobuf viewer (there's no schema registry to decode against) — SEGB's own payloads use `blackboxprotobuf`'s schema-less decode; the LevelDB classifier's `classify_leveldb_value` does schema-less structural detection (varint tag/wire-type scanning) to *recognize* a value as protobuf-shaped even when it can't fully decode it, labeling it rather than misclassifying it as garbled text. |

## 5. Artifact parser scripting system

A per-app parser is a plain script in `artifacts/ios/` or `artifacts/android/`
with a `run(ctx, paths) -> list[dict]` function. The point of the whole
system is that a new parser should be mostly **declarations**, not hand-rolled
plumbing — see **`WRITING_ARTIFACT_PARSERS.md`** (repo root) for the full,
example-driven how-to; this section is the index, not a replacement for it.

**Declarative module-level attributes a parser can set:**

- `app_path` / `app_group` — where the app's container lives (a fixed OS
  path, or a GUID-indirected App Group resolved via `guid_to_bundle` at run
  time).
- `files` / `optional_files` — which files inside the container to extract;
  `optional_files` missing is fine, `files` missing fails the run.
- `media_fields` — output fields holding a ui_path to an attachment; wires
  automatic thumbnails in the Report table and the Hex-panel "Attachment"
  mode.
- `timestamp_fields` — `{field_name: unit_code}` (`"s"`, `"ms"`,
  `"cocoa_s"`, `"cocoa_ns"`, `"webkit_us"`) — raw values are stored, format
  is applied at display time per the case's UTC/handset/acquisition/manual
  setting (never bake a formatted string into stored output).
- `record_source` — one or more `{label, file_key, table/table_field,
  rowid_fields}` entries describing exactly which raw DB cell(s) a row's
  own content came from, so the Hex panel's "Record" mode can jump straight
  to the real bytes for citation. A JOIN-heavy parser declares one entry
  per joined table, not just the main one. `source_match`/`presence_fields`
  narrow which entries apply to a given row when a report merges more than
  one real query.
- `recoverable_tables` — names a table whose deleted rows should be carved
  (`sqlite_carve.py`) automatically; never hand-roll recovery logic in a
  parser. A carved row's own confidence is gated: a `header_signature`
  match (the weakest of the four carving methods — no rowid to cross-check
  against) is labeled `(unverified match)`; any carved row (any method)
  failing a real NOT-NULL or timestamp-plausibility check against the
  table's own schema is labeled `(likely false positive — reason)` — never
  silently dropped, always still shown with the caveat attached.
- `hidden_fields` — output fields kept for internal use (a `record_source`
  join key) but never shown as a Report column.
- `core_fields` — the default-visible column subset; a report the examiner
  has never customized shows exactly these instead of every column.
- `device_wide = True` — a parser that scans the whole archive rather than
  one app (e.g. the App Report parsers), given a `CaseContext` instead of a
  single container's `paths`.
- `requires_nested_extraction` — a list of subpaths to extract (as nested
  archives) before `run()` is called, for a parser whose real data sits
  inside an embedded zip.
- `view_mode = "document"` — render as a single rendered-markdown page
  instead of a table, for a report that's a handful of unrelated facts
  (e.g. Device Info) rather than N rows of one shape.

**Shared helper library** (`app/artifact_runner.py`, import directly, e.g.
`from artifact_runner import open_db_readonly`):

- `open_db_readonly(path)` — the one correct way to open an evidence SQLite
  file: a `file:...?mode=ro` URI connect. A plain `sqlite3.connect()` can
  silently checkpoint-and-destroy a `-wal` sidecar's own deleted-content
  traces on close, even for a read-only query — confirmed directly with a
  synthetic test before this existed. An `ATTACH`ed second database needs
  its OWN `mode=ro` URI too — attaching read-only to a read-only connection
  does not make the attached db read-only automatically.
- `open_leveldb(paths, relative_dir)` — extract-then-open a LevelDB
  directory, skip-if-already-extracted.
- `decode_plist_blob` — NSKeyedArchiver-aware plist decode (see format
  table above).
- `text_plausible(value)` — control-character-ratio check used both by the
  carving confidence gate and by the LevelDB/key-naming classifiers to
  decide whether a decoded byte string is real text.
- `parse_chromium_dom_storage_key` / `resolve_chromium_session_storage_names`
  — Chromium's own Local/Session Storage key encoding, shared by the
  generic LevelDB record-naming logic and the dedicated
  `chrome_local_storage.py` parser so neither can drift from the other's
  idea of what a clean record name looks like.
- `decompress_http_body` — shared by `chrome_cache.py`'s own HTTP-cache
  parsing and the embedded-media sweep's HTTP-response unwrap.
- `first_nonempty`, `missing_ref_label`, `resolve_path_after_marker` — small
  generic helpers factored out after being found hand-duplicated across
  several parsers in a deliberate full-project sweep; add the next one here
  once a second real parser needs the same small building block, not
  speculatively.

**Standing rule for every parser, old or new**: when writing or reviewing
one, check (1) whether the app has attachments and whether the underlying
path is a real local path vs. a `content://`/remote URI (declare
`media_fields` only for the former, and verify the constructed path
actually resolves against real archive bytes); (2) whether real content
might be extensionless (check `evidence_databases`, which is magic-byte
aware) or sitting inside an unextracted embedded archive (check
`embedded_archives`) before concluding "no evidence."

---

## Case folder databases (`app/db_utils.py`)

- **`casecache.db`** — rebuildable cache, safe to delete/rebuild on schema
  mismatch: thumbnails, folder-size/search-index blobs, `header_types`,
  `guid_bundle`, the nested-archive index, `app_intelligence` (per-app
  coverage/scoring), `app_registry` (iOS bundle/container/App-Group map),
  `evidence_page_map` (per-file SQLite page-ownership, for the "Interpret
  as SQL Record" jump), `leveldb_search_index` (decoded LevelDB/IndexedDB
  text, indexed for Keyword Search), `processing_registry` (generic
  "already swept this file under this logic version" tracker, currently
  used by the embedded-media sweep).
- **`caseresults.db`** — precious, never auto-deleted: `search_index`/
  `search_results`, `bookmarks` (with a `color` per group), `device_info`,
  `run_log` (records each parser run's own version), `segb_schemas`,
  `case_settings`, `artifact_<name>` tables (one per parser's output),
  `ai_summaries`, `embedded_media_hits`, `media_seen` (Media Browser's own
  "Not Relevant" tracking — one row per file marked reviewed).

## app/ modules

| File | What it is |
|---|---|
| `adapters/ffs.py` | `FfsAdapter` — format/platform detection, path resolution, `container_parents()`, `is_android()`, `build_app_registry()`. |
| `csstore.py` | Vendored (MIT) Apple LaunchServices csstore (`bdsl` magic) parser — the primary source of iOS's `app_registry`. |
| `adapters/graykey.py` | GrayKey zip metadata (timestamps/xattrs from extra fields). |
| `ffs_metadata.py` | Qt-free first-open parsing (runs in a child process); msgpack snapshot pack/unpack; `load_snapshot_from_case` reloads it headlessly (used by `device_wide` parsers). |
| `db_utils.py` | Case DB open/schema/save-load helpers. |
| `zip_entry.py` / `zip_reader.py` | `ZipEntry` — the universal archive-read handle every viewer uses. |
| `zip_cd_cache.py` | `.zcd` central-directory sidecar cache; in-memory memoization of the parsed `ZipInfo` list per `.zcd` file (avoids a real multi-second re-parse on every call). |
| `header_scan.py` | Magic-byte/text file-type detection (`classify_magic`) — see Header-scan tiers above. |
| `embedded_media_scan.py` | The embedded-media sweep — see section 3 above. |
| `embedded_media_skip_list.py` | Known-noise database skip list — see section 3. |
| `user_media_ignore_list.py` | Global exclusion list for the Media Browser's "User Media" filter. |
| `dialog_helpers.py` | Shared Qt dialog-construction helpers (button rows, note/error labels, warning/error colors) — no case/business logic. |
| `timestamp_display.py` | `TimestampDisplayMixin` — the timestamp-mode banner, `format_ts`, the Timestamp Display dialog. See Conventions below. |
| `device_timezone.py` | Best-effort handset/acquisition/system timezone detection — never applied silently. |
| `keyword_search.py` | Search workers (main archive + nested archives + LevelDB index), `KeywordSearchMixin`, `SqlHitInterpretWorker` ("Interpret as SQL Record"). |
| `hex_viewer.py` | Hex tab; the Record/Attachment toggle + joined-record-source combo (content loaded by `ArtifactViewerMixin`, not this file). |
| `media_viewer.py` | Media Browser: paginated thumbnail grid (`MediaFileListModel`/`MediaGridDelegate`, a `QListView` + paint delegate, not one widget per file), in-process video decode via PyAV, `MediaFullViewDialog` (zoomable/rotatable full-size image view, video playback), `_load_qimage` (the one shared image-decode entry point — applies EXIF/TIFF orientation automatically via `QImageReader.setAutoTransform`, falls back to `pillow_heif` only for HEIC/HEIF once Qt's own decode has failed). Owns the "Not Relevant"/Undo seen-tracking row and the bookmark-color outlines on thumbnails. |
| `sqlite_viewer.py` | Database tab — see format table above. |
| `segb_viewer.py` / `segb_schemas.py` | SEGB/Biome tab — see format table above. |
| `leveldb_viewer.py` | `LevelDbViewerMixin` — see format table above. |
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | The parser plugin system — see section 5 above. |
| `chrome_cache.py` / `chrome_shared.py` | Qt-free Chrome HTTP-cache (Simple Cache format) decode + small helpers shared across the `chrome_*` parser family. |
| `ai_summary.py` / `ai_summary_store.py` / `local_llm.py` | Local-LLM report summarization: time-gap chunking, hierarchical reduce (avoids the same context-length ceiling a flat reduce hits), a stdlib-only HTTP client with a genuine wall-clock timeout. |
| `nested_archive.py` | Extracts one embedded/nested archive into `case_dir/nested_archives/`, idempotent — shared by the manual "Extract as Nested Archive" action and `requires_nested_extraction`. |
| `sqlite_carve.py` | Below-SQL-layer deleted-record recovery (freeblocks, freed pages, full WAL frame history) — see section 3/5 above. Also `locate_live_row`/`locate_offset`/`identify_structure`/`build_page_map` for the Hex-panel Record mode and "Interpret as SQL Record." |
| `artifact_media.py` | `MediaThumbnailDelegate`/`WebpageThumbnailRenderer` for Report-table `media_fields` columns. |
| `research_store.py` | Global artifact research notes, drives row colouring. |
| `parser_versions.py` | Global parser version tracking (content-hash-derived) + optional human changelog — drives the "newer parser version available" banner. |
| `report_columns_store.py` | Per-report column order (persisted) and visibility (session-only, by design — a hidden column should never silently persist forever). |
| `app_intelligence.py` | Per-app coverage/category/permission scoring + a deterministic interest score for apps with no parser yet — backs the MCP `list_apps` tool. Also holds `build_app_registry_lookup`/`flatten_row`, the Qt-free row-flattening the `app_report.py` device-wide parsers use. |
| `ccl_abx.py` | Vendored Android Binary XML decoder — see format table above. |
| `ccl_leveldb.py` / `ccl_simplesnappy.py` | Vendored LevelDB reader + Snappy decompressor. |
| `ccl_chromium_indexeddb.py`, `ccl_blink_value_deserializer.py`, `ccl_v8_value_deserializer.py`, `ccl_chromium_indexeddb_structures.py` | Vendored, schema-aware Chromium IndexedDB decoder (pinned upstream commit). |
| `ccl_chromium_pickle.py` / `ccl_chromium_snss.py` / `chrome_page_state.py` / `chrome_tabs.py` | Chrome tab/session persistence decode (SNSS + Android `app_tabs/` formats) — `chrome_tabs.py` is original code wiring the vendored pieces together. |
| `validation_store.py` / `parser_validation.py` | Per-parser schema/folder-structure baseline snapshots, diffed against a case on demand — never automatic. |
| `mcp_server.py` | Read-only MCP server over processed case data — `list_apps`, `get_sqlite_schema`/`sample_sqlite_rows` (opt-in, Tier 3), `build_artifact_parser` prompt, AI Summary tools. |
| `mcp_control.py` | Lifecycle for the embedded MCP server (uvicorn, localhost + per-start bearer token). |
| `highlight_delegate.py` | Yellow highlight of the active search term in views. |

## ffs-explorer.py section map (no line numbers — see why below)

Line numbers drift on routine edits; symbol names only go stale on a
(rare, deliberate) rename, which `grep` catches instantly. To find
something:
```
grep -n "def the_symbol_name" ffs-explorer.py          # one symbol
grep -n "^class \|^def \|^    def " ffs-explorer.py     # whole file's structure
```
The second command is a complete, always-current index — regenerate it
rather than trusting a stored copy.

File order, top to bottom:
- Module-level helpers: prefs load/save, archive-entry formatting,
  device-info readers, photo-flag rules, export path sanitizers.
- `ExtractorWorker` — archive discovery/classification.
- `ZipMetadataWorker` — first-open orchestration.
- `FileTableModel` + its filter proxy.
- `ExportProgressDialog`.
- Settings/preferences dialogs, small scan workers, integrity check.
- `ProcessDialog` — extraction/processing hub, artifact parser runs.
- `ArchiveSelectionDialog` — open/recent UI.
- `FastZipBrowser` — the main window (everything else is a method on it):
  `__init__`/column config, filtering, `_load_file_preview` (routes a
  selection to the right mixin tab), the tree/table/bookmark/selection
  system, jump menu, `start_loading`→`on_metadata_ready`→
  `_start_case_meta_load`, lazy tree expansion, recents, worker lifecycle,
  `closeEvent`.
- Module level again: Qt message handler, `__main__`.

## Config & resources

- `config/ffs_archives.json` — recent archives + device labels.
- `config/hardware_models.json`, `photo_flags.json`, `research_status.json`,
  `report_columns.json`, `parser_versions.json`, `parser_validation.json`,
  `embedded_media_skip_list_{android,ios}.json`,
  `user_media_ignore_list.json`, `ai_summary_settings.json` — all the same
  dev/frozen-path JSON-store convention (in `config/` in dev, next to the
  executable in a frozen build, user-editable, survive updates), cached by
  mtime+size. `user_media_ignore_list.json` is deliberately checked into
  the repo (not gitignored) — shared examiner knowledge, not personal
  config.
- `artifacts/` — drop-in parser scripts. `resources/` — icons.
- **`WRITING_ARTIFACT_PARSERS.md`** (repo root) — read this before
  re-deriving "how do I add a parser" from this file or the
  `artifact_runner.py` docstring; keep it in sync whenever a parser-facing
  convention changes.

## Build / CI gotchas

- Windows exe: CI is `.github/workflows/build-windows-exe.yml`, which runs
  `pyinstaller ffs_explorer.spec` — that's the spec that matters.
- The spec must bundle `app/ccl_segb` (vendored) and config seeds.
- `av` (PyAV) must be built from source in CI, not installed as a plain
  wheel — the prebuilt wheel can't decode HEVC (iOS's default recording
  codec) on at least macOS. A `pip install -r requirements.txt` on a dev
  machine pulls the prebuilt wheel, which is fine for dev testing but is
  NOT what the shipped exe uses.
- `ffs_explorer.spec`'s `excludes=` list has bitten this project once
  already (a stale `'PIL'` exclusion silently broke a real dependency) —
  after adding any new dependency, check
  `build/ffs_explorer/warn-ffs_explorer.txt` after a CLEAN rebuild
  (`rm -rf build dist` first) for "excluded module named X," don't just
  confirm the package installs.

## Keeping this map current (instruction to Claude)

After completing any change, check whether it invalidated a claim here, and
if so update it in the same session. Triggers: a file/module
added/removed/renamed; responsibility moved between files; case DB
tables/schema changed; a new config file or changed frozen-exe location; a
section-map symbol renamed/removed/moved between groups. Routine bugfixes
inside an existing method need no update here — that belongs in
docstrings/commit messages, not this file.

`scripts/check_claude_md.py` runs as a pre-commit hook and auto-corrects
plain line-number drift in any remaining single-symbol anchors, and blocks
the commit on anything needing judgment (a symbol that moved files, the
module table going out of sync). It only fires on commit, and only catches
mechanical drift — update the map's own prose yourself for a semantic
change.

## Human verification status (instruction to Claude)

`VERIFICATION_STATUS.md` tracks which parts of this AI-written codebase a
human has actually walked through and confirmed, separately from whether
the code merely runs. Most of it starts 🔴; moves to 🟢 only after the user
has done that with Claude, section by section, and says so.

**Before editing a file or `ffs-explorer.py` section, check whether it's
listed there as 🟢.** If it is, name the row and its verified date before
making the change, so the user can decide whether to re-review after — a
real functional change to a 🟢 row should drop it back to 🟡 until
re-checked. No need to say anything for 🔴/🟡 rows.

## Conventions (standing rules)

- `ui_path` = display path (adapter-normalised); physical zip name only via
  `FfsAdapter.resolve`. Never mix them.
- **Never read the main FFS archive via raw `zipfile.ZipFile(...)`/
  `.read(name)`.** The archive is never compressed in real FFS data — go
  through `zip_cd_cache.py` + `ZipEntry` (direct offset seek). `zipfile` is
  still correct for (1) the DEFLATED-entry fallback already inside
  `ZipEntry`/`CachedZipView` themselves, and (2) a genuinely local,
  already-extracted file (a nested archive, a small temp file). For a
  metadata-only read (`.getinfo()`/`.namelist()`), `zip_cd_cache.
  CachedZipView` is a drop-in `zipfile.ZipFile`-compatible object built
  from the same cached central directory.
- Workers: create → connect → `_retire_worker` on replace; `_stop_all_workers`
  on close. Don't block the GUI thread; batch model updates.
- Viewers must be read-only toward the archive (evidence integrity).
- **Per-tab state on switching**: the four center tabs (File Browser, Media
  Browser, Keyword Search, Artifact Viewer) resume exactly where the
  examiner left them — same selection/scroll/filter — unless the
  underlying data actually changed. `_on_center_tab_changed` is the one
  place all four tabs' switch-in behavior lives. The bottom Hex/Text/
  Database/SEGB preview panel is the one widget genuinely SHARED across
  all four tabs and needs its own explicit per-tab resync on entry (Qt's
  hide/show alone only protects a tab's own private widgets); each tab's
  OWN widgets need no special handling. When adding a new tab or a new
  shared widget, follow this same split.
- **Evidence timestamps are always UTC, always labeled** (`"...UTC"` or an
  ISO `+00:00` offset) — never a bare `"YYYY-MM-DD HH:MM:SS"`, never
  converted to the analysis machine's own zone. `_format_ts_cached`
  (`timestamp_display.py`) is the reference implementation. A per-case
  opt-in lets the examiner view the same evidence in the HANDSET's own
  zone (read from the device, iOS only), the ACQUISITION workstation's
  zone (the machine that ran the extraction tool — never guessed from the
  current reviewer, who can be a different person/machine/zone entirely;
  resolved by matching every zone sharing the `.ufd`'s recorded UTC
  offset, since no FFS/Cellebrite export records a real IANA zone name for
  it), or a MANUALLY selected zone — never auto-picked, starts on a blank
  placeholder. The active mode is shown in exactly one place (a shared
  banner above every tab), never duplicated into per-column headers. An
  Artifact Report table stores RAW unformatted timestamp values (its
  `timestamp_fields` declares the unit) and formats only at display time —
  never bakes a formatted string into stored output (the one deliberate
  exception: `photos_metadata.py`'s Taken/Added columns, which merge into
  the file browser's own table through a different mechanism).
- Never use SQLite's `'localtime'` modifier or a bare
  `datetime.fromtimestamp(x)` (no `tz=`) for an EVIDENCE timestamp — both
  silently convert using the analysis machine's own OS timezone, which can
  coincidentally look right and then be wrong from a different machine or
  season. (A bare `fromtimestamp()` is fine for a TOOL-PROVENANCE
  timestamp — "when did the examiner do X" — which is deliberately shown
  in local time with an explicit UTC-offset label.)
- **Validate against ground truth, not the tool's own citations.** When
  confirming a parser or heuristic works, check it against an app/file you
  did NOT already feed it an answer for — replaying a citation the tool
  itself produced proves nothing about whether the underlying mechanism
  generalizes.
- **Raw-carve WAL/SHM sidecars before any `sqlite3.connect()`** on an
  evidence database — checkpointing can consume deleted-data traces a
  direct carve would otherwise recover. `open_db_readonly`'s read-only URI
  connect still isn't enough on its own for an `ATTACH`ed second database —
  that needs its own `mode=ro` URI too.
- **A parser's own `description`/`warning` fields should state real,
  disclosed limitations plainly** (a format only reverse-engineered from
  real data with no public source to check against; a recovery path
  confirmed NOT to work on a specific real device/build) — this project's
  standing preference for an honest, narrower claim over a confident,
  unverified one.
- **Escalate, never silently truncate or discard.** A candidate list capped
  for display must say when there's more (and offer a way to see the
  rest); a borderline/low-confidence recovered row is labeled with the
  reason, never silently dropped. Several real features were redesigned
  specifically to stop silently truncating a results list once this was
  pointed out.
- AI-generated narrative content (the AI Summary feature) may characterize
  what a row's content is ABOUT only using words that literally appear in
  it — never infer an unstated specific (a team, category, or motive) even
  when it looks safely implied; three distinct real hallucinations were
  found and closed this way (a wrong inferred sports team, an invented
  category bucketing unlike items together, two similar-titled rows'
  timestamps merged into one claim). Grounding a literal fact and
  correctly characterizing what it means are different claims with
  different failure modes — test both against real data before trusting a
  new prompt change.
- Qt on macOS has a real, repeatable bug where `QMacStyle` fails to paint a
  tree/table item's checkbox indicator at all (not just low-contrast —
  genuinely blank). Fix: force the affected view to `QStyleFactory.
  create("Fusion")` rather than the native style; keep the created style
  object alive for the view's lifetime (`setStyle` does not take
  ownership the way `QApplication.setStyle` does).
- When embedding a real `QCheckBox`/button row directly on a `QTreeView`
  item via `setIndexWidget`, never `addStretch()` BEFORE the real content —
  the column can be far wider than the visible panel (kept wide to support
  horizontal scroll for deep nesting), and a leading stretch pushes
  content off past the visible edge. Pin child widgets to a fixed size
  policy and put the stretch AFTER them.
- A headless/offscreen Qt test must call `window.show()` before asserting
  on any widget's `isVisible()` — visibility is relative to the WHOLE
  ancestor chain, not a widget's own `setVisible()` flag; without a shown
  top-level window every child reports `False` regardless of internal
  state.
- A modal `QDialog`/`QMessageBox`/`QMenu.exec()` blocks forever under the
  offscreen QPA platform with nothing to click — every headless
  verification script must neutralize every modal its own code path can
  reach (the Timestamp Display dialog's first-load prompt is a common one
  to forget), not just the one the test author happened to expect.
- This project's own `CLAUDE.md` entries should record **design
  decisions and standing rules**, not a verification/bug-hunting
  narrative — the detailed "found via real archive X, Y checks, screenshot
  confirmed" history lives in commit messages and conversation history,
  not here.
