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
              "level: see the \"Burner — Recovered (MessageEntity)\" report.")
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


def run(paths):
    import sqlite3

    from artifact_runner import missing_ref_label

    conn = sqlite3.connect(paths["messages"])
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS main_db", (paths["main"],))

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

        out.append({
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
