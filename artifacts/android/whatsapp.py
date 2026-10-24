name     = "WhatsApp Messages"
description = ("Messages from WhatsApp's local database (msgstore.db, "
              "table message), live rows plus anything freeblocks/freed "
              "pages/WAL history/header-signature scanning recovers from "
              "the same table, merged into this same report. Checked, not "
              "skipped: on the extraction this was built against, "
              "msgstore.db has PRAGMA secure_delete = FAST set, which "
              "actively zeroes freed page bytes at delete time — confirmed "
              "by inspecting the freed ranges directly (all zero, aside "
              "from a single leftover 2-byte freeblock-link fragment "
              "followed by zeros, not record content) on a database with "
              "one confirmed-deleted message (a message._id gap). Checked "
              "the same way against message_ftsv2_content (WhatsApp's own "
              "full-text-search shadow table, the same kind of table that "
              "recovered a deleted message on LINE via WAL history) and "
              "against the live WAL for both tables — no stale/uncommitted "
              "copy of the deleted row's page was present either. A real "
              "negative for carving specifically, not evidence nothing was "
              "deleted. message_revoked (WhatsApp's own \"delete for "
              "everyone\" tombstone table) was empty on this extraction — "
              "the one documented deletion here was a local-only delete, "
              "which WhatsApp does not appear to route through that table."
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from group members from group_participant_user; the owner's number from shared_prefs/com.whatsapp_preferences_light.xml (registration_jid). Where a group's member list is empty it says so. This is membership as at extraction, not at the time of any message, so no message row claims who received it.")
app_path = "data/data/com.whatsapp"
files    = {
    "msgstore": "databases/msgstore.db",
    "wa":       "databases/wa.db",
}
optional_files = {
    # Same basenames as on-device, same directory, so sqlite3 picks them up
    # automatically on connect() — WAL can hold the most recent,
    # not-yet-checkpointed messages, and the recovery pass below can't
    # search a WAL that was never extracted from the archive.
    "msgstore_wal": "databases/msgstore.db-wal",
    "msgstore_shm": "databases/msgstore.db-shm",
    "wa_wal": "databases/wa.db-wal",
    "wa_shm": "databases/wa.db-shm",
    # The device owner's own number (registration_jid) and privacy id
    # (self_lid), for the Recipients column: 19199282177 on A14 (the same
    # number IOS17's WhatsApp shows for Liz Dehner), 19843761898 on A15.
    "prefs_light": "shared_prefs/com.whatsapp_preferences_light.xml",
}

# Lets the Artifact Viewer's Hex-panel "Record" mode jump straight to the
# on-disk cell(s) a row was actually built from. The real query below
# joins EIGHT tables across TWO database files (msgstore.db: message,
# chat, jid ×4, message_media, message_location; wa.db, ATTACHed as
# contacts_db: wa_contacts ×4) — expanded 2026-08-31 from an original
# Message+Chat-only declaration once every OTHER joined table's rowid
# status was actually checked against this project's own real WhatsApp
# casework (PRAGMA table_info on both real db files), not left as an
# unconfirmed gap:
#   - jid._id, message_media.message_row_id, message_location.message_row_id,
#     and wa_contacts._id are ALL genuine single-column INTEGER PRIMARY KEY
#     rowid aliases — confirmed, not assumed. The old comment here
#     specifically flagged wa_contacts' rowid status as unconfirmed; it
#     turned out to just need checking; there's a real one.
#   - message_media/message_location have no rowid of their own in the
#     query's SELECT list, but need none: their PK (message_row_id) is BY
#     DEFINITION identical to m._id (that's their own JOIN condition), which
#     is already captured as message_id -- reused directly, zero new SQL.
#   - The four jid joins and four wa_contacts joins DID need one more SELECT
#     column each (their own _id) added below -- previously computed and
#     joined against, but never carried out to the row's own output dict.
# Every table above lives in ONE of the two ATTACHed files, no table name
# is shared by both (unlike chrome_web_history's real "urls"-in-two-files
# collision), so no source_file_key disambiguation is needed here.
# "rowid_fields" for Message tries "message_id" (this module's own run()
# output for message._id) then "raw_rowid" (what a recoverable_tables-
# carved row carries instead) — carved rows resolve to nothing today since
# locate_live_row only walks the CURRENT live b-tree, not freed space,
# expected for a genuinely deleted row, not a bug (and only applies to the
# Message entry — recovery is only declared for "message" below, never
# any of the others). Every OTHER entry here has exactly one rowid_fields
# name since none of their own tables are ever carved.
record_source = [
    {
        "label":        "Message",
        "file_key":     "msgstore",
        "table_field":  "source_table",
        "rowid_fields": ["message_id", "raw_rowid"],
    },
    {
        "label":        "Chat",
        "file_key":     "msgstore",
        "table":        "chat",
        "rowid_fields": ["chat_id"],
    },
    {
        "label":        "Chat JID",
        "file_key":     "msgstore",
        "table":        "jid",
        "rowid_fields": ["chat_jid_id"],
    },
    {
        "label":        "Sender JID",
        "file_key":     "msgstore",
        "table":        "jid",
        "rowid_fields": ["sender_jid_id"],
    },
    {
        # LID-privacy identity variant of Chat JID -- present only when
        # jid_map has a row for this chat (see the run()-time comment on
        # jid_map sparsity); None on an ordinary chat is expected, and the
        # Hex panel correctly reports "no record-location data" rather
        # than silently resolving to the wrong (non-mapped) jid.
        "label":        "Chat Mapped JID",
        "file_key":     "msgstore",
        "table":        "jid",
        "rowid_fields": ["chat_mapped_jid_id"],
    },
    {
        "label":        "Sender Mapped JID",
        "file_key":     "msgstore",
        "table":        "jid",
        "rowid_fields": ["sender_mapped_jid_id"],
    },
    {
        # message_media's own rowid IS message_id -- see the module-level
        # note above on why no new SELECT column was needed for this one.
        "label":        "Media",
        "file_key":     "msgstore",
        "table":        "message_media",
        "rowid_fields": ["message_id"],
        # message_id is ALWAYS populated (it's every row's own PK), so it
        # can't say whether THIS row's LEFT JOIN to message_media actually
        # matched anything -- presence_fields checks the real joined value
        # instead, so a plain text message (no media_media row at all)
        # correctly drops this entry from the list rather than offering a
        # jump that would only ever report "not found".
        "presence_fields": ["media_path"],
    },
    {
        "label":        "Location",
        "file_key":     "msgstore",
        "table":        "message_location",
        "rowid_fields": ["message_id"],
        "presence_fields": ["latitude"],
    },
    {
        "label":        "Chat Contact",
        "file_key":     "wa",
        "table":        "wa_contacts",
        "rowid_fields": ["chat_contact_id"],
    },
    {
        "label":        "Sender Contact",
        "file_key":     "wa",
        "table":        "wa_contacts",
        "rowid_fields": ["sender_contact_id"],
    },
    {
        "label":        "Chat Mapped Contact",
        "file_key":     "wa",
        "table":        "wa_contacts",
        "rowid_fields": ["chat_mapped_contact_id"],
    },
    {
        "label":        "Sender Mapped Contact",
        "file_key":     "wa",
        "table":        "wa_contacts",
        "rowid_fields": ["sender_mapped_contact_id"],
    },
]

# chat_id, and every *_jid_id/*_contact_id field the entries above need,
# exist ONLY to feed record_source — pure plumbing, never useful as report
# content (an examiner already sees each one resolved into chat_subject/
# chat_jid/remote_party_jid/remote_name/media_path/latitude+longitude
# above; the no-chat-record fallback case additionally cites the raw FK
# inline via raw_chat_row_id, a separate field that stays visible).
# message_id and source_table stay visible: message_id is the row's OWN
# id, not a joined table's, and source_table is a citation label, not a
# raw key.
hidden_fields = ["chat_members", 
    "chat_id", "chat_jid_id", "sender_jid_id",
    "chat_mapped_jid_id", "sender_mapped_jid_id",
    "chat_contact_id", "sender_contact_id",
    "chat_mapped_contact_id", "sender_mapped_contact_id",
    "thumbnail_link",
]

# Declarative only — no recovery code belongs here; see description above
# for what was actually found.
recoverable_tables = ["message"]

# Raw values (never a formatted string) so the Report table can display
# them per the case's timestamp-display setting. "sent_time" is this
# module's own run() output for the message table's "timestamp" column;
# "timestamp" is that same column's own SQL name as it appears on a
# carved/recovered row instead (carving dumps the table's own column names
# verbatim, and WhatsApp's column really is called "timestamp"). Both are
# Unix epoch milliseconds.
timestamp_fields = {"sent_time": "ms", "timestamp": "ms"}

# "attachment_path" is a full archive ui_path for the thumbnail/full-view
# feature (see app/artifact_media.py) — built from message_media.file_path
# (already selected as "media_path" below, kept unchanged) plus a FIXED
# base path, not app_path/data/data/com.whatsapp: WhatsApp writes media to
# shared/external storage, a completely different location from its own
# app-private database directory. Confirmed against a real extraction —
# message_media.file_path 'Media/WhatsApp Images/Sent/IMG-....jpg' only
# resolves under data/media/0/Android/media/com.whatsapp/WhatsApp/, not
# under app_path. This is a fixed OS/app convention (unlike iOS's
# per-install GUID containers), so it's hardcoded rather than resolved via
# guid_to_bundle.
_MEDIA_BASE = "data/media/0/Android/media/com.whatsapp/WhatsApp"
media_fields = ["attachment_path"]
# Linked Report: the attachment file, and the message's thumbnail in
# msgstore.db's message_thumbnail (rowid = message _id), which the
# embedded-media sweep shows inside msgstore.db: image/video previews and
# link previews of shared URLs (2026-10-23, verified on A14/A15 data).
link_fields = ["attachment_path", "thumbnail_link"]


# Artifact Viewer "Chat Groups": one entry per conversation, and a summary
# table of them (see ArtifactViewerMixin._art_chat_groups).
chat_groups = {"key": "chat_id", "name": ["chat_subject", "remote_name", "chat_jid"], "sent": ["Sent"], "media": ["attachment_path"], "message": "text", "time": "sent_time", "participants": ["remote_name", "remote_party_jid", "group_sender"], "members": "chat_members", "deleted": {"recovered": ["True"], "deleted_for_everyone": "*"}, "type": "chat_type"}

def _owner(paths):
    """({own jids}, "Me (<number>)") from com.whatsapp_preferences_light.xml
    (registration_jid, self_lid); ({}, "Me") if it isn't there."""
    import re
    p = paths.get("prefs_light")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (TypeError, OSError):
        return set(), "Me"
    def val(key):
        m = re.search(rf'<string name="{key}">([^<]+)<', text)
        return m.group(1) if m else None
    reg, lid = val("registration_jid"), val("self_lid")
    jids = {j for j in (f"{reg}@s.whatsapp.net" if reg else None, lid) if j}
    return jids, (f"Me (+{reg})" if reg else "Me")


def _group_members(conn):
    """{group jid row id: [(member jid row id, {jids}, label)]} from
    group_participant_user (membership at extraction, not at the time of a
    message). A member's privacy (@lid) jid is mapped to its phone jid via
    jid_map; the label is the contact name from wa.db, else the phone jid."""
    try:
        rows = conn.execute("""
            SELECT gpu.group_jid_row_id, u._id, u.raw_string, mapped.raw_string, mapped._id,
                   COALESCE(cu.display_name, cu.wa_name, cu.given_name,
                            cm.display_name, cm.wa_name, cm.given_name)
            FROM group_participant_user gpu
            JOIN jid u ON u._id = gpu.user_jid_row_id
            LEFT JOIN jid_map jm ON jm.lid_row_id = u._id
            LEFT JOIN jid mapped ON mapped._id = jm.jid_row_id
            LEFT JOIN contacts_db.wa_contacts cu ON cu.jid = u.raw_string
            LEFT JOIN contacts_db.wa_contacts cm ON cm.jid = mapped.raw_string
        """).fetchall()
    except Exception:
        return {}
    out = {}
    for group, uid, raw, mapped, mapped_id, name in rows:
        jids = {j for j in (raw, mapped) if j}
        # the same form as the report's sender columns (remote_name, else
        # remote_party_jid = "<phone jid> (<jid row id>)"), so one person
        # is written one way
        label = (name or (f"{mapped} ({mapped_id})" if mapped else None)
                 or (f"{raw} ({uid})" if raw else f"[jid row {uid}]"))
        out.setdefault(group, []).append((uid, jids, label))
    return out


def _chat_type(jid):
    """From the chat's jid server: @g.us group, @newsletter channel (A15
    follows several: Reuters, Chelsea FC...), @broadcast list, else 1:1."""
    if not jid:
        return ""
    server = jid.rsplit("@", 1)[-1]
    return {"g.us": "Group", "newsletter": "Channel", "broadcast": "Broadcast",
            "status": "Status"}.get(server, "1:1")

def _chat_members(r, is_group, members, owner_jids, owner_label):
    """Everyone in the chat (for the Chat Groups summary only): the device
    owner, then the 1:1 partner or every group_participant_user member —
    membership as at extraction, never a claim about who received a
    particular message."""
    if not is_group:
        partner = (r["chat_mapped_contact_name"] or r["chat_contact_name"]
                   or (f"{r['chat_mapped_jid_raw']} ({r['chat_mapped_jid_id']})" if r["chat_mapped_jid_raw"] else None)
                   or (f"{r['chat_jid_raw']} ({r['chat_jid_id']})" if r["chat_jid_raw"] else None))
        return ", ".join([owner_label] + ([partner] if partner else []))
    listed = [label for uid, jids, label in members.get(r["chat_jid_id"], []) if not (jids & owner_jids)]
    if not listed:
        return f"{owner_label}, [group members not recorded]"
    return ", ".join([owner_label] + listed)


def run(paths):
    from artifact_runner import missing_ref_label, open_db_readonly

    # sent_time is the raw m.timestamp value (ms), not converted in SQL at
    # all — deliberately not `datetime(m.timestamp/1000, 'unixepoch')`
    # (still correct, but a needless SQL-side format now that the Report
    # table itself formats per the case's timestamp-display setting; see
    # timestamp_fields above). This also sidesteps the historical trap
    # documented at length in this project's git history: SQLite's
    # 'localtime' modifier converts using the ANALYSIS MACHINE's timezone,
    # not the device's — a bug once shipped here, verified wrong on this
    # project's own UK-based dev machine, silently 5 hours off a January
    # EST timestamp by pure winter-GMT coincidence.
    conn = open_db_readonly(paths["msgstore"])
    # A read-only main connection does NOT make an ATTACHed database
    # read-only on its own -- confirmed by direct testing, not assumed --
    # so the attached path must be its own file: URI too, or wa.db's
    # own WAL/SHM would be exposed to the exact same checkpoint-on-close
    # risk open_db_readonly exists to close for msgstore.db.
    conn.execute("ATTACH DATABASE ? AS contacts_db", (f'file:{paths["wa"]}?mode=ro',))
    cur = conn.cursor()

    cur.execute("""
        SELECT
            m._id                                                               AS message_id,
            m.from_me,
            m.message_type                                                      AS message_type,
            c._id                                                               AS chat_id,
            c.subject                                                           AS chat_subject,
            chat_jid.raw_string                                                 AS chat_jid_raw,
            chat_jid._id                                                        AS chat_jid_id,
            sender_jid.raw_string                                               AS sender_jid_raw,
            sender_jid._id                                                      AS sender_jid_id,
            chat_mapped_jid.raw_string                                          AS chat_mapped_jid_raw,
            chat_mapped_jid._id                                                 AS chat_mapped_jid_id,
            sender_mapped_jid.raw_string                                        AS sender_mapped_jid_raw,
            sender_mapped_jid._id                                               AS sender_mapped_jid_id,
            COALESCE(chat_contact.display_name, chat_contact.wa_name,
                     chat_contact.given_name)                                   AS chat_contact_name,
            chat_contact._id                                                    AS chat_contact_id,
            COALESCE(chat_jidmap_contact.display_name, chat_jidmap_contact.wa_name,
                     chat_jidmap_contact.given_name)                            AS chat_mapped_contact_name,
            chat_jidmap_contact._id                                             AS chat_mapped_contact_id,
            COALESCE(sender_contact.display_name, sender_contact.wa_name,
                     sender_contact.given_name)                                 AS sender_contact_name,
            sender_contact._id                                                  AS sender_contact_id,
            COALESCE(sender_jidmap_contact.display_name, sender_jidmap_contact.wa_name,
                     sender_jidmap_contact.given_name)                          AS sender_mapped_contact_name,
            sender_jidmap_contact._id                                           AS sender_mapped_contact_id,
            m.chat_row_id                                                       AS raw_chat_row_id,
            m.timestamp                                                         AS sent_time,
            m.text_data                                                         AS text,
            message_media.file_path                                             AS media_path,
            message_location.latitude,
            message_location.longitude
        FROM message m
        LEFT JOIN chat c                        ON c._id = m.chat_row_id
        LEFT JOIN jid chat_jid                  ON chat_jid._id = c.jid_row_id
        LEFT JOIN jid sender_jid                ON sender_jid._id = m.sender_jid_row_id
        LEFT JOIN jid_map chat_jidmap           ON chat_jidmap.lid_row_id = c.jid_row_id
        LEFT JOIN jid chat_mapped_jid           ON chat_mapped_jid._id = chat_jidmap.jid_row_id
        LEFT JOIN jid_map sender_jidmap         ON sender_jidmap.lid_row_id = m.sender_jid_row_id
        LEFT JOIN jid sender_mapped_jid         ON sender_mapped_jid._id = sender_jidmap.jid_row_id
        LEFT JOIN contacts_db.wa_contacts chat_contact
                                                ON chat_contact.jid = chat_jid.raw_string
        LEFT JOIN contacts_db.wa_contacts chat_jidmap_contact
                                                ON chat_jidmap_contact.jid = chat_mapped_jid.raw_string
        LEFT JOIN contacts_db.wa_contacts sender_contact
                                                ON sender_contact.jid = sender_jid.raw_string
        LEFT JOIN contacts_db.wa_contacts sender_jidmap_contact
                                                ON sender_jidmap_contact.jid = sender_mapped_jid.raw_string
        LEFT JOIN message_media                 ON message_media.message_row_id = m._id
        LEFT JOIN message_location              ON message_location.message_row_id = m._id
        ORDER BY m.timestamp
    """)

    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    try:   # older msgstore.db schemas have no message_thumbnail table
        thumbs = dict(conn.execute("SELECT message_row_id, thumbnail FROM message_thumbnail"))
    except Exception:
        thumbs = {}
    owner_jids, owner_label = _owner(paths)
    members = _group_members(conn)
    conn.close()

    records = []
    for r in rows:
        is_sent  = r["from_me"] == 1
        is_group = bool(r["chat_jid_raw"] and r["chat_jid_raw"].endswith("@g.us"))

        def _fmt(jid, jid_id):
            return f"{jid} ({jid_id})" if jid and jid_id is not None else None

        if not is_sent and is_group:
            group_sender = _fmt(r["sender_jid_raw"], r["sender_jid_id"])
        else:
            group_sender = None

        # jid_map only has a row for a LID-privacy-identity contact (memory:
        # present but sparse — see checklist point 1); an ordinary contact's
        # *_mapped_jid is simply absent, not an error, so fall back to the
        # plain jid the same way remote_name below falls back to the plain
        # contact name. Without this fallback, remote_party_jid came back
        # None for 229 of 230 rows on the extraction this was built against
        # — verified wrong, not a hypothetical edge case.
        if is_sent and is_group:
            remote_jid = None
        elif is_group:
            remote_jid = (_fmt(r["sender_mapped_jid_raw"], r["sender_mapped_jid_id"])
                          or _fmt(r["sender_jid_raw"], r["sender_jid_id"]))
        else:
            remote_jid = (_fmt(r["chat_mapped_jid_raw"], r["chat_mapped_jid_id"])
                          or _fmt(r["chat_jid_raw"], r["chat_jid_id"]))

        if is_group:
            remote_name = r["sender_mapped_contact_name"] or r["sender_contact_name"]
        else:
            remote_name = r["chat_mapped_contact_name"] or r["chat_contact_name"]

        chat_subject = r["chat_subject"]
        if r["chat_id"] is None:
            # message.chat_row_id points at no row in chat (dangling FK) — surface
            # it instead of silently dropping the row or leaving a blank subject.
            chat_subject = missing_ref_label("chat record", "chat_row_id", r["raw_chat_row_id"])

        chat_members = _chat_members(r, is_group, members, owner_jids, owner_label)
        records.append({
            "message_id":       r["message_id"],
            "chat_id":          r["chat_id"],
            "chat_subject":     chat_subject,
            "chat_jid":         _fmt(r["chat_jid_raw"], r["chat_jid_id"]),
            "direction":        "Sent" if is_sent else "Received",
            "group_sender":     group_sender,
            "remote_party_jid": remote_jid,
            "remote_name":      remote_name,
            "chat_members":     chat_members,
            "chat_type":        _chat_type(r["chat_jid_raw"]),
            # message_type 15 = "deleted for everyone" (a message_revoked
            # row for each one: 204/204 on A15, 2026-10-24)
            "deleted_for_everyone": "Deleted for everyone" if r["message_type"] == 15 else "",
            "sent_time":        r["sent_time"],
            "text":             r["text"],
            "media_path":       r["media_path"],
            "attachment_path":  (f"{_MEDIA_BASE}/{r['media_path']}"
                                 if r["media_path"] else ''),
            "latitude":         r["latitude"],
            "longitude":        r["longitude"],
            # Plumbing only, for record_source below (see hidden_fields) --
            # an examiner already sees these resolved into chat_jid/
            # remote_party_jid/remote_name above; the bare id adds nothing
            # as report content, only as a jump target. *_mapped_* ones are
            # None on any row with no LID-privacy identity (the common
            # case -- see the jid_map comment above), which is expected,
            # not a gap: that record_source entry correctly reports "No
            # record-location data on this row" rather than resolving to
            # the wrong (non-mapped) jid/contact by falling back silently.
            "chat_jid_id":            r["chat_jid_id"],
            "sender_jid_id":          r["sender_jid_id"],
            "chat_mapped_jid_id":     r["chat_mapped_jid_id"],
            "sender_mapped_jid_id":   r["sender_mapped_jid_id"],
            "chat_contact_id":        r["chat_contact_id"],
            "sender_contact_id":      r["sender_contact_id"],
            "chat_mapped_contact_id": r["chat_mapped_contact_id"],
            "sender_mapped_contact_id": r["sender_mapped_contact_id"],
            "recovered":        False,
            "source_table":     "message",
        })

    from artifact_runner import embedded_media_path
    for rec in records:
        mid = rec.get("message_id")
        rec["thumbnail_link"] = (embedded_media_path(paths, "msgstore", "message_thumbnail",
                                                     "thumbnail", mid, thumbs.get(mid))
                                 if mid in thumbs else "")
    return records
