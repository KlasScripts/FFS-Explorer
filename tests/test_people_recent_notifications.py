"""People recent-event notifications: one row per posted notification."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parser():
    path = os.path.join(ROOT, "artifacts", "android", "people_recent_notifications.py")
    spec = importlib.util.spec_from_file_location("people_recent_notifications", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        if value:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def _event(event_type: int, when: int) -> bytes:
    body = _varint(1 << 3) + _varint(event_type) + _varint(2 << 3) + _varint(when)
    return _varint(1 << 3 | 2) + _varint(len(body)) + body


def _run(files: dict[str, bytes]):
    mod = _parser()
    return mod.run({
        "_zip_names": list(files),
        "_read_zip_bytes": files.get,
    })


def test_a_second_post_within_a_second_keeps_the_later_time():
    path = (
        "Dump/data/system_ce/0/people/com.whatsapp/event/shortcut/"
        "15551234567%40s.whatsapp.net/events/recent"
    )
    rows = _run({path: _event(2, 1_700_000_000_100) + _event(2, 1_700_000_000_105)})
    assert rows == [{
        "posted_time": 1_700_000_000_105,
        "app": "com.whatsapp",
        "conversation_id": "15551234567@s.whatsapp.net",
        "source_path": path[len("Dump/"):],
        "seconds_after_first_unlock": None,
        "chats_same_second": 1,
    }]


def test_posts_a_second_apart_both_stay():
    path = "data/system_ce/0/people/com.google.android.apps.messaging/event/shortcut/1/events/recent"
    rows = _run({path: _event(2, 5_000) + _event(2, 6_500)})
    assert [row["posted_time"] for row in rows] == [5_000, 6_500]
    assert {row["conversation_id"] for row in rows} == {"1"}


def test_shortcut_use_and_a_class_share_are_absent():
    shortcut = "data/system_ce/0/people/com.whatsapp/event/shortcut/chat/events/recent"
    screen = (
        "data/system_ce/0/people/com.google.android.gm/event/class/"
        "com.google.android.gm.ComposeActivityGmailExternal/events/recent"
    )
    rows = _run({
        shortcut: _event(1, 1_000) + _event(2, 1_005),
        screen: _event(5, 9_000),
    })
    assert len(rows) == 1
    assert rows[0]["posted_time"] == 1_005
    assert rows[0]["conversation_id"] == "chat"


def _usage_proto_field(number, payload):
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _usage_event(offset, event_type):
    return _usage_proto_field(22, _usage_proto_field(1, 1) + _usage_proto_field(3, offset)
                              + _usage_proto_field(5, event_type))


def test_sweep_after_first_unlock_is_marked_not_removed():
    begin = 1_700_000_000_000
    # USER_UNLOCKED (28) at 0. A DEVICE_STARTUP (27) row is not needed:
    # one boot on the Galaxy A53 image has the unlock only.
    unlock = _usage_event(0, 28)
    mapping = _usage_proto_field(2, _usage_proto_field(1, 1) + _usage_proto_field(2, b"android"))
    base = "data/system_ce/0/people/com.google.android.apps.messaging/event/shortcut/{}/events/recent"
    files = {
        "data/system_ce/0/usagestats/mappings": mapping,
        f"data/system_ce/0/usagestats/daily/{begin}": unlock + _usage_event(5_000, 27),
        # Three chats 20 s after the unlock, inside one second: the sweep.
        base.format("10"): _event(2, begin + 20_000),
        base.format("11"): _event(2, begin + 20_300),
        base.format("12"): _event(2, begin + 20_700) + _event(2, begin + 50_000),
        # A post on its own 90 s after the unlock.
        base.format("13"): _event(2, begin + 90_000),
    }
    rows = {(r["conversation_id"], r["posted_time"] - begin): r for r in _run(files)}
    assert len(rows) == 5
    # Timed from the unlock, not from the later startup row.
    assert rows[("10", 20_000)]["seconds_after_first_unlock"] == 20.0
    assert rows[("10", 20_000)]["chats_same_second"] == 3
    # A later post in a swept chat, still inside the minute, stands alone.
    assert rows[("12", 50_000)]["seconds_after_first_unlock"] == 50.0
    assert rows[("12", 50_000)]["chats_same_second"] == 1
    assert rows[("13", 90_000)]["seconds_after_first_unlock"] is None
    assert rows[("13", 90_000)]["chats_same_second"] == 1
