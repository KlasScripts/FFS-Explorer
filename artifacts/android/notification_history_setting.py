"""Whether notification history was switched on.

Reads notification_history_enabled from
data/system/users/0/settings_secure.xml. ALEAPP's
get_notificationHistory_status reads the same key: 1 is on, 0 is off.
This report also returns a row when the key is absent. An absent key is
not the same as off. Scoped to user 0, the same limit as Settings Secure.
"""

name = "Notification History Setting"
description = (
    "The notification_history_enabled value from user 0's settings_secure.xml. "
    "1 means notification history was on. 0 means it was off. A blank stored "
    "value means the key is not in the file, which is not proof that history "
    "was off. This does not contain notification text."
)
app_path = "data/system/users/0"
files = {
    "settings_secure": "settings_secure.xml",
}
core_fields = ["user", "stored_value", "status"]
record_source = [
    {"label": "Settings Secure", "ui_path_field": "raw_ui_path"},
]


def _xml_root(raw: bytes):
    import xml.etree.ElementTree as ET

    import ccl_abx

    if ccl_abx.is_abx(raw):
        return ccl_abx.abx_bytes_to_xml_root(raw)
    return ET.fromstring(raw)


def run(paths):
    with open(paths["settings_secure"], "rb") as handle:
        raw = handle.read()
    try:
        root = _xml_root(raw)
    except Exception as exc:
        from artifact_runner import log_note
        log_note(f"{paths['settings_secure']} could not be parsed ({exc}); nothing read from it")
        return []

    stored = None
    for setting in root.iter("setting"):
        if setting.get("name") == "notification_history_enabled":
            stored = setting.get("value", "")
            break

    if stored is None:
        status = "not set"
        stored = ""
    elif stored == "1":
        status = "enabled"
    elif stored == "0":
        status = "disabled"
    else:
        status = "other"

    user = paths["_app_base_ui_path"].rstrip("/").rsplit("/", 1)[-1]
    return [{
        "user": user,
        "stored_value": stored,
        "status": status,
        "raw_ui_path": f"{paths['_app_base_ui_path']}/settings_secure.xml",
    }]
