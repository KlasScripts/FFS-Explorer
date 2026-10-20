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

**GUID vs. bundle id in cross-case lists — a real bug, fixed 2026-10-10.**
A per-install container GUID is specific to ONE extraction (reinstalling
the same app gets a new GUID) — any list meant to work across cases
(`user_media_ignore_list.py`, `embedded_media_skip_list.py`, both
global/git-tracked) must compare the BUNDLE id, never the raw GUID.
`FfsAdapter.canonical_app_path(ui_path, guid_map, folder_map=None)` is
the one shared fix: returns *ui_path* with its container GUID segment
replaced by the resolved bundle id (full path shape preserved), or
UNCHANGED if it doesn't apply — safe to call on any path. Confirmed via
real evidence, not just theory: the live `user_media_ignore_list.json`
already had exactly this bug — a bare GUID entry for `com.apple.stocks`
that would never have matched again once that GUID stopped existing
(migrated to canonical form as part of this fix). A second, separate
instance of the same root cause was found in `embedded_media_skip_list.py`:
`_app_scoped_skip_entry()` already stored bundle-id-scoped entries
correctly, but nothing canonicalized the CANDIDATE side at match time,
so those entries could never actually match anything — fixed by
canonicalizing every match-time candidate through `canonical_app_path()`
before calling `matches()`/`find_matching_entries()` (the stored entry
shape itself needed no change, since a bundle-id-scoped entry like
"com.example.app/foo.db" is already a substring of the canonical full
path). Every call site of both lists' add/remove/match operations now
canonicalizes: `FastZipBrowser._canonical_user_media_entry()` and the
`EmbeddedMediaScanWorker`/`ProcessDialog`/`EmbeddedMediaSkipListDialog`
constructors all thread `guid_to_bundle`/`folder_map` through for this.

## 2. Header-scan tiers

Extension-based typing is the default and is sometimes wrong (a renamed
database, a generic `.bin` MMS part that's really a JPEG). The header scan
reads real magic bytes to correct the `Type` column, and every other feature
that depends on "is this file really a database/image/archive" — Keyword
Search's coverage reminder, the embedded-media sweep's candidate list, the
raw file-browser preview — reads from the SAME `_header_type_overrides` dict
this scan populates, never a second guess.

**`scan_rules.py` — the one shared ruleset.** `scan_folders()`/
`scan_excludes()`/`user_media_folders()`/`embedded_media_excludes()`
(all on `FfsAdapter`) all read from `config/scan_rules_ios.json`/
`scan_rules_android.json`, platform-split like
`embedded_media_skip_list.py` (no shared vocabulary between
`mobile/Containers/...` and `data/data/...`, and a case is always one
platform). Added 2026-10-10 after a design discussion found the obvious
alternative — 4 independently hand-maintained folder lists, one per
consumer — had ALREADY silently drifted (`archive_discovery_folders()`
was missing folders `scan_folders()` had, purely by accident; it's now
just `scan_folders()` itself). One row per folder prefix:
`{"path", "include": 1|0, "user_media": "not_user_media"|
"user_media_only"|"also_user_media", "embedded_media_exclude": 1}` (the
last key optional). `include` is the shared base scope for header-scan
tiers 1/2, nested-archive discovery, AND the embedded sqlite/plist
sweep — those three really do want the identical "is this user data or
system noise" answer; `user_media` is an independent, deliberately
narrower tri-state consulted only by the Media Browser's own "User
Media" filter (a precision view, not a coverage tool); and
`embedded_media_exclude` is a one-directional carve-out for the embedded
sweep specifically, since it can be too expensive (parses every sqlite/
plist) for a folder that's still cheap enough for header-scan/zip-
discovery to cover.

Paths are stored CANONICAL (Cellebrite-format, no `private/var/`
prefix); `FfsAdapter.prefix_shortcut()` is applied per-path for a
GrayKey case, same mechanism `FORENSIC_SHORTCUTS` already used — one
file covers both iOS acquisition tools.

`mobile/Media/`, `mobile/Library/ContactsMetadata/`, and
`mobile/Library/Passes/` were added 2026-10-10 after a user report that
a genuinely-image extensionless file in `ContactsMetadata/` stayed
"Other" after a Tier 1 rerun — a real scope gap, not staleness (all
three confirmed to hold genuine user content; Passes/ is real Wallet
pass bundles — `pass.json`, rendered card images, Apple's own
cryptographic `signature` file, not internal templates).

`mobile/Library/Caches/` is also in scope, with 24 confirmed-noise
subfolders excluded. The real bar, refined twice on 2026-10-10 through
direct correction each time: (1) "noise" means NO RECOGNIZABLE CONTENT
AT ALL (every unknown-extension file individually magic-byte-checked
and classified `None`), not just "no media" — the exclude list was
first built checking ONLY Picture/Video/Audio hits, which missed that
19 of the original 29 "noise" subfolders actually held real JSON/
Property List/Database/Archive/XML content; (2) having a real TYPE
still isn't enough — the content also has to be LINKABLE TO THE USER,
not just structurally parseable. Re-checking those 19 by actual content
(not just type) found most were generic system/service config with
zero device-to-device variation — bundled reference data, A/B-test
bucket IDs, server bootstrap URLs, ML model binaries — indistinguishable
from any other device regardless of user. Only 5 of the 19 had genuine
user-linked content and stayed in scope: `GeoServices` (a paired Apple
Watch's own offline map cache — device-ownership evidence),
`com.apple.HomeKit` (real home/accessory configuration),
`com.apple.findmy.fmipcore` (the actual Find My device list),
`com.apple.chrono` (widget snapshot renders reflecting the user's own
configured widgets), `com.apple.ctcategories.service` (cached App Store
lookups tied to specific apps the user viewed). The other 14 — including
`com.apple.HomeKit`'s own sibling `com.apple.AppleMediaServices`, whose
real Database+JSON+Property List content turned out to be nothing but
generic experiment-bucket definitions — went back to excluded. A
denylist, not an allowlist: an unrecognized subfolder stays in scope by
default. `com.apple.geod/` and `com.apple.MobileSMS/` are kept OFF the
exclude list for a different, narrower reason — both hold real value in
their KNOWN-extension files (`geod`'s `AP.db`/`MapTiles.sqlitedb`;
MobileSMS's own `Previews/Search/*.png`), which a header-scan-signal
check (type or content) can never see, since known-extension files are
never header-scan candidates at all.

**App containers (`Application`/`PluginKitPlugin`/`AppGroup`) were
reviewed the same way, 2026-10-10, and found to have NO comparable
exclusion opportunity** — confirmed by checking every file, not a
sample (a real methodological trap hit along the way: an initial
200-file SAMPLE wrongly flagged `com.google.Gmail` and
`com.google.chrome.ios` as fully empty, missing 991 and 96 real pictures
respectively that existed elsewhere in those same apps' thousands of
cached files). Restricting to `com.apple.*` only (the safer,
Caches-like starting point) still found nothing comparable: real Apple
apps with substantial cache content (`com.apple.mobilesafari`: 1,449
unknown-ext files, `com.apple.AppStore`: 477, `com.apple.Home`: 251)
all have real Picture/JSON/Database content and must stay in scope.
The confirmed-empty containers are architecturally different from
Caches' noise — ~700 of them, each tiny (5–38 files, usually fewer),
spread across PluginKit's own extension-point scaffolding (every app
ships a NotificationServiceExtension/ShareExtension/DiagnosticExtension/
etc. with a handful of empty placeholder files whether the extension is
ever used or not) — a structural fact about iOS, not evidence about any
one app or device. No ruleset entries were added for these: the list
would be unmaintainably long, would go stale with every iOS version,
the files are mostly already zero bytes (so there's no real I/O cost
being saved), and a real trap sits right next to the empty ones —
`com.apple.stocks.widget`/`com.apple.news.widget`/`com.apple.weather.
widget` all have real JSON/Database content in superficially-identical-
looking widget-extension containers.

The "Scanned Folders…" dialog shows the include list, the excluded
subfolders, and (once any exist) the embedded-media-only excludes, for
transparency — its wording deliberately says "no recognizable file
content," never "no media," for the same reason. User Media's own
`user_media_folders()` had
no visible surface at all until a "Folders…" button was added next to
the User Media bookmark-panel row (`_show_user_media_folders_dialog`) —
a separate small dialog rather than another section on the header-scan
one, since an examiner looking at the Media Browser has no reason to
go find it inside Process Case's own dialog. That dialog's own exclude
section reads `user_media_ignore_list.load()` directly — the existing,
already-working, right-click-driven folder/file exclude list for this
filter (see section 3's own entry for that module) — NOT a second,
scan_rules.py-based mechanism; one was briefly added (a
`user_media_exclude` rule field) then reverted the same day, direct
correction, since it duplicated that already-built mechanism instead
of pointing the dialog at it. `scan_rules.py` has no equivalent field
for this reason.

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

**Stale-completion detection (`_header_scan_staleness`).** A case's own
`header_scan_complete_tier` claiming a tier is done doesn't necessarily
mean it still is — found via a real report, 2026-10-10: a case's flag
said Tier 1 while 38,141+ of its real candidates had never actually been
scanned, with the old data giving no way to tell. Fixed by persisting a
`header_scan_candidate_count` snapshot (how many files genuinely
qualified) alongside `header_scan_complete_tier` at the moment a scan
actually finishes (both write sites: the creation-time path and
`ProcessDialog`'s own tier-upgrade completion). On load, the CURRENT
candidate count for the recorded tier is recomputed and compared — a
growing gap (or a legacy case with no stored count at all, i.e. unknown
provenance) renders the main window's header-scan banner in red instead
of the normal blue, with the gap count stated plainly, and
`ProcessDialog` shows the same warning on its own status label plus
leaves that one already-run tier's radio button ENABLED (styled red,
labeled "select to rerun") instead of greyed out — selecting it is
treated as a genuine rerun request through the exact same tier-upgrade
machinery as picking a higher tier, not a special code path. Confirmed
this is unrelated to nested-archive extraction or embedded-media
recovery — both already classify correctly via `header_scan.
classify_magic` at the moment each entry is extracted/discovered
(verified directly against 1,281 real nested-archive files, zero
mismatches); the gap is specifically ordinary files already inside the
main FFS zip falling out of sync with the bulk Tier 1/2/3 scan's own
candidate set.

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
  Supports wildcard entries (`*` fragments) as of 2026-10-10, same
  convention as `embedded_media_skip_list.py`'s own, for a cross-app
  structural rule rather than one folder's own path. The one shipped so
  far: `*@2x.png*`/`*@3x.png*` ("UI Graphics" — `display_label()`'s own lookup),
  confirmed safe/generalizable by checking real User Media candidates
  across 3 different real iOS devices (6,081 of 49,840 total candidates,
  8.6%–21% per device, every sample checked genuinely bundled UI chrome,
  zero real contact/user photos caught). A DIFFERENT idea tried the same
  day — "duplicate filename = chrome" — was REJECTED with hard evidence:
  the single biggest duplicate-basename cluster, `5005.JPG` (2,491
  occurrences), is real per-photo thumbnail data
  (`mobile/Media/PhotoData/Thumbnails/V2/DCIM/.../IMG_0001.HEIC/5005.JPG`
  — iOS gives every photo's own thumbnail the same fixed filename), not
  chrome — a bare duplicate-name rule would have hidden 2,491 real
  photos. The lesson: a wildcard rule here needs a STRUCTURAL anchor
  (Apple's own Retina-asset naming convention, never used for real
  photos) to be safe, not just "this pattern repeats a lot." Hidden
  matches stay fully reviewable — the right-click "Show Media Excluded
  by Ignore List…" action (unchanged) shows exactly what's filtered.
  Three more shipped the same day after reviewing real images across
  3 iOS AND 3 Android devices (49,840 + 33,091 real candidates), same
  bar (a platform-reserved convention, verified by checking EVERY
  matching file, not a sample): `*__MACOSX/*._*` ("Zip Metadata
  (macOS)" — the AppleDouble resource-fork sidecar every Mac-created
  zip leaves, preserving the real file's own subdirectory structure
  — confirmed by its own literal `\x00\x05\x16\x07...Mac OS X`
  signature across all 2,814 real matches in one archive, present on
  both platforms); `*.9.png` ("UI Graphics (nine-patch)" — Android's
  reserved stretchable-UI-chrome format); `*/drawable-*dpi*` and
  `*/mipmap-*dpi*` ("UI Graphics (Android resource)" — Android's own
  reserved density-bucket resource folder naming). A parallel idea —
  EXIF Make/Model presence as a "real photo" signal — was tried and
  REJECTED, direct correction: real DCIM photos in this same archive
  had NO EXIF and were smaller than a real camera capture, because
  they'd passed through a messaging/social app's own lossy re-encode
  pipeline first — exactly the category of real evidence (a received/
  shared photo) the signal would have systematically misclassified as
  chrome. Content-based signals (EXIF, dimensions, color depth) are
  unsafe here for that reason; only "how the file got onto the device"
  (a build-time/packaging convention, never passed through a lossy
  re-encode) has held up as a safe signal so far.

  **A real precision bug, caught and fixed the same day**: a wildcard
  entry with no trailing `*` (`*.9.png`) was matching ANYWHERE in the
  path, not anchored to the real end of the filename — direct report,
  "is this safe? `*.9.png`" — `version1.9.pngBACKUP.zip` (real
  extension `.zip`) matched, since the substring ".9.png" just happens
  to appear mid-path. `_wildcard_matches()` now appends a real `$`
  anchor whenever the pattern doesn't end with `*`; a pattern that
  genuinely wants "appears anywhere, more can follow" (`@2x.`/`@3x.` —
  the real extension always follows) must say so explicitly with a
  trailing `*`, hence `*@2x.*`/`*@3x.*` rather than the original
  `*@2x.`/`*@3x.`. Re-verified against all 3 iOS + 3 Android archives
  after the fix: identical real-world match counts, zero behavior
  change on real data — the fix only closes the theoretical gap.

  **A second real correction, same day**: direct report on a pasted
  real path, "`...Edge/Default/Snapshots/{SyntheticIdentifier}/
  1112275668Grey@2x.jpg` — this actually be of value it looks like a
  screenshot of a website... can you change the grep to include that
  the filename must be a png." Checked rather than assumed: of 6,081
  real `@2x`/`@3x` matches, exactly 8 were `.jpg` — and all 8 were real
  Microsoft Edge/Google Chrome tab-switcher snapshots (an actual
  rendered screenshot of whatever page was open), not bundled chrome,
  despite sharing the same Retina-scale naming convention; the other
  6,073 `.png` matches stayed confirmed generic icons. Patterns changed
  to `*@2x.png*`/`*@3x.png*` (PNG-only) — re-verified across all 3 iOS
  + 3 Android archives: zero `.jpg`/`.jpeg` files now hidden by this
  rule anywhere. The lesson, consistent with the EXIF rejection above:
  a browser's own tab snapshot is REAL per-session content (varies by
  site/tab) that happens to reuse a chrome-style naming convention —
  "shares the filename pattern" still isn't sufficient on its own
  without also checking which FORMAT real bundled assets actually use
  (lossless PNG for crisp UI elements) versus what a content-snapshot
  mechanism uses (lossy JPEG, cheaper for a full-page render).

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

**Picture/Video/Graphics preset toggle (Media Browser) — added
2026-10-11**, direct request: "it is easy to miss [an] illegal movie if
the first frame is not illegal so it is better for the user to be able
to know when they are looking at images vers[us] video," extended the
same day into a full preset system: "a sort fa[c]tion like excel...
save this as a setting that can be quickly used[;] picture could be one
and that would have pictures and sort[ed] by newest first... for
graphic[s] size may be the one[,] as... the bigger one[s] are more
likel[y] to have value." Four exclusive buttons (All/Pictures/Videos/
Graphics) in the status row, each a real filter+sort PRESET
(`MediaViewerMixin._MEDIA_TYPE_PRESETS`), applied live in
`_recompute_media_all_paths` — the one function both an ordinary
folder's own file-model load AND every alternate aggregate view (User
Media, Embedded Media) already funnel through, so the preset's
filter+sort apply identically regardless of which populated the
underlying list, per the same direct request: "if a user used the
selected view to select a folder then the folder should show the media
files in that folder that meet the filter type and will be sorted in
the described manner."

Three-way classification (`MediaViewerMixin._media_kind`): 'picture'
(ordinary image extensions), 'video' (real video extensions — mov/mp4/
m4v/3gp/avi), 'graphics' (`.png`/`.gif` specifically) — GIF was
initially folded into 'video' for this feature, then moved to its OWN
'graphics' bucket the same day, direct revision. PNG/GIF are exactly
the two formats this project's own `user_media_ignore_list.py` "UI
Graphics" research already established as the common shape of bundled
app chrome — the Graphics preset's own "biggest first" sort exists
because a tiny PNG is far more likely to be an icon than a real image,
while a large one is more likely to be real content, if anything is.
Originally a SEPARATE, narrower classifier than `header_scan.py`'s own
MEDIA_EXTENSIONS/sniff_media_kind or the File Browser's Type column — a
GIF still showed as a real 'Picture' type everywhere else in the app at
first, only "which review bucket" changed here. Direct correction the
same day made it consistent instead (see "File Browser filter+sort
presets" below): a GIF/PNG now shows as Type=`"Graphic"` everywhere —
File Browser Type column, header-scan tiers, this classifier — not just
within this one feature.

The grid's own video-play badge (`MediaGridDelegate.set_video_paths`,
bottom-left corner, never collides with the top-right "seen" checkmark)
uses a DIFFERENT, deliberately broader check —
`MediaViewerMixin._has_frame_risk` — real video extensions OR gif,
regardless of which preset bucket a file is in: "could this thumbnail
be hiding something its first frame doesn't show" and "which bucket
should this be reviewed in" are two separate questions that happen to
overlap on gif, not the same question asked twice.

Verified against a real archive, 2026-10-11: a real 305-file DCIM
folder split cleanly into 255 picture + 16 video + 34 graphics (no file
dropped or double-counted), each preset's own sort order confirmed
correct (newest-first by `mtime` for Picture/Video, biggest-first by
`size` for Graphics).

**Real regression, same day, fixed**: `self._media_type_filter` was
never reset on folder navigation — picking "Videos" in one folder then
navigating to a DIFFERENT folder with no real videos silently carried
the filter over, rendering as "shows nothing," reported as the whole
feature being broken: "select an app folder... shows nothing[,] i want
it like before to show the media files in that location[,] i just want
to be able to filter for video, picture or graphics or all in that
folder." Confirmed directly (not assumed) with a real two-folder repro
before fixing: a DCIM folder's "Videos" selection (16 real matches)
left active, then navigating to a real picture-only folder showed 0 of
its own 3 real media files. `_load_media_from_file_model` and all four
alternate-view entry points (`_show_embedded_media_hits`,
`_show_user_created_media`, `_show_user_media_seen_files`,
`_show_user_media_ignored_files`) now reset `_media_type_filter` to
`'all'` (and sync the button) on every call — each one IS a fresh
"picked a folder/view" moment; same-folder refreshes ("Not Relevant"/
Undo) go straight through `_recompute_media_all_paths`/
`_load_media_page` instead, never back through here, so they correctly
keep whatever type filter was active. A second, related bug found
alongside it: the status line's old single "N hidden as seen" note
mislabeled a type-filter reduction as a seen-filter one even with the
reset in place (a folder with some-but-not-all matching files, filtered
by type, would still misreport the hidden count's cause) —
`_recompute_media_all_paths` now tracks `_media_hidden_by_seen`/
`_media_hidden_by_type` separately, and the status line reports
whichever actually applied.

**File Browser filter+sort presets — generalized the same day**, direct
request: "i want a dropdown in the filebrowser view that let the user
uses, edit and creat the filter,sorts were it was more that just one
thing like the current interface works... for the edit and new i want
to be able to filter by multiple column so i select the column and then
for most column there is a text box where i put the value and then i
can click add... for time and data and file type i want it to look
like it does in the gui currently... grouped into the 3 column types t
and d, filetype and other... then there need to be a sort section...
sort by one column after another and select[] if it is ascending or
desending." Scoped with one direct question first — should editing a
preset here also change the Media Browser's own Picture/Video/Graphics
buttons, given they classify by file EXTENSION on raw media paths while
the File Browser's table filters/sorts by COLUMN VALUE — answered
**"Fully unify both."**

`app/file_browser_presets.py` is the single shared store (JSON,
examiner-editable, checked into the repo like `user_media_ignore_list.
json`) for both. A preset is `{name, date_filters: [...], type_filter:
{types:[...]} | null, text_filters: [...], sort: [...]}` —
`date_filters`/`text_filters`/`sort` are LISTS (the direct "for each you
can have multiple" requirement) of independent, ANDed criteria (sort
entries are priority-ordered instead); `type_filter` stays one set,
since there's only ever one real `'Type'` column. The File Browser's new
preset dropdown (next to the plain filter bar, in `FastZipBrowser.
_setup_file_tab`) is a SEPARATE mode from that bar's own text/date/type
fields, never composed with them — picking a preset disables the whole
plain row (`_set_plain_filter_enabled`) and applies directly via
`_apply_preset`; `"(No Preset)"` hands control back. `Edit…`/`New…`
buttons open `FilterSortPresetDialog` — three groupboxes (Time & Date /
File Type / Other Columns) matching the user's own grouping, the first
two REUSING the plain filter bar's own widgets (`QDateEdit` range
pickers, the Type multi-select checklist) rather than a generic text
box, per that same request; a Sort groupbox with its own add-one-level,
Move Up/Down list, the same Excel-style multi-sort described above.

A real granularity gap, found empirically before writing any of this —
a real iOS17 archive's raw per-file metadata carries NO semantic type
field at all (only filesystem stat fields: atime/btime/ctime/mtime/
size/mode/...), so the File Browser's own `'Type'` column is itself
computed from extension via `ffs-explorer.py`'s `_EXT_TO_TYPE`, which
used to collapse `.jpg`/`.heic`/`.png`/`.gif` all down to ONE label,
`"Picture"` — too coarse to express the Graphics-vs-Picture split
("graphics...gif and png") the feature needs. FIRST fixed by shipping
Pictures/Videos/Graphics as `text_filters` on the `'Name'` column with
`"ends_with"` mode against extension lists instead of the `'Type'`
checklist — then, direct correction the same day ("i do not want you to
use ext for the filter i want you to seperate out png and gif into
graphic type both via the ext and the header scan[,] so that the filter
only need to use the type without filltering further and missing some
images"), fixed PROPERLY upstream instead: `_EXT_TO_TYPE` and
`header_scan.py`'s `classify_magic()` BOTH now give PNG/GIF their own
`'Graphic'` Type, separate from `'Picture'` (`header_scan.
GRAPHIC_EXTENSIONS`, the `_SIGNATURES` table's PNG/GIF87a/GIF89a
entries) — so Pictures/Videos/Graphics go back to being plain
`type_filter`s again, and an extensionless PNG/GIF recovered only by
magic bytes gets `'Graphic'` too, closing a second, independently
accepted gap (`media_viewer.py`'s `_media_kind` used to document that
exact case as falling back to `'picture'`, never `'graphics'`, since
`classify_magic` had no Graphic/Picture split at all — now it does).
`_MAGIC_KIND`/`_MEDIA_MAGIC_KIND` (`header_scan.py`/
`embedded_media_scan.py`) both gained a `'Graphic': 'image'` entry so
nothing silently stops recognizing a PNG/GIF as real image content
through either path. Same request also asked to fold TIFF/RAW into
Pictures ("i am also happy for you to in[c]lu[d]e the raw/tiff as
pictures") — `.tiff`/`.tif`/`.raw`/`.cr2`/`.nef`/`.dng` were already
Type=`"Picture"` via `_EXT_TO_TYPE` but invisible to the Media Browser,
since `MEDIA_EXTENSIONS` (the Media Browser's own "is this a media
file" gate) didn't have them — added there too, so a `type_filter` on
`"Picture"` now genuinely means every file that Type.

**Multi-level sort** (`FileTableModel.sort_multi`, `real_column, bool`
pairs with NO checkbox-column offset, unlike the header-click `sort()`
it sits beside) sorts once per level from LEAST to MOST significant —
`list.sort()`'s own stability means each pass's relative order survives
untouched among rows the next, more senior pass treats as equal, giving
a real composite sort with an independent ascending/descending per
level without needing one combined tuple key (which couldn't give each
level its own direction anyway).

**Multi-clause filtering** (`FileTableModel.set_filter`'s new
`extra_date_filters`/`extra_text_filters` params, both lists, both
`None` for every existing plain-filter-bar caller — fully backward
compatible) — the plain bar only ever has ONE text/date/type clause at a
time; a custom preset can need several ANDed together (e.g. two
independent date ranges, or Name-contains AND App-equals). Within one
`text_filters` entry, `"values"` is OR'd (any value in the list
matches) — this machinery stays fully available for a user-made
preset's own "Other Columns" criteria even though the three BUILTIN
presets (Pictures/Videos/Graphics) no longer use it themselves (see
below — they moved to `type_filter`).

**A preset survives folder navigation** (not just the folder it was
picked on) — unlike the plain filter bar, which has never kept state
across a folder change. All three file-model rebuild sites
(`_refresh_folder_view`, `_refresh_folder_view_recursive`,
`_rebuild_file_view_from_checked`) funnel their post-population filter
step through one new chokepoint, `_apply_active_filter`, which checks
`self._active_preset_name` first, regardless of that site's own
`preserve_filter` flag, before falling back to the existing plain-filter
preservation logic.

**Full unification, concretely**: `MediaViewerMixin._media_kind`/
`_recompute_media_all_paths` now call `_preset_types`/`_preset_sort`
(in `media_viewer.py`), which read the LIVE "Pictures"/"Videos"/
"Graphics" presets from the shared store first. `_media_kind` resolves
a file's real Type (`_file_type_for` — extension via the same
`GRAPHIC_EXTENSIONS`/`VIDEO_THUMB_EXTENSIONS`/`MEDIA_EXTENSIONS`
`header_scan.py` constants `_EXT_TO_TYPE` itself is built from, or the
header-scan override for an extensionless file) and checks it against
each bucket's own Type SET (`self._kind_types`, refreshed once per
`_recompute_media_all_paths` pass via `_resolve_media_type_sets`, not
per-file) — both that and each bucket's sort fall back to the ORIGINAL
hardcoded `_FALLBACK_KIND_TYPES`/`_MEDIA_TYPE_PRESETS` whenever the
matching preset is missing (deleted via the File Browser dialog), has
no `type_filter`, or the store can't be read — the Media Browser button
must never go blank just because the shared preset got edited into an
unusable shape. Verified directly, 2026-10-11: re-mapping the real
"Graphics" preset's `type_filter` to `["Picture"]` made the Media
Browser's Graphics button start showing ordinary JPGs instead of PNGs
(confirmed live, not just inferred); deleting that preset afterward
correctly fell back to the original `Graphic`-type/size-descending
behavior rather than breaking. Also confirmed: an extensionless file
whose ONLY signal is a header-scan override of `'Graphic'` now
genuinely classifies as `'graphics'` through this same path — the
narrow gap `_media_kind`'s own docstring used to document (an
extensionless PNG/GIF always fell back to `'picture'`, since
`classify_magic` had no Graphic/Picture split to report) is closed, not
just narrowed, now that the split happens upstream in `header_scan.py`
itself (see above).

**Hover-scrub in the video player (`MediaFullViewDialog`) — added
2026-10-11**, direct request: "one of the thing that griffeye does with
videos is al[l]ows you to put your curssor over the top of a view in
the media browser and it then scrolls through exstracted thumbnails so
it is like fast forwarding through the video... what about a scroll
over depending on how fast is the speed that the playhead goes and if
you stop the video stops playing and if you go backward the video plays
backwards from that point," explicitly asked to explore doing this via
real seeking rather than pre-extracting frame thumbnails: "i though it
would be worth exsploring rather than create the thumbnails." Confirmed
feasible and shipped as a feature of `_build_video`'s own player (not
the grid thumbnails — that was the scope asked for first): the video
already plays via Qt's native `QMediaPlayer` (AVFoundation/FFmpeg
backend) on a real local scratch-file copy, and `QMediaPlayer.
setPosition()` is already a cheap native seek — this reuses exactly
that, no second PyAV decode path and no frame pre-extraction at all.

**Position-mapped, not velocity-integrated** — direct clarification
once asked which Griffeye actually does: "the way it works in grifeye
is a bit like sp[osition]ing[,] i.e. at to the far left start and then
if you are middle then you would be halfway through." Mouse X position
across the video widget's own width maps linearly to a position in the
clip (`eventFilter`'s `QEvent.Type.MouseMove` branch sets
`self._scrub_target_frac`), far simpler than a velocity-driven virtual
playhead.

**Not a hard teleport, though** — direct refinement: "it needs to go to
that point relativ[el]y quickly... based on the l[e]ngth of the video
but not so quick that it is silly." `_on_scrub_tick` (a `QTimer` at
`_SCRUB_TICK_MS` = 40ms) advances `self._scrub_current_ms` toward the
live mouse-mapped target by at most a per-tick cap derived from
`_SCRUB_MIN_FULL_SWEEP_MS` (2500ms) — sweeping the ENTIRE width of even
a very long clip takes at least that long in real time, so the examiner
actually sees the intervening frames sweep by (a real "fast-forwarding
through," via ordinary cheap seeks at a fixed wall-clock rate) rather
than an instant cut; a short clip still reaches the hovered position
almost immediately since the whole clip is short relative to the floor.
Verified against a real 44-second iPhone video (HEVC/1080p, Josh
Hickman IOS17 archive): sweeping to the far end advances about 700ms of
video per 40ms tick (not an instant jump), and the REAL `QMediaPlayer.
position()` tracks the computed target within ~1ms at every step
(confirmed by polling it directly, not just the internal counter).

**"Backward" needed no special-case logic at all** — it's just a seek
to a smaller position than the one before it, which `setPosition`
already does correctly either direction; this also sidesteps real,
confirmed cross-backend unreliability of `QMediaPlayer`'s own negative
`setPlaybackRate` for true reverse DECODE, never attempted here.
"Stop" needs none either: once the live target stops changing (mouse
stationary), the next few ticks converge `_scrub_current_ms` to it and
then produce a near-zero delta forever after, which the `>= 15ms`
dead-zone check in `_on_scrub_tick` turns into "stop calling
`setPosition()` at all" — the displayed frame just stays put.

**Only engages while the player is PAUSED** — checked via `self.
_is_playing()` (a closure bound in `_build_video`, where `QMediaPlayer`
is in scope — it's only ever imported locally there, the project's
existing lazy-import convention for `QtMultimedia`, kept rather than
forcing it in at module scope for this one feature) — deliberate, so a
mouse merely resting over a video the examiner pressed Play on doesn't
hijack real playback/audio out from under them. The existing Play/
Pause button and slider-drag are untouched; this is an additional,
separate interaction mode for a stopped clip, not a second control
layer fighting the first. `video_widget.setMouseTracking(True)` +
`installEventFilter(self)` is what makes plain hover (no button held)
deliver `MouseMove` at all — off by default on a bare `QVideoWidget`.
`QEvent.Type.Leave` disengages scrubbing (`_scrub_target_frac = None`)
so the frame doesn't keep drifting once the cursor leaves. Both the
timer and the installed filter are torn down on `load_content`'s
existing teardown path (swapping to a different file in an
already-open dialog) and in `closeEvent`, the same places the
player/temp-file cleanup already happens.

**EXIF / video-metadata columns (`app/media_metadata_scan.py`) — added
2026-10-11**, direct request: "i kind of think i should just do a
media pass after the header check via type i.e. pic[t]ure, png and
video[,] get all the exif and add it as ad[d]itional columns in the
browser... it is super use[ful] where it is present." A new Process
Case stage (`MediaMetadataScanWorker`), checked by default — direct
instruction ("lets make it that it is a[u]tomat[ic]ly tick[ed] but the
user can untick it if they want") — that runs LAST (after the header
scan, nested-archive extraction, and the embedded-media sweep: "for
the zips and embe[d]ded it should be done as part of the process like
the header review," since reading nested/embedded content needs those
to have already run), scoped to `scan_folders()`/`scan_excludes()` —
the SAME in-scope-for-processing folders the header scan already uses,
direct correction when this was scoped: "we are only doing this in the
[folders] that are marked as place to include and not the one
exscluded for the header proceesing."

Seven new File Browser columns, `"(EXIF)"`-suffixed (`Taken (EXIF)`,
`Camera (EXIF)`, `GPS (EXIF)`, `Duration (EXIF)`, `Dimensions (EXIF)`,
`Software (EXIF)`, `Orientation (EXIF)`) — the suffix, not a rename of
the existing `_PHOTO_HEADERS` (`Taken`/`Camera`/`GPS`/`Place`), was the
direct answer to a real overlap flagged before building this: iOS's
Photos.sqlite already provides those for Photos-indexed images
specifically; these instead come from the file's OWN bytes and apply
to every Picture/Graphic/Video file in scope, Photos-indexed or not —
keeping the shipped names unchanged avoids touching anything already
saved in a report/column layout. Shown whenever `self._media_exif` is
non-empty for the case at all (not a narrower per-folder prediction —
same "blank cell when absent" convention `_PHOTO_HEADERS` already
uses, so a folder with no EXIF-bearing files just shows blank columns,
not a missing/wrong set).

**"a smaller check before the decode" — confirmed real, not assumed**:
neither extraction ever decodes a pixel or a video frame (`Image.
open()` is lazy; PyAV's container open only reads the moov/metadata
atoms). The real cost is I/O, not CPU — reading a whole candidate's
bytes first (the obvious approach) wastes effort on a large video
whose `moov` atom sits near the front. `_LazyZipSlice` (read/seek/tell
backed directly by `zip_reader.ZipReader.read_at()` — no `zipfile`, no
decompression, FFS archives are always stored uncompressed) lets PyAV
pull only what it needs. Verified directly against the largest real
video in the Josh Hickman archive (102MB): a full read took 25.2ms and
touched 100% of the file; the lazy reader took 6.2ms and touched 0.22%
of it (225KB). Also verified directly (a second real worry: "does a
file WITHOUT EXIF cost the same as one WITH it, or could a cheap
pre-check skip further work"): no measurable difference — 151 real
app-bundle PNGs (9 had EXIF) averaged 0.162ms/file; 139 real DCIM HEIC
photos (139 had EXIF) averaged 0.146ms/file — the cost is parsing the
file's own container structure, which happens either way just to
confirm it's openable at all; checking for EXIF afterward is then a
free dict lookup, so there's no cheaper signal below that to pre-check
with.

**A real, confirmed bug caught during this feature's own first test
run**: `extract_image_exif` initially never registered `pillow_heif`'s
HEIC opener (media_viewer.py's `_load_qimage` does this, but that
Qt-importing module can't be imported from this Qt-free one) — EVERY
real HEIC file (iOS's default photo format since iOS 11; 139/139 in
one real DCIM folder) silently raised `UnidentifiedImageError` and
returned `{}` — no width/height, no EXIF, nothing. Fixed with the same
one-time-registration flag/pattern, duplicated rather than imported.
Re-verified after the fix: 139/139 real HEIC files yielded width/
height, 138/139 yielded `taken`, 137/139 yielded real GPS coordinates.

Nested-archive entries and embedded-media hits ("for the zips and
embe[d]ded") are collected separately
(`FastZipBrowser._collect_local_media_metadata_candidates`, only
FastZipBrowser owns `_nested_archive_map`/`_embedded_media_containers`)
and handed to `ProcessDialog` as a CALLABLE, not a precomputed list —
a plain list captured at dialog-open time would miss hits THIS SAME
Process Case run's own embedded-media sweep (the stage immediately
before this one in the chain) just found. A nested-archive entry's
real bytes live inside a REPACKAGED zip (`zip_reader.
read_nested_entry`, not a plain extracted file) so those are read
eagerly into `bytes`; an embedded-media hit is already a real local
file (`embedded_media_scan.extract_media_bytes`'s content-addressed
output) so that one is opened lazily instead — `scan_local_candidates`
accepts either shape per candidate. Verified directly against this
same real case's own pre-existing nested-archive/embedded-media
results: a real nested-archive JPG (an iOS effects-bundle resource)
and two real embedded-media hits (a GroupMe cache PNG, an App Store
cache HEIC) all scanned correctly through this path.

Persisted in casecache.db's new `media_metadata` table (one row per
ui_path, mirroring `header_types` exactly — fully re-derivable from the
archive's own bytes, so it belongs there, not caseresults.db), reloaded
at case-open through the SAME background poll `_start_case_meta_load`
already uses for header types/photo index/timezone.

**LevelDB/IndexedDB indexing moved into an explicit, checked-by-default
Process Case stage — 2026-10-12**, direct request: "the leveldb
processing that is do[ne] is started after the data is loaded into the
gui and at the same time that the process dialog opens[;] can we
instead have it as an option in the process case and have [it] ticked
on... like media [meta]data processing." Previously ran unconditionally
on a `QTimer.singleShot(500, ...)` fired from `on_metadata_ready` —
confirmed the real complaint: that's the EXACT same moment a brand-new
case's Process Case dialog auto-opens (`_open_process_dialog_for_new_
case`), so the two genuinely competed for the same CPU/UI thread. Now
runs FIRST in ProcessDialog's own operation chain (before the header
scan — unlike media metadata, nothing else here needs it to have run
first, and it needs nothing else to have run first either), via a new
`ProcessDialog._start_leveldb_indexing`/`_continue_after_leveldb_
indexing` pair inserted between `_continue_after_integrity` and the old
chain start.

The real engine (`FastZipBrowser._leveldb_search_coverage`/
`_index_leveldb_folders_batched`, unchanged) is handed to ProcessDialog
as two callables, not reimplemented as a QThread worker the way every
OTHER stage here is — `_index_leveldb_folders_batched`'s own docstring
explains why it deliberately runs frame-budgeted on the MAIN thread
instead: it reads archive bytes via `self._read_zip_bytes`, the GUI's
shared reader, and this project's standing convention is that a
background worker must own an INDEPENDENT reader rather than share
that one. Turning this into a real stage gave it something it never
had before — actual cancellation: `cancel_check` is now checked once
per folder (same cadence as its own progress callback), and a
cancelled run deliberately skips writing the "fully covered" version
marker, so the folders it didn't reach stay correctly reported as
unindexed rather than being falsely marked complete.

The Keyword Search tab is still greyed out while this runs (searching
mid-index would silently miss whatever hasn't been indexed yet) —
but ONLY for the real duration of the stage now, via `leveldb_indexing_
started`/`_done` signals FastZipBrowser connects to `setTabEnabled`,
not unconditionally from the moment ANY case loads. Unticking the
checkbox just means Keyword Search runs with whatever coverage this
case already has, exactly like skipping any other optional stage —
the tab is never disabled at all if this stage doesn't run.

Verified directly against a real archive (not just "should work"): 19
real LevelDB/IndexedDB folders, forced into a stale-coverage state,
correctly dropped from 19 unindexed to 0 after running through
Process Case; a simulated real Cancel-button click fired the instant
indexing started left coverage at 19 unindexed afterward (not 0) —
confirming the version-marker skip genuinely prevents a false
"fully covered" claim, not just in theory.

**File Browser ↔ Artifact Report cross-reference ("Linked Report"
column) — added 2026-10-12**, direct request: "i want to be able to
link together the artifact view and the file browser if a[n] messaging
report ha[s] a[n] attachment... you could filter for sms in that
column and get all the atacha[t]ment for sms and then... right[-]click
to see the message." The user's own first idea was a new per-report
auto-incrementing id (e.g. "sms-attachment-1") — scoped instead around
reusing what was already there: `sms_messages.py` already resolves a
real `attachment_path` per message and declares it via `media_fields =
["attachment_path"]`, a convention `artifact_viewer.py` already used
for thumbnail pre-caching; every artifact table also gets a real,
stable SQLite rowid for free; and `_art_jump_to_report_row(script_name,
rowid)` already existed (built for "Interpret as SQL Record," with
bookmark row-citation explicitly named as shared groundwork) — a jump-
to-exact-row mechanism with its own graceful "row no longer there"
fallback already battle-tested. No new per-script convention was
needed in the parser scripts themselves: any parser that already
supports thumbnail columns automatically qualifies.

`app/artifact_db.py`'s `build_attachment_links(script_name, rows,
media_fields, label)` computes `[{ui_path, row_id, label}]` — `row_id`
is 1-based and MUST equal the real implicit rowid
`write_artifact_results` assigns that same row: true by construction
since that function always drops and recreates `artifact_<script>`
with no explicit PRIMARY KEY, so a fresh table's rowid is 1 for the
first inserted row, 2 for the second, deterministically, as long as
both are called with the exact same `rows` list in the same order
(confirmed, not assumed — see below). `label` is the report's own
display name, the SAME string for every link from that script (not a
per-row id) — deliberate, so a filter fragment like "sms" finds every
attachment that report references at once; the exact row only matters
once the examiner has already decided to jump to it.

Persisted in caseresults.db's new `artifact_attachment_links` table
(alongside the `artifact_<script>` tables it points into, not
casecache.db) — keyed by `(ui_path, script_name, row_id)`, since a
single attachment can legitimately be referenced by more than one row
(a group-MMS attachment shared across several message rows is the
common real case, confirmed against this one real archive: 19 real SMS
attachments from 91 real messages). Always fully rebuilt per
script_name on each parser re-run (`clear_attachment_links_for_script`)
— same "never let a stale link silently survive" convention
`write_artifact_results` itself already follows for the report table
these rows point into. Registered right next to
`_precache_media_thumbnails` in `ArtifactRunnerWorker.run()` (the new
`_register_attachment_links`), reusing the exact same `media_fields`
read and the exact same `rows` list, in the exact same order — the one
real constraint this whole design depends on.

The File Browser's new "Linked Report" column (conditionally shown,
same pattern as the "(EXIF)" columns) shows `_format_attachment_links`'
grouped-by-label output — e.g. `"SMS Messages"`, or `"SMS Messages
(×3)"` for several references from the same report. The right-click
menu's new "Show in Artifact Report" action (single link) or submenu
(2+, each naming its own row_id) calls the pre-existing
`_art_jump_to_report_row` directly — no new navigation code needed at
all.

Verified end-to-end against a real archive, not just unit-level: ran
the real `sms_messages.py` parser against a real iOS17 archive (91
messages, 19 with a real attachment), wrote results, built and
persisted links, reloaded them, and independently queried
`artifact_sms_messages` by the computed `row_id` to confirm it names
the EXACT same real row the link claims to (not merely "doesn't
crash") — then right-clicked (programmatically) to confirm the jump
genuinely switches tabs and selects that real row in the live
`QTableView`, not just that the function returns without error.

**File Browser "Inode" column (GrayKey + new-layout Cellebrite iOS) —
added 2026-10-16**, after a direct investigation into whether Android's
bind-mount model (the same real file appearing at multiple paths, e.g.
`/data/data/...` vs. `/data/user/0/...` vs.
`/data_mirror/data_ce/null/0/...`) could be surfaced from inode data.
Confirmed empirically across all real extraction formats before building
anything, then refined once more real archives were checked:

- **GrayKey (iOS and Android)**: real inode data in the zip extra
  fields' `0x4e49` ("IN") TLV block — `inode(8B) + devID(4B)`
  (`app/adapters/graykey.py`'s `_TAG_IN` parser). Raw inode numbers
  alone collide across Android's separate mount filesystems (a real
  false match was found: `mmssms.db-wal` sharing an inode number with an
  unrelated `.apk` on a different device) — `devid` is required
  alongside `inode` to disambiguate. With both, cross-checked against
  real duplicate groups (CRC32+size and cross-platform spot checks):
  zero false positives found.
- **Cellebrite iOS, new layout** (`user_prefix='filesystem2' != sys_prefix
  ='filesystem1'`, `old_layout=False`; separate `metadata1/metadata.msgpack`
  = system and `metadata2/metadata.msgpack` = user partition files exist
  side by side in the zip): `load_metadata` already only ever loads
  `metadata2` — the user partition — in isolation (tries that candidate
  first, returns immediately; `metadata1` is never read at all for this
  layout, matching `build_zip_entries` which only ever walks
  `user_prefix`). Checked a real such archive (`iOS16.5 CTF23
  Cellebrite`) directly: `metadata2` alone has 5,046 real same-file-
  different-path groups and **zero** size-mismatched false collisions;
  `metadata1` alone (confirmed separately, even though the app never
  loads it) also zero. A single isolated partition's inode numbers
  don't collide in practice — safe to show. No `devid` key exists in
  this schema at all, so the column shows the bare inode, no suffix.
- **Cellebrite iOS, old layout** (`user_prefix == sys_prefix`, no
  partition split available — `metadata1/metadata.msgpack` covers
  system *and* user merged into one dict, which is also everything the
  app shows, since there is no second partition to exclude): checked a
  real such archive (`IOS17 JoshHickman`) directly — 2,499 of 10,869
  same-inode groups (23%) have mismatched file sizes, i.e. real
  coincidental inode-number reuse between unrelated files (inode `2`,
  the classic root-directory inode, was "shared" by dozens of unrelated
  directories). Not reliable — never shown for this layout.
- **Cellebrite Android (`FORMAT_ZIP_EXTRAS`, the "Dump/" + UT-extras
  layout)**: no inode data anywhere — confirmed zero `0x4e49` blocks
  across ~288K real file entries; `load_metadata` hardcodes `inode: 0`
  for this format.

`_inode_col_available()` is the single gate used everywhere the column
is conditionally added (same pattern as `_EXIF_HEADERS`/
`_LINKED_REPORT_HEADER`): true for `FORMAT_GRAYKEY` always, true for
`FORMAT_CELLEBRITE` only when `not old_layout`. `_format_inode()` shows
`f"{inode}:{devid}"` when a `devid` key is present (GrayKey) or the bare
inode when it isn't (Cellebrite new-layout) — never a misleading `:0`
suffix for a format that has no device concept at all. Deliberately NOT
wired into any automatic duplicate-detection or dedup logic — the user
wants to filter/sort on it manually themselves, nothing more.
`graykey.py`'s `extract_metadata` now also returns a `devid` key
alongside `inode` (previously decoded and discarded).

Verified end-to-end against real archives (loaded through the real
adapter, not mocked), not just the collision analysis above: the
"Android 14 CTF26 Magnet" GrayKey archive's known real triplicate
`mmssms.db` (`/data/data/...`, `/data/user/0/...`,
`/data_mirror/data_ce/null/0/...`) all show the identical "Inode" value
`6981:65077` in a live `QTableView`; "iOS16.5 CTF23 Cellebrite" (new
layout) shows bare inode values with no colon suffix; "IOS17
JoshHickman" (old layout, Cellebrite) and both real
`FORMAT_ZIP_EXTRAS` Android archives correctly show no "Inode" column
at all.

**"Accessed" and "Link Count" columns — added 2026-10-16**, same batch,
direct request: "i only want the column present if the data is
present" — both gated on real sampled data rather than a format check,
unlike Inode above.

- **Accessed** (`atime`) folds into the existing `_detect_time_columns`
  sampling that already decided whether to show Created/Changed — it
  already only shows Created/Changed when real nonzero values are
  found in a 500-entry sample; Accessed now gets the same treatment,
  for BOTH platforms (previously only Android was sampled; iOS
  unconditionally got Created/Changed). Real on GrayKey (iOS and
  Android) and both Cellebrite iOS layouts; absent on Cellebrite
  Android zip-extras, where the UT block's atime flag bit claims
  presence but the stored value is always `0` in every real entry
  checked (same phenomenon already noted for Changed/ctime on that
  format). Verified end-to-end: real non-zero Accessed timestamps shown
  for both GrayKey archives and both Cellebrite iOS layouts; correctly
  absent for the real Android zip-extras archive.
- **Link Count** (`links`, the file's hardlink count) — named "Link
  Count" rather than plain "Links" specifically to avoid confusion with
  the unrelated "Linked Report" column above. Gated on
  `self._has_link_count_data`, set once per archive load in
  `on_metadata_ready` the same way `self._time_cols` is — sampling up
  to 500 metadata dicts for a non-`None` `links` value. Real only on
  Cellebrite's `metadata.msgpack` schema, for BOTH old- and new-layout
  archives (unlike Inode, a plain hardlink count isn't subject to the
  cross-partition collision problem that `old_layout` gates — it's
  just an integer field in the dict, nothing computed from it); GrayKey
  never stores it (`extract_metadata` always returns `links: None`).
  Verified end-to-end: real values (`'8'`, `'487'`, `'12'`, `'169'`,
  etc.) shown for both real Cellebrite iOS archives (old and new
  layout); correctly absent for both real GrayKey archives and the
  real Android zip-extras archive.

**Duplicate file detection ("Duplicate Count"/"MD5" columns,
`app/duplicate_scan.py`) — added 2026-10-16**, direct request following
the Inode investigation above: "i want to use the size and crc and then
md5 check to confirm. hopefully only do the md5 for the duplicates will
make it not too long. i want it to be an option that is pre[]ticked in
process case.. and again at the bottom of this dialog i want a bit of
text to indicate that it is done or not" — then "the result needs to be
a column with duplicate count and a column with md5."

New last stage in `ProcessDialog`'s operation chain (after media
metadata; `_continue_after_media_metadata` → `_start_dedup_scan`),
checked by default via `self._chk_dedup`. Algorithm: group every real
file by `(size, CRC32)` — both free, already parsed from the zip's
central directory, no I/O — then MD5-hash only the files inside a
multi-member group to confirm genuine byte-identity (CRC32 alone can
coincidentally collide). `DuplicateDetectionWorker` (`QThread`) drives
`duplicate_scan.find_duplicate_groups`; `_on_dedup_done` persists
results to casecache.db's new `duplicate_files` table (`ui_path`, `md5`,
`size`, `group_size` — always fully rebuilt, same "never let stale data
survive" convention as `artifact_attachment_links`, NOT media_metadata's
own never-cleared convention, since a file's duplicate status can
genuinely become false between runs) and a `run_log` entry
(`_RUN_TYPE_DEDUP`), then emits `duplicate_detection_done` →
`FastZipBrowser._on_duplicate_detection_done` reloads and repaints, same
"reload then repaint" shape every other per-file-results feature here
uses. The dialog's own stats block (`_refresh_stats`) gained a
"Duplicate detection: `<time>` — N file(s) in M group(s) confirmed"
line, with a cancelled-run warning — directly answering "a bit of text
to indicate that it is done or not."

`_collect_duplicate_candidates` resolves `[(ui_path, physical_path,
zinfo, inode, devid)]` the SAME way `_resolve_file_candidates` already
does for every other candidate pass (`resolve()` then `z.getinfo()`) —
deliberately NOT `FfsAdapter.build_zip_entries()`, which turned out to
be exercised only for Cellebrite elsewhere in this codebase
(`ffs_metadata.py`'s own comment: "Only Cellebrite iOS uses the Pass-1
zip-entry precompute") and was confirmed here to silently return an
EMPTY dict for a real GrayKey archive — its prefix-matching assumes no
leading slash, but GrayKey physical paths always have one. A real bug
caught before shipping, not a hypothetical. Deliberately unscoped by
`scan_folders()`/`scan_excludes()` — grouping by size+CRC32 costs no
extra I/O regardless of candidate count, and the original real-world
motivation (an app-cache copy of a user's own photo) can sit well
outside whatever folders are marked for header-scan processing.

**Inode shortcut — same day, direct follow-up**: "if we already know it
has the same inode can we not shortcut the check[?] do not bother with
the md5." `FfsAdapter.inode_trusted` (the same gate the Inode column
above uses: GrayKey always, Cellebrite only when `not old_layout`) is
passed through to `find_duplicate_groups`. POSIX guarantees `(device,
inode)` uniquely identifies a live file — not a heuristic — so members
of a `(size, CRC32)` candidate group sharing a real `(inode, devid)`
collapse into one "hash unit": only the first member is actually read;
its MD5 is applied to every sibling sharing that inode. Confirmed on a
real archive ("Android 14 CTF26 Magnet," GrayKey): 121,382 candidate
files down to 42,492 actual reads (65% fewer), with byte-for-byte
identical results (121,381 confirmed duplicate files, 34,134 groups,
same known `mmssms.db` triplicate) to the un-optimized run — the
shortcut changes performance only, never the outcome. Cellebrite has no
real `devid` field at all; candidates get `devid=0` uniformly, making
`(inode, 0)` grouping exactly equivalent to grouping on bare inode —
correct specifically because `inode_trusted` for Cellebrite only turns
on when a single partition's metadata is loaded in isolation (new
layout), so there is no second device's inode namespace to collide
with. Confirmed the `inode_trusted=False` path (old-layout Cellebrite)
is unaffected — every candidate still gets hashed individually, matching
pre-optimization behavior exactly.

**"Hide Duplicates" checkbox — Media Browser only.** Added 2026-10-16
in the File Browser; MOVED 2026-10-17, direct correction: "in the file
bowser... you would see all files and duplicates are not hiden the hiding
only happends when view the selected files in the media browser. so that
is were the hide duplicate tick should be. and it only affect how many
files are present in that view." The File Browser never hides duplicates
(`_build_entry_row` has no duplicate check; the "Duplicate Count"/"MD5"
columns are still shown there).

The checkbox (`self._media_hide_dup_chk`, flag
`self._media_hide_duplicates`, ticked by default, per-session — not reset
on folder change) sits in the Media Browser status row. It's applied in
`_recompute_media_all_paths`, the one funnel every Media Browser view
(folder, Selected Files, User Media, Embedded Media) goes through, and runs
BEFORE the hide-seen and Picture/Video filters:
- Each md5 group keeps its FIRST copy in that view's own list — not an
  archive-wide representative. An archive-wide pick would make a file
  disappear completely from a folder that only holds a non-chosen copy.
  (The 2026-10-16 File Browser version did use an archive-wide pick,
  `_duplicate_representatives`, now removed.)
- Because it runs before the seen filter, a group whose kept copy is
  already "Not Relevant" stays hidden instead of the next copy surfacing.
- The hidden count shows in the status line ("N duplicate(s) hidden"),
  built by `_update_media_status_text` (factored out of
  `_start_thumbnail_load` so the toggle can refresh it).

**"User Media (N · M to review)" — added 2026-10-17**, direct request:
"make it that user media is altered by the hide duplicates... that it
show the two values." While "Hide Duplicates" is on and duplicates exist,
the bookmark panel row shows N (files clicking it selects, the same as
"Selected Files") and M (thumbnails left after collapsing duplicates).
Both the panel and `_recompute_media_all_paths` use
`_collapse_duplicate_paths`, so M always equals the grid's own count.
Toggling the checkbox and `_on_duplicate_detection_done` both refresh the
panel. Verified on CTF26: 3,913 · 3,609 to review; the grid shows 3,609
(304 duplicates hidden); an independent count gives 3,609; unticking
shows "User Media (3,913)".

**User Media count was stale on case open — fixed 2026-10-17.** The panel
counted at tree-done, before `_start_case_meta_load` loaded the
header-type overrides that `_is_media_file` uses. CTF26 showed 1,294 when
the click selected 3,913 (reproduced exactly: 0 vs. 5,049 overrides). The
panel now refreshes after that load, after `_on_header_scan_done`,
`_on_header_types_cleared` and `_on_single_scan_done`.

`self._duplicate_groups` (md5 → sorted member `ui_path`s) is rebuilt by
`_recompute_duplicate_groups` wherever `self._duplicate_files` is
(re)loaded: `on_metadata_ready`, `_start_case_meta_load`'s background poll,
and `_on_duplicate_detection_done`.

**Bookmarking a shown duplicate bookmarks every copy — added
2026-10-17**, direct request: "if the hide duplicate is active and in the
media viewer you selected a file with duplicates all the duplicates are
bookmarked as in practise we want to have less media files to review
visually but if there is one that is important we want to know all the
locations that it apears." `media_viewer._get_media_paths_for_bookmark`
(used by both the right-click "Bookmarks" menu and Ctrl+B) expands each
selected path in `self._duplicate_files` to every member of its md5 group,
archive-wide, while `_media_hide_duplicates` is on. With it off, the
selection is taken as-is.

Verified 2026-10-17 against the real CTF26 duplicate scan (121,381 rows /
34,134 groups), on a real offscreen `FastZipBrowser`:
- The checkbox is in the Media Browser tab and not the File Browser tab.
- A 3-copy Chrome `app_textures/0.jpeg` group plus one unique file shows 2
  thumbnails, with the status line reading "(2 duplicate(s) hidden)".
- Unticking the real checkbox restores all 4; re-ticking collapses them
  again.
- A list holding only a non-first copy still shows it.
- A group whose kept copy is seen stays hidden.
- Bookmark expansion returns all 3 paths when on and 1 when off, with no
  double-adding.
- Every rebuilt group's member count matches its stored `group_size`.

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

**Import Bookmarks…** (Tools menu) — bulk-bookmarks files from a CSV/XLSX
hash list (e.g. a ProjectVic CAID differences/export report): the
examiner maps which column holds the hash (+ its algorithm), the
filepath, an optional separate filename, and which column (or one fixed
value) becomes the bookmark group name. A row's own hash is matched by
path then CONFIRMED by re-hashing that file's actual bytes from the
archive — never trusted as a label on its own. Each distinct bookmark-
value becomes/reuses its own group (`get_or_create_bookmark_group`), so
results land organized the same way the source spreadsheet already was.
`app/spreadsheet_import.py` (Qt-free) does the parsing/normalization;
`ImportBookmarksWorker`/`ImportBookmarksDialog` in `ffs-explorer.py` do
the archive matching and UI. A spreadsheet's path cells commonly carry
the source archive's own filename as their first segment (confirmed
against a real ProjectVic export, 2026-10-07) — stripped before matching,
since this app's own ui_path convention never includes it.

In "From a column" mode, a checklist (every distinct value in the
chosen column, all checked by default) lets the examiner import only
specific groups (e.g. only "Evidence - Videos" out of several values
present) instead of all-or-nothing — only shown in that mode, since a
fixed value has nothing to choose between. Only applies real filtering
once the examiner has actually unchecked something; left fully checked,
behavior is unfiltered (including the existing blank-cell-falls-back-to-
the-default-group rule) — otherwise a sheet with ANY blank cells in the
bookmark column would start silently dropping those rows the moment
this feature shipped, even for an examiner who never touched the
checklist at all.

**Separate filepath/filename columns — real bug fixed 2026-10-10.**
When "Filename is its own column" is used, the matching path MUST be
the two columns joined (`combine_filepath_and_filename`) — the first
release only ever read the filename column for the bookmark's own
display name, so the path actually looked up in the archive was just
the containing DIRECTORY, which can never match a file. Confirmed
against a real Griffeye case-analysis export, whose own "File Path"/
"File Name" columns split exactly this way — every row "worked" when
path+filename were combined in one column and silently failed the
moment they were split, with no visible error (every row just counted
as "not found").

**"Remove parent folder from Filepath"** — a second checkbox next to
"Filepath column:", for tools (confirmed: Griffeye) that prefix every
path with ANOTHER segment above the archive's own filename: the folder
the FFS zip itself was sitting in on disk, e.g.
"CaseFolder\EXTRACTION\_FFS.zip\data\...". `normalize_ffs_path`'s own
archive-name check only ever looks at the FIRST segment, so it can't
remove this on its own — the new flag strips one leading segment
unconditionally BEFORE that check runs, so the check still correctly
fires against whatever's now first. The preview table updates live
when toggled, same as every other mapping control.

**iOS `private/var/mobile`/`private/var/root` collapsing
(`ios_path_candidates`)** — a SEPARATE real quirk found testing against
a real Griffeye export + a real Cellebrite-format iOS archive: this
app's own ui_path convention collapses `private/var/mobile` and
`private/var/root` down to just `mobile`/`root` for those two per-user
home directories (baked into how `folder_map` itself gets built, not
exposed as a reusable transform — `FfsAdapter.strip_display_prefix`
only strips the archive's own internal format prefix, e.g.
"filesystem1/", never this). Griffeye's raw path spells out
"private/var/mobile/..." literally AND separately prefixes its own
generic "root/" marker for "the top of the extracted device
filesystem" — a real, confusing name collision with this app's own
different, specific meaning of "root" (`/private/var/root`). Passing a
path with that marker still attached straight to
`FfsAdapter.resolve()` produces a doubled, nonsense physical path
(confirmed directly: resolve() re-inserts "private/var/" before a
recognized "root"/"mobile" first segment). `ios_path_candidates`
offers several interpretations (with/without Griffeye's marker,
with/without the collapse) and `ImportBookmarksWorker` tries each in
turn, keeping whichever one actually resolves — confirmed end-to-end
against the real export: 3 of its 4 real rows matched with a verified
hash; the 4th has an anomalous zero `Created Date` unlike the other
three's real dates, consistent with Griffeye-recovered/synthetic
content with no live zip entry to match at all, not a bug here.

**Saved presets** — a "Preset:" row with Save/Delete, for re-running
the same spreadsheet "type" (e.g. "Griffeye") without re-picking every
column each time. Stored in `QSettings` (key
`import_bookmarks_presets`, JSON-encoded dict, same convention as
`folder_column_configs`) BY COLUMN NAME, not index/position, so a
preset still applies correctly to a later export whose columns are in
a different order. `_browse` auto-applies a saved preset the moment
exactly one of them is a clean fit for the newly-loaded file's own
headers; more than one fit (or none) leaves mapping at its normal
reset state and the preset combo on "(None)" rather than guessing.

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
| `scan_rules.py` | The shared folder-prefix ruleset (include/exclude/user_media/embedded_media_exclude) behind `FfsAdapter.scan_folders()` and friends — see Header-scan tiers above. |
| `embedded_media_scan.py` | The embedded-media sweep — see section 3 above. |
| `duplicate_scan.py` | Process Case duplicate detection: size+CRC grouping, MD5 to confirm (inode shortcut where trusted). Qt-free. |
| `embedded_media_skip_list.py` | Known-noise database skip list — see section 3. |
| `media_metadata_scan.py` | EXIF (images) / container metadata (video) extraction for the File Browser's "(EXIF)" columns — `_LazyZipSlice`, `extract_image_exif`, `extract_video_metadata` — see "EXIF / video-metadata columns" above. |
| `user_media_ignore_list.py` | Global exclusion list for the Media Browser's "User Media" filter. |
| `file_browser_presets.py` | Shared, examiner-editable filter+sort presets behind the File Browser's preset dropdown AND the Media Browser's Picture/Video/Graphics buttons — see "File Browser filter+sort presets" below. |
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
| `spreadsheet_import.py` | Qt-free CSV/XLSX reading + path/hash normalization for "Import Bookmarks…" — see above. |

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
- A config file with its own `store_path()` is invisible to PyInstaller
  unless EXPLICITLY added to the spec's `datas=` — confirmed a real,
  shipped bug this way, 2026-10-12: `app/scan_rules.py`'s two JSON files
  (added 2026-10-10) were never added to `datas=`, so a real frozen exe
  had NOTHING in scope for the header scan, nested-archive discovery,
  OR the Media Browser's "User Media" filter at all (`load_rules()`'s
  own "missing file → `[]`" fallback is deliberately safe for a
  genuinely corrupt file, not for "never shipped" — every real caller
  silently got an empty scope instead) — direct report: "for the user
  media was 0 and then i checked scanned folders in process case there
  was nothing there." Dev/venv runs never catch this: `sys._MEIPASS`
  only exists in a real frozen process. Two DIFFERENT destination
  conventions are both in play and must each match their own module's
  `store_path()` exactly — `scan_rules.py` reads via `sys._MEIPASS +
  'config'` (so `datas=('config/scan_rules_ios.json', 'config')`, same
  as `hardware_models.json`), while `user_media_ignore_list.py`/
  `file_browser_presets.py` read DIRECTLY next to the exe with NO
  `config` subfolder (so `datas=(..., '.')`, not `'config'` — the two
  look almost identical in the spec file but a mismatched destination
  silently ships the file to the wrong place, which is exactly as
  broken as not shipping it at all). When adding a new hand-maintained
  config file, check which convention its own module's `store_path()`
  actually uses before copying either example.

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
- A headless verification script that drives a real archive load can get
  its ENTIRE body re-executed a second time, racing/duplicating whatever
  it does (including real DB writes) — macOS's default `multiprocessing`
  'spawn' start method (used by the first-open metadata parse — see
  `ffs_metadata.py`'s own entry in the module table) re-imports the
  launching script as `__main__` in the child process it starts, and a
  standalone test script run directly (not through pytest, which already
  guards against this itself) has no such guard of its own. Root cause
  of several confusing "printed twice"/"ran twice" oddities this project
  hit during testing before this was diagnosed. Guard every such script
  with `if multiprocessing.current_process().name != "MainProcess":
  sys.exit(0)` right after the imports.
- After a `QTreeView`/`QStandardItemModel` subtree is destroyed and
  rebuilt under `blockSignals(True)` (needed to suppress spurious
  `itemChanged` firing while mass-constructing items), a subsequent
  `tree_view.expand(index)` on a freshly-built item can silently fail to
  register (`isExpanded()` reads back `False` immediately after) — the
  view's own layout/geometry cache never got the suppressed rowsRemoved/
  rowsInserted notifications it normally relies on to stay in sync. Call
  `tree_view.doItemsLayout()` once, right after re-enabling signals and
  before any `expand()` calls, to force it to recompute. Only shows up
  on a SECOND (or later) rebuild of the same branch in one session — easy
  to ship unnoticed if only the first rebuild is ever tested.
- `QTreeView.scrollTo(index, EnsureVisible)` only ever moves the
  VERTICAL scrollbar — confirmed on a bare `QTreeView` with no app code
  involved, so this is a real Qt/PySide6 behavior, not something specific
  to this file. Only the `PositionAtCenter`/`Top`/`Bottom` hints touch
  the horizontal one. Anything that needs a deeply-indented item's
  horizontal position adjusted (e.g. `_on_tree_current_changed`'s
  parent-stays-visible nudge) has to compute and set
  `horizontalScrollBar().value()` directly rather than relying on
  `scrollTo` for that axis.
- Never gate an entire menu's visibility on ONE feature's own on/off
  preference once that menu holds more than just that feature — a real
  bug this way: `_apply_ai_pref_visibility` used to hide the whole
  "Tools" menu (`menuAction().setVisible(...)`) when the AI-access
  preference was off (the default for a fresh install), which was
  correct back when Tools held only AI actions but silently hid every
  OTHER unrelated action added to Tools later (Timestamp Display, then
  Import Bookmarks) too — found via direct report ("i can only see
  file" in the menu bar) and confirmed with a real screenshot of the
  running app. Toggle the specific actions a preference owns, never the
  shared menu they happen to live in.
- A file bookmarked into 2+ groups renders the File Browser Name cell as
  hard-edged equal color bands (one per group, in group-id order), not a
  single earliest-group-wins color — `load_bookmark_colors_multi`
  (`db_utils.py`) + `BookmarkSplitColorDelegate`, which fills each
  band's own exact `QRect` directly rather than returning a
  `QLinearGradient` brush from `BackgroundRole` (that was tried first;
  see the delegate's own docstring for why it was dropped — not because
  the gradient was provably wrong, but because the pixel-sampling test
  used to check it had its own bug, so the delegate's simpler, harder-
  to-get-subtly-wrong approach was kept instead once both were confirmed
  correct). Direct report, 2026-10-07: an "Import Bookmarks…" run had
  put several files in two groups at once, and the old single-color
  version silently showed only the earliest group's color, making the
  second membership invisible. The Media Browser's own bookmark badge
  still uses the single-color `load_bookmark_colors` unchanged — it's a
  small outline, not a filled cell, so splitting it isn't the same kind
  of fit; only asked to change the File Browser.
- The bookmark panel's own GROUP highlight (`_active_bookmark_group_id`/
  `_active_bookmark_group_paths`) tracks whether the live tick-based
  selection still overlaps that group AT ALL, re-checked on every
  `_refresh_file_checked_paths` call (the one shared choke point a tree
  tick, "Deselect", and "User Media" already all funnel through) — not
  tied to the QListWidget's own click-selection, which only changes when
  that list itself is clicked. Retains the highlight on a PARTIAL
  overlap (one of a multi-file group's files got unticked, not all);
  clears it once the overlap hits zero — and never re-sets it later even
  if the examiner ticks back to an identical selection (counts down
  only, per the direct request this shipped for). Setting these two
  attributes must happen AFTER `_replace_selection_from_bookmark_entries`
  runs, never before — that method calls `_deselect_all_files()`
  internally FIRST (clearing everything, then re-ticking), and setting
  them early meant that internal empty-selection moment immediately
  self-cleared the very highlight the click was trying to set, found via
  a real failing test before this shipped.
- "Only Show Selected" (the "Selected Only" tree root row, next to
  "Recursive") narrows a folder's own File Browser listing down to just
  the files that are part of the current selection, hiding the rest —
  direct request: a bookmarked file can be hard to spot among many
  others in the same real folder. Unchecked by default (unchanged
  behavior); `_show_bookmark_group` resets it to unchecked on every
  ordinary group click, while `_show_user_created_media` forces it ON —
  "User Media"'s whole point is showing only the relevant files, so a
  folder opened from it should default to hiding everything else too,
  not just its own thumbnail grid. Folders are never hidden by this (an
  unselected folder can still contain selected files deeper down) — only
  FILE rows are filtered, via the same `_path_currently_selected` check
  the group-highlight feature above uses. Gated on `_selected_only_mode`
  (which tree branch is currently visible), not on the clicked item's
  ancestry — `_refresh_folder_view`'s non-recursive branch is shared by
  BOTH tree branches, so this must never apply while browsing "/ [Full
  Filesystem]", regardless of the checkbox's own state.
- Switching between "/ [Full Filesystem]" and "Selected Only"
  (`_carry_over_tree_selection`) tries, in order: (1) carry the exact
  path that was selected just before the switch, if it exists in the
  branch now shown; (2) failing that, THAT BRANCH'S OWN independently-
  remembered last selection (`_tree_last_selected_path_by_branch`,
  keyed by branch), if it's still valid; (3) select nothing. Re-added
  2026-10-09 as a fallback layered on top of the existing single-value
  carry-over (not a revert of it) — direct request, so switching back
  to a branch after a failed carry-over lands where the examiner last
  actually was in THAT branch specifically, rather than always landing
  on nothing. `_save_tree_selection_for_carryover` updates both the
  shared carry-over value AND this per-branch map from the exact same
  read, every transition, so they can never drift apart.
- This project's own `CLAUDE.md` entries should record **design
  decisions and standing rules**, not a verification/bug-hunting
  narrative — the detailed "found via real archive X, Y checks, screenshot
  confirmed" history lives in commit messages and conversation history,
  not here.
