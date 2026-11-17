name = "LINE Messages (iOS)"
description = (
    "Messages and calls from LINE's iOS database (Line.sqlite, table ZMESSAGE), one row "
    "per message, with the conversation and sender named from the app's own user table. "
    "Message is ZTEXT exactly as stored; a call, a picture, a location or a card message is "
    "named in Event, with the details in their own columns. Direction is Outgoing when the "
    "message has no sender reference (checked against documented actions on a test image), "
    "and the local user is [local user]. Calls: the stored time is when the call ended; "
    "Call started is that time minus the stored duration (all 8 calls on the test image "
    "match their documented start, direction and audio/video). Deleted and changed messages "
    "are in the separate report LINE — Deleted & Changed Messages (iOS).\n\n"
    "Reads one account's store (the first PrivateStore/P_<id> folder found). Group chats "
    "were not present on the test image, so how a group is named is not tested."
)

# Line.sqlite: app group group.com.linecorp.line, per-account folder
# Library/Application Support/PrivateStore/P_<id>/Messages/ (Core Data).
# Field names follow the Android LINE report (artifacts/android/line.py)
# so the two read alike; the readings are the iOS database's own, checked
# 2026-11-17 on the Josh Hickman iOS 17 image (test_data/josh_hickman_ios17)
# and against iLEAPP's iOS line.py / lineCalls (validated on four images):
#   - ZTIMESTAMP: Unix milliseconds.
#   - ZSENDER empty = sent by this phone: 23 of 24 documented sent messages;
#     set for 21 of 22 received (the two others share their minute with a
#     message the other way). No ZUSER row exists for the local user.
#   - ZCONTENTTYPE: 0 text (37 documented), 1 picture (4), 100 static
#     location (3), 6 call (all 8 documented calls), 17 card message (4 rows,
#     all from the one chat with no user row; named from its metadata keys
#     MARKUP_JSON / ALT_TEXT, not from ground truth).
#   - A call's ZCONTENTMETADATA is a keyed archive with TYPE (A audio, V
#     video), RESULT, CAUSE and DURATION in milliseconds; the stored time is
#     the call's end. NOT the Android layout (type 4, a parameter string).
#   - The conversation's other party: the ZUSER whose ZMID is the chat's
#     ZMID (4 of the 5 chats; the fifth has no user row).
#   - Locations: ZLATITUDE / ZLONGITUDE in plain degrees (not scaled as on
#     Android).
#   - An attachment file is in the app's OWN container (not the app group),
#     Library/Application Support/PrivateStore/P_<id>/Message Attachments/,
#     named for the message's ZID with an extension (4 of 4 on the image).
category = "Chats & Messaging"
app_group = "group.com.linecorp.line"
files = {
    "line": "Library/Application Support/PrivateStore/P_*/Messages/Line.sqlite",
}
# The -wal and -journal are extracted beside it by the runner (A-96).

timestamp_fields = {"timestamp": "ms", "call_started": "ms"}
media_fields = ["attachment_path"]

record_source = [
    {"label": "Message", "file_key": "line", "table": "ZMESSAGE",
     "rowid_fields": ["raw_message_id"]},
    {"label": "Chat", "file_key": "line", "table": "ZCHAT",
     "rowid_fields": ["raw_chat_id"]},
]

core_fields = ["conversation", "timestamp", "direction", "sender", "message", "event",
               "attachment_path"]
hidden_fields = ["raw_message_id", "raw_chat_id", "raw_sender_id", "recovered", "source_table"]

chat_groups = {"key": "raw_chat_id", "name": ["conversation"], "sent": ["Outgoing"],
               "media": ["attachment", "attachment_path"], "time": "timestamp",
               "participants": ["sender"], "call_pattern": r"\bcall\b", "call_field": "event"}

_TYPE_PICTURE = 1
_TYPE_CALL = 6
_TYPE_CARD = 17
_TYPE_LOCATION = 100


def _duration(ms):
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return ""
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}"


def _attachments(paths):
    """{ZID: archive ui_path} of the attachment files for this account."""
    import re
    db_ui = (paths.get("_ui_paths") or {}).get("line") or ""
    m = re.search(r"/PrivateStore/(P_[^/]+)/", db_ui)
    adapter = paths.get("_adapter")
    if not m or adapter is None:
        return {}
    marker = f"/PrivateStore/{m.group(1)}/Message Attachments/"
    out = {}
    for name in paths.get("_zip_names") or ():
        if marker in name and not name.endswith("/"):
            stem = name.rsplit("/", 1)[1].split(".", 1)[0]
            ui = adapter.ui_path_from_physical(name)
            if ui:
                out.setdefault(stem, ui)
    return out


def run(paths):
    from artifact_runner import decode_plist_blob, missing_ref_label, open_db_readonly

    conn = open_db_readonly(paths["line"])
    users = {r["Z_PK"]: r for r in conn.execute("SELECT Z_PK, ZMID, ZNAME FROM ZUSER")}
    name_by_mid = {}
    for r in users.values():
        if r["ZMID"] and r["ZNAME"]:
            name_by_mid.setdefault(r["ZMID"], r["ZNAME"])
    chats = {r["Z_PK"]: r for r in conn.execute("SELECT Z_PK, ZMID FROM ZCHAT")}
    attachments = _attachments(paths)

    rows = conn.execute("""
        SELECT Z_PK, ZID, ZCHAT, ZSENDER, ZTIMESTAMP, ZCONTENTTYPE, ZTEXT,
               ZLATITUDE, ZLONGITUDE, ZCONTENTMETADATA
        FROM ZMESSAGE ORDER BY ZCHAT, ZTIMESTAMP, Z_PK
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        chat = chats.get(r["ZCHAT"])
        chat_mid = chat["ZMID"] if chat else None
        conversation = (name_by_mid.get(chat_mid)
                        or missing_ref_label("chat contact", "ZCHAT.ZMID", chat_mid if chat else r["ZCHAT"]))
        incoming = r["ZSENDER"] is not None
        direction = "Incoming" if incoming else "Outgoing"
        if incoming:
            u = users.get(r["ZSENDER"])
            sender = (u["ZNAME"] if u and u["ZNAME"]
                      else missing_ref_label("user record", "ZSENDER", r["ZSENDER"]))
        else:
            sender = "[local user]"

        ctype = r["ZCONTENTTYPE"]
        event, call_type, call_result, call_cause, duration_s, call_started = "", "", "", "", None, None
        card_text = ""
        if ctype == _TYPE_CALL:
            md = decode_plist_blob(r["ZCONTENTMETADATA"]) if r["ZCONTENTMETADATA"] else None
            md = md if isinstance(md, dict) else {}
            kind = {"A": "audio", "V": "video"}.get(md.get("TYPE"), md.get("TYPE"))
            event = f"{direction} {kind} call" if kind else f"{direction} call"
            dur = _duration(md.get("DURATION"))
            if dur:
                event = f"{event}, {dur}"
            call_result = md.get("RESULT") or ""
            if call_result and call_result != "NORMAL":
                # NORMAL was the only value on the test image (iLEAPP also saw
                # CANCELED): any other is passed through, never relabelled
                event = f"{event}, result {call_result}"
            call_type = md.get("TYPE") or ""
            call_cause = md.get("CAUSE") or ""
            try:
                duration_s = int(md.get("DURATION")) / 1000
                call_started = r["ZTIMESTAMP"] - int(md.get("DURATION"))
            except (TypeError, ValueError):
                pass
        elif ctype == _TYPE_PICTURE:
            event = "Image"
        elif ctype == _TYPE_LOCATION:
            event = "Location"
        elif ctype == _TYPE_CARD:
            md = decode_plist_blob(r["ZCONTENTMETADATA"]) if r["ZCONTENTMETADATA"] else None
            card_text = (md.get("ALT_TEXT") or "") if isinstance(md, dict) else ""
            event = "Card message (content type 17)"
        elif ctype not in (0, None):
            event = f"Content type {ctype}"
        is_location = ctype == _TYPE_LOCATION
        attachment_path = attachments.get(str(r["ZID"])) if r["ZID"] is not None else None

        out.append({
            "conversation": conversation,
            "timestamp": r["ZTIMESTAMP"],
            "sender": sender,
            "direction": direction,
            "message": r["ZTEXT"],
            "event": event,
            "attachment": "Image" if ctype == _TYPE_PICTURE else "",
            "attachment_path": attachment_path or "",
            "latitude": r["ZLATITUDE"] if is_location else None,
            "longitude": r["ZLONGITUDE"] if is_location else None,
            "card_text": card_text,
            "call_started": call_started,
            "call_duration_s": duration_s,
            "call_type": call_type,
            "call_result": call_result,
            "call_cause": call_cause,
            "content_type": ctype,
            "chat_mid": chat_mid or "",
            "line_message_id": r["ZID"],
            "raw_message_id": r["Z_PK"],
            "raw_chat_id": r["ZCHAT"],
            "raw_sender_id": r["ZSENDER"],
            "recovered": False,
            "source_table": "ZMESSAGE",
        })
    return out
