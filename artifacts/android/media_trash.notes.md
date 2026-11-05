# Media Trash: read this first

**Status: experimental.** The trash time is worked out, not stored. The columns about which app did it are matched by time or file, and they support a conclusion rather than prove it.

This report lists every file in Android's shared trash, from the media store (`external.db`), and every unfinished write (a `.pending-` file). It also lists trashed files that the media store has already dropped, when Google Photos or the media store recovery log still has them.

The interpreted columns are **Trashed Time**, **Expiry Extended**, **Photos Trash Action**, **Files Open From/To** and **App On Screen**. Everything else is copied from the database.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Trashed Time is the expiry minus 30 days** | Android's default trash period is 30 days. A phone maker can change it. The time is to the second, no finer. | [Trashed Time](#trashed-time) |
| **The expiry can be moved** | If the system missed the purge, it renames the file with a new expiry, 7 days from then. Without the recovery log, the expiry then gives the wrong day. | [Expiry extended](#expiry-extended) |
| **Same second does not mean the system did it** | A user trashing several files at once in Photos or Files gives them all the same second. | [Trashed Time](#trashed-time) |
| **Photos' own trash time is not evidence that Photos did it** | Photos copies it from the media store, even when Files by Google did the trashing. This report does not show it, except for files only Photos still lists. | [Which app trashed it](#which-app-trashed-it) |
| **App On Screen can name the wrong app** | Usage Stats writes its events to disk later. A clock change before they are written moves them. | [App On Screen](#app-on-screen) |
| **A blank match is not a negative** | The Photos queue empties on a signed-in phone, Files keeps only its last few sessions, and Usage Stats keeps days, not months. | [Which app trashed it](#which-app-trashed-it) |

---

## Trashed Time

Moving a file to the trash leaves it where it is and renames it `.trashed-<expiry>-<name>`. The media store row gets `is_trashed = 1` and `date_expires`, the same number as the name, in Unix seconds. Android sets the expiry to the trash time plus 30 days, so **Trashed Time = expiry − 30 days**.

Files trashed together in one action get one expiry. On the lab phone, three files from Download, Pictures and Screenshots trashed as one Photos selection all became `.trashed-1796496033-`, and three trashed as one Files by Google selection all became `.trashed-1796500152-`. The system purge and the extension below also give a group one number. So a shared second shows the files went together, not who did it.

A **Pending** row is a file an app started writing and never finished. Its expiry is 7 days after the write started. It has no Trashed Time.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Expiry = trash time + 30 days | **Observed. Controlled.** | Lab emulator, Android 16, 4 and 5 November 2026. Photos' queued trash action fell in the same second as the expiry minus 30 days, every time (six trash actions, eight files). AOSP `DEFAULT_DURATION_TRASHED` is 30 days. |
| Real phone | **Observed.** | Poco X7 Pro, Android 15. Google Photos' `trash_timestamp` is exactly the expiry minus 30 days for all four trashed files, and Usage Stats has Photos on screen at both trash times it covers. |
| Other phone makers' trash periods | **Not tested.** | Only Google's emulator image and one Xiaomi phone. |

---

## Expiry extended

Android's idle maintenance deletes trashed files once they expire. If a file expired **more than a week** before the maintenance ran, it is not deleted. It is renamed with a new expiry of now plus 7 days, and deleted on a later run. Every file extended in the same run gets the same new number.

The media store **recovery log** (`data/media/0/.transforms/recovery/leveldb-<volume>/`) keeps each name a file has had, with its media store id. When the file had an earlier `.trashed-` name with no plain name in between, **Expiry Extended** is `Yes`. **Earlier Expiries** lists the old numbers, and Trashed Time comes from the first one.

| Value | Meaning |
|---|---|
| `No` | The recovery log has only the current trash name for this file. |
| `Yes` | An earlier `.trashed-` name in the same stay in the trash. Trashed Time uses the first. |
| `[current name not in the recovery log]` | The log has the file, but not its current name. Trashed Time is the expiry minus 30 days. |
| `[no recovery log record]` | The log has nothing for this file. It may have been compacted. |
| blank | The archive has no recovery log. |

**Trashed Before** counts earlier stays in the trash that ended in a restore, an earlier `.trashed-` name followed by the plain name.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Extension renames to one new expiry and Trashed Time is recovered | **Observed. Controlled.** | Lab emulator, 5 November 2026. Three files trashed at 07:26:36, 07:27:07 and 07:27:31, clock moved 39 days, idle job run: all three renamed `.trashed-1797838292-`. The report gives the three original times. |
| Recovery log on a real phone | **Observed.** | Poco X7 Pro: the four trashed files each have their trash name and media store id. Files trashed in May and July are no longer in it. |
| Restore and trash again | **Not tested.** | Trashed Before is worked out from the name order only. |

---

## Which app trashed it

Nothing in the media store records which app moved a file to the trash. **Created By** is the app that made the file, not the one that trashed it. Three other sources can support an answer.

**Photos Trash Action.** When the user moves items to the trash in Google Photos, Photos queues a `move_to_trash_optimistic_action` row in `gphotos-1.db` `action_queue`, with the time in milliseconds and a key for each file. The key is the file's SHA-1, base64url-encoded. **Photos Action Files** is how many files that one action held. **Photos Match** says how the row was linked:

| Photos Match | Meaning | Confidence |
|---|---|---|
| `Photos media store id` | Photos' own row for this media store id has a key in the action. | Strong |
| `File SHA-1` | The trashed file's own SHA-1 is a key in the action. | Strong |
| `Same second only` | The file's key could not be worked out (the file is gone), and an action falls in the Trashed Time second. | Weak: another file trashed in that second would match the same way |

The queue row stayed after the trash was emptied in the lab. That phone was not signed in to a Google account. On the signed-in Poco X7 Pro the queue was empty, so expect this column to be blank on most real phones.

**Files Open From/To** is a Files by Google session (`activityTimeLogStoreName.pb`) that covers the trash time. It shows Files was open, not that Files did it. The Poco X7 Pro kept only seven sessions, all on one day, so older trashes have no match.

**App On Screen** is from Usage Stats: see [App On Screen](#app-on-screen).

Photos also keeps `trash_timestamp` for files in the trash, but it does **not** show that Photos did the trashing: in the Files by Google test Photos had it too, the same second. It is used here only for files the media store no longer has (**Source** = Google Photos).

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Photos queues a row for each trash action | **Observed. Controlled.** | Lab emulator: one row for a three-file selection (18:40:33.708), one row each for five single trashes. |
| No Photos row when Files by Google trashed | **Observed. Controlled.** | Lab emulator, Files by Google 1.13157, three files trashed as one selection: no `action_queue` row. |
| Key is the file's SHA-1 | **Observed.** | Base64url SHA-1 of the trashed file equals Photos' `dedup_key` in the lab. |
| Files session covers its trash | **Observed. Controlled.** | Session 19:48:23.374–19:59:13.144 around the 19:49:12 trash. |
| Signed-in phone keeps the queue row | **Not tested.** | Empty on the Poco X7 Pro. |

---

## App On Screen

The app whose activity Usage Stats shows on screen at Trashed Time: the most recent `ACTIVITY_RESUMED` that has not been paused. When the time is only to the second, an app that came forward within that second is added: `<app>, then <app>`. With a Photos action the exact millisecond is used.

| Value | Meaning |
|---|---|
| `[before the first Usage Stats event]` | Usage Stats on this phone starts later. On the Poco X7 Pro it starts on 23 September 2025. |
| `[after the last Usage Stats event]` | The events for that time had not been written to disk when the phone was extracted. |
| `[no app on screen]` | The last activity was paused, for example the screen was off. |

**Watch out for a clock change.** Usage Stats holds recent events in memory and writes them later. In the lab, a snapshot reload moved the clock about 80 seconds forward before the events were written, and the written events were about 80 seconds late: Photos appears to open after the trash, and the column said Camera. A user or network time change on a real phone might do the same. Check the app on screen against Photos Trash Action or Files Open From/To where there is one.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Files by Google on screen at its trash | **Observed. Controlled.** | Lab emulator, 19:49:12. |
| Photos on screen at a real trash | **Observed.** | Poco X7 Pro, 23 September 2025, 20:28:35 and 23:23:22. |
| Wrong app after a clock change | **Observed. Lab only.** | Lab emulator, the emptied-trash run, as above. |

---

## Files the media store no longer has

**Status** `Gone from media store` is a file that was trashed and is no longer in `external.db`, because the trash was emptied, the file expired, or it was deleted.

- **Source** `Google Photos`: Photos still has the file's row with `trash_timestamp`. On the Poco X7 Pro, five files trashed in May to July 2025 are only here.
- **Source** `Media store recovery log`: the log's last name for the file was a `.trashed-` name.

These rows do not say when the file left the trash. The media store's own logs (`com.google.android.providers.media.module/app_logs/`) do: `Deleted … due to <package>` when an app emptied the trash, `Idle maintenance … N expired` for the system purge. They name no file, so they are not matched to rows here.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Recovery log keeps emptied files | **Observed. Controlled.** | Lab emulator: three files emptied from Photos Trash, still in the log with their trash names. |
| Photos keeps rows after the purge | **Observed.** | Poco X7 Pro, five files. |

---

## Not in this report

- When a file left the trash (see the media store `app_logs`).
- Xiaomi Gallery's own `DeleteRecord` table (`com.miui.gallery/databases/gallery_sub.db`), which has a path and a time for each delete. Not parsed yet.
- Gallery apps with their own trash folder, such as Samsung Gallery.
- Files trashed in Google Photos' cloud only.
- Android 10 and older, whose media store is `com.android.providers.media`.

## Sources

- AOSP MediaProvider: `FileUtils.DEFAULT_DURATION_TRASHED`, `DEFAULT_DURATION_PENDING`, `MediaProvider.deleteOrExtendExpiredItems`.
- Lab emulator, Android 16, Google Photos and Files by Google 1.13157, 4 and 5 November 2026: trash in Photos, a three-file Photos selection, a three-file Files selection, emptying the trash, and the idle-job extension and purge.
- Poco X7 Pro full-file image, Android 15 (Cellebrite CTF 2025): four files in the trash, five more in Photos only.
- Pixel 7a and Galaxy A53 images: nothing in the trash.
