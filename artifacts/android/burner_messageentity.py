name = "Burner — Deleted & Changed Records"
description = (
    "Deleted and changed Burner records from both of its databases: "
    "com.adhoclabs.burner.messages.db (table DbMessage, Burner's primary message "
    "store, shown live in the \"Burner\" report) and burnerDatabase.db (table "
    "MessageEntity, a separate store that keeps each message as one JSON object in "
    "its value column, shown here split into value.<key> fields).\n"
    "\n"
    "Each row is one record as found in one table, never joined to another table "
    "(ASSUMPTIONS.md A-95). Status says Deleted, Changed (an earlier version of a row "
    "still there, listed only when one of the columns that matter changed, the current "
    "value beside it as \"(now)\") or an unfinished write/delete. How found says how "
    "each record was recovered and how sure that is.\n"
    "\n"
    "What was found before this became its own report (2026-11-21; carried over): on "
    "the extraction this was built against, MessageEntity kept, via WAL history, a "
    "message the primary store had already lost entirely; DbMessage had nothing "
    "recoverable (no WAL, and carving its live pages found nothing). The same message "
    "captured at two points of the WAL history is kept as two rows: a field changing "
    "between captures (a read flag) is real signal. KNOWN ISSUE: MessageEntity's own "
    "direction is unreliable for Text messages. Cross-checked against DbMessage."
    "direction (validated against ground truth) for every message in both databases: "
    "right on all 3 Voice/Call rows, wrong on all 6 confirmed-Incoming Text rows "
    "(always \"Outbound\"). Each MessageEntity row says so in value_caveat; do not "
    "treat its direction as fact without corroboration.\n"
    "\n"
    "dateCreated is Unix milliseconds, as in the Burner report."
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md A-95,
# 2026-11-21: main reports hold live rows only; until then this file carved
# MessageEntity with recoverable_tables and the Burner report carved DbMessage).
# The shared code in artifact_runner.recovered_records_report recovers the
# records with sqlite_recovery and makes the rows. Never joined.
# Column names checked against both tables on the Josh Hickman Android 14 image
# (2026-11-21): DbMessage id, burnerId, contactId, dateCreated, direction, type,
# state, isRead, mediaUrl, text; MessageEntity value, burnerId, contactId, id.
app_path = "data/data/com.adhoclabs.burner"
files = {
    "messages": "databases/com.adhoclabs.burner.messages.db",
    "main": "databases/burnerDatabase.db",
}
# The -wal and -journal are extracted beside each file by the runner (A-96).

report_type = "recovered_records"
recovered_records = [
    {
        "file_key": "messages",
        "table": "DbMessage",
        "label": "Message (DbMessage)",
        "fields": {"id": "message_id", "burnerId": "burner_id", "contactId": "contact_id",
                   "dateCreated": "dateCreated", "direction": "direction", "type": "type",
                   "state": "state", "isRead": "isRead", "mediaUrl": "media_url",
                   "text": "message"},
        "version_fields": ["text", "direction", "type", "mediaUrl", "contactId"],
    },
    {
        "file_key": "main",
        "table": "MessageEntity",
        "label": "Message (MessageEntity)",
        "fields": {"id": "message_id", "burnerId": "burner_id", "contactId": "contact_id",
                   "value": "value"},
        "version_fields": ["value"],
        "json_fields": ["value"],
        "field_notes": {
            "value": "This row's \"direction\" is unreliable for Text messages — "
                     "confirmed by cross-checking against the other database. "
                     "Do not depend on it.",
        },
    },
]

timestamp_fields = {"dateCreated": "ms", "value.dateCreated": "ms"}

core_fields = ["status", "record", "dateCreated", "value.dateCreated", "message", "value.text",
               "direction", "value.direction", "value_caveat", "changed_fields", "how_found"]
hidden_fields = ["recovered", "recovery_method", "recovery_version", "source_table",
                 "source_file_key", "raw_rowid", "raw_page", "raw_file", "raw_offset",
                 "raw_length", "source", "table_certain"]
