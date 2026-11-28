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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'line_deleted_messages',
  'name': 'LINE — Deleted & Changed Messages (iOS)',
  'description': 'LINE messages that were deleted, or changed after they were stored, '
                 'recovered from Line.sqlite (iOS, table ZMESSAGE): rows removed from the '
                 'message table (from the WAL, free space, freed pages), the unfinished write '
                 'under a hot journal, and earlier versions of messages that are still there, '
                 'listed only when the text, kind, sender, chat or location changed (the '
                 'current value beside it, as "(now)"). Earlier versions of a chat\'s '
                 'last-message preview are in LINE — Deleted & Changed Chat Previews (iOS); '
                 'contacts in LINE — Deleted & Changed Contacts (iOS).\n'
                 '\n'
                 'Each row is the record as found in this one table, never joined '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. How found says how each '
                 'record was recovered and how sure that is; a record found by pattern has no '
                 'row id; damaged says why a record is not whole. Live messages and calls are '
                 'in LINE Messages (iOS).\n'
                 '\n'
                 'Direction (Outgoing / Incoming, as in the Android LINE report) comes from '
                 'the sender column being empty for messages this phone sent, checked against '
                 'documented actions on a test image; so are the content-type words. See the '
                 'notes page.\n'
                 '\n'
                 "linked_sender and linked_chat: where the ZUSER row this message's ZSENDER "
                 'names, and the ZCHAT row its ZCHAT names, were found: in the table now '
                 '(live), in the records the contacts and chat-preview reports find '
                 '(recovered, with its row id) or not found. ZSENDER is empty for a message '
                 'this phone sent (no ZUSER row exists for the local user). Checked 2026-11-27 '
                 "on the Josh Hickman iOS 17 image: every live message's ZSENDER is a ZUSER "
                 'Z_PK (34 of 34) and its ZCHAT a ZCHAT Z_PK (62 of 62).\n'
                 'Only ids are compared: no value of the other row is brought in. How a link '
                 "can be wrong: Z_PK comes from Core Data's own counter (Z_PRIMARYKEY.Z_MAX; "
                 'checked 2026-11-27 on the Josh Hickman iOS 17 image: the highest Z_PK of '
                 'ZMESSAGE, ZCHAT and ZUSER is its Z_MAX), so an id is not normally handed out '
                 'again, but a store rebuilt from a backup or a migration can put a different '
                 'record under an old id; an earlier version found in the WAL and a live row '
                 'with the same id were stored at different times; a record found by pattern '
                 'has no row id and cannot be linked to.\n'
                 '\n',
  'category': 'Chats & Messaging',
  'core_fields': ['status',
                  'timestamp',
                  'direction',
                  'message',
                  'changed_fields',
                  'message (now)',
                  'content_type',
                  'linked_sender',
                  'linked_chat',
                  'damaged',
                  'how_found',
                  'copies'],
  'hidden_fields': ['recovered',
                    'recovery_method',
                    'recovery_version',
                    'source_table',
                    'source_file_key',
                    'raw_rowid',
                    'raw_page',
                    'raw_file',
                    'raw_offset',
                    'raw_length',
                    'source',
                    'copy_locations'],
  'timestamp_fields': {'timestamp': 'ms'},
  'file_key': 'line',
  'table': 'ZMESSAGE',
  'label': 'Message',
  'fields': {'ZTIMESTAMP': 'timestamp',
             'ZTEXT': 'message',
             'ZCONTENTTYPE': 'content_type',
             'ZLATITUDE': 'latitude',
             'ZLONGITUDE': 'longitude',
             'ZCHAT': 'chat_id',
             'ZSENDER': 'sender_id',
             'ZID': 'line_message_id'},
  'version_fields': ['ZTEXT', 'ZCONTENTTYPE', 'ZSENDER', 'ZCHAT', 'ZLATITUDE', 'ZLONGITUDE'],
  'labels': {'ZCONTENTTYPE': {0: 'text', 1: 'picture', 100: 'location'}},
  'derived': {'direction': {'from': 'ZSENDER',
                            'values': {None: 'Outgoing'},
                            'else': 'Incoming'}},
  'links': [{'name': 'linked_sender',
             'what': 'contact',
             'column': 'ZSENDER',
             'table': 'ZUSER',
             'key': 'Z_PK',
             'report': 'line_deleted_users'},
            {'name': 'linked_chat',
             'what': 'chat',
             'column': 'ZCHAT',
             'table': 'ZCHAT',
             'key': 'Z_PK',
             'report': 'line_deleted_chats'}]},
 {'script': 'line_deleted_chats',
  'name': 'LINE — Deleted & Changed Chat Previews (iOS)',
  'description': "Earlier versions of a LINE chat's last-message preview, and deleted chats, "
                 'from Line.sqlite (iOS, table ZCHAT). A preview is listed only when its text '
                 'is in no message, live or recovered (a comparison of values, not a join: the '
                 'messages LINE — Deleted & Changed Messages (iOS) finds count); the others '
                 'are counted in the run log. Most are app-generated (call durations, '
                 'locations, official accounts), but one can be the only trace of a deleted '
                 'message. It is a preview, not a message row: it has no time, sender or '
                 'direction.\n'
                 '\n'
                 'Each row is the record as found in this one table, never joined '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. How found says how each '
                 'record was recovered and how sure that is; a record found by pattern has no '
                 'row id; damaged says why a record is not whole. Live messages and calls are '
                 'in LINE Messages (iOS).\n'
                 '\n',
  'category': 'Chats & Messaging',
  'core_fields': ['status',
                  'message',
                  'chat_mid',
                  'changed_fields',
                  'message (now)',
                  'damaged',
                  'how_found',
                  'copies'],
  'hidden_fields': ['recovered',
                    'recovery_method',
                    'recovery_version',
                    'source_table',
                    'source_file_key',
                    'raw_rowid',
                    'raw_page',
                    'raw_file',
                    'raw_offset',
                    'raw_length',
                    'source',
                    'copy_locations'],
  'file_key': 'line',
  'table': 'ZCHAT',
  'label': 'Chat preview (not a message row)',
  'fields': {'ZLASTMESSAGE': 'message', 'ZMID': 'chat_mid'},
  'version_fields': ['ZLASTMESSAGE'],
  'not_found_in': {'ZLASTMESSAGE': ['ZMESSAGE', 'ZTEXT']}},
 {'script': 'line_deleted_users',
  'name': 'LINE — Deleted & Changed Contacts (iOS)',
  'description': 'Deleted LINE contacts, and earlier versions of contacts that are still '
                 'there, from Line.sqlite (iOS, table ZUSER): who a sender id was, a name or '
                 'status message as it was before it changed, a contact marked removed. Listed '
                 'when the name, the name the user gave the contact, the address-book name, '
                 'the status message or the removed flag changed. The local user has no ZUSER '
                 'row.\n'
                 '\n'
                 'Each row is the record as found in this one table, never joined '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. How found says how each '
                 'record was recovered and how sure that is; a record found by pattern has no '
                 'row id; damaged says why a record is not whole. Live messages and calls are '
                 'in LINE Messages (iOS).\n'
                 '\n'
                 "user_id is the Z_PK a message's ZSENDER holds (see LINE — Deleted & Changed "
                 'Messages (iOS), linked_sender). created_time and status_updated_at are shown '
                 'as stored: their unit was not checked against a test image.\n'
                 '\n'
                 'Only ids are compared: no value of the other row is brought in. How a link '
                 "can be wrong: Z_PK comes from Core Data's own counter (Z_PRIMARYKEY.Z_MAX; "
                 'checked 2026-11-27 on the Josh Hickman iOS 17 image: the highest Z_PK of '
                 'ZMESSAGE, ZCHAT and ZUSER is its Z_MAX), so an id is not normally handed out '
                 'again, but a store rebuilt from a backup or a migration can put a different '
                 'record under an old id; an earlier version found in the WAL and a live row '
                 'with the same id were stored at different times; a record found by pattern '
                 'has no row id and cannot be linked to.\n'
                 '\n',
  'category': 'Chats & Messaging',
  'core_fields': ['status',
                  'user_id',
                  'name',
                  'custom_name',
                  'address_book_name',
                  'status_message',
                  'changed_fields',
                  'name (now)',
                  'damaged',
                  'how_found',
                  'copies'],
  'hidden_fields': ['recovered',
                    'recovery_method',
                    'recovery_version',
                    'source_table',
                    'source_file_key',
                    'raw_rowid',
                    'raw_page',
                    'raw_file',
                    'raw_offset',
                    'raw_length',
                    'source',
                    'copy_locations'],
  'file_key': 'line',
  'table': 'ZUSER',
  'label': 'Contact',
  'fields': {'Z_PK': 'user_id',
             'ZMID': 'mid',
             'ZNAME': 'name',
             'ZCUSTOMNAME': 'custom_name',
             'ZADDRESSBOOKNAME': 'address_book_name',
             'ZSTATUSMESSAGE': 'status_message',
             'ZISREMOVED': 'is_removed',
             'ZISFRIEND': 'is_friend',
             'ZCREATEDTIME': 'created_time',
             'ZSTATUSUPDATEDAT': 'status_updated_at'},
  'version_fields': ['ZNAME',
                     'ZCUSTOMNAME',
                     'ZADDRESSBOOKNAME',
                     'ZSTATUSMESSAGE',
                     'ZISREMOVED']}]
