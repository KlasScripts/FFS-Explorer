# Files by Google – Thumbnail Cache: read this first

This report lists **tile thumbnails** in Files by Google's Glide disk cache (indexing scan, Recents, grid, list), and **unlinked** leftovers labelled **Unknown (W×H)**.

**Kind** is trusted only when **Kind Source** is `Glide key`. Unlinked rows are never given a screen kind from pixel size (a 640×640 scan must not be called a grid tile). **User Looked** is always `No — not proof the user looked at it`. Full-screen viewer thumbs are in *Full-screen Views*.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **A scan thumbnail is not viewing** | Background indexing writes a thumbnail soon after a file appears, with the app never opened. | [Kinds](#kinds) |
| **A grid or Recents tile is not “looked at”** | It means the tile was drawn on screen (including a partly visible row). It does not show attention or duration. | [Kinds](#kinds) |
| **Unknown is not a screen** | The Glide key did not reverse. You still have the image; do not treat size as proof of which screen made it. | [Linking](#linking) |
| **“Id not in the current media store”** | The hash matches `content://media/external/file/N` for an id that is not in the live `files` table. That is not by itself proof the file was deleted (rebuild / other volume). No filename. | [Linking](#linking) |
| **Which screen on older Files builds** | A hash match proves file + recipe (size request and crop). Mapping that recipe to a screen name was controlled on lab Files `1.13157`; on older Pixel/Poco builds it is **inferred**. | [Kinds](#kinds) |

---

## Where the data is

`/data/data/com.google.android.apps.nbu.files/cache/image_manager_disk_cache/<64 hex>.0`, plus `journal`.

<a id="kinds"></a>
## Kinds (Glide-linked only)

| Kind | Why it exists | What it supports | What it does not show |
|---|---|---|---|
| Background scan (indexing) | Files indexes new media in the background | The file was processed | That anyone opened the app or saw the file |
| Recents (home screen) | Home-screen Recents strip | The tile was drawn in Recents | That the user looked at it |
| Grid tile (category view) | Images / Videos (etc.) grid | The tile was rendered, possibly only partly visible | Attention or how long |
| List row (folder or category list) | Downloads or folder list | The row was drawn in a list | Attention or how long |
| Unknown (W×H) | No Glide-key match | The cache held this image at that pixel size | Which screen or which file |

Lab sizes on a 1080 × 2424 px, 420 dpi screen (other phones differ; do not use these numbers as proof):

| Kind | Typical size in the lab |
|---|---|
| Background scan | Up to 640 px on the long side |
| Recents | 510 × 599 |
| Grid | 345 × 345 |
| List | 168 × 168 |

<a id="linking"></a>
## Linking

The cache name is rebuilt from URI or path, size, modified time, requested WxH and transform (`app/files_by_google_glide.py`). Sources, in order:

1. Files by Google index  
2. Live media store  
3. Safe folder copy (size/mtime from the copy + id search)  
4. Scan URI for an id **not in the current media store** (no path)  
5. *(Full-screen Views only)* carved media-store size/mtime for viewer keys  

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Kind from Glide key on lab Files `1.13157` | **Observed. Controlled.** | Frida + decompile; lab 163/163 linked |
| Same recipes on older Pixel / Poco | **Inferred.** | Hash matches files; screen-name mapping not re-proven per UI |
| Lab cache fully linked | **Observed.** | `files-app-usage-001` |
| Older phones mostly linked | **Observed.** | Pixel ~330/345; Poco ~1179/1187 |
| Unlinked rows not size-labelled as grid/viewer | **By design.** | Avoids scan/viewer confusion |

## Not in this report

- Full-screen viewer thumbs: *Full-screen Views*.  
- Inferred screen-sized leftovers matched to a scan: *Likely Full-screen Views* (those images are not listed here as Unknown).  
- Open times: *Opened Files*.  
- Safe folder copies: *Safe Folder*.  
- App Sessions: screen time without file names.

## Sources

- Lab `files-app-usage-001`, Files `1.13157`, 2026-11-13 / Glide 2026-11-14.  
- Pixel 7a and Poco X7 Pro.  