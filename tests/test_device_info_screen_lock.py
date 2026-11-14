"""Android Device Info: Screen Lock section from the spblob file times."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "android_device_info", os.path.join(ROOT, "artifacts", "android", "device_info.py"))
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

NS = 1_000_000_000
SP = "data/system_de/0/spblob/"


def test_pwd_present_reports_newest_pwd_time():
    lines = mod.screen_lock_lines({
        SP + "aa.pwd": {"mtime": 1740606801 * NS},
        SP + "ab.pwd": {"mtime": 1740606700 * NS, "ctime": 1740606700 * NS},
        SP + "aa.spblob": {"mtime": 1740606801 * NS},
        "data/system/locksettings.db": {"mtime": 1759254465 * NS},
    })
    text = "\n".join(lines)
    assert "set:** Yes (`.pwd` file present, 2 in the folder)" in text
    assert "written:** 2025-02-26 21:53:21 UTC (`data/system_de/0/spblob/aa.pwd`)" in text
    assert "locksettings" not in text


def test_ctime_shown_when_extraction_keeps_it():
    lines = mod.screen_lock_lines({SP + "aa.pwd": {"mtime": 1762798583 * NS,
                                                   "ctime": 1762798583 * NS}})
    assert "2025-11-10 18:16:23 UTC, changed 2025-11-10 18:16:23 UTC" in "\n".join(lines)


def test_no_pwd_reports_folder_time_only():
    lines = mod.screen_lock_lines({SP + "aa.spblob": {"mtime": 1794644601 * NS},
                                   SP + "aa.secdis": {"mtime": 1794644601 * NS}})
    text = "\n".join(lines)
    assert "set:** No (no `.pwd` file)" in text
    assert "Credential file written" not in text
    assert "folder last written:** 2026-11-14 08:23:21 UTC" in text


def test_no_spblob_files():
    lines = mod.screen_lock_lines({"data/system/locksettings.db": {"mtime": NS}})
    assert lines == ["## Screen Lock",
                     "- *No credential files (data/system_de/0/spblob) in this extraction.*"]
