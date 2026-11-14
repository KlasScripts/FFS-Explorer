"""Samsung Digital Wellbeing's own log: boots, clock and time zone changes.

dwbCommon.db table Logging (id, key, value, timeStamp in Unix ms) holds
Digital Wellbeing's diagnostic lines. On the Galaxy A53 image (Android 14)
they include the broadcasts it received (android.intent.action.BOOT_COMPLETED,
TIME_SET, TIMEZONE_CHANGED), the wall-clock boot time it computed
("prevBootTime : … currentBootTime : …", with "difference : …"), and time zone
changes ("prevTimezone(…), newTimezone(…)"). Kind is worked out from the value
text; the text itself is shown unchanged.
"""

import re

name = "Digital Wellbeing (Samsung) - Log"
description = (
    "Samsung Digital Wellbeing's log lines from dwbCommon.db, table Logging. "
    "Clock change lines (TIME_SET) and time zone changes show when the phone's "
    "clock or zone changed; boot time lines show the boot time Digital "
    "Wellbeing worked out from the clock, so a change between two of them is "
    "the size of a clock correction. Kind is worked out from the text; Value is "
    "as stored."
)
app_path = "data/data/com.samsung.android.forest"
files = {"dwb": "databases/dwbCommon.db"}
optional_files = {
    "dwb_wal": "databases/dwbCommon.db-wal",
    "dwb_shm": "databases/dwbCommon.db-shm",
}
timestamp_fields = {"log_time": "ms", "boot_time": "ms"}
core_fields = ["log_time", "kind", "value", "boot_time"]
record_source = {
    "label": "Log line",
    "file_key": "dwb",
    "table": "Logging",
    "rowid_fields": ["raw_log_id"],
}

_BOOT = re.compile(r"currentBootTime : (\d{10,})")


def _kind(value: str) -> str:
    if "android.intent.action.TIME_SET" in value:
        return "Clock set (TIME_SET)"
    if "android.intent.action.TIMEZONE_CHANGED" in value:
        return "Time zone changed broadcast"
    if "newTimezone(" in value:
        return "Time zone change"
    if "timezoneDifference(" in value:
        return "Time zone difference"
    if "android.intent.action.BOOT_COMPLETED" in value:
        return "Boot completed"
    if "currentBootTime" in value:
        return "Boot time"
    return ""


def run(paths):
    from artifact_runner import log_note, open_db_readonly

    conn = open_db_readonly(paths["dwb"])
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "Logging" not in tables:
            log_note("dwbCommon.db has no Logging table; nothing read")
            return []
        rows = []
        for r in conn.execute("SELECT id, key, value, timeStamp FROM Logging ORDER BY timeStamp, id"):
            value = r["value"] or ""
            m = _BOOT.search(value)
            rows.append({
                "log_time": r["timeStamp"] if r["timeStamp"] and r["timeStamp"] > 0 else None,
                "kind": _kind(value),
                "value": value,
                "boot_time": int(m.group(1)) if m else None,
                "key": r["key"] or "",
                "raw_log_id": r["id"],
            })
    finally:
        conn.close()
    return rows
