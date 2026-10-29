"""Epoch format guessing and the saved-format store for the Database tab."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import epoch_guess  # noqa: E402
import epoch_store  # noqa: E402

UPPER = 1793300000   # 29 October 2026


def test_each_format_is_recognised():
    cases = {
        "s": [1793265311, 1720000000],
        "ms": [1793265311042, 1720000000000],
        "us": [1793265311042000],
        "ns": [1793265311042000000],
        "cocoa_s": [783000000.5, 700000000],
        "cocoa_ns": [783000000000000000],
        "webkit_us": [13372000000000000, 13360000000000000],
        "filetime": [133720000000000000],
    }
    for code, values in cases.items():
        assert epoch_guess.guess(values, UPPER) == code, code


def test_ids_flags_and_phone_numbers_are_not_times():
    assert epoch_guess.guess([1, 2, 3, 9418], UPPER) is None
    assert epoch_guess.guess([0, 1, 0, 1], UPPER) is None
    assert epoch_guess.guess([447700900123], UPPER) is None
    assert epoch_guess.guess(["hello", None], UPPER) is None


def test_zero_and_text_digits():
    assert epoch_guess.guess([0, 0, 1793265311042], UPPER) == "ms"
    assert epoch_guess.guess(["1793265311042"], UPPER) == "ms"
    assert round(epoch_guess.to_unix(1793265311042, "ms")) == 1793265311


def test_store_layers_and_ios_bundle_paths(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin.json"
    builtin.write_text(json.dumps({"formats": [
        {"path": "com.whatsapp/databases/msgstore.db", "table": "message",
         "column": "timestamp", "format": "ms"},
        {"path": "group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
         "table": "ZWAMESSAGE", "column": "ZMESSAGEDATE", "format": "cocoa_s"},
    ]}), encoding="utf-8")
    user = tmp_path / "user.json"
    monkeypatch.setattr(epoch_store, "builtin_path", lambda: str(builtin))
    monkeypatch.setattr(epoch_store, "user_path", lambda: str(user))

    android = "Dump/data/user/0/com.whatsapp/databases/msgstore.db"
    assert epoch_store.normalize_path(android) == "data/data/com.whatsapp/databases/msgstore.db"
    assert epoch_store.lookup(android, "message", "timestamp") == ("ms", "built-in")
    assert epoch_store.lookup(android, "message", "other") == (None, None)

    # The canonical iOS path carries the group id, not the container GUID.
    ios = "mobile/Containers/Shared/AppGroup/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
    assert epoch_store.lookup(ios, "ZWAMESSAGE", "ZMESSAGEDATE") == ("cocoa_s", "built-in")

    epoch_store.save_user(android, "message", {"timestamp": "", "sort_id": "s"})
    assert epoch_store.lookup(android, "message", "timestamp") == ("", "yours")
    assert epoch_store.lookup(android, "message", "sort_id") == ("s", "yours")
    assert epoch_store.builtin(android, "message", "timestamp") == "ms"

    epoch_store.save_user(android, "message", {"timestamp": None, "sort_id": None})
    assert epoch_store.lookup(android, "message", "timestamp") == ("ms", "built-in")
    assert json.loads(user.read_text())["formats"] == []


def test_shipped_formats_use_known_codes():
    doc = json.load(open(os.path.join(ROOT, "config", "epoch_formats.json"), encoding="utf-8"))
    assert doc["formats"]
    for entry in doc["formats"]:
        assert entry["format"] in epoch_guess.FORMATS, entry
        assert entry["path"] and entry["table"] and entry["column"]
