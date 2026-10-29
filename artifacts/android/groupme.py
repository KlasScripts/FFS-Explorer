name = "GroupMe"
description = ("Messages from GroupMe's local database (groupme.db, table "
              "messages), live rows plus anything freeblocks/freed pages/WAL "
              "history/header-signature scanning recovers — merged into this "
              "same report, since it's all one source. On the extraction this "
              "was built against there's no WAL for this database and carving "
              "its live pages found nothing recoverable; a real negative, not "
              "an unexamined gap. Field-by-field validated against documented "
              "ground truth; no known reliability issues."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from members (groups) and chats (DMs); the owner is the sender id seen in DMs. This is membership as at extraction, not at the time of any message, so no message row claims who received it.")
app_path = "data/data/com.groupme.android"
files = {
    "groupme": "databases/groupme.db",
}
optional_files = {
    "groupme_journal": "databases/groupme.db-journal",
    "groupme_wal": "databases/groupme.db-wal",
    "groupme_shm": "databases/groupme.db-shm",
}

# Declarative only — no recovery code belongs here. Nothing recoverable was
# found on the extraction this was built against (see description above),
# but the declaration stays so a different extraction — where this table
# might actually have deleted content sitting in freeblocks/WAL history —
# surfaces it automatically instead of requiring another one-off
# investigation.
recoverable_tables = ["messages"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "timestamp" is this
# module's own run() output for "created_at"; "created_at" is the same
# column's raw SQL name as it appears on a carved/recovered row instead
# (carving dumps the table's own column names verbatim). "deleted_at" is
# unrenamed either way, so it only needs one entry. All three are Unix
# epoch SECONDS (not ms — confirmed by this table's own values).
timestamp_fields = {"timestamp": "s", "created_at": "s", "deleted_at": "s"}

# Checked directly against the real schema before declaring this (PRAGMA
# table_info), not assumed: messages._id, chats._id, and groups._id are
# all genuine INTEGER PRIMARY KEY AUTOINCREMENT columns (real rowid
# aliases) — but messages.conversation_id itself matches chats.user_id or
# groups.group_id (see the comment in run() above), and NEITHER of those
# is its own table's rowid (both are plain TEXT UNIQUE columns) — the
# same trap as LINE's chat_id. The "Chat/Group" entry below therefore
# points at raw_conversation_rowid (each table's own real _id, fetched
# separately in run() specifically for this citation) rather than
# conversation_id itself, and conversation_table (set to "chats" or
# "groups" per row, since which table conversation_id resolved against
# varies per message) tells it which table to jump into.
hidden_fields = ["chat_members", "raw_conversation_rowid", "conversation_table"]
record_source = [
    {"label": "Message", "file_key": "groupme", "table": "messages",
     "rowid_fields": ["raw_message_id", "_id"]},
    {"label": "Chat/Group", "file_key": "groupme", "table_field": "conversation_table",
     "rowid_fields": ["raw_conversation_rowid"]},
]


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "raw_conversation_id", "name": ["conversation"], "sent": ["Outgoing"], "media": ["photo_url"], "time": "timestamp", "participants": ["sender"], "members": "chat_members", "system": ["System"], "deleted": {"recovered": ["True"], "deleted_at": "*"}, "call_pattern": r"^Call started", "call_field": "event", "type": "chat_type"}

def run(paths):
    import json as _json

    from artifact_runner import missing_ref_label, open_db_readonly

    conn = open_db_readonly(paths["groupme"])

    # Conversation label lookup. Unlike Viber, no participant-indirection
    # table is needed here — messages.name already carries the sender's
    # display name denormalized on every row. DM conversations are keyed by
    # the peer's user_id (chats.user_id), group conversations by
    # groups.group_id; messages.conversation_id matches one or the other
    # directly (not chats._id / groups._id, which are unrelated local
    # autoincrement ids). ALEAPP's own groupMe.py (same author as this test
    # image) only LEFT JOINs `groups`, never `chats` — every DM message in
    # its report gets a blank conversation label; deliberately joining both
    # here instead.
    # _id fetched explicitly alongside user_id/group_id -- neither of
    # those business keys is its own table's real rowid (both are plain
    # TEXT UNIQUE columns, confirmed via PRAGMA table_info, not assumed),
    # so record_source's "Chat/Group" entry needs each table's genuine
    # _id to cite the correct bytes.
    convo_label = {}
    convo_rowid = {}
    convo_table = {}
    for r in conn.execute("SELECT _id, user_id, name FROM chats"):
        convo_label[r["user_id"]] = r["name"] or f"[DM with raw user_id={r['user_id']}]"
        convo_rowid[r["user_id"]] = r["_id"]
        convo_table[r["user_id"]] = "chats"
    for r in conn.execute("SELECT _id, group_id, name FROM groups"):
        convo_label[r["group_id"]] = r["name"] or f"[group, raw group_id={r['group_id']}]"
        convo_rowid[r["group_id"]] = r["_id"]
        convo_table[r["group_id"]] = "groups"

    # Self (device owner) identification: no explicit "this is me" flag
    # exists anywhere in this schema. A first attempt used "whichever
    # sender_id appears across the most distinct conversations" — WRONG,
    # caught by checking against ground truth: in a 1:1 DM both parties
    # trivially appear in exactly that one conversation, so when the peer
    # also happens to post in a shared group, the count ties and the query
    # picks whichever the tie-break favors (the peer, in the case tested —
    # would have mislabeled every DM's direction backwards).
    #
    # Reliable derivation instead: a DM conversation's conversation_id IS
    # the peer's own user_id (confirmed against real data — chats.user_id
    # equals the value found in messages.conversation_id for that DM), so
    # within any DM, the sender who is NOT conversation_id is self, by
    # construction rather than a population-count guess.
    dm_peer_ids = {r["user_id"] for r in conn.execute("SELECT user_id FROM chats")}
    self_id = None
    for r in conn.execute("""
        SELECT DISTINCT sender_id, conversation_id FROM messages
        WHERE sender_id IS NOT NULL AND sender_id != 'system'
    """):
        if r["conversation_id"] in dm_peer_ids and r["sender_id"] != r["conversation_id"]:
            self_id = r["sender_id"]
            break
    if self_id is None:
        # No DM to derive it from (e.g. groups only) — fall back to the
        # most-distinct-conversations heuristic; less reliable, but better
        # than leaving every row unattributed.
        fallback = conn.execute("""
            SELECT sender_id, COUNT(DISTINCT conversation_id) AS n
            FROM messages WHERE sender_id IS NOT NULL AND sender_id != 'system'
            GROUP BY sender_id ORDER BY n DESC LIMIT 1
        """).fetchone()
        self_id = fallback["sender_id"] if fallback else None

    # Recipients (2026-10-24): group members from `members` (at extraction;
    # A14: DFIR Two, This Is DFIR, Liz Dehner), a DM's peer from `chats`;
    # the device owner is self_id (worked out above).
    group_members = {}
    for gid, uid, nick in conn.execute("SELECT group_id, user_id, nickname FROM members"):
        group_members.setdefault(gid, []).append((uid, nick or f"[user_id {uid}]"))
    me = next((f"Me ({nick})" for g in group_members.values() for uid, nick in g
               if uid == self_id), "Me")

    def _chat_members(r):
        """Everyone in the conversation (Chat Groups summary only), as at
        extraction: the owner, then the DM peer or the group's members."""
        cid = r["conversation_id"]
        if cid in dm_peer_ids:
            return f"{me}, {convo_label.get(cid, cid)}"
        members = [n for u, n in group_members.get(cid, []) if u != self_id]
        return ", ".join([me] + (members or ["[group members not recorded]"]))

    rows = conn.execute("""
        SELECT _id, conversation_id, message_id, created_at, sender_id, sender_type,
               name, message_text, is_system, favorited_by, photo_url,
               location_lat, location_lng, location_name,
               reply_id, deleted_at, deletion_actor, system_event_type, event
        FROM messages
        ORDER BY conversation_id, created_at, _id
    """).fetchall()

    out = []
    for r in rows:
        conversation = convo_label.get(
            r["conversation_id"], missing_ref_label("chat/group record", "conversation_id", r["conversation_id"]))

        if r["sender_id"] == "system":
            direction = "System"
        elif self_id is not None and r["sender_id"] == self_id:
            direction = "Outgoing"
        else:
            direction = "Incoming"

        # message is message_text exactly as stored. This report's own
        # labels go in event; location, photo and likes in their own columns.
        body = r["message_text"]
        event = ""
        if r["system_event_type"] in ("group.call.started", "group.call.ended"):
            # message_text is the app's own call line ("X started a call" /
            # "Call ended 1m 38s"). call_duration in the event JSON is
            # milliseconds. No audio-vs-video distinction is recoverable
            # from this table; do not claim one.
            dur_s = None
            if r["event"]:
                try:
                    dur_s = _json.loads(r["event"])["data"]["call_duration"] / 1000
                except Exception:
                    dur_s = None
            event = ("Call started" if r["system_event_type"] == "group.call.started"
                     else "Call ended")
            if dur_s:
                event = f"{event}, {dur_s:.1f} s"
        elif r["location_lat"] or r["location_lng"]:
            event = "Location"
        elif r["photo_url"]:
            event = "Photo"
        likers = [x for x in (r["favorited_by"] or "").split(",") if x]

        entry = {
            "conversation": conversation,
            "timestamp": r["created_at"],
            "sender": r["name"] or missing_ref_label("name", "sender_id", r["sender_id"]),
            "chat_members": _chat_members(r),
            "chat_type": "1:1" if r["conversation_id"] in dm_peer_ids else "Group",
            "direction": direction,
            "message": body,
            "event": event,
            "photo_url": r["photo_url"] or "",
            "location_name": r["location_name"] or "",
            "latitude": r["location_lat"],
            "longitude": r["location_lng"],
            "liked_by": ", ".join(f"raw user_id {x}" for x in likers),
            "raw_message_id": r["_id"],
            "raw_conversation_id": r["conversation_id"],
            "raw_conversation_rowid": convo_rowid.get(r["conversation_id"]),
            "conversation_table": convo_table.get(r["conversation_id"]),
            "recovered": False,
            "source_table": "messages",
        }
        if r["deleted_at"]:
            # The row survives as a tombstone: message_text is already
            # overwritten by the app itself ("This message was deleted") —
            # original content is not recoverable from this table. Surface
            # the deletion metadata rather than hiding or silently keeping
            # the placeholder text unlabeled.
            entry["deleted_at"] = r["deleted_at"]
            entry["deletion_actor"] = r["deletion_actor"]
        out.append(entry)
    return out
