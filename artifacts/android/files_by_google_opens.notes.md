# Files by Google – Opened Files: read this first

This report lists **each time a file was opened in Files by Google's viewer**, with the time to the millisecond. It is the only record found that ties a specific file to the moment it was opened in the app.

Path and details come from Files by Google's index row for the file. Nothing is interpreted.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **An open is not viewing time** | The record is written when the viewer opens. How long the file was on screen is not stored. | [What the time is](#what-the-time-is) |
| **A video open is not playback** | Opening a video records an open. Nothing records that it played, or how far. | [What the time is](#what-the-time-is) |
| **Safe folder moves delete open records** | Moving a file to the Safe folder deleted its index row, and the database deletes the file's open records with it. A full-screen viewer thumbnail made earlier can still appear in *Full-screen Views*. | [Deleted records](#deleted-records) |

---

## Where the data is

`/data/data/com.google.android.apps.nbu.files/databases/files_master_database`, table `file_open_info_table` (`file_id`, `file_open_time_stamp_ms`). `file_id` is `files_master_table.id`. An open whose index row is missing is shown as `[no index row — file_id=N]`.

## What the time is

In the lab the stored time was 0.4–0.6 s after Files by Google's viewer screen appeared in Usage Stats:

| File (lab, 2026-11-13) | Opened (stored) | Viewer on screen (Usage Stats, corrected for a clock shift) |
|---|---|---|
| `sample-red-200x200.webp` | 23:28:50.779 | 23:28:50.234 → 23:29:12.006 |
| `sample-red-200x200.png` | 23:29:18.070 | 23:29:17.573 → 23:29:30.921 |
| `sample-5s.mp4` | 23:30:12.059 | 23:30:11.651 → 23:30:27.094 |

For how long the viewer stayed open, use *App Sessions* or Usage Stats (`FilePreviewActivity`). Neither names the file. That a picture was shown full screen at least once (without a time) is *Full-screen Views*.

`file_consumption_data_table` has columns for play state, duration and play position, but it stayed empty after the video was opened in the viewer for about 15 seconds.

## Deleted records

`file_open_info_table` is declared with a foreign key to `files_master_table` and `ON DELETE CASCADE`. When the red PNG and the video were moved to the Safe folder, their index rows went, and so did their open records. After the move, only the WebP and an unplanned open of `sample-cmyk-400x300.jpg` remained.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| One record per open, at the open | **Observed. Controlled.** | Lab run `files-app-usage-001`, four opens |
| Open records deleted with the index row | **Observed. Controlled**, and **from the schema** | Two files, `ON DELETE CASCADE` |
| Report output | **Observed.** | Lab 2 rows, Pixel 7a 4, Poco X7 Pro 43 |

## Sources

- Lab emulator `gtlab-alice`, Android 16, Files by Google `1.13157.1011540316.0-release`, run `files-app-usage-001`, 2026-11-13; Usage Stats from capture `07-after-restart-usage`, corrected by the measured 1 h 41 m 53.454 s shift.
- Pixel 7a (Android 14) and Poco X7 Pro (Android 15).
