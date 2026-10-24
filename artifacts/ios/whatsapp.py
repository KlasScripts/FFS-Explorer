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
              "report."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from ZWAGROUPMEMBER (\"(inactive)\" = ZISACTIVE 0); the owner's JID from incoming messages' ZTOJID. This is membership as at extraction, not at the time of any message, so no message row claims who received it.")
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
hidden_fields = ["chat_members", "raw_chat_id", "raw_group_member_id", "raw_media_item_id", "thumbnail_link"]
# Linked Report: the media file, and its separate .thumb file when the row
# also has a full-size file (ZXMPPTHUMBPATH; all 12 on IOS17 and 7 on CTF23
# exist in the archive, 2026-10-23).
link_fields = ["attachment_path", "thumbnail_link"]

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


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "raw_chat_id", "name": ["conversation"], "sent": ["Sent"], "media": ["attachment_path"], "time": "timestamp", "participants": ["sender"], "members": "chat_members", "deleted": {"recovered": ["True"]}, "type": "chat_type"}

def _recipient_context(conn):
    """Who a message went to, from ChatStorage itself (2026-10-24):
    - the device owner's own JID: the ZTOJID of incoming 1:1 messages
      (most common value; 19195794674 on IOS17, 12065937224 on CTF23);
    - names: 1:1 sessions' ZPARTNERNAME by ZCONTACTJID, group members'
      ZCONTACTNAME/ZFIRSTNAME, ZWAPROFILEPUSHNAME push names;
    - members per session from ZWAGROUPMEMBER, with ZISACTIVE (membership
      at extraction, NOT at the time of the message)."""
    import collections
    owner = collections.Counter(
        r[0] for r in conn.execute(
            "SELECT m.ZTOJID FROM ZWAMESSAGE m JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION "
            "WHERE m.ZISFROMME = 0 AND c.ZSESSIONTYPE = 0 AND m.ZTOJID IS NOT NULL"))
    owner_jid = owner.most_common(1)[0][0] if owner else None
    names = {}
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ZWAPROFILEPUSHNAME" in tables:
        for jid, push in conn.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
            if jid and push:
                names[jid] = push
    members = {}
    for session, jid, cname, fname, active in conn.execute(
            "SELECT ZCHATSESSION, ZMEMBERJID, ZCONTACTNAME, ZFIRSTNAME, ZISACTIVE FROM ZWAGROUPMEMBER"):
        if jid and (cname or fname):
            names[jid] = cname or fname
        if jid:
            members.setdefault(session, []).append((jid, active))
    for jid, partner, stype in conn.execute(
            "SELECT ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION"):
        if jid and partner and stype == 0:
            names[jid] = partner
    return owner_jid, names, members


def _who(jid, names, fallback=None):
    """One form for a person in both the sender column and the chat member list:
    "Name (jid)" when a name is known, else the jid."""
    name = names.get(jid) or fallback
    if jid and name:
        return f"{name} ({jid})"
    return jid or name or ""


def _chat_members(r, owner_jid, names, members):
    """Everyone in the chat (for the Chat Groups summary only): the device
    owner, then the 1:1 partner or every ZWAGROUPMEMBER of the session
    ("(inactive)" = ZISACTIVE 0) — membership as at extraction, never a
    claim about who received a particular message."""
    me = f"Me ({owner_jid})" if owner_jid else "Me"
    if r["session_type"] == 0:                       # 1:1
        partner = r["contact_jid"] or r["to_jid"]
        return ", ".join([me] + ([_who(partner, names)] if partner else []))
    listed = [(j, a) for j, a in members.get(r["chat_id"], []) if j != owner_jid]
    if not listed:
        return f"{me}, [group members not recorded]" if r["session_type"] == 1 else me
    return ", ".join([me] + [_who(j, names) + ("" if a else " (inactive)") for j, a in listed])


def run(paths):
    from artifact_runner import first_nonempty, missing_ref_label, open_db_readonly

    app_base = paths.get("_app_base_ui_path")

    conn = open_db_readonly(paths["chatstorage"])
    owner_jid, jid_names, members = _recipient_context(conn)

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
            sender = (_who(r["group_member_jid"], jid_names, r["group_member_name"])
                      or "[group member unresolved]")
        else:
            sender = _who(r["from_jid"] or r["contact_jid"], jid_names) or "[unknown sender]"

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
            "chat_members": _chat_members(r, owner_jid, jid_names, members),
            "chat_type": {0: "1:1", 1: "Group", 2: "Broadcast", 3: "Status"}.get(r["session_type"], ""),
            "direction": "Sent" if r["is_from_me"] else "Received",
            "message": body,
            "attachment_path": attachment_path,
            "thumbnail_link": (f"{app_base}/Message/{r['media_thumb_path']}"
                               if app_base and r["media_thumb_path"]
                               and r["media_thumb_path"] != local_path else ""),
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
