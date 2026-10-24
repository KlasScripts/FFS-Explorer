name = "Burner"
description = ("Messages from Burner's primary local message store "
              "(com.adhoclabs.burner.messages.db, table DbMessage), live rows "
              "plus anything DbMessage's own freeblocks/freed pages/WAL "
              "history/header-signature scan recovers — merged into this same "
              "report, not a separate one, since it's the same source. Checked, "
              "not skipped: on the extraction this was built against, DbMessage "
              "has no WAL at all and carving its live pages found nothing "
              "recoverable — a real negative, not an unexamined gap. Field-by-"
              "field validated against documented ground truth; no known "
              "reliability issues. A SEPARATE database (burnerDatabase.db) "
              "keeps its own redundant copy of some of this same content under "
              "a different table (MessageEntity) and, on that same extraction, "
              "retained one message via WAL history that this database had "
              "already lost entirely — reported separately, not here, because "
              "it's a genuinely different source with a different confidence "
              "level: see the \"Burner — Recovered (MessageEntity)\" report."
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

# Declarative only — no recovery code belongs here. DbMessage genuinely has
# nothing recoverable on this extraction (no WAL, and carving its live
# pages found nothing — see description above), but the declaration stays:
# on a different extraction, where this same table might actually have
# recoverable content, this is what makes it surface automatically instead
# of requiring another one-off investigation.
recoverable_tables = ["DbMessage"]

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
chat_groups = {"key": "conversation", "name": ["conversation"], "sent": ["Outgoing"], "media": ["media_link"], "media_prefixes": ["[Media]"], "time": "timestamp", "participants": ["sender"], "members": "chat_members", "deleted": {}, "call_pattern": r"^\[Call\]", "type": "chat_type"}

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

        if r["type"] == "Voice":
            # No call-duration column exists anywhere in this schema — per
            # the test image's own documentation, Burner calls are placed
            # through the native phone app, not tracked end-to-end by
            # Burner's own database. `state` (e.g. "CallCompleted") is
            # passed through as stored rather than invented.
            body = f"[Call] {r['state']}"
        elif r["mediaUrl"]:
            body = f"[Media] {r['mediaUrl']}" + (f"  {r['text']}" if r["text"] else "")
        else:
            body = r["text"] or ""

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
            "message": body,
            "read": bool(r["isRead"]),
            "raw_message_id": r["id"],
            "raw_contact_id": r["contactId"],
            "source_table": "DbMessage",
        })
    return out
