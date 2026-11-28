name = "Google Messages (SMS/MMS/RCS)"
description = ("Messages from Google Messages' local database (bugle_db, "
              "tables parts/messages), live rows only: deleted and changed "
              "records are in \"Google Messages — Deleted & Changed Records\" "
              "(2026-11-21), and conversation-level metadata (who, when) for "
              "a fully deleted conversation, from surviving audit-log "
              "tables, in \"Google Messages — Recovered (deleted "
              "conversations)\"."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from conversation_participants; the owner's own number per message from messages.self_id. This is membership as at extraction, not at the time of any message, so no message row claims who received it.")
app_path = "data/data/com.google.android.apps.messaging"
files = {
    "bugle_db": "databases/bugle_db",
}
optional_files = {
    # Same basenames as on-device, same directory as bugle_db, so sqlite3
    # picks them up automatically on connect() — WAL can hold the most
    # recent, not-yet-checkpointed messages.
    "bugle_db_wal": "databases/bugle_db-wal",
    "bugle_db_shm": "databases/bugle_db-shm",
}


# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "date" is this module's
# own run() output for messages.received_timestamp; "received_timestamp"
# is that same column's own SQL name as it appears on a carved/recovered
# messages row instead; "timestamp" is parts' own mirror column (kept in
# sync with received_timestamp by a DB trigger) as it appears on a carved
# parts row. All three are Unix epoch milliseconds.
timestamp_fields = {"date": "ms", "received_timestamp": "ms", "timestamp": "ms"}

# "attachment_path" is a full archive ui_path for the thumbnail/full-view
# feature (see app/artifact_media.py). parts.uri is a content:// reference
# (e.g. 'content://mms/part/12'), not a filesystem path — resolvable only
# on a live device, not from an archive. parts.local_cache_path IS a real
# on-device filesystem path though, confirmed against a real extraction:
# it lives under this same app's own data directory (this module's
# app_path), so no separate fixed base is needed the way Android WhatsApp's
# media does. Coverage is partial by nature of what "cache" means — not
# every attachment part has a surviving cache entry (27 of the ~150+
# attachment parts on the extraction this was checked against). The cache
# filename itself carries no extension ("..._part_N_.bin" regardless of
# whether the real content is a jpeg, png, or mp4 — confirmed by decoding
# several), which is why sniff_media_kind() in media_viewer.py exists
# rather than trusting the extension here.
media_fields = ["attachment_path"]

# Checked directly against the real schema before declaring this (PRAGMA
# table_info / CREATE TABLE), not assumed: messages._id, conversations._id,
# participants._id, and parts._id are all genuine
# INTEGER PRIMARY KEY AUTOINCREMENT columns (real rowid aliases).
# conversation_id is messages.conversation_id, the primary key of
# conversations. That is the Chat Groups key, and it is the shortcut id
# Recent Message Notifications stores for this app. It is not
# sms_thread_id. The query joins parts, whose conversation_id matched
# messages.conversation_id on every checked row.
# raw_sender_id can genuinely be NULL (a self-originated message with no
# LEFT JOIN match) — presence_fields drops the entry rather than citing a
# nonexistent participant row. raw_first_part_id cites only the FIRST
# part grouped under this row (parts._id) — a report row can aggregate
# several real part rows (text + one or more attachments), matching the
# same "first cached attachment" precedent this file's own attachment_path
# already follows; hidden since it's the specific representative part
# chosen for citation, not a standalone identifier for the row as a whole.
hidden_fields = ["chat_members", "raw_first_part_id"]
record_source = [
    {"label": "Message", "file_key": "bugle_db", "table": "messages",
     "rowid_fields": ["raw_message_id", "_id"]},
    {"label": "Conversation", "file_key": "bugle_db", "table": "conversations",
     "rowid_fields": ["conversation_id"]},
    {"label": "Sender", "file_key": "bugle_db", "table": "participants",
     "rowid_fields": ["raw_sender_id"], "presence_fields": ["raw_sender_id"]},
    {"label": "Part", "file_key": "bugle_db", "table": "parts",
     "rowid_fields": ["raw_first_part_id"]},
]


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "conversation_id", "id": "conversation_id", "name": ["conversation"], "sent": ["Sent"], "media": ["attachment_path"], "time": "date", "participants": ["sender"], "members": "chat_members", "message": "body", "deleted": {}, "type": "chat_type"}

def run(paths):
    from artifact_runner import first_nonempty, open_db_readonly, resolve_path_after_marker

    app_base = paths.get("_app_base_ui_path")

    conn = open_db_readonly(paths["bugle_db"])

    rows = conn.execute("""
        SELECT
            m._id            AS id,
            m.conversation_id,
            m.sender_id,
            m.received_timestamp AS ts,
            m.self_id         AS self_id,
            c.name            AS conv_name,
            sender.sub_id     AS sender_sub_id,
            sender.full_name  AS sender_full_name,
            sender.display_destination AS sender_dest,
            p._id             AS part_id,
            p.text            AS part_text,
            p.uri             AS part_uri,
            p.content_type    AS part_content_type,
            p.latitude        AS part_lat,
            p.longitude       AS part_lon,
            p.local_cache_path AS part_cache_path
        FROM parts p
        JOIN messages m         ON m._id = p.message_id
        JOIN conversations c    ON c._id = p.conversation_id
        LEFT JOIN participants sender ON sender._id = m.sender_id
        ORDER BY m.conversation_id, m.received_timestamp, m._id
    """).fetchall()
    # INNER JOIN parts (not LEFT): messages with zero parts are confirmed (by
    # inspecting real data) to be self-originated protocol/session bookkeeping
    # rows with nothing user-visible, never actual sent/received content —
    # matches Josh Hickman's ALEAPP googleMessages.py reference implementation,
    # which excludes them the same way.

    # Group part rows by message — a message can have several parts (text +
    # one or more attachments, e.g. a caption plus an image).
    # Recipients (2026-10-24): a conversation's members are
    # conversation_participants (at extraction); the device owner's own
    # entries are participants with sub_id != -2 (the same test the
    # direction uses), and messages.self_id names which of the owner's
    # numbers a message used (A15: +19843761898 and +447578947805).
    people = {}
    for pid, sub, full, disp, norm in conn.execute(
            "SELECT _id, sub_id, full_name, display_destination, normalized_destination FROM participants"):
        people[pid] = (sub, full or disp or norm or f"[participant {pid}]", norm or disp)
    conv_members = {}
    for cid, pid in conn.execute("SELECT conversation_id, participant_id FROM conversation_participants"):
        if pid in people and people[pid][0] == -2:
            conv_members.setdefault(cid, []).append(pid)
    own_numbers = [v[2] for v in people.values() if v[0] != -2 and v[2]]
    default_me = f"Me ({own_numbers[0]})" if own_numbers else "Me"
    own_by_conv = {}
    for cid, sid in conn.execute("SELECT DISTINCT conversation_id, self_id FROM messages"):
        own = people.get(sid)
        if own and own[2]:
            own_by_conv.setdefault(cid, {})[f"Me ({own[2]})"] = None

    def _chat_members(r):
        """Everyone in the conversation (Chat Groups summary only): the
        owner's number(s) used in it, then conversation_participants — as
        at extraction, never a claim about one message's recipients."""
        members = conv_members.get(r["conversation_id"], [])
        mine = own_by_conv.get(r["conversation_id"]) or ([default_me] if default_me else ["Me"])
        return ", ".join(list(mine) + [people[p][1] for p in members]
                         or ["[conversation members not recorded]"])

    by_message = {}
    order = []
    for r in rows:
        mid = r["id"]
        if mid not in by_message:
            by_message[mid] = {"row": r, "parts": []}
            order.append(mid)
        by_message[mid]["parts"].append(r)

    out = []
    for mid in order:
        entry = by_message[mid]
        r = entry["row"]
        parts = entry["parts"]

        texts = [p["part_text"] for p in parts if p["part_text"]]
        media = [p for p in parts
                 if p["part_uri"] or (p["part_content_type"] and "text" not in (p["part_content_type"] or ""))]

        if texts:
            body = " | ".join(texts)
        elif any(p["part_lat"] for p in parts):
            loc = next(p for p in parts if p["part_lat"])
            body = f"[Location] {loc['part_lat']}, {loc['part_lon']}"
        elif media:
            body = f"[{len(media)} attachment(s): " + ", ".join(
                m["part_content_type"] or "unknown" for m in media) + "]"
        else:
            body = None

        # Only the first cached attachment gets a thumbnail column (one
        # media_fields column per row) — attachment_count/attachment_types
        # below still summarize every attachment on the row regardless of
        # whether any of them has a surviving cache entry.
        attachment_path = ''
        for m in media:
            resolved = resolve_path_after_marker(
                app_base, m["part_cache_path"], 'com.google.android.apps.messaging/')
            if resolved:
                attachment_path = resolved
                break

        # Raw ms epoch, not a formatted string — the Report table formats
        # this per the case's timestamp-display setting (see
        # timestamp_fields above). A prior version of this line used a bare
        # fromtimestamp() with no tz=, which converts using the ANALYSIS
        # MACHINE's OS timezone rather than UTC — a real bug, found and
        # fixed the same way in whatsapp.py — but that whole concern is
        # moot now that no formatting happens here at all.
        date_str = r["ts"]

        # AOSP Messaging: ParticipantData.OTHER_THAN_SELF_SUB_ID = -2. A
        # sender whose sub_id is anything else is the self participant (the
        # message is outgoing). A NULL sub_id is left blank rather than
        # assumed Incoming, since a NULL comparison is neither true nor
        # false — matches Josh Hickman's ALEAPP googleMessages.py reference.
        sub_id = r["sender_sub_id"]
        if sub_id is None:
            direction, sender_name = None, first_nonempty(r["sender_full_name"], r["sender_dest"], default=None)
        elif sub_id != -2:
            direction, sender_name = "Sent", "Me"
        else:
            direction, sender_name = "Received", first_nonempty(r["sender_full_name"], r["sender_dest"], default=None)

        out.append({
            "conversation_id": r["conversation_id"],
            "date": date_str,
            "conversation": r["conv_name"],
            "sender": sender_name,
            "sender_destination": r["sender_dest"],   # raw number/handle — citation, not just resolved name
            "chat_members": _chat_members(r),
            "chat_type": "Group" if len(conv_members.get(r["conversation_id"], [])) > 1 else "1:1",
            "direction": direction,
            "attachment_path": attachment_path,
            "body": body,
            "attachment_count": len(media),
            "attachment_types": ", ".join(sorted({m["part_content_type"] for m in media if m["part_content_type"]})) or None,
            "raw_message_id": r["id"],
            "raw_sender_id": r["sender_id"],
            "raw_first_part_id": parts[0]["part_id"],
        })

    conn.close()
    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'google_messages_deleted',
  'name': 'Google Messages — Deleted & Changed Messages',
  'description': 'Deleted and changed Google Messages message records from bugle_db (table '
                 "messages). A message's text and attachments are not in this table but in "
                 'parts rows: deleted and changed parts are in Google Messages — Deleted & '
                 'Changed Parts. Live messages are in Google Messages (SMS/MMS/RCS); deleted '
                 'conversations from the audit tables are in Google Messages — Recovered '
                 '(deleted conversations).\n'
                 '\n'
                 'Each row is one record as found in the messages table, never joined to '
                 'another table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says '
                 'Deleted (no longer in the table), Changed (an earlier version of a row still '
                 'there, listed only when one of the columns that matter changed, with the '
                 'current value beside it as "(now)") or an unfinished write/delete under a '
                 'hot journal. How found says how each record was recovered and how sure that '
                 'is; a record found by pattern has no row id. Values are as stored: no code '
                 'is turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'Times are Unix milliseconds, as in the Google Messages report.\n'
                 '\n'
                 "linked_parts: where this message's parts (its text and attachments) were "
                 'found. Each parts row names its message in parts.message_id, so the link is '
                 "made by finding the parts rows whose message_id is this record's message_id, "
                 'in the table now (live, with their row ids) and in the records Google '
                 'Messages — Deleted & Changed Parts finds (recovered, with their row ids); a '
                 'message can have several (up to 3 on the test images) or none (178 of 1,278 '
                 'live messages on the Josh Hickman image). Only ids are compared: no value of '
                 'a part is brought in. How the link can be wrong: messages._id and parts._id '
                 'are AUTOINCREMENT (checked 2026-11-27 on the Josh Hickman and CTF26 Android '
                 '14 images), so SQLite does not hand out an id again, but an app that '
                 're-inserts rows with their old ids (a restore from backup) can still put a '
                 'different record under the same id; an earlier version found in the WAL and '
                 'a live row with the same id were stored at different times; a record found '
                 'by pattern has no row id and cannot be linked to by id; several copies of '
                 'one record can carry the same id.\n'
                 '\n'
                 'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found '
                 'earlier (carried over from the Google Messages report): bugle_db has PRAGMA '
                 'secure_delete = FAST; the freed page ranges were all zero on a database '
                 'known to have a deleted conversation. A real negative for recovery, not '
                 'evidence nothing was deleted.',
  'timestamp_fields': {'sent_timestamp': 'ms', 'received_timestamp': 'ms'},
  'core_fields': ['status',
                  'received_timestamp',
                  'sent_timestamp',
                  'mms_subject',
                  'linked_parts',
                  'changed_fields',
                  'how_found',
                  'copies'],
  'file_key': 'bugle_db',
  'table': 'messages',
  'label': 'Message',
  'fields': {'_id': 'message_id',
             'conversation_id': 'conversation_id',
             'sender_id': 'sender_id',
             'sent_timestamp': 'sent_timestamp',
             'received_timestamp': 'received_timestamp',
             'message_protocol': 'message_protocol',
             'message_status': 'message_status',
             'mms_subject': 'mms_subject',
             'sms_message_uri': 'sms_message_uri',
             'rcs_message_id': 'rcs_message_id'},
  'version_fields': ['conversation_id', 'sender_id', 'message_protocol', 'mms_subject'],
  'links': [{'name': 'linked_parts',
             'what': 'parts',
             'column': '_id',
             'table': 'parts',
             'key': 'message_id',
             'report': 'google_messages_deleted_parts'}]},
 {'script': 'google_messages_deleted_parts',
  'name': 'Google Messages — Deleted & Changed Parts',
  'description': 'Deleted and changed Google Messages parts records from bugle_db (table '
                 "parts: a message's text and each of its attachments is a parts row). Live "
                 'messages are in Google Messages (SMS/MMS/RCS); deleted and changed message '
                 'records (table messages) are in Google Messages — Deleted & Changed '
                 'Messages.\n'
                 '\n'
                 'Each row is one record as found in the parts table, never joined to another '
                 'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id. Values are as stored: no code is '
                 'turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'timestamp is Unix milliseconds, as in the Google Messages report.\n'
                 '\n'
                 'linked_message: where the message this part belongs to was found. '
                 'parts.message_id holds the _id of its messages row, so the link is made by '
                 'comparing the two, in the table now (live: the message is in Google '
                 'Messages) and in the records Google Messages — Deleted & Changed Messages '
                 'finds (recovered, with its row id). Every live part names a live message on '
                 'the test images (2026-11-27). Only ids are compared: no value of the message '
                 'is brought in. How the link can be wrong: messages._id and parts._id are '
                 'AUTOINCREMENT (checked 2026-11-27 on the Josh Hickman and CTF26 Android 14 '
                 'images), so SQLite does not hand out an id again, but an app that re-inserts '
                 'rows with their old ids (a restore from backup) can still put a different '
                 'record under the same id; an earlier version found in the WAL and a live row '
                 'with the same id were stored at different times; a record found by pattern '
                 'has no row id and cannot be linked to by id; several copies of one record '
                 'can carry the same id.\n'
                 '\n'
                 'Also listed: live parts rows whose message is gone (status Orphaned): not '
                 'deleted, so not recovered, and not in the main report, which lists a row '
                 'only with its message. linked_message says whether that message was '
                 'recovered. None on the test image checked (2026-11-21).\n'
                 '\n'
                 'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. bugle_db '
                 'has PRAGMA secure_delete = FAST (carried over from the Google Messages '
                 'report).',
  'timestamp_fields': {'timestamp': 'ms'},
  'core_fields': ['status',
                  'timestamp',
                  'message',
                  'content_type',
                  'file_name',
                  'linked_message',
                  'changed_fields',
                  'message (now)',
                  'how_found',
                  'copies'],
  'file_key': 'bugle_db',
  'table': 'parts',
  'label': 'Part (text or attachment)',
  'fields': {'_id': 'part_id',
             'message_id': 'message_id',
             'conversation_id': 'conversation_id',
             'text': 'message',
             'content_type': 'content_type',
             'uri': 'uri',
             'file_name': 'file_name',
             'file_size_bytes': 'file_size',
             'timestamp': 'timestamp',
             'latitude': 'latitude',
             'longitude': 'longitude',
             'local_cache_path': 'local_cache_path'},
  'orphan_of': {'column': 'message_id', 'in': ['messages', '_id']},
  'version_fields': ['text', 'content_type', 'uri', 'file_name'],
  'links': [{'name': 'linked_message',
             'what': 'message',
             'column': 'message_id',
             'table': 'messages',
             'key': '_id',
             'report': 'google_messages_deleted'}]}]
