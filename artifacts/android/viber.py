name = "Viber"
description = ("Messages from Viber's local database (viber_messages, table "
              "messages), live rows plus anything freeblocks/freed pages/WAL "
              "history/header-signature scanning recovers — merged into this "
              "same report, since it's all one source. On the extraction this "
              "was built against a known 'sent, then deleted' test message is "
              "genuinely absent — not present as bytes anywhere in the main "
              "db, WAL, or SHM files, checked rather than assumed. Field-by-"
              "field validated against documented ground truth; no known "
              "reliability issues.")
app_path = "data/data/com.viber.voip"
files = {
    "viber_messages": "databases/viber_messages",
    "viber_data": "databases/viber_data",
}
optional_files = {
    # Same basenames as on-device, same directory as viber_messages, so
    # sqlite3 picks them up automatically on connect() — WAL can hold the
    # most recent, not-yet-checkpointed messages.
    "viber_messages_wal": "databases/viber_messages-wal",
    "viber_messages_shm": "databases/viber_messages-shm",
}

# Declarative only — no recovery code belongs here; see description above
# for what was actually found. artifact_runner.py reads this after run()
# returns and, on its own, looks for deleted rows of this table (see
# app/sqlite_carve.py), appending anything found as extra rows.
recoverable_tables = ["messages"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting (UTC/handset/acquisition —
# see ffs-explorer.py's FastZipBrowser.format_ts). "timestamp" is this
# module's own run() output; "msg_date" is the SAME column's raw SQL name
# as it appears on a carved/recovered row instead (sqlite_carve dumps the
# table's own column names verbatim, it doesn't rename anything) — both
# keys can appear as separate columns in the same report once recovered
# rows are unioned in, so both need declaring.
timestamp_fields = {"timestamp": "ms", "msg_date": "ms"}

_MIME_CALL = 1002
_CALL_LABELS = {
    'outgoing_call': 'Outgoing audio call',
    'incoming_call': 'Incoming audio call',
    'missed_call': 'Missed audio call',
    'outgoing_call_video': 'Outgoing video call',
    'incoming_call_video': 'Incoming video call',
    'missed_call_video': 'Missed video call',
}


def _display_name(contact_name, viber_name, number, member_id):
    return contact_name or viber_name or number or member_id or None


def run(paths):
    import sqlite3

    conn = sqlite3.connect(paths["viber_messages"])
    conn.row_factory = sqlite3.Row

    # All members of every conversation, keyed by conversation_id — used both
    # to build a conversation label and to resolve each message's sender.
    member_rows = conn.execute("""
        SELECT p.conversation_id AS conversation_id,
               p._id             AS participant_row_id,
               pi.contact_name, pi.viber_name, pi.number, pi.member_id
        FROM participants p
        JOIN participants_info pi ON pi._id = p.participant_info_id
    """).fetchall()
    members_by_convo = {}
    name_by_participant_row = {}
    for r in member_rows:
        name = (_display_name(r["contact_name"], r["viber_name"], r["number"],
                              r["member_id"]) or "[unresolved participant]")
        members_by_convo.setdefault(r["conversation_id"], []).append(name)
        name_by_participant_row[r["participant_row_id"]] = name

    rows = conn.execute("""
        SELECT
            m._id             AS id,
            m.conversation_id,
            m.participant_id,
            m.send_type,
            m.msg_date,
            m.body,
            m.extra_mime,
            m.extra_duration,
            m.location_lat,
            m.location_lng,
            m.likes_count,
            m.my_reaction,
            m.deleted
        FROM messages m
        ORDER BY m.conversation_id, m.msg_date, m._id
    """).fetchall()
    # Sender resolution deliberately does NOT use messages.user_id: in this
    # database user_id holds the *peer's* member_id on every row of a 1:1
    # conversation regardless of direction (confirmed against real data —
    # both "sent" and "received" rows in the same conversation carry the
    # identical peer member_id). The only reliable sender is
    # participant_id -> participants -> participants_info, which correctly
    # alternates between the local user and the peer message-by-message.
    #
    # Direction comes from send_type (0=Incoming, 1=Outgoing) rather than
    # from name-matching the resolved sender against a "self" identity —
    # both signals agreed on every row tested, but send_type is the
    # unambiguous one.
    #
    # No soft-delete handling here: a message deleted on-device (confirmed
    # against a real "sent, then deleted" test message) is removed from this
    # table outright — its _id is simply missing from the sequence, with no
    # orphaned/flagged row to surface. `deleted` exists as a column but was
    # 0 on every row observed, deleted or not; do not treat it as a delete
    # flag without further verification against a case where it's actually
    # set.

    out = []
    for r in rows:
        sender = name_by_participant_row.get(
            r["participant_id"], f"[no participant record — raw participant_id={r['participant_id']}]")
        convo_members = members_by_convo.get(r["conversation_id"], [])
        conversation = (", ".join(convo_members) if convo_members
                       else f"[no participant records — raw conversation_id={r['conversation_id']}]")

        if r["extra_mime"] == _MIME_CALL:
            label = _CALL_LABELS.get(r["body"], r["body"] or "[call event]")
            dur = r["extra_duration"] or 0
            body = f"[{label}] {dur // 60}:{dur % 60:02d}" if dur else f"[{label}]"
        elif r["location_lat"] or r["location_lng"]:
            # location_lat/lng are stored as degrees * 1e7 (fixed-point int).
            body = f"[Location] {r['location_lat'] / 1e7:.6f}, {r['location_lng'] / 1e7:.6f}"
        elif r["body"] and r["body"].startswith("content://"):
            body = f"[Media] {r['body']}"
        else:
            body = r["body"] or ""

        if r["likes_count"]:
            body = f"{body}  (liked x{r['likes_count']})" if body else f"(liked x{r['likes_count']})"

        direction = {0: "Incoming", 1: "Outgoing"}.get(r["send_type"], r["send_type"])

        out.append({
            "conversation": conversation,
            "timestamp": r["msg_date"],
            "sender": sender,
            "direction": direction,
            "message": body,
            "raw_message_id": r["id"],
            "raw_conversation_id": r["conversation_id"],
            "recovered": False,
            "source_table": "messages",
        })
    return out
