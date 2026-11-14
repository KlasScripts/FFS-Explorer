# Digital Wellbeing (Google): read this first

This report lists the app usage events Google Digital Wellbeing keeps in its own database. They are **copies of Android's Usage Stats events**, made when Digital Wellbeing syncs, and they often reach further back than Usage Stats' detailed events, which are kept for about 10 days.

The interpreted column is **Event Type**: the name of the Android `UsageEvents` constant for the stored code. Everything else is copied from the database.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **Events after the last sync are missing** | In the lab, Digital Wellbeing copied only when its dashboard was opened. Everything since its previous copy, including over an hour of use the evening before, was missing until then. | [When it copies](#when-it-copies) |
| **A clock change moves every stored row, at the next sync** | After a clock change, the whole history moves by the size of the change, but only when Digital Wellbeing next syncs. Before that, its rows keep the old times. | [Clock changes](#clock-changes) |
| **Not every Usage Stats event is copied** | Screen on/off, app standby changes and locus ids are not stored. | [Which events are stored](#which-events-are-stored) |
| **A copied event can carry a shifted time** | If Usage Stats had already shifted when Digital Wellbeing copied, the shifted time is what was copied. | [Clock changes](#clock-changes) |
| **An event is not a user action** | `ACTIVITY_RESUMED` means an app screen came to the foreground, not that someone looked at it. | [Which events are stored](#which-events-are-stored) |

---

## Where the data is

| | |
|---|---|
| File | `/data/data/com.google.android.apps.wellbeing/databases/app_usage` (+ `-wal`) |
| Tables | `events` (`_id`, `timestamp` ms, `type`, `package_id`, `instance_id`, `task_root_package_id`), `packages` (`_id`, `package_name`), `component_events` and `components` |
| Columns | **Event Time**: `timestamp`. **Package**: `packages.package_name`. **Instance Id**: ties one activity's resumed, paused and stopped events together. **Task Root**: the package at the root of the activity's task. **Component**: only on `component_events` rows. |

`component_events` was empty on every image tested.

## Which events are stored

| Code | Event Type | Matched to Usage Stats in testing |
|---|---|---|
| 1 | `ACTIVITY_RESUMED` | ✅ Poco, Pixel, lab |
| 2 | `ACTIVITY_PAUSED` | ✅ Poco, Pixel, lab |
| 12 | `NOTIFICATION_INTERRUPTION` | ✅ Poco, Pixel |
| 18 | `KEYGUARD_HIDDEN` | ✅ Poco, Pixel |
| 19 | `FOREGROUND_SERVICE_START` | ✅ Poco, Pixel |
| 20 | `FOREGROUND_SERVICE_STOP` | ✅ Poco, Pixel |
| 23 | `ACTIVITY_STOPPED` | ✅ Poco, Pixel, lab |
| 26 | `DEVICE_SHUTDOWN` | ✅ Poco, Pixel |
| 27 | `DEVICE_STARTUP` | ✅ Poco, Pixel |

Any other code is shown with its `UsageEvents` name, if one exists, but has not been matched. In the lab, live Usage Stats in the same window also had `SCREEN_INTERACTIVE`, `SCREEN_NON_INTERACTIVE`, `STANDBY_BUCKET_CHANGED` and `LOCUS_ID_SET`, and none of them were copied.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Codes as in the table | **Observed. Two real phones.** | Poco X7 Pro: all 4,719 events inside Usage Stats' range matched a Usage Stats event at the same millisecond and package. Pixel 7a: 17,115 of 21,118 matched the same way, with the same codes. |
| App openings on the lab phone | **Observed. Controlled.** | Run `wellbeing-usage-001`, 2026-11-14: Settings, Chrome, Clock and Contacts opened from the launcher. All 47 events written matched live `dumpsys usagestats` on second, type, package and instance id. |
| Row counts | **Cross-tool.** | Pixel 44,715 and Poco 18,070 rows, the same as ALEAPP's published counts for images with those numbers. |

---

## When it copies

On the lab phone Digital Wellbeing did not copy as events happened.

| Time (UTC, 2026-11-14) | What happened | Rows |
|---|---|---|
| 06:56:05 | Baseline capture | 1,406, newest 2026-11-13 22:26:09 |
| 06:56–06:59 | Four apps opened, screen off and on | |
| 06:59:47 | Capture before opening the dashboard | 1,406 (unchanged) |
| 07:00:25 | Settings → *Digital Wellbeing & parental controls* opened | |
| 07:01 | Capture after the dashboard | 1,636, newest 07:00:25 |

The two `ACTIVITY_STOPPED` events at the dashboard's own opening (07:00:25) were not copied. They came after the copy.

The Pixel and Poco hold about 30 days of continuous events, so Digital Wellbeing also syncs without the dashboard being opened. How often was not tested.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Copied when the dashboard opened | **Observed. Controlled, once.** | Captures before and after, above |
| Other sync triggers and their timing | **Not tested.** | Test: leave the lab phone for a day without opening the dashboard |

---

## Clock changes

When the phone's clock jumps, Android's Usage Stats moves its whole history by the jump. Digital Wellbeing does the same to its own rows, at its next sync.

On the lab phone the clock jumped forward 7 h 10 m 34.8 s when a snapshot was loaded (06:53:18 UTC). The rows were unchanged at 06:59:47. When the dashboard was opened at 07:00:25, all 1,406 stored rows moved by +25,834,813 ms, exactly that jump.

So, depending on whether Digital Wellbeing has synced since a clock change, its rows can carry either the old or the moved times. A copy made from an already-shifted Usage Stats carries the shifted time. On the lab phone, Files by Google use from 2026-11-13 23:28 appears in Digital Wellbeing at 2026-11-14 06:50. Its dashboard then showed 1 hr 1 min of "today" screen time that was really the previous evening's use.

Samsung's Digital Wellbeing behaves differently: on the Galaxy A53 image it did not move its rows. See the Samsung report.

#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Rows move by the clock change, at the next sync | **Observed. Controlled, once.** | 1,406 of 1,406 rows, one shift value |
| Size and direction always equal Usage Stats' shift | **Inferred.** | One forward jump tested |

---

## Not in this report

- Daily totals and app limits (`app_usage_periods`, other databases such as `app_config`).
- Bedtime and focus mode (`bedtime`, `wind_down_session_history_data.db`).
- `files/AccountData.pb`.

## Sources

- Lab emulator `gtlab-alice`, Android 16 (`BE2A.250530.026.F3`), Digital Wellbeing `1.22.721609355` (700117), run `wellbeing-usage-001`, 2026-11-14.
- Pixel 7a full file system image (Android 14, Josh Hickman) and Poco X7 Pro (Android 15, Cellebrite CTF 2025).
- `UsageEvents.Event` constants: AOSP `frameworks/base`, `core/java/android/app/usage/UsageEvents.java`.
- ALEAPP `scripts/artifacts/wellbeing.py` (row counts and the events/packages join).
