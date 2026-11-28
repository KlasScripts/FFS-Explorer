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


def _deleted_fields(values):
    """deleted_tables 'decode' (A-95): the event type's name, as run() shows it."""
    code = values.get("eventType")
    return {"event_type": _event_names().get(code, str(code)) if code is not None else None}


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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'samsung_digital_wellbeing_deleted',
  'name': 'Digital Wellbeing (Samsung) — Deleted Events',
  'description': "Deleted app-usage event records from Samsung Digital Wellbeing's "
                 'dwbCommon.db (table usageEvents): an app opened, closed or moved to the '
                 'background, with the time to the millisecond. Pattern of life from before '
                 'the window the phone keeps. Live events are in Digital Wellbeing (Samsung).\n'
                 '\n'
                 'Each row is one record as found in the usageEvents table, never joined to '
                 'another table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says '
                 'Deleted (no longer in the table), Changed (an earlier version of a row still '
                 'there, listed only when one of the columns that matter changed, with the '
                 'current value beside it as "(now)") or an unfinished write/delete under a '
                 'hot journal. How found says how each record was recovered and how sure that '
                 'is; a record found by pattern has no row id. Values are as stored: no code '
                 'is turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'These apps remove their own events once they are old, so here Deleted means '
                 'only "no longer in the table", never "the user deleted it". The report '
                 'cannot tell who removed a record, but the time gives a hint, in age_check: '
                 "an event older than the oldest live event fits the app's own pruning; one "
                 'inside the live window does not, and is worth a closer look. How long the '
                 'app keeps events has not been checked against a test image, so no period is '
                 'stated here.\n'
                 '\n'
                 'timeStamp is Unix milliseconds, as in Digital Wellbeing (Samsung); '
                 'event_type is named as there (Android UsageEvents codes).\n'
                 '\n'
                 'linked_package: where the foundPackages row this event names (pkgId) was '
                 'found, live or recovered (only the id is compared; the package name is in '
                 'Digital Wellbeing (Samsung) for a live one). eventId and foundPackages.pkgId '
                 'are AUTOINCREMENT (checked 2026-11-28 on the CTF26 Android 14 image (the only one with Samsung Digital Wellbeing)), so SQLite does not hand out an id again.\n'
                 '\n'
                 'Checked 2026-11-28 on the CTF26 Android 14 image (the only one with Samsung Digital Wellbeing): on CTF26, sqlite_sequence says 3,354 event ids were '
                 'handed out and 2,307 events are live; none of the removed ones was '
                 'recovered.',
  'timestamp_fields': {'timeStamp': 'ms'},
  'core_fields': ['status',
                  'timeStamp',
                  'event_type',
                  'pkgId',
                  'linked_package',
                  'age_check',
                  'how_found',
                  'copies'],
  'file_key': 'dwb',
  'table': 'usageEvents',
  'label': 'Event',
  'fields': {'eventId': 'eventId',
             'timeStamp': 'timeStamp',
             'eventType': 'eventType',
             'className': 'className',
             'instanceId': 'instanceId',
             'pkgId': 'pkgId'},
  'decode': '_deleted_fields',
  'decoded_fields': ['event_type'],
  'derived': {'age_check': {'from': 'timeStamp',
                            'before_live': True,
                            'then': 'older than every live event (fits the app removing old '
                                    'events itself)',
                            'else': 'inside the live window: not removed for age'}},
  'version_fields': ['timeStamp', 'eventType', 'pkgId', 'className'],
  'links': [{'name': 'linked_package',
             'what': 'package',
             'column': 'pkgId',
             'table': 'foundPackages',
             'key': 'pkgId'}]}]
