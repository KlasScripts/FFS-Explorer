"""Snoozed notifications from notification_policy.xml.

ALEAPP's get_notificationHistory_snoozed reads the same file. Each child
of snoozed-notifications is one row. time is when the notification is due
to come back. key is the platform's own notification key.
"""

name = "Snoozed Notifications"
description = (
    "Notifications the user snoozed, from data/system/notification_policy.xml. "
    "The reminder time is when the phone was going to show the notification "
    "again. The key is the platform notification key, not a message id. "
    "A notification that was not snoozed is not in this file."
)
app_path = "data/system"
files = {
    "notification_policy": "notification_policy.xml",
}
timestamp_fields = {"reminder_time": "ms"}
core_fields = ["reminder_time", "notification_key", "element"]
record_source = [
    {"label": "Notification policy", "ui_path_field": "raw_ui_path"},
]


def _xml_root(raw: bytes):
    import xml.etree.ElementTree as ET

    import ccl_abx

    if ccl_abx.is_abx(raw):
        return ccl_abx.abx_bytes_to_xml_root(raw)
    return ET.fromstring(raw)


def _ms(value):
    if value in (None, ""):
        return ""
    try:
        return int(value)
    except (TypeError, ValueError):
        return ""


def run(paths):
    with open(paths["notification_policy"], "rb") as handle:
        raw = handle.read()
    try:
        root = _xml_root(raw)
    except Exception as exc:
        from artifact_runner import log_note
        log_note(f"{paths['notification_policy']} could not be parsed ({exc}); nothing read from it")
        return []

    ui_path = f"{paths['_app_base_ui_path']}/notification_policy.xml"
    rows = []
    for parent in root.iter("snoozed-notifications"):
        for child in list(parent):
            other = "; ".join(
                f"{name}={value}"
                for name, value in sorted(child.attrib.items())
                if name not in {"time", "key"}
            )
            rows.append({
                "reminder_time": _ms(child.get("time")),
                "notification_key": child.get("key", ""),
                "element": child.tag,
                "other_attributes": other,
                "raw_ui_path": ui_path,
            })
    return rows
