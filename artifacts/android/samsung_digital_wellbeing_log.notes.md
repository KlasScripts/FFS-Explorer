# Digital Wellbeing (Samsung) – Log: read this first

This report lists Samsung Digital Wellbeing's own diagnostic log lines from `dwbCommon.db`. Among them are the system broadcasts it received for **boots, clock changes and time zone changes**, so this is one of the few places in an extraction that records when the phone's clock was set.

The interpreted column is **Kind**, worked out from the text, plus **Boot Time**, read from the text. **Value** is as stored.

## What could catch you out

| ⚠️ Watch out for | What it means for you | Read |
|---|---|---|
| **A `TIME_SET` line says the clock was set, not by how much** | The size of a change has to be worked out from the boot time lines around it. | [Reading the lines](#reading-the-lines) |
| **Not every line is understood** | 34 of 130 lines on the image tested were other diagnostics. Kind is blank for those. | [Reading the lines](#reading-the-lines) |
| **Only one image tested** | Other Samsung builds may log differently. | [Validation](#validation) |

---

## Where the data is

`/data/data/com.samsung.android.forest/databases/dwbCommon.db`, table `Logging` (`id`, `key`, `value`, `timeStamp` ms).

## Reading the lines

| Kind | Example value | What it says |
|---|---|---|
| Clock set (`TIME_SET`) | `android.intent.action.TIME_SET` | Android broadcast that the clock was set, by the user or by network time |
| Boot time | `prevBootTime : 1763492917764 currentBootTime : 1763492917764 difference : 0` | The boot moment Digital Wellbeing worked out from the clock. A change between two lines of the same boot is a clock correction of that size. |
| Boot completed | `android.intent.action.BOOT_COMPLETED` | A boot finished |
| Time zone changed broadcast / Time zone change / Time zone difference | `prevTimezone(  ), newTimezone( America/New_York )`, `timezoneDifference( 18000000 )` | The time zone changed (difference in ms) |

**Example from the image tested.** Within one boot, the computed boot time went from 1763492918711 (2025-11-20) to 1763492906672 (2025-11-28). That is about 12 seconds of clock corrections in 8 days, in steps of about a second, each with a `TIME_SET` line. Small steps like these add up, and once their total passes 2 seconds Usage Stats moves its history. On the same image, Samsung's usage events are 3.275 s later than Usage Stats.

<a id="validation"></a>
#### ✅ Validation

| What we read | Status | Basis |
|---|---|---|
| Line kinds and texts as in the table | **Observed. One real phone.** | Galaxy A53 (Android 14): 130 lines, 20 `TIME_SET`, 65 boot time, 8 boot completed, 3 time zone |
| Boot time changes are clock corrections | **Inferred.** | Boot time = clock − time since boot; it only changes when the clock is moved |
| Lab test | **Not possible.** | The lab emulator has no Samsung Digital Wellbeing |

---

## Not in this report

- Samsung's usage events: see *Digital Wellbeing (Samsung)*.

## Sources

- Galaxy A53 full file system image (Android 14, Magnet Virtual Summit 2026 CTF), checked 2026-11-14.
- ALEAPP `scripts/artifacts/swellbeing.py` (its time zone changes report reads the same table).
