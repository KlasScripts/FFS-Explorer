"""ios_system_check: signs of jailbreaking or sideloading, and how much of the
iOS system partition an extraction holds (ASSUMPTIONS.md A-83).

None of the test phones is jailbroken (on IOS17, iOS16.5 CTF23 and IOS18
CTF25 the check finds nothing, and /etc/hosts is the stock file), so the
signs are tested on synthetic entry lists in each archive layout."""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import ios_system_check as isc  # noqa: E402
from adapters.ffs import FfsAdapter  # noqa: E402

STOCK_HOSTS = (b"##\n# Host Database\n#\n# localhost is used to configure the loopback interface\n"
               b"# when the system is booting.  Do not change this entry.\n##\n"
               b"127.0.0.1\tlocalhost\n255.255.255.255\tbroadcasthost\n::1             localhost\n")

OLD = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, "filesystem1", "filesystem1", True)
NEW = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, "filesystem2", "filesystem1", False)
GK = FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, "private/var", "", False)


def _stock(adapter):
    """A small stock iPhone: system folders, a user file, /etc/hosts."""
    if adapter is OLD:
        return ["filesystem1/System/Library/CoreServices/SystemVersion.plist",
                "filesystem1/Applications/MobileSMS.app/Info.plist", "filesystem1/usr/lib/dyld",
                "filesystem1/private/etc/hosts", "filesystem1/private/var/mobile/Library/SMS/sms.db",
                "Log.txt", "extra/KeychainDump/backup_keychain_v2.plist"]
    if adapter is NEW:
        return ["filesystem1/System/Library/CoreServices/SystemVersion.plist",
                "filesystem1/Applications/MobileSMS.app/Info.plist", "filesystem1/usr/lib/dyld",
                "filesystem1/private/etc/hosts", "filesystem2/mobile/Library/SMS/sms.db",
                "metadata2/metadata.msgpack", "Log.txt"]
    return ["/private/var/mobile/Library/SMS/sms.db", "/Applications/MobileSMS.app/Info.plist"]


def _run(adapter, names, hosts=STOCK_HOSTS):
    return isc.check(names, adapter, lambda n: hosts)


def _paths(result):
    return [h["path"] for h in result["signs"]]


def test_stock_phones_show_nothing_in_every_layout():
    for adapter in (OLD, NEW, GK):
        r = _run(adapter, _stock(adapter))
        assert r["signs"] == [] and not isc.has_findings(r)
    assert isc.coverage_label(_run(OLD, _stock(OLD))) == "full"
    assert _run(OLD, _stock(OLD))["hosts"]["extra_lines"] == []
    gk = _run(GK, _stock(GK))
    assert gk["hosts"] is None and isc.coverage_label(gk) == "partial"


def test_signs_in_both_partitions_and_every_layout():
    cases = {
        OLD: ["filesystem1/Applications/Cydia.app/Info.plist",                 # system partition
              "filesystem1/private/var/jb/usr/bin/apt",                        # user partition
              "filesystem1/private/preboot/ABC123/jb-XYZ/procursus/usr/bin/x"],
        NEW: ["filesystem1/Applications/Cydia.app/Info.plist",
              "filesystem2/jb/usr/bin/apt",                                    # = /private/var/jb
              "filesystem1/private/preboot/ABC123/jb-XYZ/procursus/usr/bin/x"],
        GK:  ["/Applications/Cydia.app/Info.plist", "/private/var/jb/usr/bin/apt",
              "/private/preboot/ABC123/jb-XYZ/procursus/usr/bin/x"],
    }
    for adapter, extra in cases.items():
        r = _run(adapter, _stock(adapter) + extra)
        assert _paths(r) == ["/Applications/Cydia.app", "/private/var/jb", "/private/preboot/*/jb-*"], adapter.format
        assert r["signs"][0]["example"] == extra[0]


def test_only_the_named_path_or_what_is_under_it():
    near_misses = ["filesystem1/Applications/Cydia.appx/Info.plist",       # another name
                   "filesystem1/private/var/mobile/jb/file",               # jb elsewhere
                   "filesystem1/private/var/containers/Bundle/Application/G/NotTrollStore.app/x",
                   "filesystem1/usr/bin/sshd-keygen-wrapper"]               # stock iOS file, not usr/sbin/sshd
    assert _run(OLD, _stock(OLD) + near_misses)["signs"] == []
    hit = _run(OLD, _stock(OLD) + ["filesystem1/private/var/containers/Bundle/Application/G/TrollStore.app/x"])
    assert _paths(hit) == ["/private/var/containers/Bundle/Application/*/TrollStore.app"]


def test_a_folder_counts_once_with_its_entries():
    r = _run(OLD, _stock(OLD) + ["filesystem1/Library/MobileSubstrate/",
                                 "filesystem1/Library/MobileSubstrate/MobileSubstrate.dylib",
                                 "filesystem1/Library/MobileSubstrate/DynamicLibraries/Veency.plist"])
    (h,) = r["signs"]
    assert h["entries"] == 3 and h["example"] == "filesystem1/Library/MobileSubstrate/"


def test_hosts_lines_a_stock_phone_does_not_have():
    changed = STOCK_HOSTS + b"0.0.0.0 ocsp.apple.com\n127.0.0.1 localhost   # comment\n"
    r = _run(OLD, _stock(OLD), hosts=changed)
    assert r["hosts"]["extra_lines"] == ["0.0.0.0 ocsp.apple.com"]
    assert isc.has_findings(r)
    assert "ocsp.apple.com" in isc.notice_html(r)


def test_not_ios_is_not_checked():
    android = FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, "data", "", False)
    assert isc.check(["/data/data/com.whatsapp/databases/msgstore.db"], android, lambda n: b"") is None


def test_notice_says_what_is_left_out_and_how_much_could_be_checked():
    full = isc.notice_html(_run(OLD, _stock(OLD)))
    assert "4 entries in this extraction belong to iOS itself" in full
    assert "None found" in full and "stock file" in full and "does not prove" in full
    partial = isc.notice_html(_run(GK, _stock(GK)))
    assert "only part" in partial and "<code>/usr</code>" in partial and "not in it" in partial


def test_saved_once_and_read_back(tmp_path):
    db = sqlite3.connect(tmp_path / "caseresults.db")
    db.execute("CREATE TABLE run_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_type TEXT NOT NULL, "
               "run_at TEXT, completed_at TEXT, total INTEGER, processed INTEGER, output_rows INTEGER, "
               "complete INTEGER NOT NULL DEFAULT 0, notes TEXT, parser_version INTEGER, "
               "coverage_fingerprint INTEGER)")
    assert isc.load(db) is None
    r = _run(OLD, _stock(OLD) + ["filesystem1/Applications/Cydia.app/Info.plist"])
    isc.save(db, r)
    assert isc.load(db) == r
    (run_type, out), = db.execute("SELECT run_type, output_rows FROM run_log").fetchall()
    assert run_type == isc.RUN_TYPE and out == 1
