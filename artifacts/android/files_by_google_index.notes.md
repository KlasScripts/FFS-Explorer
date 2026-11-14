# Files by Google – Indexed Files: read this first

This report lists **Files by Google's own index** of the files in shared storage (Download, DCIM, Pictures and so on). Files by Google builds the index in the background. **A row shows the file was on the phone and was scanned; it does not show the app was opened or the file was seen.**

The interpreted columns are **Media Type** (the media store constant), **Times Opened** and **Last Opened** (counted from Files by Google's own open records for the file). Everything else is copied from the database.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Indexed without the app being opened** | In the lab, every new download got a row within seconds while Files by Google was never opened. | [How rows get here](#how-rows-get-here) |
| **Files moved to the Safe folder drop out** | Moving a file to the Safe folder deleted its row and its open records. | [How rows get here](#how-rows-get-here) |
| **Times Opened counts only Files by Google's viewer** | Opening the file in another app is not counted. | [Opened Files](#columns) |

---

## Where the data is

`/data/data/com.google.android.apps.nbu.files/databases/files_master_database`, table `files_master_table`.

<a id="columns"></a>
| Column | Source |
|---|---|
| Path | `root_path` + `root_relative_file_path` |
| Size, File Modified, MIME Type | `size`, `file_date_modified_ms`, `mime_type` |
| Media Type | `media_type`: 0 None, 1 Image, 2 Audio, 3 Video, 4 Playlist, 5 Subtitle, 6 Document (media store constants) |
| Times Opened, Last Opened | count and newest `file_open_time_stamp_ms` in `file_open_info_table` for this `id` |
| Hidden, Folder, Media Store Id, URI | `is_hidden`, `parent_folder_name`, `media_store_id`, `uri` |

## How rows get here

| Event in the lab (2026-11-13) | Effect on the index |
|---|---|
| 12 JPEGs and 2 MP4s downloaded in Chrome, Files by Google never opened | A row for each file. Files by Google started itself in the background at 22:33:33 and scanned 6 to 14 s after each download. |
| Restart | No change |
| Three files moved to the Safe folder | Their rows were deleted, and with them their open records (`ON DELETE CASCADE`). No trace of the names was left in the database file. |

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Rows without the app opened | **Observed. Controlled.** | Lab run `files-app-usage-001`, three download batches (52 pictures, 2 videos) |
| Safe folder move deletes the row | **Observed. Controlled.** | Three files |
| Report output | **Observed.** | Lab 55 rows (55 linked to the file), Pixel 7a 228 (226 linked), Poco X7 Pro 434 (433 linked) |

---

## Not in this report

- Open times one by one: *Files by Google – Opened Files*.
- Scans: *Files by Google – Background Scans*.
- Thumbnails in `cache/image_manager_disk_cache/`. They cannot be tied to a file by name; see the knowledge base entry *Files by Google: what an extraction shows about its use*.

## Sources

- Lab emulator `gtlab-alice`, Android 16 (`BE2A.250530.026.F3`), Files by Google `1.13157.1011540316.0-release`, run `files-app-usage-001`, 2026-11-13.
- Pixel 7a (Android 14, Josh Hickman) and Poco X7 Pro (Android 15, Files by Google 1.8662), same table layout.
