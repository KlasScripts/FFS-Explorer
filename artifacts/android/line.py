name = "Line"
description = ("Messages from LINE's local database (naver_line, table "
              "chat_history), live rows plus anything freeblocks/freed pages/"
              "WAL history/header-signature scanning recovers from that same "
              "table — merged into this report, since it's all one source. "
              "Checked, not skipped: on the extraction this was built "
              "against naver_line has no WAL at all and carving its live "
              "pages found nothing recoverable, a real negative. A "
              "SEPARATE database (unencrypted_test_full_text_search_message."
              "db) keeps a full-text-search index of message bodies and, on "
              "that same extraction, retained one deleted message's text via "
              "WAL history that this database had already lost entirely — "
              "reported separately, not here, because that index carries no "
              "timestamp or direction for a row, only text and which "
              "conversation it belongs to: see the \"LINE — Recovered "
              "(full-text search index)\" report. Field-by-field validated "
              "against documented ground truth; no known reliability "
              "issues on the fields this report does carry.")
app_path = "data/data/jp.naver.line.android"
files = {
    "naver_line": "databases/naver_line",
}
optional_files = {
    # No WAL exists for naver_line on the extraction this was built against,
    # but recoverable_tables (below) needs this declared to have anything to
    # search on a different extraction where one does.
    "naver_line_wal": "databases/naver_line-wal",
    "naver_line_shm": "databases/naver_line-shm",
}

# Declarative only — no recovery code belongs here; see description above
# for what was actually found.
recoverable_tables = ["chat_history"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "timestamp" is this
# module's own run() output for "created_time"; "created_time" is the same
# column's raw SQL name as it appears on a carved/recovered chat_history
# row instead (carving dumps the table's own column names verbatim). Both
# are Unix epoch milliseconds.
timestamp_fields = {"timestamp": "ms", "created_time": "ms"}

# Checked directly against the real schema before declaring this (PRAGMA
# table_info), not assumed: chat_history.id IS a genuine
# INTEGER PRIMARY KEY AUTOINCREMENT (a real rowid alias) — "id" is also
# listed as a rowid_fields fallback since sqlite_carve dumps a recovered
# row's columns under the table's own names verbatim, not this parser's
# renamed "raw_message_id". chat.chat_id, by contrast, is a TEXT PRIMARY
# KEY — SQLite only aliases rowid for an INTEGER PRIMARY KEY, so chat_id's
# own value is NOT interchangeable with chat's real internal rowid; the
# Chat entry below points at raw_chat_rowid instead (chat's own real
# rowid, fetched separately in run() specifically to make this citation
# possible — chat_id alone could not have been used here without silently
# citing the wrong bytes).
hidden_fields = ["raw_chat_rowid"]
record_source = [
    {"label": "Message", "file_key": "naver_line", "table": "chat_history",
     "rowid_fields": ["raw_message_id", "id"]},
    {"label": "Chat", "file_key": "naver_line", "table": "chat",
     "rowid_fields": ["raw_chat_rowid"]},
]

# attachement_type values observed in this schema (no vendor documentation
# found; derived from cross-checking against ground-truth action labels).
_ATTACH_IMAGE = 1
_ATTACH_LOCATION = 15
_TYPE_CALL = 4
_LOC_SCALE = 1_000_000  # location_latitude/longitude are degrees * 1e6


def _parse_call_params(parameter):
    # 'parameter' is flat tab-separated key\tvalue\tkey\tvalue...
    if not parameter:
        return {}
    parts = parameter.split("\t")
    return dict(zip(parts[0::2], parts[1::2]))


def run(paths):
    import sqlite3

    from artifact_runner import missing_ref_label

    conn = sqlite3.connect(paths["naver_line"])
    conn.row_factory = sqlite3.Row

    # rowid fetched explicitly alongside chat_id -- chat_id itself is a
    # TEXT PRIMARY KEY (confirmed via PRAGMA table_info, not assumed), so
    # it is NOT chat's own real rowid the way an INTEGER PRIMARY KEY would
    # be; record_source's "Chat" entry needs the genuine rowid to cite the
    # correct bytes, not a guess.
    chat_rows = conn.execute("SELECT rowid, chat_id, chat_name FROM chat").fetchall()
    chat_names = {r["chat_id"]: r["chat_name"] for r in chat_rows}
    chat_rowid_by_chat_id = {r["chat_id"]: r["rowid"] for r in chat_rows}
    # contacts.m_id -> display name; empty on the extraction this was built
    # against (no contacts synced), but joined for cases where it isn't.
    contact_names = {r["m_id"]: (r["name"] or r["server_name"] or r["custom_name"])
                     for r in conn.execute("SELECT m_id, name, server_name, custom_name FROM contacts")
                     if r["name"] or r["server_name"] or r["custom_name"]}
    # reactions.server_message_id -> chat_history.server_id (both text form
    # of the same server-assigned message id); a message can have more than
    # one reaction (one per member), all folded into one row's body.
    reactions_by_server_id = {}
    for r in conn.execute("SELECT server_message_id, reaction_type FROM reactions"):
        reactions_by_server_id.setdefault(str(r["server_message_id"]), []).append(r["reaction_type"])

    rows = conn.execute("""
        SELECT id, server_id, type, chat_id, from_mid, content, created_time,
               attachement_type, attachement_local_uri,
               location_name, location_address, location_latitude, location_longitude,
               parameter
        FROM chat_history
        ORDER BY chat_id, created_time, id
    """).fetchall()

    out = []
    for r in rows:
        conversation = chat_names.get(r["chat_id"]) or missing_ref_label("chat name", "chat_id", r["chat_id"])
        # from_mid is populated with the peer's mid on a message the peer
        # sent, and left NULL/blank for anything the local user sent —
        # confirmed against ground truth on every call/message/location row
        # (e.g. every "Outgoing ... call" has from_mid blank, every
        # "Incoming ... call" has from_mid == chat_id).
        #
        # Deliberately not using chat_history.status for this, despite it
        # looking like the obvious direction column: on this extraction it
        # is 3 on all 30 rows, never the 1/2 ALEAPP's own line.py maps to
        # Incoming/Outgoing — that mapping produces the literal string "3"
        # for every message here. from_mid is the only field on this
        # extraction that actually varies with direction.
        incoming = bool(r["from_mid"])
        direction = "Incoming" if incoming else "Outgoing"
        sender = (contact_names.get(r["from_mid"], missing_ref_label("contact record", "mid", r["from_mid"]))
                 if incoming else "[local user]")

        if r["type"] == _TYPE_CALL:
            params = _parse_call_params(r["parameter"])
            kind = {"A": "audio", "V": "video"}.get(params.get("TYPE"), params.get("TYPE"))
            result = params.get("RESULT")
            dur_ms = params.get("DURATION")
            dur = f"{int(dur_ms) // 60000}:{(int(dur_ms) // 1000) % 60:02d}" if dur_ms else ""
            label = f"[{direction} {kind} call]" if kind else f"[{direction} call]"
            body = f"{label} {dur}".strip()
            if result and result != "NORMAL":
                # Only "NORMAL" was observed on this extraction (all 4 calls
                # completed) — pass any other value through literally rather
                # than inventing a missed/declined/failed label for it.
                body = f"{body}  (result: {result})"
        elif r["attachement_type"] == _ATTACH_LOCATION:
            addr = r["location_address"] or ""
            lat = r["location_latitude"] / _LOC_SCALE if r["location_latitude"] is not None else None
            lng = r["location_longitude"] / _LOC_SCALE if r["location_longitude"] is not None else None
            body = f"[Location] {addr}  {lat}, {lng}".strip()
        elif r["attachement_type"] == _ATTACH_IMAGE:
            body = f"[Image] {r['attachement_local_uri']}" if r["attachement_local_uri"] else "[Image]"
        elif r["content"]:
            body = r["content"].strip()
        else:
            # type 33 rows (e.g. the friend-added system event) carry no
            # content and no attachment — surface rather than silently drop.
            body = f"[System event, type {r['type']}]"

        reacts = reactions_by_server_id.get(r["server_id"] or "")
        if reacts:
            body = f"{body}  (reacted: {', '.join(reacts)})"

        out.append({
            "conversation": conversation,
            "timestamp": r["created_time"],
            "sender": sender,
            "direction": direction,
            "message": body,
            "raw_message_id": r["id"],
            "raw_chat_id": r["chat_id"],
            "raw_chat_rowid": chat_rowid_by_chat_id.get(r["chat_id"]),
            "recovered": False,
            "source_table": "chat_history",
        })
    return out
