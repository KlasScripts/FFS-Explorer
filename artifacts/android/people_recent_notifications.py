"""Posted conversation notifications from the People recent-event files.

People stores one event file per conversation shortcut:

    data/system_ce/<user>/people/<package>/event/shortcut/<id>/events/recent

PeopleEventProto field 1 is the type, field 2 is the time in Unix
milliseconds. Type 2 is TYPE_NOTIFICATION_POSTED, written from
NotificationListener.onNotificationPosted with StatusBarNotification
getPostTime(). An update of the same notification is a second type 2 a
few milliseconds later. This report keeps the later time when two posts
for the same conversation are under one second apart.

Shortcut-used, in-app, and class-share events are different types and
different folders. They are not rows here.

Two markers help with the sweep an app does after the phone starts, when
it posts again for every chat it still has a notification for. Nothing is
removed: a message held while the phone was off can arrive in that same
minute. seconds_after_first_unlock is the time since the last
USER_UNLOCKED in Usage Stats, the first unlock after a boot, when that is
a minute or less. USER_UNLOCKED is used, not DEVICE_STARTUP: one boot on
the Galaxy A53 image has the unlock and no startup row. chats_same_second
is how many different chats of that app were posted within a second of
the row.
"""

from urllib.parse import unquote

name = "Recent Message Notifications (Experimental)"
warning = (
    "Experimental, lead only. A row is a notification Android posted for "
    "a chat. It is not evidence that a message arrived. Most rows on the "
    "images checked were sweeps, an app posting again for many chats at "
    "once. A single row with no stored message is at most a tentative "
    "lead: a reaction posts the same way, and some apps notify for "
    "messages they never store. Current Android keeps these events for "
    "about four hours. Use a row only together with other evidence, such "
    "as Usage Stats and the app's own messages and reactions. Tested on "
    "two deleted one-to-one messages in one app."
)
description = (
    "Notifications Android posted for a conversation, from the People "
    "recent-event files. One row is one incoming notification. A second "
    "post under a second later is the same notification updated, and the "
    "later time is kept. Opening the chat, and a share to a screen, are "
    "not rows. The file is short-lived: current Android drops events "
    "older than about four hours. Seconds After First Unlock and Chats "
    "Same Second mark the sweep after a boot. They do not remove the row."
)
device_wide = True
existence_check_paths = ("/events/recent",)
timestamp_fields = {"posted_time": "ms"}
core_fields = ["posted_time", "app", "conversation_id",
               "seconds_after_first_unlock", "chats_same_second"]
record_source = [
    {"label": "People recent events", "ui_path_field": "source_path"},
]

_POSTED = 2
_MERGE_MS = 1000
_UNLOCK_MS = 60_000
_SAME_SECOND_MS = 1000


def _ui(name: str) -> str:
    text = str(name).replace("\\", "/").lstrip("/")
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


def _conversation(ui: str) -> tuple[str, str] | None:
    """Package and conversation id from a shortcut or locus recent file."""
    marker = "/people/"
    pos = ui.find(marker)
    if pos < 0 or not ui.endswith("/events/recent"):
        return None
    parts = ui[pos + len(marker):].split("/")
    if len(parts) != 6 or parts[1] != "event" or parts[4:] != ["events", "recent"]:
        return None
    if parts[2] not in ("shortcut", "locus"):
        return None
    app = parts[0]
    conversation_id = unquote(parts[3])
    if not app or not conversation_id:
        return None
    return app, conversation_id


def _posted(data: bytes, app: str, conversation_id: str, source_path: str) -> list[dict]:
    rows = []
    for field, value in _fields(data):
        if field != 1 or not isinstance(value, (bytes, bytearray)):
            continue
        event_type = None
        posted = None
        for inner, inner_value in _fields(value):
            if inner == 1 and isinstance(inner_value, int):
                event_type = inner_value
            elif inner == 2 and isinstance(inner_value, int):
                posted = inner_value
        if event_type == _POSTED and posted:
            rows.append({
                "posted_time": posted,
                "app": app,
                "conversation_id": conversation_id,
                "source_path": source_path,
            })
    return rows


def _collapse(rows: list[dict]) -> list[dict]:
    """Keep the later post when the same conversation is posted again within a second."""
    rows.sort(key=lambda row: (row["app"], row["conversation_id"], row["posted_time"]))
    kept: list[dict] = []
    for row in rows:
        previous = kept[-1] if kept else None
        if (
            previous
            and previous["app"] == row["app"]
            and previous["conversation_id"] == row["conversation_id"]
            and row["posted_time"] - previous["posted_time"] < _MERGE_MS
        ):
            kept[-1] = row
        else:
            kept.append(row)
    kept.sort(key=lambda row: (row["posted_time"], row["app"], row["conversation_id"]))
    return kept


def _usage_stats():
    import importlib.util
    import sys
    from pathlib import Path
    key = "ffs_android_usagestats"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name("usagestats.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


def _first_unlocks(paths) -> list[int]:
    """USER_UNLOCKED times (ms) from Usage Stats in the same extraction."""
    try:
        events = _usage_stats().run(paths)
    except Exception:
        return []
    return sorted(
        row["event_time"] for row in events
        if row.get("event_type") == "USER_UNLOCKED" and isinstance(row.get("event_time"), int)
    )


def _mark(rows: list[dict], unlocks: list[int]) -> list[dict]:
    import bisect

    by_app: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        by_app.setdefault(row["app"], []).append((row["posted_time"], row["conversation_id"]))
    for posts in by_app.values():
        posts.sort()
    for row in rows:
        when = row["posted_time"]
        index = bisect.bisect_right(unlocks, when) - 1
        gap = when - unlocks[index] if index >= 0 else None
        row["seconds_after_first_unlock"] = (
            round(gap / 1000, 1) if gap is not None and gap <= _UNLOCK_MS else None)
        posts = by_app[row["app"]]
        lo = bisect.bisect_left(posts, (when - _SAME_SECOND_MS, ""))
        hi = bisect.bisect_right(posts, (when + _SAME_SECOND_MS, "\U0010ffff"))
        row["chats_same_second"] = len({chat for _, chat in posts[lo:hi]})
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
        found = _conversation(ui)
        if found is None:
            continue
        app, conversation_id = found
        try:
            data = read(name)
        except Exception:
            continue
        if not data:
            continue
        try:
            rows.extend(_posted(data, app, conversation_id, ui))
        except ValueError:
            continue
    rows = _collapse(rows)
    return _mark(rows, _first_unlocks(paths)) if rows else rows
