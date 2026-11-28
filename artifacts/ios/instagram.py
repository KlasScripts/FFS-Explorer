name = "Instagram Direct Messages"
description = (
    "Messages from Instagram's local iOS store "
    "(Library/Application Support/DirectSQLiteDatabase/<account-id>.db, joining "
    "messages to threads on thread_id). Confirmed real and independently "
    "cross-checked against iLEAPP's own instagramThreads.py (same query shape, "
    "same table/view names) — on the iOS 16.5 CTF23 Cellebrite image this was "
    "built against, both this parser and iLEAPP's own documented sample_data "
    "for the same test-image family (abe_ios16) report the identical row "
    "count: 24. The database's own base file is a near-empty ~4KB stub — ALL "
    "real content, including the entire schema, sits in its -wal sidecar "
    "(confirmed: -wal is 1.4MB), so optional_files below is not optional in "
    "practice for this app, just declared the normal way. "
    "Message content is stored per-row as an NSKeyedArchiver-serialized plist "
    "BLOB (Apple's standard Objective-C object-archive format, not something "
    "Instagram invented), decoded via artifact_runner.decode_plist_blob (which "
    "wraps the nska_deserialize library, the same tool iLEAPP itself uses for "
    "this format). "
    "Scope of this version, stated plainly: only plain text messages are "
    "decoded into the Message column. A row with Message left blank is a "
    "real message whose payload is something else this version doesn't yet "
    "decode — a VOIP call event, a reaction, or shared media — confirmed "
    "present in the archived plist's own structure (keys like "
    "IGDirectThreadActivityAnnouncement*threadActivity and "
    "IGDirectPublishedMessageMedia*media exist and were seen in this session's "
    "manual testing) but not yet extracted here; the row is still surfaced "
    "with its real thread/sender/timestamp rather than silently dropped. "
    "record_source is declared, but expect its Hex-panel jump to usually read "
    "'may be WAL-only' for this app specifically — messages.row_id lives in "
    "an un-checkpointed WAL frame for every row tested, and locate_live_row "
    "only walks the base file's own b-tree (a known, documented project-wide "
    "limitation, not specific to this parser)."
)
# Instagram's own container (Data/Application/<GUID>), not an App Group —
# confirmed against this session's real extraction: the App-Group container
# some earlier session guesswork initially assumed held the real messages
# (messagingMailbox/ig-msys-*.db) turned out to hold account/contact/sync
# infrastructure and E2E encryption key material instead, not the message
# content itself.
app_bundle_id = "com.burbn.instagram"
files = {
    # The filename's numeric stem is the Instagram account id, different per
    # device/login — resolved via a glob at run time (see artifact_runner.py's
    # _resolve_glob_subpath, added the same day for exactly this case). If
    # more than one account's database exists in the archive, only the first
    # match found is used — a known, honest limitation, not silently handled.
    "direct_db": "Library/Application Support/DirectSQLiteDatabase/*.db",
}
optional_files = {
    "direct_db_wal": "Library/Application Support/DirectSQLiteDatabase/*.db-wal",
    "direct_db_shm": "Library/Application Support/DirectSQLiteDatabase/*.db-shm",
}
timestamp_fields = {"timestamp": "s"}
hidden_fields = ["raw_row_id"]
record_source = {
    "label": "Direct Message",
    "file_key": "direct_db",
    "table": "messages",
    "rowid_fields": ["raw_row_id"],
}


def _message_fields(archive):
    """sender_id, message, timestamp (Unix seconds) and thread_id from one
    messages.archive BLOB, as run() reads them; {} when it does not decode."""
    from datetime import timezone

    from artifact_runner import decode_plist_blob
    plist = decode_plist_blob(archive)
    if not isinstance(plist, dict):
        return {}
    metadata = plist.get("IGDirectPublishedMessageMetadata*metadata")
    content = plist.get("IGDirectPublishedMessageContent*content")
    if not isinstance(metadata, dict):
        return {}
    ts = metadata.get("NSDate*serverTimestamp")
    epoch_s = None
    if ts is not None:
        try:
            epoch_s = ts.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            epoch_s = None
    return {"sender_id": metadata.get("NSString*senderPk"),
            "message": content.get("NSString*string") if isinstance(content, dict) else None,
            "timestamp": epoch_s,
            "archive_thread_id": metadata.get("NSString*threadId")}


def _deleted_fields(values):
    """deleted_tables 'decode' (A-95): the readable fields of a recovered
    messages record, the same way run() reads a live one."""
    return _message_fields(values.get("archive"))


def run(paths):
    from datetime import timezone

    from artifact_runner import decode_plist_blob, open_db_readonly

    conn = open_db_readonly(paths["direct_db"])
    db_rows = conn.execute("""
        SELECT messages.message_id, messages.thread_id, messages.archive,
               messages.row_id, threads.viewer_id
        FROM messages, threads
        WHERE messages.thread_id = threads.thread_id
        ORDER BY messages.row_id ASC
    """).fetchall()
    conn.close()

    out = []
    for r in db_rows:
        plist = decode_plist_blob(r["archive"])
        if not isinstance(plist, dict):
            continue
        metadata = plist.get("IGDirectPublishedMessageMetadata*metadata")
        content = plist.get("IGDirectPublishedMessageContent*content")
        if not isinstance(metadata, dict):
            continue

        sender_pk = metadata.get("NSString*senderPk")
        thread_id_val = metadata.get("NSString*threadId") or r["thread_id"]
        ts = metadata.get("NSDate*serverTimestamp")
        message = content.get("NSString*string") if isinstance(content, dict) else None

        direction = None
        try:
            direction = "Sent" if int(sender_pk) == int(r["viewer_id"]) else "Received"
        except (TypeError, ValueError):
            pass  # sender_pk/viewer_id missing or non-numeric — leave direction unknown, not guessed

        # NSDate values from plistlib/nska_deserialize come back as naive
        # Python datetimes representing UTC wall-clock time (Apple's Cocoa
        # epoch, already converted) — explicitly attaching tzinfo before
        # .timestamp() is required, or Python would wrongly assume the
        # ANALYSIS MACHINE's local timezone instead (the exact bug class
        # this project's own Conventions section warns against).
        epoch_s = None
        if ts is not None:
            try:
                epoch_s = ts.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                epoch_s = None

        out.append({
            "sender_id": sender_pk,
            "direction": direction,
            "message": message,
            "timestamp": epoch_s,
            "thread_id": thread_id_val,
            "raw_message_id": r["message_id"],
            "raw_row_id": r["row_id"],
            "source_table": "messages",
        })
    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'instagram_deleted_messages',
  'name': 'Instagram — Deleted & Changed Direct Messages',
  'description': "Deleted and changed Instagram direct-message records from the account's "
                 'DirectSQLiteDatabase/<account id>.db (table messages). The text, sender and '
                 'time are inside the archive BLOB (an NSKeyedArchiver plist) and are read out '
                 'of it the same way as in Instagram Direct Messages: message, sender_id, '
                 'timestamp. A record that does not decode is listed with its stored values '
                 '(counted in the run log). Live messages are in Instagram Direct Messages.\n'
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
                 'Instagram rewrites a message under a new row id, often: on the test image '
                 '943 row ids were handed out (sqlite_sequence) for 42 live messages. So a '
                 "record whose message_id is still a live message's is an earlier version of "
                 'that message, never a deletion: it is listed as Changed only when the text '
                 'or thread differs, with the current text beside it as "message (now)"; '
                 'otherwise it is counted in the run log ("tied to live row" in how_found). A '
                 'record listed as Deleted is one whose message_id is in no live row: a '
                 'message unsent or deleted, or one removed when its thread went.\n'
                 '\n'
                 'timestamp is Unix seconds, as in Instagram Direct Messages.\n'
                 '\n'
                 'linked_thread: where the thread this record names (thread_id) was found, '
                 'live or recovered (the threads table has no deleted report of its own; only '
                 'the id is compared). thread_id is a text id from Instagram, not a row id, so '
                 'SQLite does not reuse it; the link can still be wrong where a record found '
                 'by pattern has a cut-off value.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman iOS 17 image: 464 records carrying 43 '
                 'message ids; 42 of those are live messages (rewrites, counted in the run '
                 'log), and 1 message is gone, found 3 times (one row, copies 3).',
  'timestamp_fields': {'timestamp': 's'},
  'core_fields': ['status',
                  'timestamp',
                  'message',
                  'sender_id',
                  'linked_thread',
                  'changed_fields',
                  'message (now)',
                  'how_found',
                  'copies'],
  'file_key': 'direct_db',
  'table': 'messages',
  'label': 'Message',
  'fields': {'message_id': 'message_id',
             'thread_id': 'thread_id',
             'class_name': 'class_name'},
  'decode': '_deleted_fields',
  'decoded_fields': ['sender_id', 'message', 'timestamp', 'archive_thread_id'],
  'identity': ['message_id'],
  'stable_id': ['message_id'],
  'version_fields': ['thread_id', 'message'],
  'links': [{'name': 'linked_thread',
             'what': 'thread',
             'column': 'thread_id',
             'table': 'threads',
             'key': 'thread_id'}]}]
