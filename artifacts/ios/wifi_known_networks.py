"""WiFi Known Networks (iOS) — every network the device has joined, from
`private/var/preferences/com.apple.wifi.known-networks.plist`: SSID, BSSID,
security type, captive-portal/CarPlay flags, and (real forensic value) five
separate timestamps per network — when it was first added, last updated,
last joined by the user, last joined automatically by the system, and last
seen during a scan — none of which a live "Settings > Wi-Fi" screen shows.

Modeled on iLEAPP's own `appleWifiPlist.py` (`appleWifiKnownNetworks`
processor) — same real source file, same core field set (SSID/BSSID/
networkUsage/CarPlayNetwork/Hidden/CaptiveNetwork/AddReason/BundleID) — but
NOT a line-for-line port. Two deliberate differences, both checked against
this project's own real data before choosing them, not assumed:

  1. iLEAPP's own `appleWifiKnownNetworks` handles TWO different real plist
     shapes with one function — the legacy `'List of known networks'` LIST
     shape (older `com.apple.wifi.plist`) and the newer dict-of-dicts shape
     keyed by `'wifi.network.ssid.<name>'` (`com.apple.wifi.known-
     networks.plist`). This parser targets ONLY the newer file/shape —
     confirmed directly against this project's own real IOS17 JoshHickman
     archive that `com.apple.wifi.plist` on iOS 17.3 carries NO
     'List of known networks' key at all (checked: its real top-level keys
     are all device-wide WiFi *settings*, e.g. `WiFiMacRandomizationInternalUI`
     — nothing per-network), so replicating that branch here would be
     dead code on this project's own real test data, not a real second
     data source.
  2. This project's own declarative `timestamp_fields` lets the Report
     table format a timestamp per the case's UTC/handset/acquisition
     setting — iLEAPP has no equivalent (it puts the five timestamps in a
     SEPARATE `appleWifiKnownNetworksTimes` report instead, since its own
     output model has no per-column unit declaration). Here they're columns
     on the same row as the network's own identity, using this project's
     own convention instead of iLEAPP's split.

Exists specifically to exercise `artifact_runner.decode_plist_blob` against
a REAL, WHOLE plist FILE in a shipped parser — every previous caller
(`artifacts/ios/instagram.py`) only ever fed it a plist BLOB out of a SQL
column. Verified directly against this project's own real IOS17
JoshHickman archive (2026-09-18): the real file is `bplist00` (binary)
format, decodes to exactly 17 real network entries — matching iLEAPP's OWN
documented `sample_data` count for its `iphone11_ios17` test archive
(same iPhone 11 / iOS 17.3 device this project's own test data uses)
exactly, a real cross-tool number check, not just "the code ran". Real
SSIDs present include "Matt_Foley", "Hilton Garden Inn Guest", and
"DNCR-Aquarium_Visitor" — plausible, real-looking travel/hotel/venue
networks for a real device, not placeholder data. All five timestamp
fields decoded correctly as real `datetime.datetime` values (Apple's
CFDate format, UTC by construction — see `_to_unix` below for the exact
conversion and why it's correct); none of the 17 real entries hit the
CFDate-out-of-range case `decode_plist_blob`'s own tolerant fallback
exists for (see that function's docstring for a real example elsewhere on
this same archive that DOES hit it: `com.apple.sleepd.plist`) — this
report's own happy path and that fallback's path are both real, both now
covered by real shipped code, just not by the SAME file.

Known, deliberate scope limits, stated rather than silently dropped:
`BSSList` (a per-network array of every physical access point/BSSID seen
under that SSID, matching iLEAPP's own separate `appleWifiBSSList`
report) is summarized here as a plain count only (`known_bss_count`), not
exploded into its own per-BSSID report — real content, left out on
purpose to keep this parser's own scope to "one row per known network".
`Moving` (a real key seen on every entry checked) is NOT surfaced at all —
its actual forensic meaning wasn't confirmed against any authoritative
source, and this project's own standing rule is to never present a field
whose meaning isn't actually verified."""

# The real on-device path is /private/var/preferences/... — "preferences"
# here (not "private/var/preferences") is deliberate, not a typo: this
# project's own ui_path convention already implies the private/var/
# prefix for an "old_layout" archive (adapter.resolve adds it internally
# — see app/adapters/ffs.py's own resolve()). Confirmed directly against
# this project's own real IOS17 JoshHickman archive rather than assumed
# from sms_messages.py's similar-looking app_path (a genuinely different
# case: mobile/Library/SMS lives under mobile/, not private/var/,
# so its own app_path never needed this prefix question at all).
app_path = "preferences"
files = {
    "known_networks": "com.apple.wifi.known-networks.plist",
}
description = (
    "Every WiFi network this device has joined, from private/var/"
    "preferences/com.apple.wifi.known-networks.plist -- SSID, BSSID, "
    "security type, CarPlay/captive-portal flags, and five separate real "
    "timestamps per network (added/updated/joined-by-user/joined-by-"
    "system/last-discovered), none of which a live Settings > Wi-Fi "
    "screen shows. Modeled on iLEAPP's own appleWifiPlist.py -- see this "
    "module's own docstring for the two deliberate differences and what "
    "was checked against this project's own real test data before "
    "choosing them. known_bss_count is a plain count of that network's "
    "own BSSList (every physical access point seen under that SSID) -- "
    "the individual BSSIDs themselves are NOT broken out into their own "
    "rows here (iLEAPP does this as a separate report); left out on "
    "purpose to keep this parser's scope to one row per known network, "
    "not silently dropped without a count at all."
)
timestamp_fields = {
    "added_at": "s",
    "updated_at": "s",
    "joined_by_user_at": "s",
    "joined_by_system_at": "s",
    "last_discovered_at": "s",
}
byte_fields = ["network_usage_bytes"]
core_fields = ["ssid", "bssid", "joined_by_user_at", "last_discovered_at",
               "hidden", "add_reason"]
hidden_fields = ["raw_ui_path"]
record_source = [
    {"label": "WiFi Known Network", "ui_path_field": "raw_ui_path"},
]


def _to_unix(value):
    """A CFDate decodes (via plistlib, see decode_plist_blob) to a naive
    datetime.datetime whose WALL-CLOCK VALUE already represents the
    correct UTC moment -- CFDate has no timezone concept of its own, it's
    always seconds-since-2001-01-01-UTC by definition, and plistlib's own
    binary parser computes `datetime(2001,1,1) + timedelta(seconds=f)`
    with no timezone adjustment. Attaching tzinfo=UTC (never
    datetime.timestamp() on a still-naive value, which would silently use
    the ANALYSIS MACHINE's own local zone -- exactly the bug class
    WRITING_ARTIFACT_PARSERS.md's own timestamp_fields section warns
    against) before calling .timestamp() is what actually makes this
    conversion correct, not just convenient. Returns None (not 0 or '')
    for anything that isn't a real datetime -- a missing field should
    render as blank, never as the Unix epoch."""
    import datetime
    if not isinstance(value, datetime.datetime):
        return None
    return value.replace(tzinfo=datetime.timezone.utc).timestamp()


def _decode_ssid(value):
    """SSID is stored as raw bytes (confirmed on this project's own real
    archive) -- UTF-8 is the overwhelmingly common real case, hex is the
    honest fallback for anything else rather than a decode exception or
    a silently mangled string."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value if isinstance(value, str) else ""


def run(paths):
    from artifact_runner import decode_plist_blob

    with open(paths["known_networks"], "rb") as f:
        raw = f.read()
    plist = decode_plist_blob(raw)
    if not isinstance(plist, dict):
        return []

    ui_path = f"{paths['_app_base_ui_path']}/com.apple.wifi.known-networks.plist"

    rows = []
    for network_key, net in plist.items():
        if not isinstance(net, dict):
            continue
        os_specific = net.get("__OSSpecific__") or {}
        captive     = net.get("CaptiveProfile") or {}
        bss_list    = net.get("BSSList") or []

        rows.append({
            "ssid": _decode_ssid(net.get("SSID")),
            "bssid": os_specific.get("BSSID", ""),
            "security_type": net.get("SupportedSecurityTypes", ""),
            "hidden": net.get("Hidden", ""),
            "add_reason": net.get("AddReason", ""),
            "bundle_id": net.get("BundleID", ""),
            "carplay_network": os_specific.get(
                "CarPlayNetwork", net.get("CARPLAY_NETWORK", "")),
            "captive_network": captive.get("CaptiveNetwork", ""),
            "network_usage_bytes": os_specific.get("networkUsage", ""),
            "known_bss_count": len(bss_list) if isinstance(bss_list, list) else 0,
            "added_at": _to_unix(net.get("AddedAt")),
            "updated_at": _to_unix(net.get("UpdatedAt")),
            "joined_by_user_at": _to_unix(net.get("JoinedByUserAt")),
            "joined_by_system_at": _to_unix(net.get("JoinedBySystemAt")),
            "last_discovered_at": _to_unix(net.get("LastDiscoveredAt")),
            "network_key": network_key,
            "raw_ui_path": ui_path,
        })
    return rows
