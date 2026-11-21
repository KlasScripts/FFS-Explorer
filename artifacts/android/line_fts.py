name = "LINE — Recovered (full-text search index)"
description = (
    "Deleted and changed records of LINE's separate full-text-search index of "
    "message bodies (unencrypted_test_full_text_search_message.db, tables "
    "fts_message_content and message_chat_relation), independent of the chat_history "
    "table the \"Line\" report reads. The index keeps only message text and which "
    "conversation it belongs to: no time, no sender, no direction; do not infer those "
    "for a row here, they are not in this source. The two tables share an id "
    "(fts_message_content.docid = message_chat_relation.message_id = chat_history.id "
    "in naver_line), shown as stored; the tables are not joined.\n"
    "\n"
    "Each row is one record as found in one table (ASSUMPTIONS.md A-95). How found "
    "says how each record was recovered and how sure that is.\n"
    "\n"
    "Checked on the Josh Hickman Android 14 image: one message deleted from "
    "chat_history (no trace left there) is still here, from the index's WAL history "
    "(its text in fts_message_content, its conversation in message_chat_relation); a "
    "row-level match confirmed the text and conversation id line up exactly with the "
    "gap in chat_history. Deleted messages from chat_history itself are in LINE — "
    "Deleted & Changed Messages."
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md A-95,
# 2026-11-21; until then this file carved both tables with recoverable_tables
# and returned no live rows). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined.
# Columns checked on the Josh Hickman Android 14 image (2026-11-21):
# fts_message_content docid, c0formatted_message; message_chat_relation
# message_id, chat_id.
files = {
    "main": "databases/unencrypted_test_full_text_search_message.db",
}

deleted_of = "line"   # its main report: the app location comes from there (A-95)

report_type = "recovered_records"
recovered_records = [
    {
        "file_key": "main",
        "table": "fts_message_content",
        "label": "Message text (index)",
        "fields": {"docid": "message_id", "c0formatted_message": "message"},
        "version_fields": ["c0formatted_message"],
    },
    {
        "file_key": "main",
        "table": "message_chat_relation",
        "label": "Message's conversation (index)",
        "fields": {"message_id": "message_id", "chat_id": "chat_id"},
        "version_fields": ["chat_id"],
    },
]

core_fields = ["status", "record", "message_id", "message", "chat_id", "changed_fields",
               "message (now)", "how_found", "copies"]
hidden_fields = ["recovered", "recovery_method", "recovery_version", "source_table",
                 "source_file_key", "raw_rowid", "raw_page", "raw_file", "raw_offset",
                 "raw_length", "source", "table_certain", "copy_locations"]
