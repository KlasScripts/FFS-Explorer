name = "SMS Messages"
target_paths = ["mobile/Library/SMS/sms.db"]


def run(db):
    rows = db.execute("""
        SELECT
            datetime(m.date / 1000000000 + 978307200, 'unixepoch', 'localtime') AS date,
            h.id                                                                  AS contact,
            CASE m.is_from_me WHEN 1 THEN 'Sent' ELSE 'Received' END             AS direction,
            m.text                                                                AS body,
            m.service                                                             AS service
        FROM message m
        LEFT JOIN handle h ON m.handle_id = h.rowid
        ORDER BY m.date
    """).fetchall()

    return [
        {
            "date":      r[0],
            "contact":   r[1],
            "direction": r[2],
            "body":      r[3],
            "service":   r[4],
        }
        for r in rows
    ]
