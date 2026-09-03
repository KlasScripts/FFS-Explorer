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


def run(paths):
    import sqlite3
    from datetime import timezone

    from artifact_runner import decode_plist_blob

    conn = sqlite3.connect(paths["direct_db"])
    conn.row_factory = sqlite3.Row
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
