# FFS Explorer — codebase map

Desktop forensics tool (PySide6) for browsing iOS/Android Full File System
extractions (Cellebrite / GrayKey zips) without extracting them. Single-window
app; one "case folder" per exhibit holds local caches and results.

**Always run with the project venv:** `venv/bin/python ffs-explorer.py`
(system Python lacks PySide6). Line counts ~16.5k total; `ffs-explorer.py`
alone is ~7.3k — use the section map below instead of reading it whole.

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
2. `ZipMetadataWorker` (ffs-explorer.py:1003) → `app/ffs_metadata.py
   parse_archive_metadata()` in a child process: central-directory parse,
   `ui_metadata` build, folder tree/sizes; snapshot persisted to case dir
   (msgpack) so re-opens are instant.
3. `FfsAdapter` (`app/adapters/ffs.py`) detects format and maps `ui_path`
   (what the UI shows) ⇄ physical zip path; GrayKey specifics in
   `adapters/graykey.py`; GUID→bundle-id map for `/private/var/mobile/…`.
4. Zip reading: normal zips via `zipfile` + `zip_cd_cache.py` (.zcd sidecar so
   network archives open fast); Cellebrite *streaming* zips (no central
   directory, bit-3 descriptors) via `streaming_zip.py`. Every viewer receives
   an `app/zip_entry.ZipEntry` and never cares which strategy applies
   (stored → direct seek, deflated → zipfile).

## Case folder databases (`app/db_utils.py`)

- `casecache.db` — rebuildable cache: thumbnails, blobs (folder sizes, search
  entry index), header_types, guid_bundle, nested-archive index. On schema
  mismatch it is auto-deleted and rebuilt.
- `caseresults.db` — precious results (never auto-deleted): search_index /
  search_results, bookmarks, device_info, run_log, user SEGB schemas
  (`segb_schemas`), artifact parser output (`artifact_<name>` tables). On
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
| `streaming_zip.py` | Forward scan index for Cellebrite streaming zips (no usable CD) |
| `header_scan.py` | Magic-byte/text file-type detection by direct offset reads |
| `keyword_search.py` | Search workers (live + nested archives), saved-search DB loaders, dialogs, `KeywordSearchMixin` |
| `hex_viewer.py` | Hex tab (`HexViewerMixin`, `HexLoadWorker`) |
| `media_viewer.py` | Thumbnail grid, ffmpeg video frames (`MediaViewerMixin`) |
| `sqlite_viewer.py` | Database tab: temp-copy extraction, table browser, **WAL net-change diff view** (`SqliteDiffModel`) |
| `segb_viewer.py` | SEGB/Biome tab: parses records via vendored `app/ccl_segb`, decodes protobuf with `blackboxprotobuf`; empty-record hiding + deleted-record toggle |
| `segb_schemas.py` | Built-in per-stream protobuf typedefs + field labels for known Biome streams; user-authored schemas persist to `caseresults.db` via `db_utils.save_segb_schema` and override the built-ins |
| `artifact_runner.py` / `artifact_db.py` / `artifact_viewer.py` | Plugin system: parser scripts in `artifacts/ios|android/` (e.g. `photos_metadata.py`, `sms_messages.py`, `whatsapp.py`) run against the archive, results into `casedata.db`, browsed in Artifacts tab |
| `research_store.py` | Global (cross-case) artifact research notes in `config/research_status.json`, keyed by stream/bundle identity, drives row colouring |
| `highlight_delegate.py` | Yellow highlight of active search term in views |

## ffs-explorer.py section map (approx. lines)

- 100–580: prefs, archive-entry formatting, device-info readers (UFD/plist/build.prop)
- 586–1002: `ExtractorWorker`; archive discovery/classification, photo-flag rules
- 1003–1432: `ZipMetadataWorker` (first-open orchestration, cache load/save)
- 1433–1740: `FileTableModel`, filter proxy, `ExportProgressDialog`
- 1744–2384: settings/preferences dialogs, small scan workers, integrity check
- 2385–3111: `ProcessDialog` (extraction/processing hub, artifact runs)
- 3112–3496: `ArchiveSelectionDialog` (open/recent UI)
- 3497–7283: `FastZipBrowser` — the main window:
  - 3497–3999: `__init__` + column config; 4123–4460: filtering (text/date/type)
  - 4527–4750: preview dispatch (`_load_file_preview` routes to mixin tabs)
  - 4756–5320: tree/table selection, entry rows, context menus, export
  - 5327–5460: time-column detection, Android detection, jump menu
  - 5494–5880: processing entry, photo index, `start_loading` → `on_metadata_ready`, nested archives
  - 5987–6540: folder view refresh, batched tree population, bookmarks
  - 6542–6780: research-status styling/dialog (uses `research_store`)
  - 6783–7280: lazy tree expansion, recents/`config/ffs_archives.json`, worker retirement, `closeEvent`
- 7284+: Qt message handler, `__main__` (incl. stall-detector timer)

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
- `requirements.txt`: msgpack, PySide6, blackboxprotobuf (+ pyobjc on macOS).

## Keeping this map current (instruction to Claude)

After completing any change, check whether it invalidated a claim in this
file, and if so update the relevant line(s) in the same session. Triggers:
- a file/module added, removed, or renamed (update the module table)
- responsibility moved between files, or a new mixin/tab added
- case DB tables/schema versions changed (update the databases section)
- new config file, or changed frozen-exe file locations
- edits to `ffs-explorer.py` large enough to shift the section map by more
  than ~100 lines (re-check the affected ranges only)

Routine bugfixes inside an existing method need **no** map update — do not
add method-level detail here; that belongs in docstrings.

## Conventions

- `ui_path` = display path (adapter-normalised); physical zip name only via
  `FfsAdapter.resolve`. Never mix them.
- Workers: create → connect → `_retire_worker` on replace; `_stop_all_workers`
  on close. Don't block the GUI thread; batch model updates (see
  `_populate_tree_children_batched`).
- Viewers must be read-only towards the archive (evidence integrity).
