# Files by Google – Safe Folder: read this first

This report lists the files in **Files by Google's Safe folder**, its PIN-protected hidden folder: where each file came from, and where the copy is kept.

**Copy In Archive** is worked out by looking for the copy in the extraction. Everything else is copied from the database.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **The copies were not encrypted** | In the version tested (1.13157) the Safe folder copy is the original file, byte for byte, under its original name. | [What a move does](#what-a-move-does) |
| **File Modified is not the move time** | It is the file's own modified time. The copy's change time (ctime) and the media store log are closer to when it was moved. | [What a move does](#what-a-move-does) |
| **Files by Google removes its own records of the file** | The file's index row and open records are deleted on the move. Other parts of the phone still show it existed. | [What a move does](#what-a-move-does) |

---

## Where the data is

| | |
|---|---|
| Database | `/data/data/com.google.android.apps.nbu.files/databases/safe_folder_db`, table `safe_folder_metadata_table` |
| Copies | `/data/data/com.google.android.apps.nbu.files/files/.FilesByGoogle/<original name>` (stored as `/data/user/0/…`, the same folder) |
| Original Path | storage location 1 = `/storage/emulated/0` + `original_file_root_relative_path` |

## What a move does

Lab, 2026-11-13: three files moved with file menu → *Move to Safe folder* → PIN.

| Where | What happened |
|---|---|
| `files/.FilesByGoogle/` | A copy under the original name; SHA-256 identical to the download for all three |
| `Download/` | The original was deleted |
| `safe_folder_metadata_table` | One row each: original path, Safe folder path, file's modified time |
| Files by Google's index and open records | Deleted |
| Media store `deleted_media`, recovery log, `app_logs` | Still show the files. The log has `Deleted … due to com.google.android.apps.nbu.files` at each move (23:34:10, 23:36:19, 23:37:36) |
| Thumbnail cache | Thumbnails made before the move stayed |
| Usage Stats | `SafeFolderAuthActivity` and `SafeFolderBrowserActivity` each time the Safe folder was used |

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Copies not encrypted | **Observed. Controlled.** | Three files, SHA-256 match |
| Report output | **Observed.** | Lab 3 rows (3 copies in archive), Pixel 7a 1 (copy present), Poco X7 Pro 1 (copy present) |
| Newer versions encrypt | **Not tested.** | Repeat with a current version |

## Sources

- Lab emulator `gtlab-alice`, Android 16, Files by Google `1.13157.1011540316.0-release`, run `files-app-usage-001`, 2026-11-13.
- Pixel 7a (Android 14) and Poco X7 Pro (Android 15).
