"""App usage events from Android's usagestats store.

The files under data/system_ce/<user>/usagestats/ (and the older
data/system/usagestats/<user>/) are interval protobufs. Package and class
names are tokens. The names live in the mappings file beside them. A stored
time is an offset from the interval file's numeric name, which is the
interval start in milliseconds. A negative stored time is already an
absolute time.

The same event is written into the daily file and again into the weekly,
monthly, and yearly files. This report keeps one row, from the finest
interval that has it.

NOTIFICATION_SEEN and NOTIFICATION_INTERRUPTION record that an app's
notification was shown. They carry the package and, when stored, the
channel id. They do not carry the notification title or text.

SHORTCUT_INVOCATION stores the shortcut id (proto field 7) and
LOCUS_ID_SET stores the locus id (field 13), both as tokens in that
package's mappings list. For a messaging app that id is the chat. On
Google Messages it is conversations._id, the id Recent Message
Notifications shows. A notification row stores no chat id.
"""

name = "Usage Stats"
description = (
    "App usage events from system_ce and system usagestats: when an activity "
    "came forward, and when a notification was seen or interrupted. Package "
    "names are read from the mappings file. The same event is stored in the "
    "daily, weekly, monthly, and yearly files; one row is kept, from the "
    "finest of those. Notification rows name the app and channel, not the "
    "message. Conversation id is the shortcut id on a shortcut row and the "
    "locus id on a locus row, as stored. For Google Messages that is the "
    "conversation's id in bugle_db. A notification row has no conversation id. "
    "DEVICE_SHUTDOWN is not the moment the phone went off. On a linked lab "
    "phone (29 October 2026) it was logged about 12 minutes before a "
    "controlled shutdown. On the Galaxy A53 image it was logged at 16:43:34 "
    "on 22 December 2025 while Messages kept posting notifications in People "
    "until 16:44:24, and Usage Stats holds nothing in between. Events in "
    "the last minutes before a shutdown can be missing. Recent events are "
    "held in memory and written later, so the last minutes before seizure "
    "can also be missing on a phone that was not shut down cleanly."
)
device_wide = True
existence_check_paths = ("/usagestats/",)
timestamp_fields = {"event_time": "ms"}
core_fields = [
    "event_time",
    "package",
    "event_type",
    "conversation_id",
    "class_name",
    "notification_channel",
    "interval",
]
record_source = [
    {"label": "Usage stats", "ui_path_field": "source_path"},
]

_INTERVAL_RANK = {"daily": 0, "weekly": 1, "monthly": 2, "yearly": 3}
_EVENT_TYPES = {
    0: "NONE",
    1: "ACTIVITY_RESUMED",
    2: "ACTIVITY_PAUSED",
    3: "END_OF_DAY",
    4: "CONTINUE_PREVIOUS_DAY",
    5: "CONFIGURATION_CHANGE",
    6: "SYSTEM_INTERACTION",
    7: "USER_INTERACTION",
    8: "SHORTCUT_INVOCATION",
    9: "CHOOSER_ACTION",
    10: "NOTIFICATION_SEEN",
    11: "STANDBY_BUCKET_CHANGED",
    12: "NOTIFICATION_INTERRUPTION",
    13: "SLICE_PINNED_PRIV",
    14: "SLICE_PINNED",
    15: "SCREEN_INTERACTIVE",
    16: "SCREEN_NON_INTERACTIVE",
    17: "KEYGUARD_SHOWN",
    18: "KEYGUARD_HIDDEN",
    19: "FOREGROUND_SERVICE_START",
    20: "FOREGROUND_SERVICE_STOP",
    21: "CONTINUING_FOREGROUND_SERVICE",
    22: "ROLLOVER_FOREGROUND_SERVICE",
    23: "ACTIVITY_STOPPED",
    24: "ACTIVITY_DESTROYED",
    25: "FLUSH_TO_DISK",
    26: "DEVICE_SHUTDOWN",
    27: "DEVICE_STARTUP",
    28: "USER_UNLOCKED",
    29: "USER_STOPPED",
    30: "LOCUS_ID_SET",
    31: "APP_COMPONENT_USED",
}


def _ui(name: str) -> str:
    text = str(name).lstrip("/")
    if text.startswith("Dump/"):
        text = text[5:]
    return text


def _varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while index < len(data) and shift <= 70:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise ValueError("truncated varint")


def _fields(data: bytes) -> list[tuple[int, object]]:
    found = []
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _varint(data, index)
            found.append((field, value))
        elif wire == 2:
            length, index = _varint(data, index)
            if index + length > len(data):
                raise ValueError("truncated length")
            found.append((field, data[index:index + length]))
            index += length
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            raise ValueError(f"unsupported wire {wire}")
    return found


def _i64(value: int) -> int:
    if value >= 1 << 63:
        return value - (1 << 64)
    return value


def _store_root(ui: str) -> str:
    marker = "/usagestats/"
    if marker in ui:
        return ui.split(marker, 1)[0] + "/usagestats"
    if ui.rstrip("/").endswith("/usagestats"):
        return ui.rstrip("/")
    return ""


def _interval(ui: str, root: str) -> str:
    rest = ui[len(root):].strip("/")
    parts = rest.split("/")
    for part in parts[:-1]:
        if part in _INTERVAL_RANK:
            return part
    return ""


def _event_type(value: int) -> str:
    return _EVENT_TYPES.get(value, str(value))


def _token_text(packages: dict[int, list[str]], package_token: int, string_token: int = 0) -> str:
    """Package name when string_token is 0. Later tokens are 1-based indexes."""
    strings = packages.get(package_token) or []
    if not strings:
        return ""
    if not string_token:
        return strings[0]
    index = string_token - 1
    if 0 <= index < len(strings):
        return strings[index]
    return ""


def _extra_text(packages: dict[int, list[str]], package_token: int, string_token: int) -> str:
    if not string_token:
        return ""
    return _token_text(packages, package_token, string_token)


def _mappings(data: bytes) -> dict[int, list[str]]:
    packages: dict[int, list[str]] = {}
    for field, value in _fields(data):
        if field != 2 or not isinstance(value, bytes):
            continue
        token = None
        strings = []
        for sub, raw in _fields(value):
            if sub == 1 and isinstance(raw, int):
                token = raw
            elif sub == 2 and isinstance(raw, bytes):
                strings.append(raw.decode("utf-8", "replace"))
        if token is not None:
            packages[token] = strings
    return packages


def _event_time(stored: int, interval_begin: int) -> int:
    signed = _i64(stored)
    if signed < 0:
        return abs(signed)
    return interval_begin + signed


def _events_from_proto(data: bytes, interval_begin: int, packages: dict[int, list[str]]) -> list[dict]:
    rows = []
    for field, value in _fields(data):
        if field != 22 or not isinstance(value, bytes):
            continue
        event = {sub: raw for sub, raw in _fields(value) if isinstance(raw, int)}
        if 3 not in event:
            continue
        package_token = event.get(1, 0)
        conversation = (_extra_text(packages, package_token, event.get(7, 0))
                        or _extra_text(packages, package_token, event.get(13, 0)))
        rows.append({
            "event_time": _event_time(event[3], interval_begin),
            "package": _token_text(packages, package_token),
            "event_type": _event_type(event.get(5, 0)),
            "conversation_id": conversation,
            "class_name": _extra_text(packages, package_token, event.get(2, 0)),
            "notification_channel": _extra_text(
                packages, package_token, event.get(9, 0)),
        })
    return rows


def _events_from_xml(raw: bytes, interval_begin: int) -> list[dict]:
    import xml.etree.ElementTree as ET

    import ccl_abx

    root = ccl_abx.abx_bytes_to_xml_root(raw) if ccl_abx.is_abx(raw) else ET.fromstring(raw)
    rows = []
    for node in root.iter("event-log"):
        for event in list(node):
            stored = event.get("time")
            if stored is None:
                continue
            rows.append({
                "event_time": _event_time(int(stored), interval_begin),
                "package": event.get("package") or "",
                "event_type": _event_type(int(event.get("type") or 0)),
                "conversation_id": event.get("shortcutId") or "",
                "class_name": event.get("class") or "",
                "notification_channel": event.get("notificationChannel") or "",
            })
    return rows


def _read_events(raw: bytes, interval_begin: int, packages: dict[int, list[str]]) -> list[dict]:
    if raw.startswith(b"<") or raw.startswith(b"ABX\x00"):
        return _events_from_xml(raw, interval_begin)
    return _events_from_proto(raw, interval_begin, packages)


def _user(root: str) -> str:
    parts = root.split("/")
    if "system_ce" in parts:
        index = parts.index("system_ce")
        if index + 1 < len(parts):
            return parts[index + 1]
    if parts[-1:] == ["usagestats"] and len(parts) >= 2 and parts[-2].isdigit():
        return parts[-2]
    return ""


def run(paths):
    read = paths.get("_read_zip_bytes")
    names = paths.get("_zip_names") or []
    if read is None:
        return []

    grouped: dict[str, list[str]] = {}
    ui_by_name = {}
    for name in names:
        if str(name).endswith("/"):
            continue
        ui = _ui(name)
        root = _store_root(ui)
        if not root:
            continue
        ui_by_name[name] = ui
        grouped.setdefault(root, []).append(name)

    best: dict[tuple, dict] = {}
    for root, members in grouped.items():
        packages: dict[int, list[str]] = {}
        for name in members:
            ui = ui_by_name[name]
            if ui.rsplit("/", 1)[-1] != "mappings":
                continue
            try:
                packages = _mappings(read(name))
            except Exception:
                packages = {}
            break
        user = _user(root)
        for name in members:
            ui = ui_by_name[name]
            file_name = ui.rstrip("/").rsplit("/", 1)[-1]
            if not file_name.isdigit():
                continue
            interval = _interval(ui, root)
            try:
                events = _read_events(read(name), int(file_name), packages)
            except Exception:
                continue
            rank = _INTERVAL_RANK.get(interval, 9)
            for event in events:
                row = {
                    **event,
                    "interval": interval,
                    "user": user,
                    "source_path": ui,
                }
                key = (
                    row["event_time"],
                    row["package"],
                    row["event_type"],
                    row["conversation_id"],
                    row["class_name"],
                    row["notification_channel"],
                    row["user"],
                )
                current = best.get(key)
                if current is None or rank < current["_rank"]:
                    row["_rank"] = rank
                    best[key] = row

    rows = list(best.values())
    for row in rows:
        row.pop("_rank", None)
    rows.sort(key=lambda row: (
        row["event_time"] if isinstance(row["event_time"], int) else 0,
        row["package"],
        row["event_type"],
    ))
    return rows
