name = "WhatsApp Messages (iOS)"
description = ("Messages from WhatsApp's local iOS store (ChatStorage.sqlite, "
              "a Core Data database — a different schema from the Android "
              "parser's msgstore.db, same app), table ZWAMESSAGE joined to "
              "ZWACHATSESSION for the conversation and ZWAGROUPMEMBER for "
              "the specific sender within a group chat. Live rows only: "
              "deleted and changed records are in \"WhatsApp — Deleted & "
              "Changed Records (iOS)\" (2026-11-21). A deletion is visible "
              "here without recovery, though: on the extraction this was "
              "built against, ZWAMESSAGE's Z_PK sequence has a gap of exactly "
              "one row (174, between live rows 173 and 175) whose timing "
              "matches the documented receive-then-delete window, the same "
              "signature used to confirm a deletion on Burner (Android). "
              "Message text for a handful of early "
              "rows (message types 5/6/10, all from the app's install/"
              "setup period with no ZTEXT at all) could not be identified "
              "against any documented WhatsApp Core Data enum and are "
              "surfaced generically rather than guessed; likewise type 59, "
              "which in this dataset appears immediately adjacent to every "
              "audio/video call mentioned in ground truth (a strong "
              "pattern) but isn't independently confirmed the way Apple's "
              "own documented iMessage reaction-type enum was for the SMS "
              "report."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from ZWAGROUPMEMBER (\"(inactive)\" = ZISACTIVE 0); the owner's JID from incoming messages' ZTOJID. This is membership as at extraction, not at the time of any message, so no message row claims who received it."
              " Validation of the deletion / disappearing-messages fields (2026-10-24): [PUBLISHED + VERIFIED] chat_sort (ZSORT) is the message's position in its chat; a missing number means a message removed on this device with no placeholder (Belkasoft; matches the documented 'delete for me' on the IOS17 test image exactly). [PUBLISHED + OBSERVED] chat_issued (ZWACHATSESSION.ZMESSAGECOUNTER) is the number of messages ever numbered in the chat (Belkasoft: 'the number of messages exchanged'); it was not reduced by the IOS17 deletion. Behaviour after 'Clear chat' is NOT tested. [PUBLISHED + OBSERVED] disappears_at (ZWAMESSAGE.ZMESSAGEERRORSTATUS) is the message's expiry time when disappearing messages applied (Heath et al. 2023, Forensic Science International: Digital Investigation 46, Fig. 5; on CTF23 every message after the setting change expires exactly 90 days after it was sent). [COMMUNITY TOOL + OBSERVED] message type 14 is a 'deleted for everyone' placeholder (WhatsApp-Chat-Exporter; one CTF23 example followed by 'Sorry wrong pic'). [OBSERVED ONLY — NOT PUBLISHED] message type 28 is a disappearing-messages setting change, ZISFROMME on it says who made it, and field 36 of its media item's ZMETADATA is the timer in seconds: seen twice on CTF23 (90 days by the owner, 7 days by the contact), consistent with the expiry times and with the contact's later messages having expired before acquisition; no paper, blog, iLEAPP or ALEAPP decodes it, and a 'turned off' event has not been seen. In the Chat Groups summary, 'Missing Consistent With Timer' is certain for gaps (the surviving message after a gap bounds when the missing ones were sent) but only 'consistent with' for messages missing at the end, whose send time is unknown; 'Missing Not Explained' means no timer applied, i.e. removed on this device by other means.")
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
hidden_fields = ["chat_members", "raw_chat_id", "raw_group_member_id", "raw_media_item_id", "thumbnail_link",
                 "chat_sort", "chat_issued"]
# Linked Report: the media file, and its separate .thumb file when the row
# also has a full-size file (ZXMPPTHUMBPATH; all 12 on IOS17 and 7 on CTF23
# exist in the archive, 2026-10-23).
link_fields = ["attachment_path", "thumbnail_link"]


# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "timestamp" is this
# module's own run() output for ZMESSAGEDATE; "ZMESSAGEDATE" is that same
# column's own SQL name as it appears on a carved/recovered row instead.
# Core Data TIMESTAMP columns are Cocoa/Mac epoch SECONDS (not iOS SMS's
# nanosecond variant — that's message.date specifically, not a general
# Core Data convention).
timestamp_fields = {"timestamp": "cocoa_s", "ZMESSAGEDATE": "cocoa_s", "disappears_at": "cocoa_s"}

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
    1: "Media attachment — no caption text stored in this table",
    14: "Deleted for everyone — content not kept",
    59: "System event, type 59 — in this dataset always immediately "
        "adjacent to a call mentioned in ground truth, but not "
        "independently confirmed against a documented enum",
}


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "raw_chat_id", "name": ["conversation"], "sent": ["Sent"], "system": ["System"], "media": ["attachment_path"], "time": "timestamp", "participants": ["sender"], "members": "chat_members", "deleted": {"recovered": ["True"], "deleted_for_everyone": "*"}, "type": "chat_type",
               # deletion / disappearing-messages activity (see description: Validation)
               "sort": "chat_sort", "issued": "chat_issued", "expires": "disappears_at",
               "setting": "disappearing_setting", "deleted_for_everyone": "deleted_for_everyone"}


def _duration_label(seconds):
    if not seconds:
        return "Off"
    for n, unit in ((86400, "day"), (3600, "hour")):
        if seconds % n == 0:
            k = seconds // n
            return f"{k} {unit}{'s' if k != 1 else ''}"
    return f"{seconds} seconds"


def _metadata_field(blob, field):
    """Varint *field* from a ZMETADATA protobuf blob, or None."""
    if not isinstance(blob, (bytes, bytearray)):
        return None
    pos, end = 0, len(blob)

    def varint(p):
        v, shift = 0, 0
        while p < end:
            b = blob[p]; p += 1
            v |= (b & 0x7F) << shift
            if not b & 0x80:
                return v, p
            shift += 7
        raise ValueError
    try:
        while pos < end:
            tag, pos = varint(pos)
            num, wire = tag >> 3, tag & 7
            if wire == 0:
                v, pos = varint(pos)
                if num == field:
                    return v
            elif wire == 2:
                n, pos = varint(pos)
                pos += n
            elif wire == 1:
                pos += 8
            elif wire == 5:
                pos += 4
            else:
                return None
    except (ValueError, IndexError):
        return None
    return None

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
            m.ZSORT         AS zsort,
            m.ZMESSAGEERRORSTATUS AS expires_at,
            c.ZMESSAGECOUNTER AS chat_counter,
            med.ZMETADATA   AS media_metadata,
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

        # message is ZTEXT exactly as stored. When there is no text, the
        # label for the row's type goes in event, never into the message.
        event = "" if r["text"] else _MESSAGE_TYPE_LABELS.get(
            r["message_type"], f"No text — message type {r['message_type']}")
        if r["message_type"] == 28:
            secs = _metadata_field(r["media_metadata"], 36)
            event = (f"Disappearing messages {'turned off' if secs == 0 else 'set to ' + _duration_label(secs)}"
                     f" by {'me' if r['is_from_me'] else 'the other party'} — decoded, see Validation"
                     if secs is not None else "Disappearing-messages setting change — timer not decoded")

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
            # Type 6 is a group event (Belkasoft), not a message either party
            # sent: System, whatever ZISFROMME says. ZISFROMME stays visible.
            "direction": ("System" if r["message_type"] == 6
                          else "Sent" if r["is_from_me"] else "Received"),
            "is_from_me": r["is_from_me"],
            "message": r["text"],
            "event": event,
            "attachment_path": attachment_path,
            "thumbnail_link": (f"{app_base}/Message/{r['media_thumb_path']}"
                               if app_base and r["media_thumb_path"]
                               and r["media_thumb_path"] != local_path else ""),
            # Deletion / disappearing-messages activity — validation status
            # of each field is in `description` (Validation section).
            "disappears_at": r["expires_at"] if r["expires_at"] else None,
            "disappearing_setting": (_duration_label(_metadata_field(r["media_metadata"], 36) or 0)
                                     if r["message_type"] == 28 else ""),
            "deleted_for_everyone": ((f"Deleted for everyone (by {'me' if r['is_from_me'] else 'them'})")
                                     if r["message_type"] == 14 else ""),
            "chat_sort": r["zsort"],
            "chat_issued": r["chat_counter"],
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


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'whatsapp_deleted',
  'name': 'WhatsApp — Deleted & Changed Messages (iOS)',
  'description': 'Deleted and changed WhatsApp messages from ChatStorage.sqlite (table '
                 'ZWAMESSAGE; Core Data: Z_PK is the row id). Live messages are in WhatsApp '
                 'Messages (iOS); deleted and changed media records (ZWAMEDIAITEM) are in '
                 'WhatsApp — Deleted & Changed Media (iOS).\n'
                 '\n'
                 'Each row is one record as found in ZWAMESSAGE, never joined to another table '
                 '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id. Values are as stored: no code is '
                 'turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'Dates are Cocoa seconds, as in WhatsApp Messages (iOS).\n'
                 '\n'
                 "linked_media: where this message's media record was found. "
                 'ZWAMESSAGE.ZMEDIAITEM holds the Z_PK of its ZWAMEDIAITEM row, so the link is '
                 'made by comparing the two, in the table now (live) and in the records '
                 'WhatsApp — Deleted & Changed Media (iOS) finds (recovered, with its row id); '
                 '"no media id" for a message with none. The media row also names its message '
                 '(ZWAMEDIAITEM.ZMESSAGE); on the test image both ways agree for all 19 pairs '
                 '(2026-11-27). Only ids are compared: no value of the media row is brought '
                 "in. How the link can be wrong: Z_PK comes from Core Data's own counter "
                 '(Z_PRIMARYKEY.Z_MAX; checked 2026-11-27 on the Josh Hickman iOS 17 image: '
                 'the highest Z_PK of each table is its Z_MAX), so an id is not normally '
                 'handed out again, but a store rebuilt from a backup or a migration can put a '
                 'different record under an old id; an earlier version found in the WAL and a '
                 'live row with the same id were stored at different times; a record found by '
                 'pattern has no row id and cannot be linked to; several copies of one record '
                 'can carry the same id.\n'
                 '\n'
                 'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 0 records. Found '
                 'earlier (carried over from WhatsApp Messages (iOS)): ChatStorage.sqlite has '
                 'PRAGMA secure_delete = FAST; carving found nothing for a documented deleted '
                 'message, and a byte search of the file for its text found no trace. The '
                 'deletion itself shows as a one-row gap in ZWAMESSAGE.Z_PK (see that report).',
  'timestamp_fields': {'message_date': 'cocoa_s', 'sent_date': 'cocoa_s'},
  'core_fields': ['status',
                  'message_date',
                  'message',
                  'linked_media',
                  'changed_fields',
                  'message (now)',
                  'how_found',
                  'copies'],
  'file_key': 'chatstorage',
  'table': 'ZWAMESSAGE',
  'label': 'Message',
  'fields': {'Z_PK': 'message_id',
             'ZCHATSESSION': 'chat_session_id',
             'ZGROUPMEMBER': 'group_member_id',
             'ZISFROMME': 'is_from_me',
             'ZMESSAGEDATE': 'message_date',
             'ZSENTDATE': 'sent_date',
             'ZTEXT': 'message',
             'ZMESSAGETYPE': 'message_type',
             'ZFROMJID': 'from_jid',
             'ZTOJID': 'to_jid',
             'ZPUSHNAME': 'push_name',
             'ZMEDIAITEM': 'media_item_id',
             'ZSTANZAID': 'stanza_id',
             'ZMESSAGESTATUS': 'message_status',
             'ZSTARRED': 'starred'},
  'version_fields': ['ZTEXT',
                     'ZMESSAGETYPE',
                     'ZCHATSESSION',
                     'ZFROMJID',
                     'ZTOJID',
                     'ZISFROMME',
                     'ZMEDIAITEM'],
  'links': [{'name': 'linked_media',
             'what': 'media',
             'column': 'ZMEDIAITEM',
             'table': 'ZWAMEDIAITEM',
             'key': 'Z_PK',
             'report': 'whatsapp_deleted_media'}]},
 {'script': 'whatsapp_deleted_media',
  'name': 'WhatsApp — Deleted & Changed Media (iOS)',
  'description': 'Deleted and changed WhatsApp media records from ChatStorage.sqlite (table '
                 'ZWAMEDIAITEM; Core Data: Z_PK is the row id, ZMESSAGE the message it belongs '
                 'to). Live media are in WhatsApp Messages (iOS); deleted and changed messages '
                 '(ZWAMESSAGE) are in WhatsApp — Deleted & Changed Messages (iOS).\n'
                 '\n'
                 'Each row is one record as found in ZWAMEDIAITEM, never joined to another '
                 'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id. Values are as stored: no code is '
                 'turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'media_url_date is Cocoa seconds, as in WhatsApp Messages (iOS).\n'
                 '\n'
                 'linked_message: where the message this media belongs to was found. ZMESSAGE '
                 'holds the Z_PK of its ZWAMESSAGE row, so the link is made by comparing the '
                 'two, in the table now (live: the message is in WhatsApp Messages (iOS)) and '
                 'in the records WhatsApp — Deleted & Changed Messages (iOS) finds (recovered, '
                 'with its row id). The message also names its media (ZWAMESSAGE.ZMEDIAITEM); '
                 'on the test image both ways agree for all 19 pairs (2026-11-27). Only ids '
                 'are compared: no value of the message is brought in. How the link can be '
                 "wrong: Z_PK comes from Core Data's own counter (Z_PRIMARYKEY.Z_MAX; checked "
                 '2026-11-27 on the Josh Hickman iOS 17 image: the highest Z_PK of each table '
                 'is its Z_MAX), so an id is not normally handed out again, but a store '
                 'rebuilt from a backup or a migration can put a different record under an old '
                 'id; an earlier version found in the WAL and a live row with the same id were '
                 'stored at different times; a record found by pattern has no row id and '
                 'cannot be linked to; several copies of one record can carry the same id.\n'
                 '\n'
                 'Also listed: live ZWAMEDIAITEM rows whose message is gone (status Orphaned): '
                 'not deleted, so not recovered, and not in the main report, which lists a row '
                 'only with its message. linked_message says whether that message was '
                 'recovered. None on the test image checked (2026-11-21).\n'
                 '\n'
                 'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 0 records. '
                 'ChatStorage.sqlite has PRAGMA secure_delete = FAST (carried over from '
                 'WhatsApp Messages (iOS)).',
  'timestamp_fields': {'media_url_date': 'cocoa_s'},
  'core_fields': ['status',
                  'media_path',
                  'title',
                  'linked_message',
                  'changed_fields',
                  'media_path (now)',
                  'how_found',
                  'copies'],
  'file_key': 'chatstorage',
  'table': 'ZWAMEDIAITEM',
  'label': 'Media',
  'fields': {'Z_PK': 'media_item_id',
             'ZMESSAGE': 'message_id',
             'ZMEDIALOCALPATH': 'media_path',
             'ZTHUMBNAILLOCALPATH': 'thumbnail_path',
             'ZMEDIAURL': 'media_url',
             'ZTITLE': 'title',
             'ZFILESIZE': 'file_size',
             'ZMOVIEDURATION': 'duration',
             'ZLATITUDE': 'latitude',
             'ZLONGITUDE': 'longitude',
             'ZAUTHORNAME': 'author_name',
             'ZVCARDNAME': 'vcard_name',
             'ZMEDIAURLDATE': 'media_url_date'},
  'orphan_of': {'column': 'ZMESSAGE', 'in': ['ZWAMESSAGE', 'Z_PK']},
  'version_fields': ['ZMEDIALOCALPATH', 'ZTITLE', 'ZMEDIAURL', 'ZLATITUDE', 'ZLONGITUDE'],
  'links': [{'name': 'linked_message',
             'what': 'message',
             'column': 'ZMESSAGE',
             'table': 'ZWAMESSAGE',
             'key': 'Z_PK',
             'report': 'whatsapp_deleted'}]}]
