name = "LINE — Recovered (full-text search index)"
description = ("LINE keeps a separate full-text-search index of message "
              "bodies (unencrypted_test_full_text_search_message.db, tables "
              "fts_message_content and message_chat_relation) independent "
              "of the main chat_history table this app's primary \"Line\" "
              "report reads. On the extraction this was built against, one "
              "message deleted from chat_history (with no trace left there — "
              "see the \"Line\" report's description) was still recoverable "
              "here via WAL frame history: this table's WAL had not yet been "
              "checkpointed past it. This index carries only message text "
              "and which conversation it belongs to — no timestamp, no "
              "sender, no direction; do not infer those for a recovered row, "
              "they simply aren't in this source. Row-level rowid match "
              "confirmed the message text and its conversation id line up "
              "exactly with the gap left in chat_history.")
app_path = "data/data/jp.naver.line.android"
files = {
    "main": "databases/unencrypted_test_full_text_search_message.db",
}
optional_files = {
    "main_wal": "databases/unencrypted_test_full_text_search_message.db-wal",
    "main_shm": "databases/unencrypted_test_full_text_search_message.db-shm",
}

# Declarative only — no recovery code belongs here. Both tables share a
# rowid (fts_message_content.docid == message_chat_relation.message_id ==
# chat_history.id in the main database), which is how a recovered row here
# gets tied back to a conversation without needing its own lookup table.
recoverable_tables = ["fts_message_content", "message_chat_relation"]


def run(paths):
    return []
