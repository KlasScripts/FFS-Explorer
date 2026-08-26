name = "GroupMe"
description = ("Messages from GroupMe's local database (groupme.db, table "
              "messages), live rows plus anything freeblocks/freed pages/WAL "
              "history/header-signature scanning recovers — merged into this "
              "same report, since it's all one source. On the extraction this "
              "was built against there's no WAL for this database and carving "
              "its live pages found nothing recoverable; a real negative, not "
              "an unexamined gap. Field-by-field validated against documented "
              "ground truth; no known reliability issues.")
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


def run(paths):
    import json as _json
    import sqlite3

    from artifact_runner import missing_ref_label

    conn = sqlite3.connect(paths["groupme"])
    conn.row_factory = sqlite3.Row

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
    convo_label = {}
    for r in conn.execute("SELECT user_id, name FROM chats"):
        convo_label[r["user_id"]] = r["name"] or f"[DM with raw user_id={r['user_id']}]"
    for r in conn.execute("SELECT group_id, name FROM groups"):
        convo_label[r["group_id"]] = r["name"] or f"[group, raw group_id={r['group_id']}]"

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

        body = r["message_text"] or ""
        if r["system_event_type"] in ("group.call.started", "group.call.ended"):
            # message_text is already a human-formatted call event string
            # ("X started a call" / "Call ended 1m 38s"); pull the precise
            # duration in seconds out of the accompanying event JSON too —
            # call_duration is milliseconds. No audio-vs-video distinction
            # is recoverable from this table; do not claim one.
            dur_s = None
            if r["event"]:
                try:
                    dur_s = _json.loads(r["event"])["data"]["call_duration"] / 1000
                except Exception:
                    dur_s = None
            body = body or "[call event]"
            if dur_s:
                body = f"{body} ({dur_s:.1f}s)"
        elif r["location_lat"] or r["location_lng"]:
            loc = r["location_name"] or ""
            body = f"[Location] {loc}  {r['location_lat']}, {r['location_lng']}".strip()
        elif r["photo_url"]:
            body = f"[Media] {r['photo_url']}" + (f"  {body}" if body else "")

        if r["favorited_by"]:
            likers = [x for x in r["favorited_by"].split(",") if x]
            body = f"{body}  (liked by raw user_id(s) {', '.join(likers)})"

        entry = {
            "conversation": conversation,
            "timestamp": r["created_at"],
            "sender": r["name"] or missing_ref_label("name", "sender_id", r["sender_id"]),
            "direction": direction,
            "message": body,
            "raw_message_id": r["_id"],
            "raw_conversation_id": r["conversation_id"],
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
