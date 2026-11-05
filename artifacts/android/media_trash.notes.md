# Media Trash: read this first

**Status: experimental.** Trashed Time is worked out, not stored. **Trashed By** is a conclusion; its reason says what it rests on.

One row per file moved to Android's shared trash. It answers three questions, and gives the source of each answer in the column beside it:

| Question | Column | Its source column |
|---|---|---|
| When was it trashed? | **Trashed Time** | **Trashed Time Source** |
| Did Android extend its time in the trash? | **Expiry Extended** | **Extended Source** |
| Which app trashed it? | **Trashed By** | **Trashed By Reason** |

**File Link** opens the file itself when it is still on the phone. **Status** is `In trash`, or `Gone (trash emptied, or expired)` for a file the media store no longer has.

Every source names the file and field: for example `external.db files.date_expires 1796500152 (2026-12-05 19:49:12 UTC) minus 30 days`, or `Google Photos gphotos-1.db action_queue: a move to trash queued at 2026-11-05 18:40:33.708 UTC lists this file`. The detail columns (Photos Trash Action, Files Open From/To, App On Screen, Expires, Media Store Id and others) hold the raw values.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Trashed Time is the expiry minus 30 days** | Android's trash period is 30 days. A phone maker can change it. The time is to the second. | [Trashed Time](#trashed-time) |
| **An extended file may have no Trashed Time** | If Android extended the expiry and the old one is lost, the trash time is not known. The row says before which date it was. | [Expiry Extended](#expiry-extended) |
| **"Probably" is not proof** | Only a Photos trash action that lists the file's own content is firm. The rest shows which app was open, not which app did it. | [Trashed By](#trashed-by) |
| **Usage Stats times move with the clock** | Each clock change of more than 2 seconds moves every earlier Usage Stats time by the same amount. Trash times do not move. | [App On Screen](#app-on-screen-detail-column) |
| **Unknown is not a negative** | The Photos queue empties, Files by Google keeps few screens, and Usage Stats keeps days, not months. | [Trashed By](#trashed-by) |

---

## Trashed Time

Moving a file to the trash leaves it where it is and renames it `.trashed-<expiry>-<name>`. The media store row gets `is_trashed = 1` and `date_expires`, the same number as the name, in Unix seconds. Android sets the expiry to the trash time plus 30 days (AOSP `FileUtils.computeDateExpires`, `DEFAULT_DURATION_TRASHED`), so **Trashed Time = expiry − 30 days**, rounded down to the second.

Files trashed together in one action get the same expiry. So a shared second shows that the files went together, not which app did it.

**Trashed Time Source** is one of:
- `external.db files.date_expires …`: the file is still in the trash.
- `…/leveldb-external_primary: first trash name …`: the recovery log shows the expiry was extended. The first expiry gives the trash time.
- `…/leveldb-external_primary: expiry in the last trash name …`: the file is gone, and the recovery log keeps its trash name.
- `Google Photos gphotos-1.db local_media.trash_timestamp …`: the file is gone, and only Photos still lists it. Photos copies this time from the media store expiry, so it is the same rule.
- `[Unknown: …]`: the expiry was probably extended and the earlier one is lost (see below).

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Trash time = expiry − 30 days | **Observed. Controlled.** | Lab emulator, Android 16, 4–5 November 2026, six Photos trashes (eight files) and one Files by Google trash (three files). An on-device file watcher logged each `.trashed-` rename in the same second as the expiry minus 30 days (e.g. 23:15:32.839, 19:49:12.592). |
| Real phone | **Observed, in part.** | Poco X7 Pro, Android 15: Usage Stats has Photos open from 23:23:14 to 23:23:23 on 23 September 2025, around the 23:23:22 trash time. Photos' `trash_timestamp` also matches, but it is copied from the expiry, so it is not a separate check. |
| Other phone makers' trash periods | **Not tested.** | Only Google's emulator image and one Xiaomi phone. |

---

## Expiry Extended

Android's idle maintenance (AOSP `MediaProvider.deleteOrExtendExpiredItems`) deletes a trashed file when its expiry has passed, but only if it passed less than a week ago. A file that expired a week or more ago is renamed instead, with a new expiry of the run's time plus 7 days. Files extended in one run normally get the same number. When two new names would clash, Android adds 1, 2, … seconds.

| Value | Meaning | Extended Source |
|---|---|---|
| `Yes` | The recovery log has an earlier `.trashed-` name for this file, with no plain name between. Trashed Time comes from the first one. | The recovery log folder and both names. |
| `Probably` | The expiry is exactly 7 days after an idle maintenance run in the media store's `app_logs`. That is what an extension sets, and the earlier name is lost from the recovery log (compaction). Trashed Time is blank; its source says the file was trashed before a date (the expiry minus 44 days). | The `app_logs` file and the run's time. |
| `No sign of it` | The recovery log has only the current trash name, and no idle maintenance run in `app_logs` matches. | What was checked. |
| `Not known` | There is no recovery log, it has nothing for this file, or only Photos lists the file. | Which of these. |

The recovery log is `data/media/0/.transforms/recovery/leveldb-external_primary/`. It keeps each name a file has had, with its media store id. Older entries are compacted: on the Poco X7 Pro, a file trashed three weeks before extraction had only its trash name left, while a newer one still had its download and rename. So `No sign of it` on an old trash depends on `app_logs`, which on the Poco went back only two weeks.

`app_logs` lines are in the phone's local time. Each log file is named with the UTC time it was started, so the report reads the offset from the file's first line (UTC−4 on the Poco X7 Pro).

**Trashed Before** counts earlier stays in the trash that ended in a restore.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Extension renames to one new expiry; Trashed Time recovered | **Observed. Controlled.** | Lab emulator, 5 November 2026. Three files trashed at 07:26:36, 07:27:07 and 07:27:31. The clock was moved 39 days and all three were renamed `.trashed-1797838292-` at 07:31:32.573. The report gives the three original times. |
| Expiry = idle maintenance run + 7 days | **Observed. Controlled.** | Same run: `Idle maintenance on external … taking 162ms` logged at 07:31:32.581, and the new expiry minus 7 days is 07:31:32. With the old names removed, the report says `Probably` and "trashed before 2026-11-07". |
| An extension leaves no count in `app_logs` | **Observed. Controlled.** | That run logged `0 expired`; the later purge logged `3 expired`. |
| Restore and trash again | **Not tested.** | Trashed Before is worked out from the name order only. |

---

## Trashed By

Nothing in the media store records which app moved a file to the trash. **Created By** (a detail column) is the app that made the file, not the one that trashed it. Trashed By takes the strongest evidence there is, in this order:

| Trashed By | Rests on | How firm |
|---|---|---|
| `Google Photos` | Google Photos' `action_queue` has a `move_to_trash_optimistic_action` queued in the trash second or the next, and it lists this file's content key: through Photos' own row for the media store id, or the SHA-1 of the file. | Firm |
| `Probably Google Photos` | Such an action in the trash second, for a file that is gone, so its content cannot be checked. The action lists a key no file on the phone has. | Strong in the lab; another file trashed in the same second would match the same way |
| `Probably Files by Google` | Files by Google's own log has a Files screen open over the trash time, and there was no Photos action. | Files was open, not proof that it did it |
| `Probably <app>` | Usage Stats shows that one app on screen at the trash time, and there was nothing above. | That app was open, nothing more |
| `Unknown` | None of the above. The reason says what was missing. | — |

**The Photos key is the file's content, not the file.** It is the base64url SHA-1 of the bytes (Photos' `dedup_key`). The same picture downloaded twice has the same key; in the lab, `Example.jpg` had one key across three runs. So an action counts only when it was queued in the trash second or the next. In the lab, Photos queued it 51 to 81 ms after the rename. An action for the same content at another time is named in **Photos Match** (`[a Photos trash action at … lists the same content, not at this trash]`) and not used.

**Photos' queue is read from every Photos database**: `gphotos-1.db` (no account) and `gphotos<N>.db` for each signed-in account. In the lab, the row stayed after the trash was emptied. On the signed-in Poco X7 Pro, the queue was empty in both databases, nine days after its last trash. When or why Photos clears it is not known, so expect this to be missing on many phones.

**Files by Google's log** (`activityTimeLogStoreName.pb`) is the `onResume` and `onPause` time of each Files screen (decompiled 1.13157: the activities' shared base class writes the pair on pause). One screen switching to another gives two entries a few milliseconds apart. The Poco X7 Pro (Files 1.8662) kept seven entries, all within two minutes on one day, so older trashes have no match. How long entries are kept is not known; that code did not decompile.

Photos also keeps `trash_timestamp` for files in the trash. It does **not** show that Photos did the trashing: in the Files by Google test, Photos had it too, to the same second.

Files by Google's `PlayProtectCleanStats.pb` is not a deletion record. It is a card type and the time the card state was stored (decompiled 1.13157). In the lab it was first written 120 ms after the Files trash, when Files rebuilt its cards, so its time can sit next to a deletion without recording one.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Photos queues one row per trash action | **Observed. Controlled.** | Lab emulator: one row for a three-file selection (18:40:33.708), one row each for five single trashes. All six gave `Google Photos` or `Probably Google Photos`. |
| No Photos row when Files by Google trashed | **Observed. Controlled.** | Lab emulator, Files by Google 1.13157, three files in one selection: no `action_queue` row. All three gave `Probably Files by Google`. |
| Key is the file's SHA-1 | **Observed.** | The base64url SHA-1 of each trashed file equals Photos' `dedup_key`: eight lab files and the Poco X7 Pro's four. |
| Files screen covers its trash | **Observed. Controlled.** | Screen open 19:48:23.374–19:59:13.144 around the 19:49:12 trash; Usage Stats has `FileBrowserRegularActivity` resumed 19:48:23.329 to 19:59:13.155. |
| Photos on screen at a real trash | **Observed.** | Poco X7 Pro, 23 September 2025, 20:28:35 and 23:23:22: `Probably Google Photos`. Who really did it is not known. |

---

## App On Screen (detail column)

The apps whose activities Usage Stats shows resumed at the trash time, latest first. Two at once (`A + B`) means split screen or a switch in progress. On the Poco X7 Pro, the next activity is often resumed before the last one is paused. When the time is only to the second, an app that came forward within that second is added: `<app>, then <app>`. With a Photos action, its exact millisecond is used.

| Value | Meaning |
|---|---|
| `[before the first Usage Stats event]` | Usage Stats on this phone starts later. On the Poco X7 Pro it starts on 23 September 2025. |
| `[after the last Usage Stats event]` | The events for that time had not been written to disk when the phone was extracted. |
| `[no app on screen]` | No activity was resumed then, for example the screen was off. |
| `… [Usage Stats does not show <app> here, though …]` | Photos' trash action or Files by Google's log puts that app on screen, and Usage Stats does not. Its times have probably been moved by a clock change. |

**A clock change moves every earlier Usage Stats time.** When the clock jumps by more than 2 seconds, Android writes out the events it holds and renames every Usage Stats file by the jump (AOSP `UserUsageStatsService.onTimeChanged`, `UsageStatsDatabase.onTimeChanged`). An event's time is stored relative to its file's name, so every earlier event moves by the jump, days back too. The media store expiry, Photos' action time and Files by Google's log keep the time they were written with. In the lab, a snapshot reload moved the clock 83.758 seconds. Every event from before it, back to 29 October, came out 83.758 seconds late, and the Photos trash showed `com.android.camera2` on screen. A user or network time change on a real phone does the same.

---

## Files the media store no longer has

**Status** `Gone (trash emptied, or expired)` is a file that was trashed and is no longer in `external.db`, because the trash was emptied, it expired, or it was deleted. The report does not say which, or when. The media store's `app_logs` record it (`Deleted … due to <package>` when an app emptied the trash, one line per file; `Idle maintenance … N expired` for the system purge), but they name no file.

- **Source** `Media store recovery log`: the log's last name for the file was a `.trashed-` name.
- **Source** `Google Photos (…)`: Photos still has the file's row with `trash_timestamp`. On the Poco X7 Pro, five files trashed in May to July 2025 are only here.

These files have no File Link: the file itself is gone.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Recovery log keeps emptied files | **Observed. Controlled.** | Lab emulator: three files emptied from Photos Trash, still in the log with their trash names. |
| Photos keeps rows after the purge | **Observed.** | Poco X7 Pro, five files. |

---

## Not in this report

- When a file left the trash, and whether it was emptied or expired (see `app_logs`).
- Pending files (`.pending-`, an unfinished write). They are not deletions.
- Xiaomi Gallery's own `DeleteRecord` table (`com.miui.gallery/databases/gallery_sub.db`). On the Poco X7 Pro it has no row for any of the nine trashed files.
- Photos' `local_trash.db` (empty on every image checked) and the media store's `deleted_media` table (ids only, no time).
- Gallery apps with their own trash folder, such as Samsung Gallery.
- Files trashed in Google Photos' cloud only.
- Android 10 and older, whose media store is `com.android.providers.media`.

## Sources

- AOSP MediaProvider: `FileUtils.computeDateExpires`, `DEFAULT_DURATION_TRASHED`, `DEFAULT_DURATION_EXTENDED`; `MediaProvider.deleteOrExtendExpiredItems`, `extendExpiredItem`; `util/Logging.java` (app_logs time format and file names).
- AOSP frameworks/base: `UserUsageStatsService.onTimeChanged`, `checkAndGetTimeLocked`; `UsageStatsDatabase.onTimeChanged`; `UsageStatsService.TIME_CHANGE_THRESHOLD_MILLIS` (2 seconds).
- Files by Google 1.13157.1011540316.0-release, decompiled with jadx 1.5.1: `hzz` (onResume/onPause screen log), `hzp`/`hzo` (the log's messages), `idh`/`ibj`/`icv` (PlayProtectCleanStats).
- Lab emulator, Android 16, Google Photos and Files by Google 1.13157, 4 and 5 November 2026 (GTDs media-trash-delete-001, -expiry-001, -expiry-extend-001, -group-001, -group-empty-001, -files-group-001), with each run's ground truth and on-device file-watcher log.
- Poco X7 Pro full-file image, Android 15 (Cellebrite CTF 2025): four files in the trash, five more in Photos only.
- Pixel 7a and Galaxy A53 images: nothing in the trash.
