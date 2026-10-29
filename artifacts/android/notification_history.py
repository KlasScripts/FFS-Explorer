"""Posted notifications from NotificationHistoryProto files.

ALEAPP's get_notificationHistory reads
data/system_ce/<user>/notification_history/history/* and the same protobuf
(notificationhistory.proto, NotificationHistoryProto). The string pool is
1-based: a package or channel stored only as an index is looked up there.
Title and text are the notification's own strings.

AOSP's NotificationHistoryDatabase.prune deletes files older than one day
when history is on. An empty folder can mean the switch was off, or that
nothing was posted inside the retention window. The filename is often the
posted time in milliseconds. That number is reported only when the whole
filename is digits.
"""

name = "Notification History"
description = (
    "Notification title, text, package, and channel from the system's "
    "notification history protobufs under system_ce and system_de. "
    "This can include message text the user was shown. History is short: "
    "Android deletes files older than one day while the feature is on. "
    "The folder is absent or empty when the feature is off or nothing was "
    "posted in that window."
)
device_wide = True
# The runner dialog lists a device-wide report on every case. This one
# only has something to read when a history protobuf is in the archive.
existence_check_paths = ("/notification_history/history/",)
timestamp_fields = {"posted_time": "ms", "file_time": "ms"}
core_fields = ["posted_time", "package", "title", "text", "channel_name", "conversation_id"]
record_source = [
    {"label": "Notification history", "ui_path_field": "source_path"},
]


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
    """Protobuf fields this history file uses: varint or length-delimited."""
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


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None:
        return ""
    return str(value)


def _grouped(data: bytes) -> dict[int, list]:
    grouped: dict[int, list] = {}
    for field, value in _fields(data):
        grouped.setdefault(field, []).append(value)
    return grouped


def _one(grouped: dict[int, list], field: int):
    values = grouped.get(field) or []
    return values[0] if values else None


def _pool(history: dict[int, list]) -> dict[int, str]:
    raw = _one(history, 1)
    if not isinstance(raw, bytes):
        return {}
    strings = []
    for field, value in _fields(raw):
        if field == 2 and isinstance(value, bytes):
            strings.append(_text(value))
    return {index + 1: text for index, text in enumerate(strings)}


def _lookup(grouped: dict[int, list], pool: dict[int, str], text_field: int, index_field: int) -> str:
    raw = _one(grouped, text_field)
    if isinstance(raw, bytes) and raw:
        return _text(raw)
    index = _one(grouped, index_field)
    if isinstance(index, int):
        return pool.get(index, "")
    return ""


def _rows_from_proto(data: bytes, source_path: str) -> list[dict]:
    history = _grouped(data)
    pool = _pool(history)
    major = _one(history, 2)
    file_name = source_path.rstrip("/").rsplit("/", 1)[-1]
    file_time = int(file_name) if file_name.isdigit() else ""
    rows = []
    for raw in history.get(3) or []:
        if not isinstance(raw, bytes):
            continue
        note = _grouped(raw)
        posted = _one(note, 9)
        rows.append({
            "posted_time": posted if isinstance(posted, int) else "",
            "title": _text(_one(note, 10)) if isinstance(_one(note, 10), bytes) else "",
            "text": _text(_one(note, 11)) if isinstance(_one(note, 11), bytes) else "",
            "package": _lookup(note, pool, 1, 2),
            "user_id": _one(note, 8) if isinstance(_one(note, 8), int) else "",
            "uid": _one(note, 7) if isinstance(_one(note, 7), int) else "",
            "channel_name": _lookup(note, pool, 3, 4),
            "channel_id": _lookup(note, pool, 5, 6),
            "conversation_id": _lookup(note, pool, 13, 14),
            "package_index": _one(note, 2) if isinstance(_one(note, 2), int) else "",
            "channel_name_index": _one(note, 4) if isinstance(_one(note, 4), int) else "",
            "channel_id_index": _one(note, 6) if isinstance(_one(note, 6), int) else "",
            "conversation_id_index": _one(note, 14) if isinstance(_one(note, 14), int) else "",
            "major_version": major if isinstance(major, int) else "",
            "source_path": source_path,
            "file_time": file_time,
        })
    return rows


def run(paths):
    read = paths.get("_read_zip_bytes")
    if read is None:
        return []
    rows = []
    for name in paths.get("_zip_names") or []:
        if str(name).endswith("/"):
            continue
        ui = _ui(name)
        if "/notification_history/history/" not in ui:
            continue
        try:
            data = read(name)
            rows.extend(_rows_from_proto(data, ui))
        except Exception:
            continue
    rows.sort(key=lambda row: (row["posted_time"] if isinstance(row["posted_time"], int) else 0, row["source_path"]))
    return rows
