"""Files by Google's screen-session log (files/activityTimeLogStoreName.pb).

A protobuf: repeated field 1 = one session {1: start, 2: end}, each a
google.protobuf.Timestamp {1: seconds, 2: nanos}; field 2 = the app version.
In the lab (Files 1.13157, 2026-11-13) each entry was one screen visit, and
the boundaries matched the action log and Usage Stats' activity events. No
entry names a file. The Poco X7 Pro image (Files 1.8662) held seven entries,
all on one day, so older entries may be dropped.
"""

name = "App Sessions"
app_group_label = "Google Files"
group_sort_key = 6
description = (
    "Start and end of each screen visit in Files by Google, from its session "
    "log. A session shows the app was in the foreground, not which file was "
    "shown; line it up with Opened Files to see what was opened in it."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {"sessions": "files/activityTimeLogStoreName.pb"}
timestamp_fields = {"start": "ms", "end": "ms"}
timeline_fields = {
    "start": {
        "meaning": "Files by Google screen visit started (app in the foreground)",
        "app_name": "Files by Google", "event_name": "Session start",
        "detail": ["duration_s"],
        "caution": "Does not say which file was shown.",
    },
    "end": {
        "meaning": "Files by Google screen visit ended",
        "app_name": "Files by Google", "event_name": "Session end",
        "detail": ["duration_s"],
        "caution": "Does not say which file was shown.",
    },
}
core_fields = ["start", "end", "duration_s"]
record_source = [{"label": "Session entry", "ui_path_field": "raw_ui_path"}]
hidden_fields = ["raw_ui_path", "raw_offset", "raw_length"]


def _varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while i < len(data) and shift <= 70:
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    raise ValueError("truncated varint")


def fields(data: bytes, base: int = 0) -> list[tuple[int, object, int, int]]:
    """(field, value, offset, length) for each top-level field."""
    out = []
    i = 0
    while i < len(data):
        start = i
        key, i = _varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            length, i = _varint(data, i)
            if i + length > len(data):
                raise ValueError("truncated length")
            value = data[i:i + length]
            i += length
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((field, value, base + start, i - start))
    return out


def _stamp(raw) -> int | None:
    if not isinstance(raw, bytes):
        return None
    parts = {f: v for f, v, _, _ in fields(raw)}
    seconds = parts.get(1)
    if not isinstance(seconds, int):
        return None
    nanos = parts.get(2) if isinstance(parts.get(2), int) else 0
    return seconds * 1000 + nanos // 1_000_000


def run(paths):
    from artifact_runner import log_note

    with open(paths["sessions"], "rb") as f:
        data = f.read()
    ui = (paths.get("_ui_paths") or {}).get("sessions", "")
    rows = []
    version = ""
    try:
        top = fields(data)
    except ValueError as exc:
        log_note(f"activityTimeLogStoreName.pb: not decoded ({exc})")
        return []
    for field, value, offset, length in top:
        if field == 2 and isinstance(value, bytes):
            version = value.decode("utf-8", "replace")
    for field, value, offset, length in top:
        if field != 1 or not isinstance(value, bytes):
            continue
        try:
            inner = {f: v for f, v, _, _ in fields(value)}
            start, end = _stamp(inner.get(1)), _stamp(inner.get(2))
        except ValueError as exc:
            log_note(f"activityTimeLogStoreName.pb: entry at byte {offset} not decoded ({exc})")
            continue
        rows.append({
            "start": start,
            "end": end,
            "duration_s": round((end - start) / 1000, 3) if start is not None and end is not None else None,
            "app_version": version,
            "raw_ui_path": ui,
            "raw_offset": offset,
            "raw_length": length,
        })
    return rows
