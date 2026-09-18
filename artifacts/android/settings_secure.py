"""Settings Secure (Android) — a handful of well-known per-user device
identifiers from `data/system/users/<uid>/settings_secure.xml`: the
Android ID (a per-device, per-Google-account 64-bit identifier used
across apps for tracking/telemetry — a genuinely common real ask in
casework), the Bluetooth adapter's own name and MAC address, and whether
Mock Location (developer-option GPS spoofing) was enabled.

Exists specifically to exercise `app/ccl_abx.py`'s Android Binary XML
decoder — including the two real correctness fixes made to it earlier
this session (see that file's own module docstring and CLAUDE.md's
Conventions entry) — against a real shipped parser, the same "prove it
in anger" reason `artifacts/ios/wifi_known_networks.py` exists for
`decode_plist_blob`. `settings_secure.xml` is real ABX on every one of
this project's own three Android test archives (confirmed: `b'ABX\\x00'`
magic on all three, not `'<?xml'`) — and it's one of the exact files
that FAILED OUTRIGHT under this decoder before this session's own
multi-root fix (a real one-to-one link between this parser and that
fix, not a coincidence: `settings_secure.xml`'s real top-level content
is multiple sibling `<settings>`/`<setting>` elements with no single
enclosing root, exactly the shape that needs `multi_root=True`).

Modeled on ALEAPP's own `settingsSecure.py` (`get_settingsSecure`) —
same real source file, same four named settings
(`android_id`/`bluetooth_name`/`bluetooth_address`/`mock_location`) —
but reshaped into ONE ROW WITH NAMED COLUMNS rather than ALEAPP's own
long/melted (User, Name, Value) shape, matching every other parser's
own convention in this project (see e.g. `ios/device_info.py`,
`ios/wifi_known_networks.py`) rather than ALEAPP's own generic-across-
many-settings-files shape. Scoped to user 0 (the primary/default
Android user) only — ALEAPP's own `paths` glob
(`*/system/users/*/settings_secure.xml`) covers every user directory a
multi-user/work-profile device might have; this parser does not, a
real, stated limitation rather than a silently-narrower one (every
real archive this project has only ever has a user 0 to begin with, so
this has not actually cost anything checked so far, but a genuine
multi-user device would only ever show user 0's own values here).

Verified directly against all three of this project's own real Android
archives (2026-09-18), all real ABX (`b'ABX\\x00'`) format, not reasoned
through:

  - Android 14 JoshHickman (Pixel 7a): android_id `f5a980ef785579f3`,
    bluetooth_name "Pixel 7a", bluetooth_address `94:45:60:1B:98:CC` —
    matching this project's own documented ground truth
    (test_data/josh_hickman_android14/device_metadata.json)
    `bt_mac: "94:45:60:1b:98:cc"` EXACTLY (case-insensitive), a direct
    ground-truth confirmation, not just "the code ran". mock_location
    "0" (disabled).
  - Android 15 CTF25 Cellebrite (Poco X7 Pro): android_id
    `ecaf255c358efbe2`, bluetooth_name "POCO X7 Pro", bluetooth_address
    `9C:9E:D5:76:0A:FD`, mock_location "0".
  - Android 14 CTF26 Magnet (Samsung Galaxy A53): android_id
    `a16f5e80a7d6f05b`, bluetooth_name "Daniel's A53", bluetooth_address
    `8C:6A:3B:A1:2A:9A`, mock_location "0" — real, plausible device-owner
    names appearing in real Bluetooth names on two of the three
    archives ("Pixel 7a" is a stock/unmodified default name, "Daniel's
    A53" and "POCO X7 Pro" are real per-device names an owner or
    manufacturer default set), not placeholder data.

Also confirmed a real Android-version-shape difference directly rather
than assumed from ALEAPP's own handling: this project's own three real
archives (Android 14/14/15) are all ABX-format for this file. Android
10 and earlier ship it as plain XML instead (ALEAPP's own
`parse_settings_root` handles both) — this parser does too, checking
`ccl_abx.is_abx()` first exactly like `app_intelligence.py`'s existing
`packages.xml`/`runtime-permissions.xml` handling already does, rather
than assuming every real device is ABX-format."""

app_path = "data/system/users/0"
files = {
    "settings_secure": "settings_secure.xml",
}
description = (
    "Four well-known settings from data/system/users/0/settings_secure.xml "
    "-- the Android ID (a per-device, per-Google-account 64-bit identifier "
    "used across apps for tracking/telemetry), the Bluetooth adapter's own "
    "name and MAC address, and whether Mock Location (developer-option GPS "
    "spoofing) was enabled. Modeled on ALEAPP's own settingsSecure.py -- "
    "see this module's own docstring for the reshaping into named columns "
    "and the real cross-check against this project's own documented "
    "ground truth (the decoded Bluetooth MAC address matches the "
    "Android 14 JoshHickman archive's own documented bt_mac exactly). "
    "Scoped to user 0 (the primary/default Android user) only -- "
    "a genuine multi-user/work-profile device's other users are not "
    "covered here, a real stated limitation rather than a silent one. "
    "The source file is Android Binary XML (ABX) on every real archive "
    "checked (Android 14/14/15) -- decoded via app/ccl_abx.py, which "
    "checks for plain XML first (Android 10 and earlier ship it that "
    "way) before falling back to ABX, same as app_intelligence.py's "
    "existing packages.xml handling."
)
core_fields = ["android_id", "bluetooth_name", "bluetooth_address",
               "mock_location_enabled"]
hidden_fields = ["raw_ui_path"]
record_source = [
    {"label": "Settings Secure", "ui_path_field": "raw_ui_path"},
]

_WANTED = {"android_id", "bluetooth_name", "bluetooth_address", "mock_location"}


def run(paths):
    import ccl_abx
    import xml.etree.ElementTree as ET

    with open(paths["settings_secure"], "rb") as f:
        raw = f.read()

    # Plain XML first (Android 10 and earlier ship this file that way),
    # ABX otherwise -- same order app_intelligence.py's existing
    # packages.xml/runtime-permissions.xml handling already uses. Every
    # real archive this project has checked is ABX, but a device this
    # old genuinely isn't, and this doesn't cost anything to check.
    if ccl_abx.is_abx(raw):
        try:
            root = ccl_abx.abx_bytes_to_xml_root(raw)
        except Exception:
            return []
    else:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []

    found = {}
    for setting in root.iter("setting"):
        name = setting.get("name")
        if name in _WANTED:
            found[name] = setting.get("value", "")

    ui_path = f"{paths['_app_base_ui_path']}/settings_secure.xml"

    return [{
        "android_id": found.get("android_id", ""),
        "bluetooth_name": found.get("bluetooth_name", ""),
        "bluetooth_address": found.get("bluetooth_address", ""),
        "mock_location_enabled": found.get("mock_location", ""),
        "raw_ui_path": ui_path,
    }]
