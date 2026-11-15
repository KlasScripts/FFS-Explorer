"""App usage events copied by Google Digital Wellbeing into its app_usage database.

Digital Wellbeing (com.google.android.apps.wellbeing) keeps its own copy of
Android's Usage Stats events in databases/app_usage: table events (one row per
event, package joined from packages) and table component_events (events
recorded against a component, joined through components). Type codes are the
UsageEvents.Event constants; the name shown is the one the Usage Stats report
uses for the same code.

Verified 2026-11-14:
- Poco X7 Pro (Android 15): all 4,719 events inside Usage Stats' range match a
  Usage Stats event with the same millisecond and package. Codes 1, 2, 12, 18,
  19, 20, 23, 26 and 27 line up with ACTIVITY_RESUMED, ACTIVITY_PAUSED,
  NOTIFICATION_INTERRUPTION, KEYGUARD_HIDDEN, FOREGROUND_SERVICE_START/STOP,
  ACTIVITY_STOPPED, DEVICE_SHUTDOWN and DEVICE_STARTUP.
- Lab emulator (Android 16, run wellbeing-usage-001): every one of the 47
  events written for known app openings matched live dumpsys usagestats on
  second, type, package and instance id. Events were copied only when the
  Digital Wellbeing dashboard was opened. At that sync all 1,406 rows already
  stored moved by +25,834,813 ms, the size of the clock change at the
  snapshot load seven minutes earlier; they had not moved before the sync.
"""

name = "Digital Wellbeing (Google)"
warning = (
    "These are copies of Usage Stats events, made when Digital Wellbeing "
    "syncs (in the lab: when its dashboard was opened). Events after the last "
    "sync are not here. After a clock change Digital Wellbeing moves every "
    "stored row by the size of the change, as Usage Stats does, but only at "
    "its next sync: until then its rows keep the old times. Line up an event "
    "with an independent record before relying on the time to the second."
)
description = (
    "App usage events from Google Digital Wellbeing's app_usage database: "
    "activities coming to the foreground and leaving it, foreground services, "
    "notification interruptions, keyguard hidden, device startup and "
    "shutdown. Event type names are the UsageEvents constants for the stored "
    "code. Screen on/off, standby bucket changes and locus ids are not "
    "stored. Instance id ties an activity's resumed, paused and stopped "
    "events together. Task root is the package at the root of the activity's "
    "task."
)
app_path = "data/data/com.google.android.apps.wellbeing"
files = {"app_usage": "databases/app_usage"}
optional_files = {
    "app_usage_wal": "databases/app_usage-wal",
    "app_usage_shm": "databases/app_usage-shm",
}
timestamp_fields = {"event_time": "ms"}
timeline_fields = {
    "event_time": {
        "meaning": "App event copied from Usage Stats by Digital Wellbeing (Google)",
        "app": "package", "event": "event_type",
        "detail": ["task_root_package", "component"],
        "caution": ("Copied at Digital Wellbeing's syncs: events after the last sync are "
                    "missing. After a clock change the times move only at the next sync."),
    },
}
core_fields = ["event_time", "package", "event_type", "task_root_package", "component"]
record_source = {
    "label": "Event",
    "file_key": "app_usage",
    "table_field": "source_table",
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
    conn = open_db_readonly(paths["app_usage"])
    rows = []
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" in tables and "packages" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
            inst = "e.instance_id" if "instance_id" in cols else "NULL"
            root = "r.package_name" if "task_root_package_id" in cols else "NULL"
            root_join = ("LEFT JOIN packages r ON r._id = e.task_root_package_id"
                         if "task_root_package_id" in cols else "")
            for r in conn.execute(
                    f"SELECT e._id, e.timestamp, e.type, p.package_name, {inst} AS instance_id, "
                    f"{root} AS task_root FROM events e LEFT JOIN packages p ON p._id = e.package_id "
                    f"{root_join} ORDER BY e.timestamp, e._id"):
                rows.append({
                    "event_time": r["timestamp"],
                    "package": r["package_name"] or "",
                    "event_type": names.get(r["type"], str(r["type"])),
                    "event_code": r["type"],
                    "component": "",
                    "instance_id": r["instance_id"],
                    "task_root_package": r["task_root"] or "",
                    "source_table": "events",
                    "raw_event_id": r["_id"],
                })
        else:
            log_note("app_usage has no events/packages tables; no package events read")
        if {"component_events", "components", "packages"} <= tables:
            for r in conn.execute(
                    "SELECT ce._id, ce.timestamp, ce.type, c.component_name, p.package_name "
                    "FROM component_events ce LEFT JOIN components c ON c._id = ce.component_id "
                    "LEFT JOIN packages p ON p._id = c.package_id ORDER BY ce.timestamp, ce._id"):
                rows.append({
                    "event_time": r["timestamp"],
                    "package": r["package_name"] or "",
                    "event_type": names.get(r["type"], str(r["type"])),
                    "event_code": r["type"],
                    "component": r["component_name"] or "",
                    "instance_id": None,
                    "task_root_package": "",
                    "source_table": "component_events",
                    "raw_event_id": r["_id"],
                })
    finally:
        conn.close()
    rows.sort(key=lambda row: (row["event_time"] or 0, row["source_table"], row["raw_event_id"]))
    return rows
