name = "Burner"
description = ("Messages from Burner's primary local message store "
              "(com.adhoclabs.burner.messages.db, table DbMessage), live rows "
              "only: deleted and changed records of this table and of "
              "burnerDatabase.db's MessageEntity (a separate store that, on "
              "the extraction this was built against, kept one message this "
              "database had lost) are in \"Burner — Deleted & Changed "
              "Records\" (2026-11-21). Field-by-field validated against "
              "documented ground truth; no known reliability issues."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from always 1:1: the contact, or the burner number the message arrived on. This is membership as at extraction, not at the time of any message, so no message row claims who received it.")
app_path = "data/data/com.adhoclabs.burner"
files = {
    "messages": "databases/com.adhoclabs.burner.messages.db",
    "main": "databases/burnerDatabase.db",
}
optional_files = {
    # No WAL exists for either database on the extraction this was built
    # against, but recoverable_tables (below) needs these declared to have
    # anything to search on a different extraction where one does — a
    # WAL/SHM the runner never extracted can't be found by the recovery
    # pass, silently, no error.
    "messages_wal": "databases/com.adhoclabs.burner.messages.db-wal",
    "messages_shm": "databases/com.adhoclabs.burner.messages.db-shm",
}


# Lets the Artifact Viewer's Hex-panel "Record" mode jump straight to this
# row's own on-disk cell in com.adhoclabs.burner.messages.db (see
# ArtifactViewerMixin._art_load_record_hex / sqlite_carve.locate_live_row).
# "rowid_fields" tried in order: "raw_message_id" is this module's own
# run() output for a live row's DbMessage.id (already confirmed a genuine
# SQLite rowid alias, not a synthetic id, by the existing recoverable_tables
# carving pass above — carving backfills exactly this column from the
# cell's own rowid, which only works if it already IS one), "raw_rowid" is
# what a recoverable_tables-carved row carries instead
# (sqlite_carve.recover_deleted_rows) — carved rows resolve to nothing
# here today since locate_live_row only walks the CURRENT live b-tree, not
# freed space, which is the expected outcome for a genuinely deleted row,
# not a bug. Only the "messages" file/table is wired — burnerDatabase.db's
# BurnerEntity/ContactEntity (attached as main_db, looked up by phone
# number rather than by rowid above) aren't; a follow-up, not done here.
record_source = {
    "label":        "Burner Message",
    "file_key":     "messages",
    "table_field":  "source_table",
    "rowid_fields": ["raw_message_id", "raw_rowid"],
}

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "timestamp" is this
# module's own run() output; "dateCreated" is the same column's raw SQL
# name as it appears on a carved/recovered DbMessage row instead (carving
# dumps the table's own column names verbatim) — both can appear as
# separate columns once recovered rows are unioned in.
timestamp_fields = {"timestamp": "ms", "dateCreated": "ms"}


# Linked Report: the cached copy of a message's media (see run()).
link_fields = ["media_link"]
hidden_fields = ["chat_members", "media_link"]

# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "conversation", "name": ["conversation"], "sent": ["Outgoing"], "media": ["media_link", "media_url"], "time": "timestamp", "participants": ["sender"], "members": "chat_members", "deleted": {}, "call_pattern": r"^Call\b", "call_field": "event", "type": "chat_type"}

def run(paths):
    from artifact_runner import missing_ref_label, open_db_readonly

    conn = open_db_readonly(paths["messages"])
    # A read-only main connection does NOT make an ATTACHed database
    # read-only on its own -- confirmed by direct testing, not assumed --
    # so the attached path must be its own file: URI too, or main.db's
    # own WAL/SHM would be exposed to the exact same checkpoint-on-close
    # risk open_db_readonly exists to close for messages.db.
    conn.execute("ATTACH DATABASE ? AS main_db", (f'file:{paths["main"]}?mode=ro',))

    # Own Burner number(s) — BurnerEntity.id is the internal burner uuid,
    # phoneNumber is the actual assigned number ("My Burner" in this data).
    own_numbers = {r["id"]: r["phoneNumber"]
                  for r in conn.execute("SELECT id, phoneNumber FROM main_db.BurnerEntity")}
    # Contact display names, keyed by phone number — lives in a separate
    # database (burnerDatabase.db) from the messages themselves
    # (com.adhoclabs.burner.messages.db); DbMessage.contactId is the raw
    # phone number directly, no intermediate id layer.
    contact_names = {r["phoneNumber"]: r["contactName"]
                     for r in conn.execute("SELECT phoneNumber, contactName FROM main_db.ContactEntity")
                     if r["contactName"]}

    rows = conn.execute("""
        SELECT id, burnerId, contactId, dateCreated, direction, type, state,
               isRead, mediaUrl, text
        FROM DbMessage ORDER BY dateCreated
    """).fetchall()

    out = []
    for r in rows:
        outgoing = r["direction"] == "Outbound"
        direction = "Outgoing" if outgoing else ("Incoming" if r["direction"] == "Inbound"
                                                  else f"[unrecognized direction: {r['direction']!r}]")
        if outgoing:
            party = own_numbers.get(r["burnerId"], missing_ref_label("burner record", "burnerId", r["burnerId"]))
        else:
            party = contact_names.get(r["contactId"], r["contactId"] or "[unknown contact]")

        # message is DbMessage.text exactly as stored. A call or a media
        # message is named in event, not written into the text.
        if r["type"] == "Voice":
            # No call-duration column exists anywhere in this schema — per
            # the test image's own documentation, Burner calls are placed
            # through the native phone app, not tracked end-to-end by
            # Burner's own database. `state` (e.g. "CallCompleted") is
            # passed through as stored rather than invented.
            event = f"Call, {r['state']}" if r["state"] else "Call"
        elif r["mediaUrl"]:
            event = "Media"
        else:
            event = ""

        media_link = ""
        if r["mediaUrl"]:
            # The app's Glide image cache names each download by the SHA-256
            # of its URL + ".0" (verified 2026-10-23 on Android 14
            # JoshHickman: both media URLs have their PNG/JPEG there).
            import hashlib
            from artifact_runner import archive_has
            cached = (f"{paths.get('_app_base_ui_path', 'data/data/com.adhoclabs.burner')}"
                      f"/cache/image_manager_disk_cache/"
                      f"{hashlib.sha256(r['mediaUrl'].encode('utf-8')).hexdigest()}.0")
            media_link = cached if archive_has(paths, cached) else ""
        # Everyone in the chat (Chat Groups summary only): always 1:1 — the
        # burner number used (BurnerEntity, by burnerId) and the contact.
        num = own_numbers.get(r["burnerId"])
        chat_members = ", ".join([f"Me (burner {num})" if num else "Me",
                                  contact_names.get(r["contactId"], r["contactId"] or "[unknown contact]")])
        out.append({
            "chat_members": chat_members,
            "chat_type": "1:1",
            "media_link": media_link,
            "conversation": contact_names.get(r["contactId"], r["contactId"]),
            "timestamp": r["dateCreated"],
            "sender": party,
            "direction": direction,
            "message": r["text"],
            "event": event,
            "media_url": r["mediaUrl"] or "",
            "read": bool(r["isRead"]),
            "raw_message_id": r["id"],
            "raw_contact_id": r["contactId"],
            "source_table": "DbMessage",
        })
    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'burner_deleted_dbmessage',
  'name': 'Burner — Deleted & Changed Messages (DbMessage)',
  'description': 'Deleted and changed Burner messages from com.adhoclabs.burner.messages.db '
                 "(table DbMessage, Burner's primary message store, shown live in the "
                 '"Burner" report). Deleted and changed records of the other store, '
                 'burnerDatabase.db (MessageEntity), are in Burner — Deleted & Changed '
                 'Messages (MessageEntity).\n'
                 '\n'
                 'Each row is one record as found in this table, never joined to another table '
                 '(ASSUMPTIONS.md A-95). Status says Deleted, Changed (an earlier version of a '
                 'row still there, listed only when one of the columns that matter changed, '
                 'the current value beside it as "(now)") or an unfinished write/delete. How '
                 'found says how each record was recovered and how sure that is; damaged says '
                 'why a record is not whole.\n'
                 '\n'
                 'The other Burner store keeps the same messages under the same text id '
                 '(checked 2026-11-27 on the Josh Hickman Android 14 image: the one '
                 'MessageEntity id is also a DbMessage id). The two are separate databases, so '
                 'no link column is made: compare message_id with the other report by hand.\n'
                 '\n'
                 'What was found before (2026-11-21; carried over): on the extraction this was '
                 'built against, DbMessage had nothing recoverable (no WAL, and carving its '
                 'live pages found nothing).\n'
                 '\n'
                 'dateCreated is Unix milliseconds, as in the Burner report.',
  'core_fields': ['status',
                  'dateCreated',
                  'message',
                  'direction',
                  'changed_fields',
                  'message (now)',
                  'damaged',
                  'how_found',
                  'copies'],
  'timestamp_fields': {'dateCreated': 'ms'},
  'file_key': 'messages',
  'table': 'DbMessage',
  'label': 'Message (DbMessage)',
  'fields': {'id': 'message_id',
             'burnerId': 'burner_id',
             'contactId': 'contact_id',
             'dateCreated': 'dateCreated',
             'direction': 'direction',
             'type': 'type',
             'state': 'state',
             'isRead': 'isRead',
             'mediaUrl': 'media_url',
             'text': 'message'},
  'version_fields': ['text', 'direction', 'type', 'mediaUrl', 'contactId']},
 {'script': 'burner_messageentity',
  'name': 'Burner — Deleted & Changed Messages (MessageEntity)',
  'description': 'Deleted and changed Burner records from burnerDatabase.db (table '
                 'MessageEntity, a separate store that keeps each message as one JSON object '
                 'in its value column, shown here split into value.<key> fields). Deleted and '
                 'changed records of the primary store (DbMessage) are in Burner — Deleted & '
                 'Changed Messages (DbMessage).\n'
                 '\n'
                 'Each row is one record as found in this table, never joined to another table '
                 '(ASSUMPTIONS.md A-95). Status says Deleted, Changed (an earlier version of a '
                 'row still there, listed only when one of the columns that matter changed, '
                 'the current value beside it as "(now)") or an unfinished write/delete. How '
                 'found says how each record was recovered and how sure that is; damaged says '
                 'why a record is not whole.\n'
                 '\n'
                 'The other Burner store keeps the same messages under the same text id '
                 '(checked 2026-11-27 on the Josh Hickman Android 14 image: the one '
                 'MessageEntity id is also a DbMessage id). The two are separate databases, so '
                 'no link column is made: compare message_id with the other report by hand.\n'
                 '\n'
                 'Burner rewrites a message here by delete + insert under a new row id, so '
                 'most "Deleted" records are earlier copies of a message that is still there, '
                 'not deletions: identical copies are one row with their number in copies '
                 '(A-95; on the Josh Hickman Android 14 image 99 rows for 17 messages, 18 once '
                 'collapsed, the 18th a real change, the read flag). A record whose message id '
                 'is still live is a rewrite.\n'
                 '\n'
                 'What was found before (2026-11-21; carried over): on the extraction this was '
                 'built against, MessageEntity kept, via WAL history, a message the primary '
                 'store had already lost entirely. The same message captured at two points of '
                 'the WAL history is kept as two rows: a field changing between captures (a '
                 "read flag) is real signal. KNOWN ISSUE: MessageEntity's own direction is "
                 'unreliable for Text messages. Cross-checked against DbMessage.direction '
                 '(validated against ground truth) for every message in both databases: right '
                 'on all 3 Voice/Call rows, wrong on all 6 confirmed-Incoming Text rows '
                 '(always "Outbound"). Each row says so in value_caveat; do not treat its '
                 'direction as fact without corroboration.\n'
                 '\n'
                 'value.dateCreated is Unix milliseconds, as in the Burner report.',
  'core_fields': ['status',
                  'value.dateCreated',
                  'value.text',
                  'value.direction',
                  'value_caveat',
                  'changed_fields',
                  'damaged',
                  'how_found',
                  'copies'],
  'timestamp_fields': {'value.dateCreated': 'ms'},
  'file_key': 'main',
  'table': 'MessageEntity',
  'label': 'Message (MessageEntity)',
  'fields': {'id': 'message_id',
             'burnerId': 'burner_id',
             'contactId': 'contact_id',
             'value': 'value'},
  'version_fields': ['value'],
  'json_fields': ['value'],
  'field_notes': {'value': 'This row\'s "direction" is unreliable for Text messages — '
                           'confirmed by cross-checking against the other database. Do not '
                           'depend on it.'}}]
