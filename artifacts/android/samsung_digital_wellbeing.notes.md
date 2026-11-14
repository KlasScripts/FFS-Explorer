# Digital Wellbeing (Samsung): read this first

This report lists the app usage events Samsung Digital Wellbeing (`com.samsung.android.forest`) keeps in `dwbCommon.db`. They are **copies of Android's Usage Stats events**, with the activity's class name added.

The interpreted column is **Event Type**: the name of the Android `UsageEvents` constant for the stored code. Everything else is copied from the database.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Times can differ from Usage Stats by a fixed amount** | On the image tested, every event was exactly 3.275 s later than the same event in Usage Stats. Usage Stats had moved after small clock corrections; these copies had not. | [Times and clock changes](#times-and-clock-changes) |
| **Only one image tested** | One Galaxy A53, Android 14. Other Samsung builds may differ. | [Validation](#validation) |
| **An event is not a user action** | `ACTIVITY_RESUMED` means an app screen came to the foreground, not that someone looked at it. | [Which events are stored](#which-events-are-stored) |

---

## Where the data is

| | |
|---|---|
| File | `/data/data/com.samsung.android.forest/databases/dwbCommon.db` (+ `-wal`) |
| Table | `usageEvents` (`eventId`, `timeStamp` ms, `eventType`, `className`, `instanceId`, `pkgId`), with package names in `foundPackages` (`pkgId`, `name`) |
| Newer builds | Reported to store `pkgName` on the event row instead of `pkgId`. The report reads either. Not seen in testing. |

## Which events are stored

| Code | Event Type | Matched to Usage Stats in testing |
|---|---|---|
| 1 | `ACTIVITY_RESUMED` | ✅ 73 events |
| 2 | `ACTIVITY_PAUSED` | ✅ 72 events |
| 12 | `NOTIFICATION_INTERRUPTION` | ✅ 42 events |
| 17 | `KEYGUARD_SHOWN` | ✅ 2 events |
| 23 | `ACTIVITY_STOPPED` | ✅ 62 events |
| 28 | `USER_UNLOCKED` | ❓ 1 event, outside Usage Stats' range |

## Times and clock changes

On the Galaxy A53 image, the 251 events inside Usage Stats' range all match a Usage Stats event with the same package and type exactly **3,275 ms earlier**, and none at any other offset.

The most likely explanation is a clock shift in Usage Stats. Android moves the whole Usage Stats history when the clock jumps by more than 2 seconds. The same image's Digital Wellbeing log (the *Digital Wellbeing (Samsung) – Log* report) holds 20 `TIME_SET` broadcasts over a month, with the computed boot time drifting by about a second at each. Samsung's copies did not move with them.

So these times may be the original ones and Usage Stats the moved ones. Before relying on a time to the second, line up one event against an independent record.

Google's Digital Wellbeing behaves differently: in the lab it moved its own rows after a clock change. See the Google report.

<a id="validation"></a>
#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Codes as in the table | **Observed. One real phone.** | Galaxy A53 (Android 14), 251 of 251 events in range matched at a fixed 3,275 ms offset |
| The offset is a Usage Stats shift | **Inferred.** | A constant offset for every event type, plus 20 `TIME_SET` lines in Samsung's log |
| Row count | **Cross-tool.** | 2,307 rows, the same as ALEAPP's published count for an image with that number |
| Lab test | **Not possible.** | The lab emulator is a Google image without Samsung Digital Wellbeing |

---

## Not in this report

- Samsung's own log lines (boots, clock and time zone changes): see *Digital Wellbeing (Samsung) – Log*.
- App timers, focus mode, bedtime and screen-time goals (other tables in `dwbCommon.db`), which were empty on the image tested.

## Sources

- Galaxy A53 full file system image (Android 14, serial `R5CW32F0PQH`, Magnet Virtual Summit 2026 CTF), checked 2026-11-14.
- `UsageEvents.Event` constants: AOSP `frameworks/base`, `core/java/android/app/usage/UsageEvents.java`.
- ALEAPP `scripts/artifacts/swellbeing.py` (table and join, the `pkgName` variant, row count).
