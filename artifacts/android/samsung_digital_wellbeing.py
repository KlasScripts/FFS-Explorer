"""App usage events kept by Samsung Digital Wellbeing in dwbCommon.db.

Samsung Digital Wellbeing (com.samsung.android.forest) stores events in
databases/dwbCommon.db, table usageEvents (timeStamp in Unix ms, eventType,
className, instanceId, pkgId), with package names in foundPackages. Newer
builds are reported to store pkgName on the event row instead; both are read.
Type codes are the UsageEvents.Event constants; the name shown is the one the
Usage Stats report uses for the same code.

Verified 2026-11-14 on the Galaxy A53 image (Android 14, Magnet Virtual
Summit 2026 CTF): all 251 events inside Usage Stats' range match a Usage
Stats event with the same package and type exactly 3,275 ms earlier, and
none at any other offset. Codes 1, 2, 12, 17 and 23 line up with
ACTIVITY_RESUMED, ACTIVITY_PAUSED, NOTIFICATION_INTERRUPTION, KEYGUARD_SHOWN
and ACTIVITY_STOPPED. The constant gap is Usage Stats' history having been
shifted by clock corrections after Samsung copied the events (the Log report
on the same image has 20 TIME_SET broadcasts).
"""

name = "Digital Wellbeing (Samsung)"
warning = (
    "These are copies of Usage Stats events. On the one image tested, every "
    "event was 3.275 s later than the same event in Usage Stats, because "
    "Usage Stats had shifted after clock corrections and these copies had not. "
    "Line up an event with an independent record before relying on the time "
    "to the second."
)
description = (
    "App usage events from Samsung Digital Wellbeing's dwbCommon.db: "
    "activities coming to the foreground and leaving it, notification "
    "interruptions, keyguard shown and user unlocked. Class is the activity "
    "class name as stored. Event type names are the UsageEvents constants "
    "for the stored code."
)
app_path = "data/data/com.samsung.android.forest"
files = {"dwb": "databases/dwbCommon.db"}
optional_files = {
    "dwb_wal": "databases/dwbCommon.db-wal",
    "dwb_shm": "databases/dwbCommon.db-shm",
}
timestamp_fields = {"event_time": "ms"}
timeline_fields = {
    "event_time": {
        "meaning": "App event copied from Usage Stats by Digital Wellbeing (Samsung)",
        "app": "package", "event": "event_type",
        "detail": ["class_name"],
        "caution": ("A copy of a Usage Stats event. On the one image tested, every event "
                    "was 3.275 s later than the same event in Usage Stats."),
    },
}
core_fields = ["event_time", "package", "event_type", "class_name"]
record_source = {
    "label": "Event",
    "file_key": "dwb",
    "table": "usageEvents",
    "rowid_fields": ["raw_event_id"],
}


def _event_names() -> dict:
    import importlib.util
    import sys
    from pathlib import Path
    key = "ffs_android_usagestats"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name("usagestats.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod._EVENT_TYPES


def run(paths):
    from artifact_runner import log_note, open_db_readonly

    names = _event_names()
    conn = open_db_readonly(paths["dwb"])
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "usageEvents" not in tables:
            log_note("dwbCommon.db has no usageEvents table; nothing read")
            return []
        cols = {r[1] for r in conn.execute("PRAGMA table_info(usageEvents)")}
        if "pkgName" in cols:
            pkg, join = "e.pkgName", ""
        elif "pkgId" in cols and "foundPackages" in tables:
            pkg, join = "p.name", "LEFT JOIN foundPackages p ON p.pkgId = e.pkgId"
        else:
            log_note("usageEvents has neither pkgName nor pkgId with foundPackages; packages are blank")
            pkg, join = "NULL", ""
        cls = "e.className" if "className" in cols else "NULL"
        inst = "e.instanceId" if "instanceId" in cols else "NULL"
        rows = []
        for r in conn.execute(
                f"SELECT e.eventId, e.timeStamp, e.eventType, {pkg} AS package, {cls} AS class_name, "
                f"{inst} AS instance_id FROM usageEvents e {join} ORDER BY e.timeStamp, e.eventId"):
            rows.append({
                "event_time": r["timeStamp"],
                "package": r["package"] or "",
                "event_type": names.get(r["eventType"], str(r["eventType"])),
                "event_code": r["eventType"],
                "class_name": r["class_name"] or "",
                "instance_id": r["instance_id"],
                "raw_event_id": r["eventId"],
            })
    finally:
        conn.close()
    return rows
