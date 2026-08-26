name = "WhatsApp Messages (iOS)"
description = ("Messages from WhatsApp's local iOS store (ChatStorage.sqlite, "
              "a Core Data database — a different schema from the Android "
              "parser's msgstore.db, same app), table ZWAMESSAGE joined to "
              "ZWACHATSESSION for the conversation and ZWAGROUPMEMBER for "
              "the specific sender within a group chat. Live rows plus "
              "anything freeblocks/freed pages/WAL history/header-signature "
              "scanning recovers from the same table, merged into this same "
              "report. Checked, not skipped: on the extraction this was "
              "built against, ChatStorage.sqlite has PRAGMA secure_delete "
              "= FAST set (matches every other messaging app checked in "
              "this project), and the `recoverable_tables` carving pass "
              "below — run against real ground-truth-documented deleted "
              "content — came back with zero recovered rows, consistent "
              "with a raw byte-level search of the file for that same "
              "deleted message's exact text also finding no trace. A real "
              "negative for carving specifically, not evidence nothing was "
              "deleted (see next). Separately, and NOT carving: the "
              "deletion itself is real and independently confirmed, not "
              "just claimed by ground truth — ZWAMESSAGE's Z_PK primary-key "
              "sequence has a gap of exactly one row (174, between a live "
              "row at 173 and one at 175) whose timing matches the "
              "documented receive-then-delete window precisely, the same "
              "signature this project used to confirm a genuine deletion "
              "on Burner (Android). Message text for a handful of early "
              "rows (message types 5/6/10, all from the app's install/"
              "setup period with no ZTEXT at all) could not be identified "
              "against any documented WhatsApp Core Data enum and are "
              "surfaced generically rather than guessed; likewise type 59, "
              "which in this dataset appears immediately adjacent to every "
              "audio/video call mentioned in ground truth (a strong "
              "pattern) but isn't independently confirmed the way Apple's "
              "own documented iMessage reaction-type enum was for the SMS "
              "report.")
# WhatsApp's own container (Containers/Data/Application/<GUID>/…) does not
# hold ChatStorage.sqlite — it lives in the app's SHARED container instead
# (confirmed against a real extraction's guid_to_bundle map). This exact
# string is WhatsApp's App Group identifier, not its bundle id (that's
# net.whatsapp.WhatsApp, a different string) — see artifact_runner.py's
# docstring for how this gets resolved to this device's actual GUID.
app_group = "group.net.whatsapp.WhatsApp.shared"
files = {
    "chatstorage": "ChatStorage.sqlite",
}
optional_files = {
    "chatstorage_wal": "ChatStorage.sqlite-wal",
    "chatstorage_shm": "ChatStorage.sqlite-shm",
}

# Lets the Artifact Viewer's Hex-panel "Record" mode jump straight to the
# on-disk cell(s) a row was actually built from — a joined report like
# this one has more than one, so record_source is a LIST: this row's own
# ZWAMESSAGE cell, PLUS the three tables it LEFT JOINed in (chat session,
# group member, media item), each independently resolvable since a
# LEFT JOIN with no match just leaves that entry's rowid None/unresolvable
# (locate_live_row already handles a not-found rowid gracefully). All four
# share one file ("chatstorage" — everything here lives in the single
# ChatStorage.sqlite, unlike e.g. Android WhatsApp's separate msgstore.db/
# wa.db). "table_field" reads the source table's name from an OUTPUT field
# on the row (used for the main entry, matching both live and
# recoverable_tables-carved rows via "source_table"/"raw_rowid" — see
# below); a joined entry's table never varies per row, so it's given as a
# literal "table" string instead, needing no row lookup. "rowid_fields"
# are tried in order.
record_source = [
    {
        "label":        "Message",
        "file_key":     "chatstorage",
        "table_field":  "source_table",
        "rowid_fields": ["raw_message_id", "raw_rowid"],
    },
    {
        "label":        "Chat Session",
        "file_key":     "chatstorage",
        "table":        "ZWACHATSESSION",
        "rowid_fields": ["raw_chat_id"],
    },
    {
        "label":        "Group Member",
        "file_key":     "chatstorage",
        "table":        "ZWAGROUPMEMBER",
        "rowid_fields": ["raw_group_member_id"],
    },
    {
        "label":        "Media Item",
        "file_key":     "chatstorage",
        "table":        "ZWAMEDIAITEM",
        "rowid_fields": ["raw_media_item_id"],
    },
]

# raw_chat_id/raw_group_member_id/raw_media_item_id exist ONLY to feed
# record_source above — pure plumbing, never useful as report content (an
# examiner already sees the resolved conversation/sender/attachment
# columns) — so they're kept out of the Report table. raw_message_id and
# source_table stay visible: raw_message_id is the row's OWN id, not a
# joined table's, and source_table is a citation label, not a raw key.
hidden_fields = ["raw_chat_id", "raw_group_member_id", "raw_media_item_id"]

# Declarative only — no recovery code belongs here; see description above
# for what was actually found.
recoverable_tables = ["ZWAMESSAGE"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "timestamp" is this
# module's own run() output for ZMESSAGEDATE; "ZMESSAGEDATE" is that same
# column's own SQL name as it appears on a carved/recovered row instead.
# Core Data TIMESTAMP columns are Cocoa/Mac epoch SECONDS (not iOS SMS's
# nanosecond variant — that's message.date specifically, not a general
# Core Data convention).
timestamp_fields = {"timestamp": "cocoa_s", "ZMESSAGEDATE": "cocoa_s"}

# "attachment_path" holds an archive ui_path (never a filesystem path) to
# the message's media file, painted as a thumbnail in the Report table and
# opened full-size/played on double-click — see app/artifact_media.py.
# ZWAMEDIAITEM.ZMEDIALOCALPATH is stored relative to a "Message/" folder
# under the App Group container, NOT directly under the container root —
# confirmed against a real extraction: ZMEDIALOCALPATH
# 'Media/<jid>/a/9/<uuid>.jpg' only resolves once "Message/" is prepended
# to the App Group base (mobile/Containers/Shared/AppGroup/<GUID>/Message/
# Media/<jid>/a/9/<uuid>.jpg — the bare .../AppGroup/<GUID>/Media/... path
# does not exist). A second, differently-sized copy of the same asset also
# exists under the app's own Data/Application container
# (Library/Caches/ChatMedia/<jid>/<uuid>.jpg, no jid subdirectory sharding)
# — not used here since that container isn't one this parser resolves, and
# ZWAMEDIAITEM.ZFILESIZE matches the App Group copy, not the cache copy.
media_fields = ["attachment_path"]

# WhatsApp's own message-type enum (ZWAMESSAGE.ZMESSAGETYPE). Only 0 and 1
# are asserted with confidence (0 = every row with ZTEXT populated and
# nothing else does; 1 = every row that also has a picture-send ground-
# truth action nearby, e.g. rows immediately following a "Sent/Received
# picture" GTD action). The rest are NOT a documented/verified enum —
# labeled generically rather than guessed, per this project's convention
# of not asserting false precision (contrast with iOS SMS's reaction-type
# table, drawn from Apple's own documented enum).
_MESSAGE_TYPE_LABELS = {
    1: "[Media attachment — no caption text stored in this table]",
    59: "[System event, type 59 — in this dataset always immediately "
        "adjacent to a call mentioned in ground truth, but not "
        "independently confirmed against a documented enum]",
}


def run(paths):
    import sqlite3
    from artifact_runner import first_nonempty, missing_ref_label

    app_base = paths.get("_app_base_ui_path")

    conn = sqlite3.connect(paths["chatstorage"])
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            m.Z_PK          AS message_id,
            m.ZCHATSESSION  AS chat_id,
            m.ZISFROMME     AS is_from_me,
            m.ZMESSAGETYPE  AS message_type,
            m.ZGROUPEVENTTYPE AS group_event_type,
            m.ZMESSAGEDATE  AS msg_date,
            m.ZFROMJID      AS from_jid,
            m.ZTOJID        AS to_jid,
            m.ZTEXT         AS text,
            c.ZSESSIONTYPE  AS session_type,
            c.ZPARTNERNAME  AS partner_name,
            c.ZCONTACTJID   AS contact_jid,
            gm.ZCONTACTNAME AS group_member_name,
            gm.ZMEMBERJID   AS group_member_jid,
            m.ZGROUPMEMBER  AS group_member_row_id,
            med.ZMEDIALOCALPATH AS media_local_path,
            med.ZXMPPTHUMBPATH  AS media_thumb_path,
            med.Z_PK        AS media_item_id
        FROM ZWAMESSAGE m
        LEFT JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION
        LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
        LEFT JOIN ZWAMEDIAITEM med ON med.ZMESSAGE = m.Z_PK
        ORDER BY m.ZMESSAGEDATE
    """).fetchall()

    out = []
    for r in rows:
        conversation = r["partner_name"] or missing_ref_label("chat record", "chat_id", r["chat_id"])

        if r["is_from_me"]:
            sender = "Me"
        elif r["session_type"] == 1:   # group chat
            sender = (r["group_member_name"] or r["group_member_jid"]
                      or "[group member unresolved]")
        else:
            sender = r["from_jid"] or r["contact_jid"] or "[unknown sender]"

        body = r["text"] or _MESSAGE_TYPE_LABELS.get(
            r["message_type"], f"[No text — message type {r['message_type']}]")

        # ZXMPPTHUMBPATH fallback: confirmed on a real extraction as a
        # genuine, complete JPEG under the same "Message/" base as the
        # full-size case (not a different location/format) — see
        # first_nonempty's own docstring for why this pattern exists.
        attachment_path = ''
        local_path = first_nonempty(r["media_local_path"], r["media_thumb_path"])
        if local_path and app_base:
            attachment_path = f"{app_base}/Message/{local_path}"

        entry = {
            "conversation": conversation,
            "timestamp": r["msg_date"],
            "sender": sender,
            "direction": "Sent" if r["is_from_me"] else "Received",
            "message": body,
            "attachment_path": attachment_path,
            "raw_message_id": r["message_id"],
            "raw_chat_id": r["chat_id"],
            "raw_group_member_id": r["group_member_row_id"],
            "raw_media_item_id": r["media_item_id"],
            "recovered": False,
            "source_table": "ZWAMESSAGE",
        }
        if r["group_event_type"]:
            # Raw code surfaced for citation, not decoded — no verified
            # enum reference for this column (see description above).
            entry["group_event_type"] = r["group_event_type"]
        out.append(entry)

    conn.close()
    return out
