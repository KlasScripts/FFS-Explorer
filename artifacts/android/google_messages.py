name = "Google Messages (SMS/MMS/RCS)"
description = ("Messages from Google Messages' local database (bugle_db, "
              "tables parts/messages), live rows plus anything freeblocks/"
              "freed pages/WAL history/header-signature scanning recovers "
              "from those same tables — merged into this report, since "
              "it's all one source. Checked, not skipped: on the "
              "extraction this was built against, bugle_db has "
              "PRAGMA secure_delete = FAST set, which actively zeroes "
              "freed page bytes at delete time rather than leaving them as "
              "recoverable garbage — confirmed by inspecting the freed "
              "page ranges directly (all zero bytes) on a database known "
              "to have a deleted conversation. A genuine negative for this "
              "carving technique specifically, not evidence nothing was "
              "ever deleted: see the \"Google Messages — Recovered "
              "(deleted conversations)\" report, which recovers "
              "conversation-level metadata (who, when) for a fully "
              "deleted conversation through a different mechanism — "
              "surviving audit-log tables, not byte carving.")
app_path = "data/data/com.google.android.apps.messaging"
files = {
    "bugle_db": "databases/bugle_db",
}
optional_files = {
    # Same basenames as on-device, same directory as bugle_db, so sqlite3
    # picks them up automatically on connect() — WAL can hold the most
    # recent, not-yet-checkpointed messages.
    "bugle_db_wal": "databases/bugle_db-wal",
    "bugle_db_shm": "databases/bugle_db-shm",
}

# Declarative only — no recovery code belongs here; see description above
# for what was actually found.
recoverable_tables = ["messages", "parts"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "date" is this module's
# own run() output for messages.received_timestamp; "received_timestamp"
# is that same column's own SQL name as it appears on a carved/recovered
# messages row instead; "timestamp" is parts' own mirror column (kept in
# sync with received_timestamp by a DB trigger) as it appears on a carved
# parts row. All three are Unix epoch milliseconds.
timestamp_fields = {"date": "ms", "received_timestamp": "ms", "timestamp": "ms"}

# "attachment_path" is a full archive ui_path for the thumbnail/full-view
# feature (see app/artifact_media.py). parts.uri is a content:// reference
# (e.g. 'content://mms/part/12'), not a filesystem path — resolvable only
# on a live device, not from an archive. parts.local_cache_path IS a real
# on-device filesystem path though, confirmed against a real extraction:
# it lives under this same app's own data directory (this module's
# app_path), so no separate fixed base is needed the way Android WhatsApp's
# media does. Coverage is partial by nature of what "cache" means — not
# every attachment part has a surviving cache entry (27 of the ~150+
# attachment parts on the extraction this was checked against). The cache
# filename itself carries no extension ("..._part_N_.bin" regardless of
# whether the real content is a jpeg, png, or mp4 — confirmed by decoding
# several), which is why sniff_media_kind() in media_viewer.py exists
# rather than trusting the extension here.
media_fields = ["attachment_path"]


def _display_name(row_full_name, row_display_dest):
    return row_full_name or row_display_dest or None


def _resolve_cache_path(app_base, local_cache_path):
    """local_cache_path is an absolute on-device path (either
    '/data/user/0/<pkg>/...' or '/data/data/<pkg>/...' depending on Android
    version) into this same app's data directory — find the package-name
    marker and rejoin everything after it onto app_base, rather than
    assuming which of the two forms is present."""
    marker = 'com.google.android.apps.messaging/'
    if not local_cache_path or not app_base:
        return ''
    i = local_cache_path.find(marker)
    if i < 0:
        return ''
    return f"{app_base}/{local_cache_path[i + len(marker):]}"


def run(paths):
    import sqlite3

    app_base = paths.get("_app_base_ui_path")

    conn = sqlite3.connect(paths["bugle_db"])
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            m._id            AS id,
            m.conversation_id,
            m.sender_id,
            m.received_timestamp AS ts,
            c.name            AS conv_name,
            sender.sub_id     AS sender_sub_id,
            sender.full_name  AS sender_full_name,
            sender.display_destination AS sender_dest,
            p._id             AS part_id,
            p.text            AS part_text,
            p.uri             AS part_uri,
            p.content_type    AS part_content_type,
            p.latitude        AS part_lat,
            p.longitude       AS part_lon,
            p.local_cache_path AS part_cache_path
        FROM parts p
        JOIN messages m         ON m._id = p.message_id
        JOIN conversations c    ON c._id = p.conversation_id
        LEFT JOIN participants sender ON sender._id = m.sender_id
        ORDER BY m.conversation_id, m.received_timestamp, m._id
    """).fetchall()
    # INNER JOIN parts (not LEFT): messages with zero parts are confirmed (by
    # inspecting real data) to be self-originated protocol/session bookkeeping
    # rows with nothing user-visible, never actual sent/received content —
    # matches Josh Hickman's ALEAPP googleMessages.py reference implementation,
    # which excludes them the same way.

    # Group part rows by message — a message can have several parts (text +
    # one or more attachments, e.g. a caption plus an image).
    by_message = {}
    order = []
    for r in rows:
        mid = r["id"]
        if mid not in by_message:
            by_message[mid] = {"row": r, "parts": []}
            order.append(mid)
        by_message[mid]["parts"].append(r)

    out = []
    for mid in order:
        entry = by_message[mid]
        r = entry["row"]
        parts = entry["parts"]

        texts = [p["part_text"] for p in parts if p["part_text"]]
        media = [p for p in parts
                 if p["part_uri"] or (p["part_content_type"] and "text" not in (p["part_content_type"] or ""))]

        if texts:
            body = " | ".join(texts)
        elif any(p["part_lat"] for p in parts):
            loc = next(p for p in parts if p["part_lat"])
            body = f"[Location] {loc['part_lat']}, {loc['part_lon']}"
        elif media:
            body = f"[{len(media)} attachment(s): " + ", ".join(
                m["part_content_type"] or "unknown" for m in media) + "]"
        else:
            body = None

        # Only the first cached attachment gets a thumbnail column (one
        # media_fields column per row) — attachment_count/attachment_types
        # below still summarize every attachment on the row regardless of
        # whether any of them has a surviving cache entry.
        attachment_path = ''
        for m in media:
            resolved = _resolve_cache_path(app_base, m["part_cache_path"])
            if resolved:
                attachment_path = resolved
                break

        # Raw ms epoch, not a formatted string — the Report table formats
        # this per the case's timestamp-display setting (see
        # timestamp_fields above). A prior version of this line used a bare
        # fromtimestamp() with no tz=, which converts using the ANALYSIS
        # MACHINE's OS timezone rather than UTC — a real bug, found and
        # fixed the same way in whatsapp.py — but that whole concern is
        # moot now that no formatting happens here at all.
        date_str = r["ts"]

        # AOSP Messaging: ParticipantData.OTHER_THAN_SELF_SUB_ID = -2. A
        # sender whose sub_id is anything else is the self participant (the
        # message is outgoing). A NULL sub_id is left blank rather than
        # assumed Incoming, since a NULL comparison is neither true nor
        # false — matches Josh Hickman's ALEAPP googleMessages.py reference.
        sub_id = r["sender_sub_id"]
        if sub_id is None:
            direction, sender_name = None, _display_name(r["sender_full_name"], r["sender_dest"])
        elif sub_id != -2:
            direction, sender_name = "Sent", "Me"
        else:
            direction, sender_name = "Received", _display_name(r["sender_full_name"], r["sender_dest"])

        out.append({
            "date": date_str,
            "conversation": r["conv_name"],
            "sender": sender_name,
            "sender_destination": r["sender_dest"],   # raw number/handle — citation, not just resolved name
            "direction": direction,
            "attachment_path": attachment_path,
            "body": body,
            "attachment_count": len(media),
            "attachment_types": ", ".join(sorted({m["part_content_type"] for m in media if m["part_content_type"]})) or None,
        })

    conn.close()
    return out
