"""Phone time zone helpers (app/device_timezone.py), the two Timezone &
time sources parsers, the Device Info time zone rows, and Handset local
re-detection for cases saved under the iOS-only rule."""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import device_timezone as dt                      # noqa: E402
from adapters.ffs import FfsAdapter               # noqa: E402

LAB = os.path.join(ROOT, "test_data", "alice_timezone_probe")
DUMP = FfsAdapter(FfsAdapter.FORMAT_ZIP_EXTRAS, "Dump", "")
GK = FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, "private/var", "")
IOS = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, "filesystem1", "filesystem1", True)


def _ld(field, payload: bytes) -> bytes:
    out, n = bytes([(field << 3) | 2]), len(payload)
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out + payload


def _props(**kv) -> bytes:
    return b"".join(_ld(1, _ld(1, k.encode()) + _ld(2, v.encode())) for k, v in kv.items())


def _reader(files: dict):
    return lambda name: files.get(name)


def _load(rel):
    spec = importlib.util.spec_from_file_location("p_" + rel.replace("/", "_"), os.path.join(ROOT, rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SETTINGS_ON = (b'<?xml version="1.0"?><settings version="-1">'
               b'<setting id="7" name="auto_time" value="1" package="android" />'
               b'<setting id="9" name="auto_time_zone" value="0" package="root" /></settings>')
NETPOLICY = (b'<?xml version="1.0"?><policy-list><network-policy networkTemplate="1" '
             b'cycleStart="2023-01-16T00:00-05:00[America/New_York]" /></policy-list>')


class PersistentProperties(unittest.TestCase):
    def test_parse(self):
        data = _props(**{"persist.sys.fuse": "true", "persist.sys.timezone": "Europe/London",
                         "persist.sys.timezone_confidence": "100", "vendor.qemu.timezone": "Asia/Tokyo"})
        got = dt.parse_persistent_properties(data)
        self.assertEqual(got["persist.sys.timezone"], "Europe/London")
        self.assertEqual(got["persist.sys.timezone_confidence"], "100")

    def test_truncated_keeps_what_was_read(self):
        data = _props(**{"persist.sys.timezone": "Europe/London", "persist.x": "y"})
        self.assertEqual(dt.parse_persistent_properties(data[:-3]), {"persist.sys.timezone": "Europe/London"})

    def test_lab_file(self):
        path = os.path.join(LAB, "persistent_properties.after")
        if not os.path.exists(path):
            self.skipTest("lab probe not present")
        self.assertIn("/", dt.parse_persistent_properties(open(path, "rb").read())["persist.sys.timezone"])


class PhoneZone(unittest.TestCase):
    def test_android_cellebrite_dump_layout(self):
        read = _reader({"Dump/data/property/persistent_properties":
                        _props(**{"persist.sys.timezone": "America/New_York",
                                  "persist.sys.timezone_confidence": "100"})})
        got = dt.phone_zone(read, DUMP)
        self.assertEqual((got["zone"], got["confidence"], got["source"], got["known"]),
                         ("America/New_York", "100", "Dump/data/property/persistent_properties", True))

    def test_android_graykey_layout(self):
        read = _reader({"/data/property/persistent_properties": _props(**{"persist.sys.timezone": "Europe/Paris"})})
        self.assertEqual(dt.phone_zone_android(read, GK)["source"], "/data/property/persistent_properties")

    def test_emulator_property_is_ignored(self):
        read = _reader({"Dump/data/property/persistent_properties": _props(**{"vendor.qemu.timezone": "Asia/Tokyo"})})
        self.assertIsNone(dt.phone_zone(read, DUMP))

    def test_android_old_one_file_per_property(self):
        read = _reader({"Dump/data/property/persist.sys.timezone": b"Australia/Sydney\n"})
        self.assertEqual(dt.phone_zone_android(read, DUMP)["zone"], "Australia/Sydney")

    def test_ios_localtime(self):
        read = _reader({"filesystem1/private/var/db/timezone/localtime":
                        b"/var/db/timezone/zoneinfo/America/New_York"})
        got = dt.phone_zone(read, IOS)
        self.assertEqual((got["zone"], got["setting"]), ("America/New_York", "symlink target"))

    def test_unknown_name_is_flagged(self):
        read = _reader({"Dump/data/property/persistent_properties": _props(**{"persist.sys.timezone": "Mars/Olympus"})})
        self.assertFalse(dt.phone_zone(read, DUMP)["known"])

    def test_reader_errors_are_none(self):
        def boom(_name):
            raise OSError("bad entry")
        self.assertIsNone(dt.phone_zone(boom, DUMP))


class AutoTimeZone(unittest.TestCase):
    def test_text_xml(self):
        got = dt.auto_time_zone_android(_reader({"Dump/data/system/users/0/settings_global.xml": SETTINGS_ON}), DUMP)
        self.assertEqual((got["auto_time_zone"], got["auto_time"]), (False, True))

    def test_lab_abx_on_and_off(self):
        on, off = (os.path.join(LAB, f) for f in ("settings_global.after_on.xml", "settings_global.after_off.xml"))
        if not (os.path.exists(on) and os.path.exists(off)):
            self.skipTest("lab probe not present")
        key = "Dump/data/system/users/0/settings_global.xml"
        self.assertTrue(dt.auto_time_zone_android(_reader({key: open(on, "rb").read()}), DUMP)["auto_time_zone"])
        self.assertFalse(dt.auto_time_zone_android(_reader({key: open(off, "rb").read()}), DUMP)["auto_time_zone"])

    def test_missing(self):
        self.assertIsNone(dt.auto_time_zone_android(_reader({}), DUMP))


class WeakHints(unittest.TestCase):
    def test_allow_listed_files_only_and_capped(self):
        battery = "data/user_de/0/com.android.settings/databases/battery-usage-db-v11"
        files = {"Dump/data/system/netpolicy.xml": NETPOLICY,
                 "Dump/" + battery: b"\x00Europe/London\x00Asia/Kolkata\x00America/Chicago\x00",
                 "Dump/apex/com.android.tzdata/etc/tz/tzdata": b"Europe/Berlin"}
        ui = [battery, "apex/com.android.tzdata/etc/tz/tzdata"]
        hints = dt.weak_hints_android(_reader(files), DUMP, ui)
        self.assertEqual(len(hints), dt.MAX_WEAK_HINTS)
        self.assertEqual(hints[0]["value"], "America/New_York")
        self.assertIn("cycleStart", hints[0]["detail"])
        self.assertNotIn("Europe/Berlin", [h["value"] for h in hints])


class Comparisons(unittest.TestCase):
    DET = {"status": "dst", "n": 4000, "bins": [(-14400, 2185), (-18000, 1815)],
           "zones": ["America/New_York", "America/Detroit"]}

    def test_compare_with_archive(self):
        self.assertEqual(dt.compare_with_archive("America/New_York", self.DET), "agree")
        self.assertEqual(dt.compare_with_archive("America/Los_Angeles", self.DET), "disagree")
        self.assertIsNone(dt.compare_with_archive("America/New_York", {"status": "none"}))
        self.assertIsNone(dt.compare_with_archive("", self.DET))

    def test_archive_disagreement_says_so_and_why(self):
        text = "\n".join(dt.archive_lines(self.DET, "America/Los_Angeles"))
        self.assertIn("Disagrees with the phone", text)
        self.assertIn("downloaded on a computer set to another zone", text)
        self.assertNotIn("Handset", text)

    def test_fixed_offset_agreement_is_floored(self):
        det = {"status": "fixed", "offset_s": -18000, "n": 4000, "bins": [(-18000, 3999), (-14400, 1)],
               "zones": ["America/New_York"]}
        self.assertIn("3,999 of 4,000 sampled files agree, 99.9%", "\n".join(dt.archive_lines(det)))

    def test_acquisition_pc_differs_from_phone(self):
        settings = {"acquisition_offset_hours": "-4.0", "acquisition_dt": "2024-07-28T16:23:49"}
        text = "\n".join(dt.acquisition_pc_lines(settings, "/x/EXTRACTION_FFS.zip", "America/Los_Angeles"))
        self.assertIn("Acquisition PC offset:** UTC−04:00", text)
        self.assertIn("Different from the phone", text)
        same = "\n".join(dt.acquisition_pc_lines(settings, "/x/EXTRACTION_FFS.zip", "America/New_York"))
        self.assertIn("had the same offset", same)

    def test_acquisition_pc_not_found(self):
        self.assertIn("Not found", "\n".join(dt.acquisition_pc_lines({}, "/nonexistent/x.zip")))


def _case_dir(settings: dict):
    d = tempfile.mkdtemp()
    c = sqlite3.connect(os.path.join(d, "caseresults.db"))
    c.execute("CREATE TABLE case_settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE device_info (field_name TEXT, data TEXT, source TEXT)")
    c.executemany("INSERT INTO case_settings VALUES (?, ?)", settings.items())
    c.commit()
    c.close()
    return d


ARCHIVE = ('{"status": "fixed", "offset_s": -18000, "n": 4000, "bins": [[-18000, 4000]], '
           '"zones": ["America/New_York"], "rule": 1}')


class Parsers(unittest.TestCase):
    def _paths(self, files, case_dir, adapter):
        ctx = SimpleNamespace(case_dir=case_dir, zip_path=os.path.join(case_dir, "x.zip"),
                              get_ui_metadata=lambda: {})
        return {"_case_context": ctx, "_adapter": adapter, "_read_zip_bytes": _reader(files),
                "_parser_files_dir": case_dir}

    def test_android_report(self):
        m = _load("artifacts/android/timezone_report.py")
        self.assertEqual((m.view_mode, m.device_wide, m.name), ("document", True, "Timezone & time sources"))
        files = {"Dump/data/property/persistent_properties": _props(**{"persist.sys.timezone": "America/Los_Angeles"}),
                 "Dump/data/system/users/0/settings_global.xml": SETTINGS_ON,
                 "Dump/data/system/netpolicy.xml": NETPOLICY}
        md = m.run(self._paths(files, _case_dir({"archive_offset": ARCHIVE}), DUMP))[0]["document_markdown"]
        for want in ("**Timezone at acquisition is a snapshot.**", "## Phone timezone (handset)",
                     "America/Los_Angeles — from `Dump/data/property/persistent_properties`",
                     "**Automatic time zone:** Off", "## Acquisition PC (not the phone)",
                     "## Archive (zip DOS vs UT)", "Disagrees with the phone",
                     "Weak / incidental — not a change log"):
            self.assertIn(want, md)

    def test_ios_report(self):
        m = _load("artifacts/ios/timezone_report.py")
        files = {"filesystem1/private/var/db/timezone/localtime": b"/var/db/timezone/zoneinfo/America/New_York"}
        md = m.run(self._paths(files, _case_dir({"archive_offset": ARCHIVE}), IOS))[0]["document_markdown"]
        self.assertIn("**Phone timezone:** America/New_York", md)
        self.assertIn("**Automatic time zone:** Not determined", md)
        self.assertIn("The phone's zone (America/New_York) is one of them.", md)
        self.assertNotIn("Other weak hints", md)

    def test_device_info_rows_are_short(self):
        android = _load("artifacts/android/device_info.py")
        files = {"Dump/data/property/persistent_properties": _props(**{"persist.sys.timezone": "Europe/London"}),
                 "Dump/data/system/users/0/settings_global.xml": SETTINGS_ON,
                 "Dump/data/system/netpolicy.xml": NETPOLICY}
        rows = android.timezone_lines(_reader(files), DUMP)
        self.assertEqual(rows, [
            "- **Phone Timezone:** Europe/London — from `Dump/data/property/persistent_properties` "
            "(`persist.sys.timezone`)",
            "- **Automatic Time Zone:** Off — from `Dump/data/system/users/0/settings_global.xml` "
            "(`auto_time_zone`)",
            "- *More detail: the **Timezone & time sources** report.*"])
        ios = _load("artifacts/ios/device_info.py")
        rows = ios.timezone_lines(_reader({}), IOS)
        self.assertEqual(rows[0], "- **Phone Timezone:** Not found")
        self.assertTrue(all("Acquisition" not in r and "DOS" not in r for r in rows))


class HandsetRedetect(unittest.TestCase):
    def test_saves_zone_source_and_rule(self):
        import timestamp_display as td
        from db_utils import load_case_setting
        d = _case_dir({"handset_timezone_name": ""})
        db = sqlite3.connect(os.path.join(d, "caseresults.db"))
        orig = dt.detect_handset_zone_info
        dt.detect_handset_zone_info = lambda *a: {"zone": "America/New_York", "setting": "persist.sys.timezone",
                                                  "source": "Dump/data/property/persistent_properties"}
        try:
            zone = td.TimestampDisplayMixin._detect_and_save_handset_zone(db, "x.zip", DUMP, d)
        finally:
            dt.detect_handset_zone_info = orig
        self.assertEqual(zone, "America/New_York")
        self.assertEqual(load_case_setting(db, "handset_timezone_rule"), td.HANDSET_ZONE_RULE)
        self.assertEqual(load_case_setting(db, "handset_timezone_source"),
                         "Dump/data/property/persistent_properties (persist.sys.timezone)")


if __name__ == "__main__":
    unittest.main()
