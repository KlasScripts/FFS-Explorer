name = "Viber"
description = ("Messages from Viber's local database (viber_messages, table "
              "messages), live rows plus anything freeblocks/freed pages/WAL "
              "history/header-signature scanning recovers — merged into this "
              "same report, since it's all one source. On the extraction this "
              "was built against a known 'sent, then deleted' test message is "
              "genuinely absent — not present as bytes anywhere in the main "
              "db, WAL, or SHM files, checked rather than assumed. Field-by-"
              "field validated against documented ground truth; no known "
              "reliability issues."
              " Recipients (added 2026-10-24): who each message went to, from participants (participant_type 0 is the device owner). Group membership is as at extraction, not at the time of the message: someone who joined later is listed, someone who left may not be.")
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

# Checked directly against the real schema before declaring this (PRAGMA
# table_info), not assumed: messages._id and participants._id are both
# genuine INTEGER PRIMARY KEY AUTOINCREMENT columns (real rowid aliases).
# "_id" is also listed as a rowid_fields fallback for the Message entry
# since sqlite_carve dumps a recovered row's columns under the table's own
# names verbatim, not this parser's renamed "raw_message_id". No
# "Conversation" entry: conversation_id has no directly-queried table row
# in this script to cite (no `conversations` table is read here) — citing
# it would mean guessing at an unverified table, which this project's own
# standing rule says not to do. participants_info (contact_name/viber_name/
# number, already folded into the visible sender/conversation fields) is
# similarly not given its own entry — reaching it needs a second hop
# (participants.participant_info_id) not currently carried per output row.
hidden_fields = ["raw_participant_id", "media_link"]
# Linked Report: the media file a message refers to. messages.extra_uri is
# either a MediaStore URI or Viber's own internal_files URI wrapping one in
# base64; it is resolved to the file through MediaStore's database
# (artifact_runner.resolve_mediastore_uri). Verified 2026-10-23 on Android
# 14 JoshHickman: message 19 -> Download/Imgur/3qaBztA.png (Viber's own
# msg_info FileName for it), message 21 -> Pictures/Viber/IMG-...-V.jpg.
link_fields = ["media_link"]


def _media_uri(extra_uri):
    """The MediaStore/file URI inside messages.extra_uri, or ''."""
    import base64
    import re
    if not extra_uri:
        return ''
    if extra_uri.startswith(('content://media/', 'file://')):
        return extra_uri
    m = re.match(r'^content://com\.viber\.voip\.provider\.internal_files/message/\w+/([A-Za-z0-9_=-]+)', extra_uri)
    if m:
        token = m.group(1)
        try:
            inner = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode('utf-8')
        except Exception:
            return ''
        if inner.startswith(('content://media/', 'file://')):
            return inner
    return ''

record_source = [
    {"label": "Message", "file_key": "viber_messages", "table": "messages",
     "rowid_fields": ["raw_message_id", "_id"]},
    {"label": "Participant", "file_key": "viber_messages", "table": "participants",
     "rowid_fields": ["raw_participant_id"]},
]

_MIME_CALL = 1002
_CALL_LABELS = {
    'outgoing_call': 'Outgoing audio call',
    'incoming_call': 'Incoming audio call',
    'missed_call': 'Missed audio call',
    'outgoing_call_video': 'Outgoing video call',
    'incoming_call_video': 'Incoming video call',
    'missed_call_video': 'Missed video call',
}


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "raw_conversation_id", "name": ["conversation"], "sent": ["Outgoing"], "media": ["media_link"], "media_prefixes": ["[Media]"], "time": "timestamp", "participants": ["sender"], "recipients": "recipients", "deleted": {"recovered": ["True"]}, "call_pattern": r"^\[(Outgoing|Incoming|Missed) (audio|video) call", "type": "chat_type"}

def run(paths):
    from artifact_runner import first_nonempty, missing_ref_label, open_db_readonly

    conn = open_db_readonly(paths["viber_messages"])

    # All members of every conversation, keyed by conversation_id — used both
    # to build a conversation label and to resolve each message's sender.
    member_rows = conn.execute("""
        SELECT p.conversation_id AS conversation_id,
               p._id             AS participant_row_id,
               pi._id            AS info_id,
               {ptype}           AS participant_type,
               pi.contact_name, pi.viber_name, pi.number, pi.member_id
        FROM participants p
        JOIN participants_info pi ON pi._id = p.participant_info_id
    """.replace("{ptype}", "pi.participant_type" if any(
        c[1] == "participant_type" for c in conn.execute("PRAGMA table_info(participants_info)"))
        else "NULL")).fetchall()
    members_by_convo = {}
    name_by_participant_row = {}
    info_by_participant_row = {}
    convo_others = {}
    owner_info = set()
    owner_label = "Me"
    for r in member_rows:
        name = first_nonempty(r["contact_name"], r["viber_name"], r["number"],
                              r["member_id"], default="[unresolved participant]")
        members_by_convo.setdefault(r["conversation_id"], []).append(name)
        name_by_participant_row[r["participant_row_id"]] = name
        # Recipients (2026-10-24): conversation members from participants
        # (at extraction); participant_type 0 is the device owner (A14:
        # Liz Dehner, +19199282177 — the owner on this image's other apps too).
        info_by_participant_row[r["participant_row_id"]] = r["info_id"]
        if r["participant_type"] == 0:
            owner_label = f"Me ({r['number']})" if r["number"] else "Me"
            owner_info.add(r["info_id"])
        else:
            others = convo_others.setdefault(r["conversation_id"], {})
            others.setdefault(r["info_id"], name)

    has_extra_uri = any(c[1] == "extra_uri" for c in conn.execute("PRAGMA table_info(messages)"))
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
            m.deleted,
            {extra_uri}
        FROM messages m
        ORDER BY m.conversation_id, m.msg_date, m._id
    """.replace("{extra_uri}", "m.extra_uri" if has_extra_uri else "NULL AS extra_uri")).fetchall()
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
            r["participant_id"], missing_ref_label("participant record", "participant_id", r["participant_id"]))
        convo_members = members_by_convo.get(r["conversation_id"], [])
        conversation = (", ".join(convo_members) if convo_members
                       else missing_ref_label("participant records", "conversation_id", r["conversation_id"]))

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
        others = convo_others.get(r["conversation_id"], {})
        if r["send_type"] == 1:
            recipients = ", ".join(others.values()) or "[conversation members not recorded]"
        else:
            sender_info = info_by_participant_row.get(r["participant_id"])
            recipients = ", ".join([owner_label] + [n for i, n in others.items() if i != sender_info])

        out.append({
            "conversation": conversation,
            "timestamp": r["msg_date"],
            "sender": sender,
            "recipients": recipients,
            "chat_type": "Group" if len(convo_others.get(r["conversation_id"], {})) > 1 else "1:1",
            "direction": direction,
            "message": body,
            "raw_message_id": r["id"],
            "raw_conversation_id": r["conversation_id"],
            "raw_participant_id": r["participant_id"],
            "media_link": _media_link(paths, r["extra_uri"]),
            "recovered": False,
            "source_table": "messages",
        })
    return out


def _media_link(paths, extra_uri):
    from artifact_runner import device_path_to_ui_path, resolve_mediastore_uri
    uri = _media_uri(extra_uri)
    if uri.startswith('content://media/'):
        return resolve_mediastore_uri(paths, uri)
    return device_path_to_ui_path(uri)
