# Files by Google – Likely Full-screen Views: read this first

This report lists **inferred** leftovers only. Each row is a Glide cache image that Glide could not name, but that:

- fills the full-screen box of a **Glide-proven** viewer thumbnail in **this** case, and
- looks like the same picture as the **Glide-proven** background-scan thumbnail of a file whose media-store id is **not in the current media store**, and
- looks like no other file's scan thumbnail.

It is **weaker** than *Full-screen Views*. Prefer that report whenever a Glide viewer key matches (including after carving size/mtime from `external.db`).

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Not Glide-proven** | No size/mtime/FitCenter hash. Confidence is always Inferred. | [How a row gets here](#how) |
| **Not an open time** | No clock. Use *Opened Files* when that record still exists. | [How a row gets here](#how) |
| **The id can be another copy's** | If the same picture existed as two files, both are gone from the media store, and only one file's scan thumbnail is left, the row names that file's id. The cache cannot tell identical copies apart. | [Limits](#limits) |
| **Not “deleted”** | *Id not in the current media store* is not by itself proof of deletion (a media-store rebuild or another volume can look the same). | [Gates](#gates) |
| **An empty report proves nothing** | Many real viewer leftovers are dropped by the gates on purpose (see the blind test below). No row is not evidence that a picture was not viewed. | [Validation](#validation) |
| **Needs a proven viewer in the case** | The screen box comes only from Glide-proven full-screen keys in this extraction. If there are none, this report is empty. | [Gates](#gates) |

---

<a id="how"></a>
## How a row gets here

1. Run the normal Glide link (index → media store → Safe → absent ids → carve). Proven viewer rows go to *Full-screen Views* only.
2. Take the requested screen box (width × height) from every Glide-proven viewer key in this case.
3. Group every Glide-proven **background-scan** thumbnail by file (a file can have a 640 and a 256 scan).
4. Consider **unlinked** cache files only.
5. Keep a candidate only if it passes every [gate](#gates).

**Check every row by eye.** Both pictures are in the table. Double-click either one to open them **side by side** at full size, each captioned with its column, file name and pixel size. The buttons above switch to one picture at a time, and the choice is kept as you move between rows. The scan thumbnail is small (often 256 px on its long side), so it looks soft when enlarged; that is expected.

| Column | Meaning |
|---|---|
| Confidence | Always `Inferred` |
| Link Method | `Content match to scan thumb` |
| Match Distance | Largest difference between the two pictures over a 16 × 16 grid of grey averages, 0–1. 0 = identical at that scale. Listed only at 0.05 or below |
| Runner Up Distance | The same distance to the closest **other** file's scan. Always above 0.10 here; blank if no other scan has the same shape |
| Same File Rows | How many rows in this report point to the same file (Glide can keep more than one full-screen size of a picture) |
| Paired Scan Thumb | The Glide-proven scan image used as the anchor |
| Scan Link Source | Always *Id not in the current media store* |

<a id="gates"></a>
## Gates (all required)

| Gate | Why |
|---|---|
| Fills the case's viewer box | Width equals the requested box width, or height equals the requested box height, and neither is larger. FitCenter fills the box edge to edge one way. Sides of other phones and output sizes of other pictures are not used |
| Not already Glide-linked | Proven viewer/tile rows are never re-listed here |
| Enough detail | Flat or nearly flat pictures (grey-level spread below 10) are not matched: different flat pictures look alike |
| Same shape as the scan | Within 8 %. FitCenter keeps the picture's shape |
| Close match | Distance 0.05 or below to the best file's scan |
| No close rival | No other file's scan (from any source, including files still on the phone) within 0.10 |
| File's id not in the current media store | A file still in the index or media store has a known size and modified time. If it had been shown full screen, its viewer key would normally have been proven. A picture match to such a file points to another copy of the picture, so it is not listed |

<a id="validation"></a>
## Validation (2026-11-15)

Ground truth: the Glide-proven viewer thumbnails on three images, each hidden from the matcher in turn.

| Test | Lab | Pixel 7a | Poco X7 Pro |
|---|---|---|---|
| Proven viewer thumbnails | 3 | 4 | 15 |
| Blind: true file made to look deleted; correct file named | 0 | 3 | 9 |
| Blind: wrong file named | 0 | 0 | 0 |
| Blind: dropped (no row) | 3 (lab colour samples: flat, or too close to each other) | 1 | 6 |
| Negative: true file's scan hidden, other files keep their real source; any row | 0 | 0 | 0 |
| Worst case: true file's scan hidden, every other file made to look deleted; wrong file named | 0 | 0 | **5** |

The 5 worst-case rows are copies of the same picture saved as different files (3 with pixel-identical scans). That is the limit in the table above: it needs two copies both gone from the media store with only the wrong copy's scan left.

Distances, on the same three images (the matcher's own measure): own-file pairs 0.0–0.024. Pictures saved as separate identical copies: the same as own pairs (that is why the rival gate exists). Closest genuinely different picture: 0.082, a nearly flat picture that the detail gate drops; among pictures with normal detail, 0.318 or more. Lab colour samples: 0.035–0.051 apart, so the rival gate drops them.

**Not done:** the controlled lab run (picture viewed then deleted / deleted without viewing / near-duplicates, only one viewed). Until it is, the thresholds are tuned on these three images only, and nothing here shows how deletion itself affects the cache.

<a id="limits"></a>
## Limits

- Identical copies: see above. The picture is right; the id can be the other copy's.
- A viewer thumbnail can be evicted while the scan remains: then there is nothing to list.
- Whether deleting a file writes, keeps or removes its viewer thumbnail has **not been tested**.
- Videos: lab play did not leave a full-screen still the way opening a picture did.
- Pixel 7a live result: one row (media-store id 1000000059, distance 0.0118, runner-up 0.494). Not checked against the image's documented ground truth.

## Not in this report

- Glide-proven full-screen: *Full-screen Views*.
- Tiles / Unknown: *Thumbnail Cache* (images listed here are omitted from Unknown).
- Timed opens: *Opened Files*.

## Sources

- Design: carve first, then a separate inferred report (review 2026-11-15).
- Lab `files-app-usage-001`, Pixel 7a and Poco X7 Pro copies for the validation above.
