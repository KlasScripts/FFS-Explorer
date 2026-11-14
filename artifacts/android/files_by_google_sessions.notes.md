# Files by Google – App Sessions: read this first

This report lists the **start and end of each screen visit** in Files by Google, from its own session log. It shows when the app was in the foreground. **It does not show which file was shown.**

**Duration** is worked out from start and end. The times are as stored.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **One row is one screen, not one use of the app** | Opening the app, a category and a file gave three rows. | [What a row is](#what-a-row-is) |
| **No file names** | Line a session up with *Opened Files* to see what was opened in it. | [What a row is](#what-a-row-is) |
| **Old entries may be dropped** | The Poco image held only seven entries, all on one day. | [Validation](#validation) |

---

## Where the data is

`/data/data/com.google.android.apps.nbu.files/files/activityTimeLogStoreName.pb`, a protobuf. Field 1 repeats, once per session, with start and end as `google.protobuf.Timestamp` (seconds and nanoseconds). Field 2 is the app version, shown as App Version. Each row links to its bytes in the file.

## What a row is

In the lab, each row's start and end matched a change of screen in the action log and in Usage Stats. For example, the video viewer was open from 23:30:11 to 23:30:27, and that was one row. The lab run of 2026-11-13 left 42 rows.

<a id="validation"></a>
#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Rows match screen visits | **Observed. Controlled.** | Lab run `files-app-usage-001`, against the action log and Usage Stats |
| Report output | **Observed.** | Lab 42 rows, Poco X7 Pro 7 (Files 1.8662). The Pixel 7a image has no session log file. |
| How long entries are kept | **Not tested.** | |

## Sources

- Lab emulator `gtlab-alice`, Android 16, Files by Google `1.13157.1011540316.0-release`, run `files-app-usage-001`, 2026-11-13.
- Poco X7 Pro (Android 15, Files by Google 1.8662).
