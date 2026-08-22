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
# on-disk cell(s) a row was actually built from — a joined report like
# this one has more than one, so record_source is a LIST: this row's own
# message cell, plus the chat table it's LEFT JOINed to. Both live in the
# SAME db file (msgstore — "msgstore" key), unlike iOS WhatsApp's
# single-file case; Android WhatsApp's actual second file, wa.db
# ("wa" key, ATTACHed as contacts_db for the wa_contacts identity-lookup
# joins), is NOT wired here — this parser's SELECT never selects
# wa_contacts' own rowid, and its schema (specifically whether the
# declared TEXT `jid` primary key is the table's real rowid or a separate
# WITHOUT-ROWID-style key) hasn't been confirmed against a real
# extraction. Declaring a record_source entry for it without that
# verification would risk citing the WRONG cell — worse than the current
# "not available" gap, per this project's standing rule of never guessing
# at schema. Revisit once a real wa.db schema dump is available to check
# against, same rigor as every other record_source/media_fields entry in
# this project. "rowid_fields" tried in order: "message_id"/"chat_id" are
# this module's own run() output for message._id/chat._id (Android's
# standard rowid-alias primary key convention), "raw_rowid" is what a
# recoverable_tables-carved row carries instead
# (sqlite_carve.recover_deleted_rows) — carved rows resolve to nothing
# here today since locate_live_row only walks the CURRENT live b-tree, not
# freed space, which is the expected outcome for a genuinely deleted row,
# not a bug (and only applies to the Message entry — recovery is only
# declared for the "message" table below, never "chat").
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
]

# chat_id exists ONLY to feed the "Chat" record_source entry above — pure
# plumbing, never useful as report content (an examiner already sees the
# resolved chat_subject/chat_jid columns, and the no-chat-record fallback
# case already cites the raw FK inline via raw_chat_row_id, a separate
# field that stays visible). message_id and source_table stay visible:
# message_id is the row's OWN id, not a joined table's, and source_table
# is a citation label, not a raw key.
hidden_fields = ["chat_id"]

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
            COALESCE(chat_jidmap_contact.display_name, chat_jidmap_contact.wa_name,
                     chat_jidmap_contact.given_name)                            AS chat_mapped_contact_name,
            COALESCE(sender_contact.display_name, sender_contact.wa_name,
                     sender_contact.given_name)                                 AS sender_contact_name,
            COALESCE(sender_jidmap_contact.display_name, sender_jidmap_contact.wa_name,
                     sender_jidmap_contact.given_name)                          AS sender_mapped_contact_name,
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
            chat_subject = f"[no chat record — raw chat_row_id={r['raw_chat_row_id']}]"

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
            "recovered":        False,
            "source_table":     "message",
        })

    return records
