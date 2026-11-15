# Files by Google – Full-screen Views: read this first

This report lists **only Glide-proven full-screen viewer thumbnails**: cache entries whose name matches Files by Google's FitCenter viewer recipe for a known size, modified time, and source URI/path.

It does **not** list images just because they are large. Tile thumbs stay in *Thumbnail Cache*. Open times stay in *Opened Files*.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Not an open time** | The cache has no clock. Use *Opened Files* for when the viewer opened. | [How a row gets here](#how) |
| **No size guessing** | A wide leftover image with no Glide match is omitted, even if it “looks” full screen. | [How a row gets here](#how) |
| **Safe folder vs opens** | Moving a file to the Safe folder deletes open records; a viewer thumb made earlier can still appear here. | [How a row gets here](#how) |
| **Carved link needs residue** | If the live media-store row is gone, size/mtime may still be in `external.db` WAL or free pages. Many older deletes leave nothing — then there is no Glide viewer row from carve. | [Carving](#carving) |
| **Videos** | Lab: playing a video did not add a new full-screen still the way opening a picture did. | [How a row gets here](#how) |
| **Older Files builds** | Viewer recipe confirmed on lab `1.13157`. On older phones the same FitCenter + size/mtime pattern is used when it matches; treat screen mapping as supported by hash, not a second lab UI study. | [Validation](#validation) |

---

<a id="how"></a>
## How a row gets here

| | |
|---|---|
| Recipe | Signature `size/mtime` + source key + FitCenter + requested screen box + resource class → SHA-256 cache name |
| Supports | That picture was shown full screen in Files' viewer at least once |
| Does not show | When, for how long, or that a video played |

**Link Source** may be:

| Link Source | Meaning |
|---|---|
| Index | Size/mtime/URI from Files' index (file still indexed) |
| Media store | From the live `files` table |
| Safe folder | Size/mtime from the Safe copy; id found by search |
| Media store (carved) | Size/mtime from a deleted `files` row still in WAL/free pages |

<a id="carving"></a>
## Carving (when the live row is gone)

1. Collect media-store ids that are absent from the live `files` table (including ids already tied to a scan thumb as *Id not in the current media store*).  
2. Run FFS Explorer's SQLite recovery on `external.db` (+ WAL).  
3. If a recovered row has `_size` and `date_modified`, rebuild the viewer Glide key (and scan keys).  
4. A hash hit is listed here like any other Glide-proven viewer row.

**Limits:** Checkpointed/overwritten WAL → no carve. Lab Safe-delete id 130 still carved and matched; Josh ids with only `deleted_media` and no WAL residue did not.

<a id="validation"></a>
#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Viewer recipe (size/mtime + FitCenter) | **Observed. Controlled.** | Lab Files `1.13157`; Frida + decompile |
| Lab viewer thumbs | **Observed.** | 3 entries in `files-app-usage-001` |
| Carve → viewer for id still in WAL | **Observed.** | Lab id 130 → same cache key as Safe-folder link |
| Carve when no residue | **Observed (negative).** | Josh `1000000059` / `68` / `69`: no carved viewer |
| Size-only rows excluded | **By design.** | Unlinked large images not listed |

## Not in this report

- Timed opens: *Opened Files*.  
- Scan / Recents / grid / list / Unknown: *Thumbnail Cache*.  
- Safe folder inventory: *Safe Folder*.  
- Picture-match without a Glide key: *Likely Full-screen Views* (inferred only).

## Sources

- Lab `files-app-usage-001`, Files `1.13157`, 2026-11-13 / Glide + carve spike 2026-11-14–15.  
- Pixel 7a / Poco X7 Pro for link rates and negative carve cases.
