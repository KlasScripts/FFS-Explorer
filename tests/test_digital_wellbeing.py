"""Google and Samsung Digital Wellbeing reports: joins, type names, log kinds."""
import importlib.util
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parser(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "artifacts", "android", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _google_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE packages (_id INTEGER PRIMARY KEY, package_name TEXT);
        CREATE TABLE components (_id INTEGER PRIMARY KEY, component_name TEXT NOT NULL, package_id INTEGER NOT NULL);
        CREATE TABLE component_events (_id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, type INTEGER NOT NULL,
                                       component_id INTEGER NOT NULL);
        CREATE TABLE events (_id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, type INTEGER NOT NULL,
                             package_id INTEGER NOT NULL, instance_id INTEGER DEFAULT NULL,
                             task_root_package_id INTEGER DEFAULT NULL);
        INSERT INTO packages VALUES (1, 'com.android.settings'), (2, 'com.google.android.apps.nexuslauncher');
        INSERT INTO events VALUES (10, 1794639383096, 1, 1, 254940826, 1);
        INSERT INTO events VALUES (11, 1794639406429, 2, 1, 254940826, 1);
        INSERT INTO events VALUES (12, 1794639407742, 23, 1, 254940826, 1);
        INSERT INTO events VALUES (13, 1794639500000, 99, 2, NULL, NULL);
        INSERT INTO components VALUES (5, 'com.android.settings/.Settings', 1);
        INSERT INTO component_events VALUES (7, 1794639383100, 1, 5);
    """)
    conn.commit()
    conn.close()


def test_google_events_named_joined_and_ordered(tmp_path):
    db = str(tmp_path / "app_usage")
    _google_db(db)
    rows = _parser("google_digital_wellbeing").run({"app_usage": db})
    assert [(r["source_table"], r["raw_event_id"], r["event_type"]) for r in rows] == [
        ("events", 10, "ACTIVITY_RESUMED"),
        ("component_events", 7, "ACTIVITY_RESUMED"),
        ("events", 11, "ACTIVITY_PAUSED"),
        ("events", 12, "ACTIVITY_STOPPED"),
        ("events", 13, "99"),
    ]
    first = rows[0]
    assert first["package"] == "com.android.settings" and first["task_root_package"] == "com.android.settings"
    assert first["instance_id"] == 254940826 and first["event_code"] == 1
    assert rows[1]["component"] == "com.android.settings/.Settings"


def test_google_without_component_tables(tmp_path):
    db = str(tmp_path / "app_usage")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE packages (_id INTEGER PRIMARY KEY, package_name TEXT);
        CREATE TABLE events (_id INTEGER PRIMARY KEY, timestamp INTEGER NOT NULL, type INTEGER NOT NULL,
                             package_id INTEGER NOT NULL);
        INSERT INTO packages VALUES (1, 'a.b');
        INSERT INTO events VALUES (1, 5, 26, 1);
    """)
    conn.commit()
    conn.close()
    rows = _parser("google_digital_wellbeing").run({"app_usage": db})
    assert rows == [{"event_time": 5, "package": "a.b", "event_type": "DEVICE_SHUTDOWN", "event_code": 26,
                     "component": "", "instance_id": None, "task_root_package": "", "source_table": "events",
                     "raw_event_id": 1}]


def _samsung_db(path, pkg_name_column=False):
    conn = sqlite3.connect(path)
    if pkg_name_column:
        conn.executescript("""
            CREATE TABLE usageEvents (eventId INTEGER PRIMARY KEY AUTOINCREMENT, timeStamp INTEGER, eventType INTEGER,
                                      className TEXT, instanceId INTEGER, pkgName TEXT);
            INSERT INTO usageEvents VALUES (1, 1763492953000, 1, 'com.x.Main', 7, 'com.x');
        """)
    else:
        conn.executescript("""
            CREATE TABLE foundPackages (pkgId INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
            CREATE TABLE usageEvents (eventId INTEGER PRIMARY KEY AUTOINCREMENT, timeStamp INTEGER, eventType INTEGER,
                                      className TEXT, instanceId INTEGER, pkgId INTEGER NOT NULL);
            INSERT INTO foundPackages VALUES (76, 'android'), (90, 'com.sec.android.app.launcher');
            INSERT INTO usageEvents VALUES (1048, 1763492952082, 17, NULL, 0, 76);
            INSERT INTO usageEvents VALUES (1054, 1763492953000, 1, 'com.sec.android.app.launcher.Launcher', 257990188, 90);
            INSERT INTO usageEvents VALUES (1055, 1763492954000, 2, NULL, 0, 999);
        """)
    conn.executescript("""
        CREATE TABLE Logging (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value TEXT NOT NULL,
                              timeStamp LONG NOT NULL DEFAULT -1);
        INSERT INTO Logging VALUES (1, 'DWBActivityThread', 'android.intent.action.TIME_SET', 1763627638777);
        INSERT INTO Logging VALUES (2, 'DWBThread',
            'prevBootTime : 1763492917764 currentBootTime : 1763492917764 difference : 0', 1763627639064);
        INSERT INTO Logging VALUES (3, 'DWBThread', 'prevTimezone(  ), newTimezone( America/New_York ), now( 1 )', 1);
        INSERT INTO Logging VALUES (4, 'DWBThread', 'something else', -1);
    """)
    conn.commit()
    conn.close()


def test_samsung_events_with_found_packages(tmp_path):
    db = str(tmp_path / "dwbCommon.db")
    _samsung_db(db)
    rows = _parser("samsung_digital_wellbeing").run({"dwb": db})
    assert [(r["raw_event_id"], r["event_type"], r["package"], r["class_name"]) for r in rows] == [
        (1048, "KEYGUARD_SHOWN", "android", ""),
        (1054, "ACTIVITY_RESUMED", "com.sec.android.app.launcher", "com.sec.android.app.launcher.Launcher"),
        (1055, "ACTIVITY_PAUSED", "", ""),
    ]


def test_samsung_events_with_pkgname_column(tmp_path):
    db = str(tmp_path / "dwbCommon.db")
    _samsung_db(db, pkg_name_column=True)
    rows = _parser("samsung_digital_wellbeing").run({"dwb": db})
    assert [(r["package"], r["event_type"], r["instance_id"]) for r in rows] == [("com.x", "ACTIVITY_RESUMED", 7)]


def test_samsung_log_kinds_and_boot_time(tmp_path):
    db = str(tmp_path / "dwbCommon.db")
    _samsung_db(db)
    rows = _parser("samsung_digital_wellbeing_log").run({"dwb": db})
    by_id = {r["raw_log_id"]: r for r in rows}
    assert by_id[1]["kind"] == "Clock set (TIME_SET)"
    assert by_id[2]["kind"] == "Boot time" and by_id[2]["boot_time"] == 1763492917764
    assert by_id[3]["kind"] == "Time zone change"
    assert by_id[4]["kind"] == "" and by_id[4]["log_time"] is None
    assert by_id[2]["value"].startswith("prevBootTime")
