import sqlite3

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
              "which WhatsApp does not appear to route through that table.")
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
hidden_fields = [
    "chat_id", "chat_jid_id", "sender_jid_id",
    "chat_mapped_jid_id", "sender_mapped_jid_id",
    "chat_contact_id", "sender_contact_id",
    "chat_mapped_contact_id", "sender_mapped_contact_id",
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


def run(paths):
    from artifact_runner import missing_ref_label

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
    conn = sqlite3.connect(paths["msgstore"])
    conn.execute("ATTACH DATABASE ? AS contacts_db", (paths["wa"],))
    cur = conn.cursor()

    cur.execute("""
        SELECT
            m._id                                                               AS message_id,
            m.from_me,
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

        records.append({
            "message_id":       r["message_id"],
            "chat_id":          r["chat_id"],
            "chat_subject":     chat_subject,
            "chat_jid":         _fmt(r["chat_jid_raw"], r["chat_jid_id"]),
            "direction":        "Sent" if is_sent else "Received",
            "group_sender":     group_sender,
            "remote_party_jid": remote_jid,
            "remote_name":      remote_name,
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

    return records
