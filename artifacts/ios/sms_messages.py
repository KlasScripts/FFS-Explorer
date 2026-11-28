name = "SMS Messages"
description = ("Messages from iOS's SMS/iMessage database (sms.db, table "
              "message), joined to chat/handle for conversation and sender, "
              "with attributedBody (typedstream) decoded as a fallback for "
              "rows whose plain-text `text` column is empty — edited "
              "messages and several share-link message types store their "
              "content only there. Live rows only: deleted and changed "
              "records are in \"SMS & iMessage — Deleted & Changed Records\" "
              "(2026-11-21). iOS 16+ keeps a message "
              "deleted from a conversation (but not yet past its ~30-40 day "
              "retention window) fully intact in this same `message` table "
              "— it's simply no longer linked via chat_message_join, "
              "instead linked via chat_recoverable_message_join, which "
              "still resolves to the same live row with its own text intact. "
              "This report detects that case via that join and marks the "
              "row recovered=True, recovery_method='recently_deleted_window' "
              "with its own delete_date — no carving needed, the content "
              "was never actually gone. On the extraction this was built "
              "against, one message deleted ~6 hours before the extraction "
              "was still present this way; two older ones (deleted ~6+ "
              "months prior) were fully expired from both this table and "
              "Apple's own recoverable-message tables. Two further "
              "confirmed limitations, checked directly on this extraction: "
              "(1) editing a message overwrites `text`/`attributedBody` in "
              "place — `edited_at` on a row means the CURRENT text is "
              "shown, the pre-edit version is gone from this table with no "
              "history kept (message_summary_info holds only a length/range "
              "value for the old text, never the text itself, confirmed by "
              "decoding it directly on an edited row). (2) 'Unsend' behaves "
              "differently from 'Delete': the row is kept and `edited_at` "
              "gets set exactly like a real edit, but message_summary_info "
              "decodes to an explicit `{'ust': True, ...}` flag and both "
              "`text`/`attributedBody` are wiped — confirmed on a "
              "documented-unsent message on this extraction. It never "
              "entered chat_recoverable_message_join, so it does not get "
              "Delete's 30-40 day recovery window at all; the original "
              "text is gone the moment it's unsent, not gradually."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from chat_handle_join; the owner's number from message.destination_caller_id. This is membership as at extraction, not at the time of any message, so no message row claims who received it.")
app_path = "mobile/Library/SMS"
files = {
    "sms": "sms.db",
}
optional_files = {
    "sms_wal": "sms.db-wal",
    "sms_shm": "sms.db-shm",
}


# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. The first six are this
# module's own run() output fields; the last five are the SAME message
# table columns' own SQL names ("date", "date_edited", ...) as they'd
# appear on a row from generic carving instead (relevant only if that
# backstop ever finds something on a different extraction — checked
# negative on this one, see description above). All are Cocoa/Mac epoch
# NANOSECONDS (message.date and its siblings — NOT the more common Cocoa
# seconds or Unix ms used elsewhere in this project).
timestamp_fields = {
    "timestamp": "cocoa_ns", "delete_date": "cocoa_ns", "edited_at": "cocoa_ns",
    "retracted_at": "cocoa_ns", "delivered_at": "cocoa_ns", "read_at": "cocoa_ns",
    "date": "cocoa_ns", "date_edited": "cocoa_ns", "date_retracted": "cocoa_ns",
    "date_delivered": "cocoa_ns", "date_read": "cocoa_ns",
}

# "attachment_path" is a full archive ui_path for the thumbnail/full-view
# feature (see app/artifact_media.py). attachment.filename is stored as
# '~/Library/SMS/Attachments/<hash>/<hash>/<GUID>/<basename>' — the '~' is
# the mobile user's home directory, confirmed against a real extraction (a
# stored '~/Library/SMS/Attachments/e0/00/<GUID>/image.000000.jpg' resolves
# to a real JPEG at mobile/Library/SMS/Attachments/e0/00/<GUID>/
# image.000000.jpg, the same base this module's own app_path already uses
# minus "/Attachments"). A message can carry more than one attachment
# (attachments_by_message is already a list, below); only the first is
# surfaced here as the thumbnail — the text summary in `message` still
# names every attachment.
media_fields = ["attachment_path"]

# Checked directly against the real schema before declaring this (PRAGMA
# table_info), not assumed: message.ROWID, chat.ROWID, handle.ROWID, and
# attachment.ROWID are all genuine INTEGER PRIMARY KEY AUTOINCREMENT
# columns (real rowid aliases, the column literally named "ROWID" itself
# in every one of these tables) -- so "ROWID" doubles as the rowid_fields
# fallback for a carved row of any of them (sqlite_carve backfills a
# carved row's rowid under the table's own declared PK column name).
# chat_message_join.chat_id and message.handle_id were both confirmed to
# resolve cleanly to real chat.ROWID/handle.ROWID values with zero
# orphans on real data before relying on that, not assumed from the
# schema's own FK relationship alone. raw_handle_id/raw_attachment_rowid
# are genuinely absent on many rows (an outgoing "Me" message has no
# handle to resolve; not every message has an attachment) --
# presence_fields drops those entries rather than citing a row that was
# never really there. Attachment cites only the FIRST attachment on a
# message with more than one, matching the same "first attachment only"
# precedent this file's own attachment_path/media_fields already follows.
hidden_fields = ["chat_members", "raw_attachment_rowid"]
record_source = [
    {"label": "Message", "file_key": "sms", "table": "message",
     "rowid_fields": ["raw_message_id", "ROWID"]},
    {"label": "Chat", "file_key": "sms", "table": "chat",
     "rowid_fields": ["raw_chat_id"], "presence_fields": ["raw_chat_id"]},
    {"label": "Sender", "file_key": "sms", "table": "handle",
     "rowid_fields": ["raw_handle_id"], "presence_fields": ["raw_handle_id"]},
    {"label": "Attachment", "file_key": "sms", "table": "attachment",
     "rowid_fields": ["raw_attachment_rowid"], "presence_fields": ["raw_attachment_rowid"]},
]

# Apple's own tapback/reaction enum (message.associated_message_type). The
# `text` column on a reaction row already contains a full human-readable
# sentence generated by the OS itself (e.g. 'Loved "..."') — these codes
# are surfaced alongside that text for citation, not used to generate it.
_REACTION_TYPES = {
    2000: "Loved", 2001: "Liked", 2002: "Disliked", 2003: "Laughed",
    2004: "Emphasized", 2005: "Questioned",
    3000: "Removed Loved", 3001: "Removed Liked", 3002: "Removed Disliked",
    3003: "Removed Laughed", 3004: "Removed Emphasized", 3005: "Removed Questioned",
}


def _parse_attributed_body(blob):
    if not blob:
        return None
    try:
        import typedstream
        root = typedstream.unarchive_from_data(blob)
    except Exception:
        # Best-effort: a handful of blobs on real data fail to unarchive
        # (e.g. legacy/truncated formats) — not treated as an error, since
        # `text` already covers the common case and this is only a fallback.
        return None
    if hasattr(root, "contents") and root.contents:
        first_item = root.contents[0]
        if hasattr(first_item, "value") and hasattr(first_item.value, "value"):
            return first_item.value.value
    return None


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "raw_chat_id", "name": ["conversation"], "sent": ["Sent"], "media": ["attachment_path"], "time": "timestamp", "participants": ["sender"], "members": "chat_members", "deleted": {"recovered": ["True"]}, "type": "chat_type"}

def run(paths):
    mobile_root = paths.get('_ui_path_for', lambda p: p)('mobile')
    from artifact_runner import missing_ref_label, open_db_readonly, resolve_path_after_marker

    conn = open_db_readonly(paths["sms"])

    chats = {r["ROWID"]: r for r in conn.execute(
        "SELECT ROWID, chat_identifier, display_name, room_name FROM chat")}
    handles = {r["ROWID"]: r["id"] for r in conn.execute("SELECT ROWID, id FROM handle")}
    # message_id -> chat_id for messages still in a normal conversation.
    chat_by_message = {r["message_id"]: r["chat_id"]
                       for r in conn.execute("SELECT message_id, chat_id FROM chat_message_join")}
    # message_id -> (chat_id, delete_date) for messages inside the "Recently
    # Deleted" retention window — a message here is not in the join above.
    recoverable_by_message = {r["message_id"]: (r["chat_id"], r["delete_date"])
                              for r in conn.execute(
                                  "SELECT message_id, chat_id, delete_date FROM chat_recoverable_message_join")}

    rows = conn.execute("""
        SELECT ROWID, guid, text, attributedBody, handle_id, is_from_me, date,
               date_edited, date_retracted, associated_message_type,
               associated_message_guid, service,
               is_sent, is_delivered, is_read, date_delivered, date_read,
               {dest}
        FROM message
        ORDER BY date
    """.replace("{dest}", "destination_caller_id" if any(
        c[1] == "destination_caller_id" for c in conn.execute("PRAGMA table_info(message)"))
        else "NULL AS destination_caller_id")).fetchall()

    # Recipients (2026-10-24): a chat's members are chat_handle_join (current
    # membership, at extraction); the device owner's own number/address is
    # an incoming message's destination_caller_id (IOS17 +19195794674,
    # CTF23 +12065937224), else the most common one.
    import collections
    chat_members = {}
    for chat_id_, handle_id_ in conn.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
        if handle_id_ in handles:
            chat_members.setdefault(chat_id_, []).append(handles[handle_id_])
    owner_ids = collections.Counter(r["destination_caller_id"] for r in rows
                                    if not r["is_from_me"] and r["destination_caller_id"])
    default_owner = owner_ids.most_common(1)[0][0] if owner_ids else None

    # message_id -> list of {name, mime_type, size, filename, rowid} for
    # inline attachment info.
    attachments_by_message = {}
    for r in conn.execute("""
        SELECT maj.message_id, a.ROWID AS rowid, a.transfer_name, a.mime_type,
               a.total_bytes, a.filename
        FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id
    """):
        attachments_by_message.setdefault(r["message_id"], []).append(
            {"name": r["transfer_name"], "mime_type": r["mime_type"],
             "size": r["total_bytes"], "filename": r["filename"], "rowid": r["rowid"]})

    def cocoa_ts(ns):
        # Named passthrough, not a no-op: keeps every call site below
        # reading the same way now that formatting moved to display time
        # (see timestamp_fields above) as it did when this actually
        # converted to a UTC string — same "cocoa nanoseconds in, raw
        # cocoa_ns value out" contract either way.
        return ns or None

    out = []
    for r in rows:
        recovered_entry = recoverable_by_message.get(r["ROWID"])
        chat_id = chat_by_message.get(r["ROWID"]) or (recovered_entry[0] if recovered_entry else None)
        chat = chats.get(chat_id)
        conversation = ((chat["display_name"] or chat["room_name"] or chat["chat_identifier"])
                        if chat else missing_ref_label("chat record", "message ROWID", r["ROWID"]))

        sender = "Me" if r["is_from_me"] else handles.get(
            r["handle_id"], missing_ref_label("handle record", "handle_id", r["handle_id"]))
        members = chat_members.get(chat_id) or ([chat["chat_identifier"]] if chat else [])
        everyone = ", ".join([f"Me ({default_owner})" if default_owner else "Me"] + members)

        body = r["text"] or _parse_attributed_body(r["attributedBody"])

        atts = attachments_by_message.get(r["ROWID"], [])
        attachment_path = ''
        if atts:
            att_desc = ", ".join(f"{a['name']} ({a['mime_type']})" if a["mime_type"] else (a["name"] or "?")
                                 for a in atts)
            body = f"{body}  [Attachment: {att_desc}]" if body else f"[Attachment: {att_desc}]"
            # Only the first attachment gets a thumbnail column (one
            # media_fields column per row) — att_desc above still names
            # every attachment on the row regardless of count. 'mobile' is
            # the device root (attachment.filename's leading '~/' is the
            # mobile user's HOME directory, not this module's own app_path
            # — 'mobile/Library/SMS' — so the device root is passed here,
            # not _app_base_ui_path), translated to this archive's own
            # layout ('private/var/mobile' on GrayKey iOS).
            attachment_path = resolve_path_after_marker(
                mobile_root, atts[0]["filename"], '~/')

        entry = {
            "conversation": conversation,
            "timestamp": cocoa_ts(r["date"]),
            "sender": sender,
            "chat_members": everyone,
            "chat_type": "Group" if len(members) > 1 else "1:1",
            "direction": "Sent" if r["is_from_me"] else "Received",
            "message": body,
            "attachment_path": attachment_path,
            "service": r["service"],
            "raw_message_id": r["ROWID"],
            "raw_message_guid": r["guid"],
            "raw_chat_id": chat_id,
            "raw_handle_id": r["handle_id"] or None,
            "raw_attachment_rowid": atts[0]["rowid"] if atts else None,
            "recovered": recovered_entry is not None,
            "source_table": "message",
        }
        if recovered_entry is not None:
            entry["recovery_method"] = "recently_deleted_window"
            entry["delete_date"] = cocoa_ts(recovered_entry[1])
        if r["associated_message_type"]:
            entry["reaction_type"] = _REACTION_TYPES.get(
                r["associated_message_type"], f"[unrecognized type {r['associated_message_type']}]")
            entry["reaction_target_guid"] = r["associated_message_guid"]
        if r["date_edited"]:
            entry["edited_at"] = cocoa_ts(r["date_edited"])
        if r["date_retracted"]:
            entry["retracted_at"] = cocoa_ts(r["date_retracted"])
        if r["is_from_me"]:
            # Sent/delivered status only means something for the local
            # user's own outgoing messages (was it sent, did it deliver).
            entry["sent"] = bool(r["is_sent"])
            entry["delivered"] = bool(r["is_delivered"])
            if r["date_delivered"]:
                entry["delivered_at"] = cocoa_ts(r["date_delivered"])
        # is_read/date_read is meaningful either direction, just different
        # things: on an outgoing row it's the recipient's read receipt; on
        # an incoming row it's whether the local user read it, and when.
        entry["read"] = bool(r["is_read"])
        if r["date_read"]:
            entry["read_at"] = cocoa_ts(r["date_read"])
        out.append(entry)
    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'sms_deleted',
  'name': 'SMS & iMessage — Deleted & Changed Messages',
  'description': 'Deleted and changed messages from sms.db (table message). Live messages, and '
                 "the ones in iOS's own Recently Deleted window, are in SMS Messages; deleted "
                 'and changed attachment records (table attachment) are in SMS & iMessage — '
                 "Deleted & Changed Attachments. From iOS 16 a message's text is often only in "
                 'attributedBody (a typedstream blob), shown here as stored, not decoded.\n'
                 '\n'
                 'Each row is one record as found in the message table, never joined to '
                 'another table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says '
                 'Deleted (no longer in the table), Changed (an earlier version of a row still '
                 'there, listed only when one of the columns that matter changed, with the '
                 'current value beside it as "(now)") or an unfinished write/delete under a '
                 'hot journal. How found says how each record was recovered and how sure that '
                 'is; a record found by pattern has no row id. Values are as stored: no code '
                 'is turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'message dates are Cocoa nanoseconds, as in SMS Messages.\n'
                 '\n'
                 "linked_attachments: where this message's attachments were found. "
                 'message_attachment_join holds one row per (message_id, attachment_id) pair, '
                 'both the ROWIDs of their rows, so the link goes through it: the join rows '
                 "holding this record's id are taken live or as recovered records of "
                 'message_attachment_join, and the other id in each is looked up live and in '
                 'the records the other deleted report finds. Each part of the cell says '
                 'whether its join row was live or recovered. Only ids are compared: no value '
                 'of the other row is brought in. On the Josh Hickman iOS 17 image '
                 '(2026-11-27): 27 join rows, up to 3 attachments per message, every join row '
                 'names a live message. How the link can be wrong: message.ROWID and '
                 'attachment.ROWID are AUTOINCREMENT, so SQLite does not hand out an id again, '
                 'but an app that re-inserts rows with their old ids (a restore from backup) '
                 'can still put a different record under the same id; when a message goes, its '
                 "join rows go with it, and sms.db's own trigger then deletes an attachment no "
                 'other join row names, so a deleted record is linked only when its join row '
                 'was recovered too, and a recovered join row may be from a different time '
                 'than the rows it names; an earlier version found in the WAL and a live row '
                 'with the same id were stored at different times; a record found by pattern '
                 'has no row id and cannot be linked; several copies of one record can carry '
                 'the same id.\n'
                 '\n'
                 'linked_handle: where the handle (the other party\'s number or email) this '
                 'message names in handle_id was found, live or in the records SMS & iMessage — '
                 'Deleted & Changed Handles finds; handle_id 0 means no handle. linked_chat: '
                 'its conversation, through chat_message_join (one chat per message on the '
                 'test image, 2026-11-28), live or in SMS & iMessage — Deleted & Changed Chats. '
                 'Only ids are compared, as above; chat.ROWID and handle.ROWID are '
                 'AUTOINCREMENT too. chat_message_join keeps the message\'s date '
                 '(message_date, equal to message.date on all 90 live rows of the test '
                 'image), so each join row is checked against this record\'s date and the '
                 'cell says whether its message_date is this record\'s date. A recovered '
                 'join row with another date may belong to another message; an earlier '
                 'version whose date has since changed differs from its live join row. When a '
                 'message is deleted its join rows go with it and are seldom recovered; '
                 'then, if the handle it names is in exactly one chat (chat_handle_join; '
                 'true of every handle on the test image), that chat is named, marked '
                 '"inferred, not stored"; a handle in several chats is said so, and no chat '
                 'is guessed.\n'
                 '\n'
                 'iOS\'s own record of deletions: a trigger on the message table writes the '
                 'guid of every deleted message into sync_deleted_messages (with its '
                 'CloudKit recordID). Rows there whose guid is in no live message are listed '
                 'too, status "Deleted (recorded by the app)": a message that existed, '
                 'with no other trace left, or whose record was also recovered (how_found '
                 'says which). linked_deletion_record on a recovered record says whether '
                 'its guid is in that table. On the Josh Hickman iOS 17 image (2026-11-28): '
                 '2 such rows, neither a live message; deleted_messages, written by another '
                 'trigger the same way, held 2 rows once (sqlite_sequence) and is empty.\n'
                 '\n'
                 'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 4 earlier versions of 2 '
                 'messages (from the WAL and overflow pages), all Changed; more versions that '
                 "differ only in columns that don't matter here are counted in the run log. "
                 'Found earlier (carried over from SMS Messages): sms.db has PRAGMA '
                 'secure_delete = FAST (freed ranges all zero); two messages deleted 6+ months '
                 "before the extraction are gone, from this table and from Apple's own "
                 "recoverable-message tables. A message deleted within iOS's ~30-40 day window "
                 'is still live, and shown in SMS Messages.',
  'timestamp_fields': {'date': 'cocoa_ns',
                       'date_read': 'cocoa_ns',
                       'date_delivered': 'cocoa_ns',
                       'date_edited': 'cocoa_ns',
                       'date_retracted': 'cocoa_ns'},
  'core_fields': ['status',
                  'date',
                  'message',
                  'linked_attachments',
                  'linked_handle',
                  'linked_chat',
                  'linked_deletion_record',
                  'changed_fields',
                  'message (now)',
                  'how_found',
                  'copies'],
  'file_key': 'sms',
  'table': 'message',
  'label': 'Message',
  'fields': {'ROWID': 'message_id',
             'guid': 'guid',
             'handle_id': 'handle_id',
             'service': 'service',
             'account': 'account',
             'date': 'date',
             'date_read': 'date_read',
             'date_delivered': 'date_delivered',
             'date_edited': 'date_edited',
             'date_retracted': 'date_retracted',
             'is_from_me': 'is_from_me',
             'text': 'message',
             'attributedBody': 'attributed_body',
             'item_type': 'item_type',
             'group_title': 'group_title',
             'cache_has_attachments': 'has_attachments',
             'associated_message_guid': 'associated_message_guid'},
  'version_fields': ['text',
                     'attributedBody',
                     'handle_id',
                     'is_from_me',
                     'item_type',
                     'group_title'],
  'links': [{'name': 'linked_attachments',
             'what': 'attachment',
             'column': 'ROWID',
             'via': ['message_attachment_join', 'message_id', 'attachment_id'],
             'table': 'attachment',
             'key': 'ROWID',
             'report': 'sms_deleted_attachments'},
            {'name': 'linked_handle',
             'what': 'handle',
             'column': 'handle_id',
             'none': [0],
             'table': 'handle',
             'key': 'ROWID',
             'report': 'sms_deleted_handles'},
            {'name': 'linked_chat',
             'what': 'chat',
             'column': 'ROWID',
             'via': ['chat_message_join', 'message_id', 'chat_id'],
             'via_check': {'column': 'date', 'join_column': 'message_date'},
             'fallback': {'via': ['chat_handle_join', 'handle_id', 'chat_id'],
                          'column': 'handle_id',
                          'what': 'handle'},
             'none': [0],
             'table': 'chat',
             'key': 'ROWID',
             'report': 'sms_deleted_chats'},
            {'name': 'linked_deletion_record',
             'what': 'deletion record',
             'column': 'guid',
             'table': 'sync_deleted_messages',
             'key': 'guid'}],
  'deletion_log': {'table': 'sync_deleted_messages',
                   'key': 'guid',
                   'column': 'guid',
                   'fields': {'recordID': 'cloudkit_record_id'}}},
 {'script': 'sms_deleted_attachments',
  'name': 'SMS & iMessage — Deleted & Changed Attachments',
  'description': 'Deleted and changed attachment records from sms.db (table attachment). Live '
                 'attachments are in SMS Messages, with their message; deleted and changed '
                 'messages (table message) are in SMS & iMessage — Deleted & Changed '
                 'Messages.\n'
                 '\n'
                 'Each row is one record as found in the attachment table, never joined to '
                 'another table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says '
                 'Deleted (no longer in the table), Changed (an earlier version of a row still '
                 'there, listed only when one of the columns that matter changed, with the '
                 'current value beside it as "(now)") or an unfinished write/delete under a '
                 'hot journal. How found says how each record was recovered and how sure that '
                 'is; a record found by pattern has no row id. Values are as stored: no code '
                 'is turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'created_date is Cocoa seconds (checked 2026-11-21 on the Josh Hickman iOS 17 '
                 'image: the earliest attachment is 2.5 s after the earliest message).\n'
                 '\n'
                 'linked_message: where the message this attachment belongs to was found. '
                 'message_attachment_join holds one row per (message_id, attachment_id) pair, '
                 'both the ROWIDs of their rows, so the link goes through it: the join rows '
                 "holding this record's id are taken live or as recovered records of "
                 'message_attachment_join, and the other id in each is looked up live and in '
                 'the records the other deleted report finds. Each part of the cell says '
                 'whether its join row was live or recovered. Only ids are compared: no value '
                 'of the other row is brought in. On the Josh Hickman iOS 17 image '
                 '(2026-11-27): 27 join rows, up to 3 attachments per message, every join row '
                 'names a live message. How the link can be wrong: message.ROWID and '
                 'attachment.ROWID are AUTOINCREMENT, so SQLite does not hand out an id again, '
                 'but an app that re-inserts rows with their old ids (a restore from backup) '
                 'can still put a different record under the same id; when a message goes, its '
                 "join rows go with it, and sms.db's own trigger then deletes an attachment no "
                 'other join row names, so a deleted record is linked only when its join row '
                 'was recovered too, and a recovered join row may be from a different time '
                 'than the rows it names; an earlier version found in the WAL and a live row '
                 'with the same id were stored at different times; a record found by pattern '
                 'has no row id and cannot be linked; several copies of one record can carry '
                 'the same id.\n'
                 '\n'
                 'Also listed: attachment rows still live whose message is gone (status '
                 'Orphaned): not deleted, so not recovered, and not in SMS Messages, which '
                 'lists a row only with its message. On the Josh Hickman iOS 17 image '
                 '(2026-11-21): 1 of 28 attachments, an outgoing image/jpeg with no '
                 'message_attachment_join row.\n'
                 '\n'
                 'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 6 earlier versions of '
                 'attachments (from the WAL), all Changed. sms.db has PRAGMA secure_delete = '
                 'FAST (carried over from SMS Messages).',
  'timestamp_fields': {'created_date': 'cocoa_s'},
  'core_fields': ['status',
                  'created_date',
                  'filename',
                  'mime_type',
                  'linked_message',
                  'changed_fields',
                  'filename (now)',
                  'how_found',
                  'copies'],
  'file_key': 'sms',
  'table': 'attachment',
  'label': 'Attachment',
  'fields': {'ROWID': 'attachment_id',
             'guid': 'guid',
             'created_date': 'created_date',
             'filename': 'filename',
             'transfer_name': 'transfer_name',
             'mime_type': 'mime_type',
             'uti': 'uti',
             'total_bytes': 'total_bytes',
             'is_outgoing': 'is_outgoing',
             'is_sticker': 'is_sticker'},
  'orphan_of': {'column': 'ROWID',
                'via': ['message_attachment_join', 'attachment_id', 'message_id'],
                'in': ['message', 'ROWID']},
  'version_fields': ['filename', 'transfer_name', 'mime_type'],
  'links': [{'name': 'linked_message',
             'what': 'message',
             'column': 'ROWID',
             'via': ['message_attachment_join', 'attachment_id', 'message_id'],
             'table': 'message',
             'key': 'ROWID',
             'report': 'sms_deleted'}]},
 {'script': 'sms_deleted_chats',
  'name': 'SMS & iMessage — Deleted & Changed Chats',
  'description': 'Deleted and changed conversation records from sms.db (table chat): the other '
                 "party or group (chat_identifier), the group's name (display_name), the "
                 'service. A deleted chat is a conversation that existed. Live chats are in '
                 'SMS Messages, as the conversation of each message; deleted and changed '
                 'messages are in SMS & iMessage — Deleted & Changed Messages, handles (the '
                 'phone numbers and emails) in SMS & iMessage — Deleted & Changed Handles.\n'
                 '\n'
                 'Each row is one record as found in the chat table, never joined to another '
                 'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id. Values are as stored: no code is '
                 'turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'last_read_message_timestamp is Cocoa nanoseconds, as message dates in SMS '
                 'Messages. properties is a plist BLOB, shown as stored.\n'
                 '\n'
                 'linked_handles: where the handles of this chat were found. chat_handle_join '
                 'holds one row per (chat_id, handle_id) pair, the ROWIDs of their rows, so '
                 "the link goes through it: the join rows holding this record's id, live or "
                 'recovered, and the handle each names, looked up live and in the records SMS '
                 '& iMessage — Deleted & Changed Handles finds. Only ids are compared. On the '
                 'Josh Hickman iOS 17 image (2026-11-28): 36 join rows, one handle per chat, '
                 'every join row names a live chat and handle. A message names its chat '
                 'through chat_message_join, so the link is on the message report '
                 '(linked_chat), not here. How the link can be wrong: chat.ROWID and '
                 'handle.ROWID are AUTOINCREMENT (checked 2026-11-28 on the Josh Hickman iOS '
                 '17 image), so SQLite does not hand out an id again, but an app that '
                 're-inserts rows with their old ids (a restore from backup) can still put a '
                 'different record under the same id; an earlier version found in the WAL and '
                 'a live row with the same id were stored at different times; a record found '
                 'by pattern has no row id and cannot be linked; several copies of one record '
                 'can carry the same id.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman iOS 17 image: 12 earlier versions of '
                 '1 chat, all differing only in properties and last_read_message_timestamp, so '
                 'none is listed (counted in the run log); no deleted chat. sqlite_sequence '
                 'says chat ids up to 36 were handed out and 36 chats are live: none was '
                 'deleted on this phone.',
  'timestamp_fields': {'last_read_message_timestamp': 'cocoa_ns'},
  'core_fields': ['status',
                  'chat_identifier',
                  'display_name',
                  'service_name',
                  'linked_handles',
                  'changed_fields',
                  'how_found',
                  'copies'],
  'file_key': 'sms',
  'table': 'chat',
  'label': 'Chat',
  'fields': {'ROWID': 'chat_id',
             'guid': 'guid',
             'chat_identifier': 'chat_identifier',
             'service_name': 'service_name',
             'display_name': 'display_name',
             'room_name': 'room_name',
             'group_id': 'group_id',
             'account_login': 'account_login',
             'last_addressed_handle': 'last_addressed_handle',
             'style': 'style',
             'state': 'state',
             'is_archived': 'is_archived',
             'is_filtered': 'is_filtered',
             'is_recovered': 'is_recovered',
             'last_read_message_timestamp': 'last_read_message_timestamp',
             'properties': 'properties'},
  'version_fields': ['chat_identifier',
                     'display_name',
                     'room_name',
                     'group_id',
                     'service_name',
                     'style',
                     'is_archived',
                     'is_recovered'],
  'links': [{'name': 'linked_handles',
             'what': 'handle',
             'column': 'ROWID',
             'via': ['chat_handle_join', 'chat_id', 'handle_id'],
             'table': 'handle',
             'key': 'ROWID',
             'report': 'sms_deleted_handles'}]},
 {'script': 'sms_deleted_handles',
  'name': 'SMS & iMessage — Deleted & Changed Handles',
  'description': 'Deleted and changed handle records from sms.db (table handle): the phone '
                 'number or email of the other party (id), with its service. A message names '
                 'its handle by handle_id, so a deleted message without its handle has no '
                 'counterpart; this report is where that handle may still be. Live handles are '
                 'in SMS Messages, as the sender of each message.\n'
                 '\n'
                 'Each row is one record as found in the handle table, never joined to another '
                 'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id. Values are as stored: no code is '
                 'turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'linked_chats: the chats this handle is in, through chat_handle_join (as '
                 'linked_handles in SMS & iMessage — Deleted & Changed Chats, the other way '
                 'round), looked up live and in the records that report finds. Only ids are '
                 'compared. How the link can be wrong: chat.ROWID and handle.ROWID are '
                 'AUTOINCREMENT (checked 2026-11-28 on the Josh Hickman iOS 17 image), so '
                 'SQLite does not hand out an id again, but an app that re-inserts rows with '
                 'their old ids (a restore from backup) can still put a different record under '
                 'the same id; an earlier version found in the WAL and a live row with the '
                 'same id were stored at different times; a record found by pattern has no row '
                 'id and cannot be linked; several copies of one record can carry the same '
                 'id.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman iOS 17 image: no deleted or changed '
                 'handle. sqlite_sequence says handle ids up to 36 were handed out and 36 '
                 'handles are live: none was deleted on this phone.',
  'timestamp_fields': {},
  'core_fields': ['status',
                  'handle',
                  'service',
                  'linked_chats',
                  'changed_fields',
                  'how_found',
                  'copies'],
  'file_key': 'sms',
  'table': 'handle',
  'label': 'Handle',
  'fields': {'ROWID': 'handle_id',
             'id': 'handle',
             'service': 'service',
             'country': 'country',
             'uncanonicalized_id': 'uncanonicalized_id',
             'person_centric_id': 'person_centric_id'},
  'version_fields': ['id', 'service', 'country', 'uncanonicalized_id'],
  'links': [{'name': 'linked_chats',
             'what': 'chat',
             'column': 'ROWID',
             'via': ['chat_handle_join', 'handle_id', 'chat_id'],
             'table': 'chat',
             'key': 'ROWID',
             'report': 'sms_deleted_chats'}]}]
