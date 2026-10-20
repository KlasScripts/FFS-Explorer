# FFS Explorer — Formats and Assumptions Register

**Purpose.** This file lists the facts about the input data that the design
depends on, and the decisions built on them. It sits alongside `CLAUDE.md`
(the code map) and `FFS_processing.md` (the processing walkthrough). The
point is to make every "this is only safe because…" visible in one place.

**How to use it.**
- Before changing code that reads, resolves or parses archive data, find the
  entries below that name that code and check they still hold.
- When a new decision starts relying on a fact about the data, add an entry.
- When a fact is confirmed or disproved on a real archive, update its
  evidence and date.

**Evidence levels used below.**
- **Confirmed (real)**: checked against at least one real archive, named.
- **Examiner**: stated by the project owner from working knowledge.
- **Code only**: what the code does today; not checked against real data here.
- **Unverified**: assumed; nothing has checked it yet.

Last reviewed: 2026-10-20. Written from the code on branch
`claude/blissful-gates-yav73e` and the history recorded in `CLAUDE.md`;
nothing in this document changes code.

---

## 1. Acquisition tools × platforms

There are two tools (Cellebrite, GrayKey) and two platforms (iOS, Android),
giving four combinations. In the code, though, a case is described by an
**archive layout** (`FfsAdapter.format`), not by a tool, and Cellebrite
Android has been seen in **two** different layouts. So the code really deals
with five cases:

| # | Combination | `FfsAdapter.format` | Seen on real archive |
|---|---|---|---|
| A | Cellebrite iOS | `cellebrite` | IOS17 JoshHickman; iOS 16.5 CTF23 Cellebrite |
| B | GrayKey iOS | `graykey` | IOS18 CTF25 Magnet |
| C | GrayKey Android | `graykey` | Android 14 CTF26 Magnet |
| D1 | Cellebrite Android, `Dump/` layout | `zip_extras` | Android 14 JoshHickman (as recorded in `CLAUDE.md` history; not re-checked for this document) |
| D2 | Cellebrite Android, `filesystemN/` layout | `cellebrite` | Android 15 CTF25 Cellebrite |

**A-1. Format is a layout, not a tool.** *Confirmed (real).*
The "CTF26 Magnet" archives (B and C) are labelled Magnet but detect as
`graykey`, and the Cellebrite Android archive in D2 detects as `cellebrite`,
the same as iOS. Never present `.format` to an examiner as "acquired with
tool X". `artifacts/*/device_info.py` labels it as an "archive layout" for
this reason.

**A-2. The format alone does not tell you the platform.** *Confirmed (real).*
`graykey` covers B and C; `cellebrite` covers A and D2. Only `zip_extras` is
unambiguously Android.
- Platform is decided by `FfsAdapter.is_android(folder_map)`: true for
  `zip_extras`; otherwise true only if `data/data` is in the folder map.
- `data/data` was confirmed absent from every real iOS archive checked.
- With no folder map yet (some first-parse callers), it falls back to "iOS".
  A caller that never passes the folder map will treat C and D2 as iOS.
- Relied on by: `container_parents()`, `bundle_id_for_path()`, app
  intelligence / App Report, MCP `list_apps`, the User Media filter, scan
  rules. `container_bundle_id()` uses a local version of the same test (does
  the path start with `data/data/`).

## 2. How each layout is detected

Detection is `FfsAdapter.detect()` (`app/adapters/ffs.py`), tried in this order.

| Order | Test | Result |
|---|---|---|
| 1 | Any of the **first 20** entries carries a GrayKey extra-field block (tag `GK`, or the older tag `0x0004`) | `graykey`, user prefix `private/var` |
| 2 | Some entry starts with `Dump/` **and** one of the first 20 entries has a UT (Unix timestamp) extra block | `zip_extras`, user prefix `Dump` |
| 3 | Anything else | `cellebrite` (see below) |

For `cellebrite`, the user and system partitions are found by name:
- **User partition:** the first `filesystemN/` (N = 1–9) containing `mobile/`
  or `wireless/` directly (new layout). Failing that, the first containing
  `private/var/mobile/` or `private/var/wireless/` (old layout,
  `old_layout = True`). Failing both, `filesystem2`, new layout.
  - The new layout is checked across all N first, because the iOS system
    partition can hold a few stub entries under `private/var/mobile/` that
    would otherwise be mistaken for an old-layout user partition.
- **System partition:** the first `filesystemN/` containing `System/Library`
  or `System/`. Failing that, `filesystem1`.

**A-3. Detection heuristics.** *Code only, except where noted.*
- **GrayKey is decided from the first 20 entries.** A GrayKey zip whose first
  20 entries carry no `GK` block would be misread as Cellebrite. *Unverified.*
- **Only GrayKey block version 1 is understood.** Any other version raises
  `ValueError` while parsing metadata, which stops the whole metadata build
  rather than skipping one entry.
- **Cellebrite is the default.** An archive from any other tool (or a plain
  zip) is treated as Cellebrite and falls back to `filesystem2`. Nothing tells
  the examiner the layout wasn't actually recognised.
- **D2's user partition is unconfirmed.** An Android `filesystemN/` archive
  has no `mobile/` or `wireless/` folder, so the user-partition search
  presumably falls through to the `filesystem2` default. This is
  *unverified*: see open question Q2.

## 3. Paths: what is stored, what is shown, how they map

Three kinds of path appear in the code:
- **physical**: the zip entry name;
- **ui_path**: the path the code works with and the tree is built from;
- **display**: what the examiner sees, e.g. with a GUID replaced by an app
  name.

Never mix physical and ui_path. Convert with `FfsAdapter.resolve()` (ui_path →
physical) and `strip_display_prefix()` (physical → display-style).

| | Physical entry (example) | ui_path | `resolve(ui_path)` |
|---|---|---|---|
| A Cellebrite iOS, new layout | `filesystem2/mobile/Library/SMS/sms.db` | `mobile/Library/SMS/sms.db` | `filesystem2/` + ui_path |
| A Cellebrite iOS, old layout | `filesystem2/private/var/mobile/Library/SMS/sms.db` | `mobile/Library/SMS/sms.db` | `filesystem2/private/var/` + ui_path |
| B GrayKey iOS | `/private/var/mobile/Library/SMS/sms.db` | `private/var/mobile/Library/SMS/sms.db` | `/` + ui_path |
| C GrayKey Android | `/data/data/com.whatsapp/…` | `data/data/com.whatsapp/…` | `/` + ui_path |
| D1 Cellebrite Android (`Dump/`) | `Dump/data/data/com.whatsapp/…` | `data/data/com.whatsapp/…` | `Dump/` + ui_path |
| D2 Cellebrite Android (`filesystemN/`) | `filesystemN/…` | everything after `filesystemN/` | as A (unverified, Q2) |

**A-4. Cellebrite iOS ui_paths start at the user partition root;
GrayKey iOS ui_paths keep `private/var/`.** *Code only.*
- The same file is `mobile/…` on Cellebrite but `private/var/mobile/…` on
  GrayKey.
- Any fixed iOS path written in code or config is stored in Cellebrite form
  and adapted per case by `prefix_shortcut()`. Examples are scan rules,
  forensic shortcuts and parser `app_path`s; `prefix_shortcut()` adds
  `private/var/` for GrayKey.
- Container parents differ the same way: `mobile/Containers/…` versus
  `private/var/mobile/Containers/…`.
- `strip_display_prefix()` removes the GrayKey `private/var/` again for
  display.
- **Discrepancy to fix in `CLAUDE.md`:** its section 1 says GrayKey "always
  needs a `private/var/` prefix prepended to a ui_path before it resolves".
  In the code, the ui_path *already contains* `private/var/`; what gets
  prefixed is the Cellebrite-form shortcut path.

**A-5. GrayKey zip entry names start with `/`.** *Code only.*
`resolve()` prepends `/`, and GrayKey metadata keys are stored with the
leading `/` stripped.

**A-6. Old-layout Cellebrite is folded into new-layout paths.** *Code only.*
Old-layout archives keep `private/var/` in both the zip paths and the
`metadata.msgpack` keys. Both are stripped, so the ui_paths look identical to
a new-layout archive. As a result, `private/var/mobile` and `private/var/root`
appear as just `mobile` and `root`. This matters when importing paths from
other tools such as Griffeye; see `ios_path_candidates` in
`app/spreadsheet_import.py`.

**A-7. Cellebrite shortens some path segments in the zip itself
("truncation").** *Code only; the cause is not recorded anywhere (Q1).*
- In Cellebrite iOS archives, some path segments look different in
  `metadata.msgpack` (the manifest) and in the zip.
  - Manifest: `<readable part>-<32 or more hex characters>`.
  - Zip: only the trailing hex characters.
- `resolve()` reproduces this. Any ui_path segment containing `-` whose part
  after the last `-` is at least 32 hex characters is cut down to that hex
  part.
- Consequences:
  - The tree and the file browser are built from the **zip's** names (see
    A-9), so the examiner sees the shortened hex form, not the readable name
    in the manifest.
  - To attach manifest metadata to these entries, `build_ui_metadata()` builds
    a second index keyed by `resolve(manifest key)` (`guid_meta`).
  - A path that already holds only the hex form passes through `resolve()`
    unchanged.
  - A normal iOS container GUID (`8-4-4-4-12` hex) is never shortened: its
    last group is only 12 characters.
- Risk: any genuine filename on the device matching the pattern
  `…-<32+ hex>` would also be shortened by `resolve()`, and then not found in
  the zip. *Unverified whether such names occur.*
- Applies only to `cellebrite`. GrayKey and `zip_extras` paths are used as-is.

**A-8. iOS app containers are GUID-named; Android ones are package-named.**
*Confirmed (real).*
- On iOS (A, B), `Containers/Data/Application/<GUID>` etc. are mapped to
  bundle ids. The main source is the LaunchServices csstore, topped up from
  each container's
  `.com.apple.mobile_container_manager.metadata.plist`.
  - The csstore was confirmed sometimes needed because the per-container
    plists are missing on GrayKey extractions.
  - A GUID is specific to one installation. Anything stored across cases must
    use the bundle id: `FfsAdapter.canonical_app_path()`.
- Android (C, D1, D2) uses `data/data/<package>` directly, with no mapping.

## 4. What each layout provides

| | A Cellebrite iOS | B GrayKey iOS | C GrayKey Android | D1 Cellebrite Android (`Dump/`) | D2 Cellebrite Android (`filesystemN/`) |
|---|---|---|---|---|---|
| File metadata source | `metadata2/` or `metadata1/metadata.msgpack` | zip extra fields (GK/UT/UX/inode) | zip extra fields (GK/UT/UX/inode) | UT extra field only | presumably msgpack, as A (Q2) |
| Timestamps | as recorded in the msgpack (stored as ns) | mtime/atime/ctime/btime; whole seconds (32-bit UT values) | as B | **mtime only**; atime/ctime/btime are always 0 | as A (Q2) |
| uid/gid, inode, xattrs | from msgpack | from UX/inode/GK blocks | as B | none (0) | as A (Q2) |
| What the tree contains | **user partition only** | every entry in the zip | every entry in the zip | everything under `Dump/` | the detected `filesystemN/` only (Q2) |
| System partition files | not in the tree; read only for device info via `system_candidates()` | in the tree (no separate partition) | in the tree | under `Dump/` | (Q2) |
| GUID → bundle-id map | yes | yes | no | no | no |
| Capture record (integrity check) | `<zip name>.ufd` | `<zip>.sha256`, else a GrayKey PDF report named after the device UDID | as B | `.ufd` | `.ufd` |
| Acquisition-machine timezone guess | from the `.ufd` UTC offset | none | none | from the `.ufd` | from the `.ufd` |
| Handset timezone | from `private/var/db/timezone/localtime` | same | none found on Android | none | none |
| Excluded from keyword search | `metadata1/`, `metadata2/` | — | — | — | `metadata1/`, `metadata2/` |

**A-9. In Cellebrite iOS, the zip decides what exists.** *Code only.*
The tree is built from the zip's own entries. The msgpack only adds metadata
to entries the zip already has: anything listed in the msgpack but not in the
zip never appears. In GrayKey and `zip_extras`, the metadata is read from the
zip entries themselves, so the two can't disagree.

**A-10. A missing UT block in GrayKey falls back to the zip's DOS time,
treated as UTC.** *Code only.*
Applies to B and C. The zip's DOS timestamp has no timezone and only 2-second
precision, so if a tool wrote local time there, those mtimes would be shifted
by the acquisition machine's offset. A UT block that is present with mtime 0
is kept as 0 (shown as `---`), not replaced.

**A-11. 32-bit UT timestamps.** *Code only.*
GrayKey and `zip_extras` times are unsigned 32-bit seconds: no sub-second
precision, and nothing representable before 1970 or after 2106.

## 5. The main FFS zip itself

These are what allow the app's own direct-seek zip reader (`ZipEntry`,
`ZipReader`, `zip_cd_cache.compute_data_offsets`) to skip the checks Python's
`zipfile` makes. That is where the speed comes from. See the review dated
2026-10-20.

**A-12. Entries are never compressed.** *Examiner; Confirmed (real) on one
case: 0 of 124,949 entries compressed (Android 14 JoshHickman).*
- Relied on by: every direct-offset reader, i.e. the header scan, EXIF/video
  metadata scan, keyword search, hex view and export.
- If wrong: those readers would return compressed bytes as if they were the
  file. Keyword search filters out non-stored entries, so such files would be
  silently not searched, which the Search Coverage dialog does not mention.
  `ZipEntry` alone would still work, but very slowly.
- No runtime check exists.

**A-13. The zip never changes after acquisition.** *Examiner.*
- Relied on by: the `.zcd` central-directory sidecar, which is built once
  and never re-checked against the zip on normal opens.
  `_verify_archive_matches_case` runs only when an archive is relocated.
- If wrong: every read would land at the wrong position.

**A-14. Entries are not encrypted.** *Examiner.*
If wrong, encrypted bytes would be returned as the file. No check exists.

**A-15. Files are stored back to back in the zip.** *Confirmed on synthetic
test zips only; not yet confirmed on a real FFS archive.*
- `compute_data_offsets` works out each entry's data position as the next
  entry's header offset minus its own size, with no reads from the archive.
- Guards: once per session, a sample is checked against the real local
  headers. Anything that can't be derived is read from its own header
  instead: the last entry, entries with a data descriptor, and any entry
  implying a header shape the sample never saw (e.g. a gap).
- If the sample check fails, every entry is read from its own header: slower
  but never wrong.

**A-16. ZIP64 appears in every large FFS zip.** *Reasoned from the zip
format; reproduced on a synthetic archive.*
Archives over 4 GiB must store the real header position of every entry past
the 4 GiB mark in a ZIP64 block in the central directory, which the local
header doesn't have. This is what made the old single-delta offset
calculation 12 bytes wrong for one group of entries. Fixed in
`zip_cd_cache.py` (KlasScripts/FFS-Explorer#2 and the follow-up commit).

**A-17. CRC is not verified on reads.** *Decision, not a fact.*
Deliberate, for speed. It is acceptable only because acquisition integrity is
established separately: the integrity check compares the whole zip's SHA256
with the capture record (A-4 table). If that check isn't run, nothing detects
a corrupted entry.

**A-18. The main zip is never read with raw `zipfile`.** *Decision.*
- Reads go through the sidecar plus `ZipEntry`, because a `zipfile` open
  re-parses the whole central directory, which takes seconds on a large or
  network-hosted archive.
- Allowed exceptions: `ZipEntry`'s own compressed-entry fallback, and
  already-extracted local files (nested archives).

## 6. Nested archives (embedded zip/gzip files)

**A-19. Repacked nested archives are uncompressed and written once.** *Code
only.*
- `nested_archive.extract_one` rewrites an embedded zip entry by entry as a
  new `ZIP_STORED` zip in `case_dir/nested_archives/<12-char hash>_<name>`,
  skipping directory entries.
- An embedded gzip is stored as its raw decompressed bytes, which is not a
  zip.
- Nothing modifies these files afterwards, except a re-extraction.
- Relied on by: `read_nested_entry`'s cached per-archive index, read by
  direct seek; the index is rebuilt if the file's mtime or size changes.
- If wrong: a stale index would read the wrong bytes. Mitigated by the
  mtime/size check.

**A-20. Embedded archives are never extracted automatically.** *Decision.*
Extraction is an examiner action (Process Case, or right-click), or is
declared by a parser through `requires_nested_extraction`. Content inside an
unextracted embedded archive is invisible to search and parsers; `list_apps`
reports it as `embedded_archives` so the gap is visible.

## 7. Other data facts the design relies on

**A-21. Evidence timestamps are UTC.** *Decision, plus Code only for each
source.*
- All evidence times are shown as labelled UTC by default; other zones are
  opt-in per case.
- Each source is assumed to be UTC as stored: msgpack times, UT fields, the
  DOS fallback (A-10), and app database epochs.
- Parsers store raw values and declare the unit (`timestamp_fields`).

**A-22. The handset timezone is only a snapshot.** *Confirmed (real).*
The iOS `localtime` file gives the zone at acquisition. Ground-truth images
show devices changing zone over time, so it doesn't apply to older evidence
automatically.

**A-23. The acquisition machine's timezone is never recorded.** *Confirmed
(real) on two cases.*
Only a UTC offset exists, in the `.ufd`, so the zone is guessed from that
offset and must be confirmed by the examiner. GrayKey provides no offset.

**A-24. Header-scan scope comes from one shared ruleset.** *Decision.*
`config/scan_rules_ios.json` and `scan_rules_android.json` (via
`scan_rules.py`) decide which folders the header scan, nested-archive
discovery and the embedded-media sweep cover. For GrayKey, both platforms'
iOS and Android rules are applied, because the format can't say which
platform the archive is (A-2).

---

## 8. Corrections needed elsewhere (found while writing this)

Recorded here only; no other file was changed.

1. **`FFS_processing.md`** says Cellebrite Android is handled as
   `FORMAT_ZIP_EXTRAS`. That is only layout D1; layout D2 detects as
   `cellebrite` (A-2).
2. **`FFS_processing.md`** says Cellebrite physical entries "contain
   32-character hex suffixes". The reverse is true: the msgpack key carries
   `<name>-<hex>`, while the zip stores only `<hex>` (A-7).
3. **`FFS_processing.md`** refers to a `StreamingZipIndex`, which no longer
   exists in the code.
4. **`FFS_processing.md`** says GrayKey Android gets a GUID mapping "if app
   containers exist". In fact `build_ui_metadata` builds the map only when
   `/private/var/mobile/Containers/` entries exist, i.e. iOS.
5. **`CLAUDE.md`** section 1 wording on GrayKey and `private/var/` (A-4).

## 9. Open questions for the examiner

- **Q1.** Why does Cellebrite shorten `<name>-<32+ hex>` segments to the hex
  part in the zip (A-7)? Which real folders does it apply to, and is the
  readable part useful to show the examiner?
- **Q2.** For a Cellebrite Android archive in the `filesystemN/` layout (D2):
  - Which `filesystemN` holds the user data?
  - Does it have a `metadata.msgpack`?
  - Does the tree show all of it?
- **Q3.** Are there GrayKey zips whose first 20 entries lack a `GK` block, or
  that use a GrayKey block version other than 1 (A-3)?
- **Q4.** Should an unrecognised layout produce a warning, rather than
  silently being treated as Cellebrite with a `filesystem2` default (A-3)?
- **Q5.** Does any tool write local time (not UTC) into the zip's DOS
  timestamp (A-10)?
- **Q6.** Should A-12 and A-14 (no compressed or encrypted entries) be checked
  automatically when a case loads, so a violation is reported rather than
  silently assumed? This is cheap: the sidecar's entry list is already in
  memory.
