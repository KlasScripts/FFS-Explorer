name = "SMS Messages"
description = ("Messages from iOS's SMS/iMessage database (sms.db, table "
              "message), joined to chat/handle for conversation and sender, "
              "with attributedBody (typedstream) decoded as a fallback for "
              "rows whose plain-text `text` column is empty — edited "
              "messages and several share-link message types store their "
              "content only there. Live rows plus anything freeblocks/freed "
              "pages/WAL history/header-signature scanning recovers from "
              "the same table, merged into this same report. Checked, not "
              "skipped: on the extraction this was built against, sms.db "
              "has PRAGMA secure_delete = FAST set, which actively zeroes "
              "freed page bytes at delete time — confirmed by inspecting "
              "the freed ranges directly (all zero) on a database with two "
              "confirmed-deleted messages old enough to have fully expired "
              "past Apple's own recovery mechanism (below). A real negative "
              "for carving specifically, not evidence nothing was deleted. "
              "Separately, and NOT byte carving: iOS 16+ keeps a message "
              "deleted from a conversation (but not yet past its ~30-40 day "
              "retention window) fully intact in this same `message` table "
              "— it's simply no longer linked via chat_message_join, "
              "instead linked via chat_recoverable_message_join, which "
              "still resolves to the same live row with its own text intact. "
              "This report detects that case via that join and marks the "
              "row recovered=True, recovery_method='recently_deleted_window' "
              "with its own delete_date — no carving needed, the content "
              "was never actually gone. On the extraction this was built "
              "against, one message deleted ~6 hours before the extraction "
              "was still present this way; two older ones (deleted ~6+ "
              "months prior) were fully expired from both this table and "
              "Apple's own recoverable-message tables, and only checkable "
              "via carving (see above — genuinely gone). Two further "
              "confirmed limitations, checked directly on this extraction, "
              "not carving-related and not covered by recoverable_tables: "
              "(1) editing a message overwrites `text`/`attributedBody` in "
              "place — `edited_at` on a row means the CURRENT text is "
              "shown, the pre-edit version is gone from this table with no "
              "history kept (message_summary_info holds only a length/range "
              "value for the old text, never the text itself, confirmed by "
              "decoding it directly on an edited row). (2) 'Unsend' behaves "
              "differently from 'Delete': the row is kept and `edited_at` "
              "gets set exactly like a real edit, but message_summary_info "
              "decodes to an explicit `{'ust': True, ...}` flag and both "
              "`text`/`attributedBody` are wiped — confirmed on a "
              "documented-unsent message on this extraction. It never "
              "entered chat_recoverable_message_join, so it does not get "
              "Delete's 30-40 day recovery window at all; the original "
              "text is gone the moment it's unsent, not gradually.")
app_path = "mobile/Library/SMS"
files = {
    "sms": "sms.db",
}
optional_files = {
    "sms_wal": "sms.db-wal",
    "sms_shm": "sms.db-shm",
}

# Declarative only — no recovery code belongs here; see description above
# for what was actually found (a real negative for carving specifically —
# the SQL-level recently-deleted-window recovery above is handled in run()
# directly, since it's a live join, not carved content).
recoverable_tables = ["message"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. The first six are this
# module's own run() output fields; the last five are the SAME message
# table columns' own SQL names ("date", "date_edited", ...) as they'd
# appear on a row from generic carving instead (relevant only if that
# backstop ever finds something on a different extraction — checked
# negative on this one, see description above). All are Cocoa/Mac epoch
# NANOSECONDS (message.date and its siblings — NOT the more common Cocoa
# seconds or Unix ms used elsewhere in this project).
timestamp_fields = {
    "timestamp": "cocoa_ns", "delete_date": "cocoa_ns", "edited_at": "cocoa_ns",
    "retracted_at": "cocoa_ns", "delivered_at": "cocoa_ns", "read_at": "cocoa_ns",
    "date": "cocoa_ns", "date_edited": "cocoa_ns", "date_retracted": "cocoa_ns",
    "date_delivered": "cocoa_ns", "date_read": "cocoa_ns",
}

# "attachment_path" is a full archive ui_path for the thumbnail/full-view
# feature (see app/artifact_media.py). attachment.filename is stored as
# '~/Library/SMS/Attachments/<hash>/<hash>/<GUID>/<basename>' — the '~' is
# the mobile user's home directory, confirmed against a real extraction (a
# stored '~/Library/SMS/Attachments/e0/00/<GUID>/image.000000.jpg' resolves
# to a real JPEG at mobile/Library/SMS/Attachments/e0/00/<GUID>/
# image.000000.jpg, the same base this module's own app_path already uses
# minus "/Attachments"). A message can carry more than one attachment
# (attachments_by_message is already a list, below); only the first is
# surfaced here as the thumbnail — the text summary in `message` still
# names every attachment.
media_fields = ["attachment_path"]

# Apple's own tapback/reaction enum (message.associated_message_type). The
# `text` column on a reaction row already contains a full human-readable
# sentence generated by the OS itself (e.g. 'Loved "..."') — these codes
# are surfaced alongside that text for citation, not used to generate it.
_REACTION_TYPES = {
    2000: "Loved", 2001: "Liked", 2002: "Disliked", 2003: "Laughed",
    2004: "Emphasized", 2005: "Questioned",
    3000: "Removed Loved", 3001: "Removed Liked", 3002: "Removed Disliked",
    3003: "Removed Laughed", 3004: "Removed Emphasized", 3005: "Removed Questioned",
}


def _parse_attributed_body(blob):
    if not blob:
        return None
    try:
        import typedstream
        root = typedstream.unarchive_from_data(blob)
    except Exception:
        # Best-effort: a handful of blobs on real data fail to unarchive
        # (e.g. legacy/truncated formats) — not treated as an error, since
        # `text` already covers the common case and this is only a fallback.
        return None
    if hasattr(root, "contents") and root.contents:
        first_item = root.contents[0]
        if hasattr(first_item, "value") and hasattr(first_item.value, "value"):
            return first_item.value.value
    return None


def run(paths):
    import sqlite3

    from artifact_runner import missing_ref_label, resolve_path_after_marker

    conn = sqlite3.connect(paths["sms"])
    conn.row_factory = sqlite3.Row

    chats = {r["ROWID"]: r for r in conn.execute(
        "SELECT ROWID, chat_identifier, display_name, room_name FROM chat")}
    handles = {r["ROWID"]: r["id"] for r in conn.execute("SELECT ROWID, id FROM handle")}
    # message_id -> chat_id for messages still in a normal conversation.
    chat_by_message = {r["message_id"]: r["chat_id"]
                       for r in conn.execute("SELECT message_id, chat_id FROM chat_message_join")}
    # message_id -> (chat_id, delete_date) for messages inside the "Recently
    # Deleted" retention window — a message here is not in the join above.
    recoverable_by_message = {r["message_id"]: (r["chat_id"], r["delete_date"])
                              for r in conn.execute(
                                  "SELECT message_id, chat_id, delete_date FROM chat_recoverable_message_join")}

    rows = conn.execute("""
        SELECT ROWID, guid, text, attributedBody, handle_id, is_from_me, date,
               date_edited, date_retracted, associated_message_type,
               associated_message_guid, service,
               is_sent, is_delivered, is_read, date_delivered, date_read
        FROM message
        ORDER BY date
    """).fetchall()

    # message_id -> list of {name, mime_type, size, filename} for inline
    # attachment info.
    attachments_by_message = {}
    for r in conn.execute("""
        SELECT maj.message_id, a.transfer_name, a.mime_type, a.total_bytes, a.filename
        FROM message_attachment_join maj JOIN attachment a ON a.ROWID = maj.attachment_id
    """):
        attachments_by_message.setdefault(r["message_id"], []).append(
            {"name": r["transfer_name"], "mime_type": r["mime_type"],
             "size": r["total_bytes"], "filename": r["filename"]})

    def cocoa_ts(ns):
        # Named passthrough, not a no-op: keeps every call site below
        # reading the same way now that formatting moved to display time
        # (see timestamp_fields above) as it did when this actually
        # converted to a UTC string — same "cocoa nanoseconds in, raw
        # cocoa_ns value out" contract either way.
        return ns or None

    out = []
    for r in rows:
        recovered_entry = recoverable_by_message.get(r["ROWID"])
        chat_id = chat_by_message.get(r["ROWID"]) or (recovered_entry[0] if recovered_entry else None)
        chat = chats.get(chat_id)
        conversation = ((chat["display_name"] or chat["room_name"] or chat["chat_identifier"])
                        if chat else missing_ref_label("chat record", "message ROWID", r["ROWID"]))

        sender = "Me" if r["is_from_me"] else handles.get(
            r["handle_id"], missing_ref_label("handle record", "handle_id", r["handle_id"]))

        body = r["text"] or _parse_attributed_body(r["attributedBody"])

        atts = attachments_by_message.get(r["ROWID"], [])
        attachment_path = ''
        if atts:
            att_desc = ", ".join(f"{a['name']} ({a['mime_type']})" if a["mime_type"] else (a["name"] or "?")
                                 for a in atts)
            body = f"{body}  [Attachment: {att_desc}]" if body else f"[Attachment: {att_desc}]"
            # Only the first attachment gets a thumbnail column (one
            # media_fields column per row) — att_desc above still names
            # every attachment on the row regardless of count. 'mobile' is
            # the device root (attachment.filename's leading '~/' is the
            # mobile user's HOME directory, not this module's own app_path
            # — 'mobile/Library/SMS' — so the literal device root is
            # passed here, not _app_base_ui_path).
            attachment_path = resolve_path_after_marker(
                'mobile', atts[0]["filename"], '~/')

        entry = {
            "conversation": conversation,
            "timestamp": cocoa_ts(r["date"]),
            "sender": sender,
            "direction": "Sent" if r["is_from_me"] else "Received",
            "message": body,
            "attachment_path": attachment_path,
            "service": r["service"],
            "raw_message_id": r["ROWID"],
            "raw_message_guid": r["guid"],
            "raw_chat_id": chat_id,
            "recovered": recovered_entry is not None,
            "source_table": "message",
        }
        if recovered_entry is not None:
            entry["recovery_method"] = "recently_deleted_window"
            entry["delete_date"] = cocoa_ts(recovered_entry[1])
        if r["associated_message_type"]:
            entry["reaction_type"] = _REACTION_TYPES.get(
                r["associated_message_type"], f"[unrecognized type {r['associated_message_type']}]")
            entry["reaction_target_guid"] = r["associated_message_guid"]
        if r["date_edited"]:
            entry["edited_at"] = cocoa_ts(r["date_edited"])
        if r["date_retracted"]:
            entry["retracted_at"] = cocoa_ts(r["date_retracted"])
        if r["is_from_me"]:
            # Sent/delivered status only means something for the local
            # user's own outgoing messages (was it sent, did it deliver).
            entry["sent"] = bool(r["is_sent"])
            entry["delivered"] = bool(r["is_delivered"])
            if r["date_delivered"]:
                entry["delivered_at"] = cocoa_ts(r["date_delivered"])
        # is_read/date_read is meaningful either direction, just different
        # things: on an outgoing row it's the recipient's read receipt; on
        # an incoming row it's whether the local user read it, and when.
        entry["read"] = bool(r["is_read"])
        if r["date_read"]:
            entry["read_at"] = cocoa_ts(r["date_read"])
        out.append(entry)
    return out
