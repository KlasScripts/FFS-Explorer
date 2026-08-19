name = "Google Messages — Recovered (deleted conversations)"
description = ("Google Messages (bugle_db) hard-deletes a conversation by "
              "cascading through messages/parts, and on the case this was "
              "built against those tables' freed bytes are actively zeroed "
              "at delete time (PRAGMA secure_delete = FAST, confirmed by "
              "inspecting the freed page ranges directly) — message CONTENT "
              "is not recoverable, and this report does not attempt to "
              "carve any (see the main \"Google Messages (SMS/MMS/RCS)\" "
              "report's notes). What does survive is conversation-level "
              "metadata: conversation_to_participants_audit_log and "
              "conversation_participants_audit_log are plain audit tables "
              "with no cascade foreign key to conversations, so they still "
              "hold every conversation_id that ever existed, including ones "
              "now gone from the live conversations table. This report "
              "lists exactly that gap — conversation_ids present in the "
              "audit trail but absent from conversations — with who the "
              "conversation was with (resolved from the live participants "
              "table, still present since participants aren't cascade-"
              "deleted) and the first/last audit timestamps. Row count is "
              "expected to be 0 on a case with no deleted conversations; a "
              "0-row report here is a checked negative, not a skipped "
              "check. This is a different recovery mechanism from the "
              "freeblock/WAL/header-signature carving used for other "
              "apps' companion reports (Burner, LINE) — it reconstructs "
              "from surviving relational rows in the live database, not "
              "from raw page bytes, so it has no recoverable_tables "
              "declaration and does not go through sqlite_carve.")
app_path = "data/data/com.google.android.apps.messaging"
files = {
    "bugle_db": "databases/bugle_db",
}
optional_files = {
    "bugle_db_wal": "databases/bugle_db-wal",
    "bugle_db_shm": "databases/bugle_db-shm",
}

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. Both are this module's
# own fields (no carving/recoverable_tables involved here at all — see
# description above), Unix epoch milliseconds.
timestamp_fields = {"first_audit_event": "ms", "last_audit_event": "ms"}


def run(paths):
    import sqlite3

    conn = sqlite3.connect(paths["bugle_db"])
    conn.row_factory = sqlite3.Row

    deleted_ids = [r["conversation_id"] for r in conn.execute("""
        SELECT DISTINCT conversation_id FROM conversation_to_participants_audit_log
        WHERE conversation_id NOT IN (SELECT _id FROM conversations)
    """)]

    out = []
    for conv_id in deleted_ids:
        audit_rows = conn.execute("""
            SELECT operation_datetime, operation_type, participant_id
            FROM conversation_to_participants_audit_log
            WHERE conversation_id = ?
            ORDER BY operation_datetime, _id
        """, (conv_id,)).fetchall()

        participant_ids = sorted({r["participant_id"] for r in audit_rows})
        parties = []
        for pid in participant_ids:
            live = conn.execute(
                "SELECT display_destination, normalized_destination, full_name FROM participants WHERE _id = ?",
                (pid,)).fetchone()
            if live:
                parties.append(live["full_name"] or live["display_destination"] or live["normalized_destination"]
                               or f"[participant _id={pid}, no destination on file]")
            else:
                # Participant record itself is also gone (not the case
                # observed when this was built, but participants aren't
                # exempt from deletion in general) — fall back to its own
                # audit log rather than reporting nothing.
                hist = conn.execute("""
                    SELECT display_destination, normalized_destination, full_name
                    FROM participants_audit_log WHERE participant_id = ?
                    ORDER BY operation_datetime DESC LIMIT 1
                """, (pid,)).fetchone()
                parties.append(
                    (hist["full_name"] or hist["display_destination"] or hist["normalized_destination"]
                     if hist else None) or f"[participant _id={pid}, no surviving record]")

        first_ts = audit_rows[0]["operation_datetime"]
        last_ts = audit_rows[-1]["operation_datetime"]

        # operation_type 1/3 line up with join/leave in every case checked
        # on this extraction (paired 1-then-3 per participant, and a
        # conversation missing its own follow-up "1" after the last "3" is
        # exactly the set that's actually gone) — but this is inferred from
        # the audit table's own column names (rcs_group_join_status) and
        # observed pairing, not a documented enum, so it's reported as
        # "first/last audit event" rather than asserted as create/delete.
        out.append({
            "conversation_id": conv_id,
            "conversation_with": ", ".join(parties),
            "first_audit_event": first_ts,
            "last_audit_event": last_ts,
            "audit_event_count": len(audit_rows),
            "message_content": "[not recoverable — secure_delete zeroed the freed bytes, see report description]",
            "recovered": True,
            "source_table": "conversation_to_participants_audit_log",
        })

    conn.close()
    return out
