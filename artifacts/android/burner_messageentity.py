name = "Burner — Recovered (MessageEntity)"
description = ("Carved/recovered content from a SEPARATE database — "
              "burnerDatabase.db, table MessageEntity — not Burner's primary "
              "message store (that's com.adhoclabs.burner.messages.db / "
              "DbMessage; see the \"Burner\" report). MessageEntity keeps its "
              "own redundant JSON-blob copy of message content and, on this "
              "extraction, retained a message via WAL history that the primary "
              "store had already lost entirely. Every row here is carved, not a "
              "normal query result — lower confidence than the \"Burner\" "
              "report by construction: page/rowid citations reflect exactly "
              "what carving found, and duplication across rows (e.g. the same "
              "message captured at two points in its WAL history) is preserved "
              "rather than collapsed, since a field genuinely changing between "
              "captures (e.g. a read flag) is real signal, not noise. KNOWN "
              "ISSUE: this table's own \"direction\" field is unreliable for "
              "Text-type messages — cross-checked against DbMessage.direction "
              "(already validated against ground truth) for every message "
              "present in both databases: correct on all 3 Voice/Call rows, "
              "wrong on all 6 confirmed-Incoming Text rows (always reports "
              "\"Outbound\" regardless of true direction). Do not treat a "
              "recovered row's direction as fact without independent "
              "corroboration — see each row's own value_caveat.")
app_path = "data/data/com.adhoclabs.burner"
files = {
    "main": "databases/burnerDatabase.db",
}
optional_files = {
    # The WAL is where the recoverable history actually lives — without
    # extracting it here, the recovery pass has nothing to search; it
    # can't discover a file that was never pulled from the archive.
    "main_wal": "databases/burnerDatabase.db-wal",
    "main_shm": "databases/burnerDatabase.db-shm",
}

# Declarative only — no recovery code belongs in an artifact script. This
# whole report's content IS this declaration: artifact_runner.py carves
# MessageEntity for anything not returned by run() below (nothing is), and
# writes the result as this script's entire output.
recoverable_tables = ["MessageEntity"]

# Declarative only, same spirit as recoverable_tables — a caveat attached
# to a recovered field the shared runner already knows is unreliable,
# rather than recovery logic. Keyed by the actual SQL column name: "value",
# not "direction" — MessageEntity stores its whole row as one JSON blob in
# a single TEXT column, so "direction" is a key *inside* that JSON, not a
# column of its own. See `description` above for what was found.
recovery_field_notes = {
    "MessageEntity": {
        "value": "This row's \"direction\" is unreliable for Text messages — "
                 "confirmed by cross-checking against the other database. "
                 "Do not depend on it.",
    },
}

# Raw value (never a formatted string) so the Report table can display it
# per the case's timestamp-display setting. "value.dateCreated" is the
# flattened key sqlite_carve.flatten_json_fields produces for the
# "dateCreated" key inside MessageEntity's single JSON-blob "value" column
# — there is no separate live-row timestamp to also declare here, since
# run() always returns [] and every row is carved.
timestamp_fields = {"value.dateCreated": "ms"}


def run(paths):
    return []
