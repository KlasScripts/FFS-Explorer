# FFS Processing Overview

This document describes how `ffs-explorer.py` processes an FFS extraction and how that processing differs between:

- GrayKey iOS
- GrayKey Android
- Cellebrite Android
- Cellebrite iOS

The core of the processing is in `ffs-explorer.py` and `app/adapters/ffs.py`, with GrayKey/UT metadata parsing in `app/adapters/graykey.py`.

## 1. High-level processing pipeline

When an archive is opened, the app performs these major steps:

1. Open the ZIP archive.
2. Detect the archive format using `FfsAdapter.detect()`.
3. Load or build a bundle-ID map for iOS app container GUIDs.
4. Build `ui_metadata` from the archive metadata source.
5. Build the folder tree from `ui_metadata`.
6. Compute folder sizes and identify missing container metadata.
7. Emit data to the UI so the tree and file browser can render.

This pipeline is implemented in `ZipMetadataWorker.run()` in `ffs-explorer.py`.

## 2. Format detection

Detection is centralized in `app/adapters/ffs.py`.

### GrayKey detection

- `FfsAdapter.detect()` opens the ZIP and inspects the first entries.
- It looks for a GrayKey extra-field block in the ZIP entry headers.
- If found, the adapter becomes `FORMAT_GRAYKEY`.
- `user_prefix` is set to `private/var`, and `sys_prefix` is empty.

### Zip-extras detection (Android-style UT/UX metadata)

- If the ZIP contains a top-level `Dump/` prefix and the entries carry UT extra fields,
  the adapter becomes `FORMAT_ZIP_EXTRAS`.
- This path is used for Android archives whose metadata comes from UT/UX ZIP extra fields,
  not from GrayKey msgpack metadata.
- `user_prefix` is set to `Dump`.

### Cellebrite detection

- For all other archives, `FfsAdapter.detect_from_names()` assumes a Cellebrite layout.
- It examines entry names to find:
  - the user partition prefix such as `filesystem2/` or `filesystemN/`
  - whether the archive uses the old Cellebrite layout with `private/var/`
  - the system partition prefix containing `System/Library`
- The adapter becomes `FORMAT_CELLEBRITE`.

## 3. Metadata sources by format

### GrayKey iOS

- Metadata is extracted from ZIP entry extra fields via `graykey.extract_metadata()`.
- The returned metadata keys are full paths with the leading `/` stripped.
- `FfsAdapter.load_metadata()` returns this raw metadata directly.
- Bundle-ID mapping is built from `.com.apple.mobile_container_manager.metadata.plist`
  files located under `private/var/mobile/Containers/`.

### GrayKey Android

- If GrayKey extra-field blocks are present in an Android-style archive, it is still handled
  as `FORMAT_GRAYKEY`.
- The same `graykey.extract_metadata()` path is used.
- The physical path resolution uses `"/" + ui_path`.
- This keeps the same GrayKey-style behavior, but the archive contents are Android paths.

### Cellebrite Android

- These archives are handled as `FORMAT_ZIP_EXTRAS`.
- Metadata is not stored in `metadata.msgpack`; it is reconstructed from ZIP extra fields.
- `FfsAdapter.load_metadata()` scans all entries under `Dump/`.
- It reads the UT timestamp block from each ZIP entry and uses `ZipInfo.file_size`.
- It sets `atime`, `btime`, `ctime` to zero and only populates `mtime` from UT metadata.
- `guid_to_bundle` is empty for this format.

### Cellebrite iOS

- Metadata is loaded from `metadata2/metadata.msgpack` or `metadata1/metadata.msgpack`.
- If the archive uses the old Cellebrite layout, returned msgpack keys with `private/var/`
  are normalized to bare user paths.
- This format also supports GUID-style application container paths,
  where physical entries contain 32-character hex suffixes.

## 4. Path resolution differences

The adapter implements `resolve(ui_path)` differently depending on format.

### GrayKey (`FORMAT_GRAYKEY`)

- The path is resolved by prefixing with `/`.
- Example: `mobile/Library/SMS/sms.db` → `/mobile/Library/SMS/sms.db`

### Zip-extras / Cellebrite Android (`FORMAT_ZIP_EXTRAS`)

- The path is resolved as `Dump/<ui_path>`.
- Example: `data/data/com.example/files` → `Dump/data/data/com.example/files`

### Cellebrite iOS (`FORMAT_CELLEBRITE`)

- If the file path contains GUID-style segments (like `some-name-<32hex>`),
  the adapter strips each GUID-style suffix down to the 32-char hex portion.
- If the archive is an old layout, paths gain `private/var/` after the partition prefix.
- Example:
  - new layout: `filesystem2/mobile/...`
  - old layout: `filesystem2/private/var/mobile/...`

## 5. Metadata and UI tree assembly

`FfsAdapter.build_ui_metadata()` builds the data the browser uses to show the folder tree.

### For GrayKey and Zip-extras

- The metadata dictionary itself is the source of truth.
- The method returns `ui_metadata` plus `zip_ui_paths = frozenset(ui_metadata.keys())`.
- There is no separate zip-entry scan step.

### For Cellebrite iOS

- The method may first build a `guid_to_bundle` map from iOS container metadata.
- It loads raw msgpack metadata and then resolves physical paths only for user-partition entries.
- It scans the ZIP directory for user-partition entries and builds `zip_ui_paths`.
- The final `ui_metadata` contains only entries that actually exist in the ZIP.
- Metadata from msgpack is used as an enrichment layer, not as a source of new entries.

## 6. Bundle-ID mapping for iOS app containers

- For GrayKey iOS and Cellebrite iOS, the application container GUIDs are mapped to bundle IDs.
- This is done by scanning `.com.apple.mobile_container_manager.metadata.plist` files.
- For filesystem paths that include GUID segments, the browser can display the corresponding bundle ID.
- For Android zip-extras, no bundle-ID mapping is built.

## 7. Special cases and differences summary

| Case | Detection | Metadata source | Path prefix | GUID container mapping |
|------|-----------|-----------------|-------------|------------------------|
| GrayKey iOS | GrayKey extra block | ZIP extra fields | `/private/var/...` | yes |
| GrayKey Android | GrayKey extra block | ZIP extra fields | `/...` | yes (if app containers exist) |
| Cellebrite Android | `Dump/` + UT extras | ZIP extra fields | `Dump/...` | no |
| Cellebrite iOS | metadata.msgpack | `metadata2/metadata.msgpack` or `metadata1/metadata.msgpack` | `filesystemN/...` | yes |

## 8. Why the format matters

Because each extraction format stores metadata differently, the browser must:

- choose the right metadata loader (`graykey` vs msgpack vs UT extra fields)
- normalize `ui_path` keys so the UI tree is consistent
- resolve logical paths to physical ZIP entries correctly
- preserve iOS container bundle labels when GUID-based paths are present

That is why `FfsAdapter` exists: it hides the format-specific differences behind a common interface.

## 9. Relevant files

- `ffs-explorer.py` — main UI and archive-loading workflow
- `app/adapters/ffs.py` — format detection, path resolution, metadata assembly
- `app/adapters/graykey.py` — GrayKey / UT extra-field metadata parsing

## 10. Notes

- `ffs-explorer.py` also supports streaming ZIP archives via `StreamingZipIndex`.
- `Cellebrite iOS` is the only format that performs a two-pass build of ZIP entries plus msgpack enrichment.
- `GrayKey` and `zip-extras` archives are handled in a simpler, one-pass manner.
