import sqlite3

name     = "WhatsApp Messages"
app_path = "data/data/com.whatsapp"
files    = {
    "msgstore": "databases/msgstore.db",
    "wa":       "databases/wa.db",
}


def run(paths):
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
            datetime(m.timestamp / 1000, 'unixepoch', 'localtime')             AS sent_time,
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

        if is_sent and is_group:
            remote_jid = None
        elif is_group:
            remote_jid = _fmt(r["sender_mapped_jid_raw"], r["sender_mapped_jid_id"])
        else:
            remote_jid = _fmt(r["chat_mapped_jid_raw"], r["chat_mapped_jid_id"])

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
            "latitude":         r["latitude"],
            "longitude":        r["longitude"],
        })

    return records
