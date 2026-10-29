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
              " Chat members (Chat Groups summary only, hidden in this report; 2026-10-24): everyone in each conversation, from group members from group_participant_user; the owner's number from shared_prefs/com.whatsapp_preferences_light.xml (registration_jid). Where a group's member list is empty it says so. This is membership as at extraction, not at the time of any message, so no message row claims who received it."
              " Reactions (2026-10-29): message_add_on type 56 with its emoji in message_add_on_reaction, shown on the message that was reacted to as \"emoji person time\", comma separated; that time is written in UTC. Last Reaction Time is the newest one."
              " Text and Event (2026-10-29): Text is exactly what the database stores. This report's own labels (calls, system lines, Photo/GIF/Sticker, location kind, Poll, unknown types) are in Event, never in Text. Group lines name the person message_system_chat_participant stores, not \"you\". Direction of a system row (type 7) is System, not Sent: the database stores from_me 1 on every system row, including groups dated years before the phone's first message (Pixel 7a image), so it does not show who acted. from_me is a column as stored.")
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
# chat_jid/phone_jid/lid/remote_name/media_path/latitude+longitude
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
    "thumbnail_link", "group_icon",
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
timestamp_fields = {"sent_time": "ms", "timestamp": "ms", "last_reaction_time": "ms"}

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
# Name falls through to phone_jid before chat_jid so a 1:1 chat stored as a
# LID is titled with its phone JID when jid_map has one, not with the LID.
# No sort/issued/setting. Android msgstore has no per-message sequence
# counter and no disappearing-message setting column, so the Chat Groups
# page does not show those headers. call_pattern counts the voice and
# video call rows by the Event column (call_field), never by the message
# text, so a typed "video call later?" is not counted as a call.
# icon: the group picture, files/Avatars/<jid>.j, shown on the Chat
# Groups page. Only written when that file is in the extraction.
chat_groups = {"key": "chat_id", "name": ["chat_subject", "remote_name", "phone_jid", "chat_jid"], "sent": ["Sent"], "system": ["System"], "media": ["attachment_path"], "message": "text", "time": "sent_time", "participants": ["remote_name", "phone_jid", "lid"], "members": "chat_members", "deleted": {"recovered": ["True"], "deleted_for_everyone": "*"}, "type": "chat_type", "call_pattern": r"^(Voice|Video) call\b", "call_field": "event", "icon": "group_icon"}

_AVATAR_DIR = "data/data/com.whatsapp/files/Avatars"


def _avatar_ui_path(paths, jid):
    """Archive path of a group's picture.

    WhatsApp stores it as files/Avatars/<jid>.j (a JPEG named with the
    .j extension). ALEAPP reads the same file. A 1:1 chat is left blank
    here: that filename is a phone address. '' when the extraction has
    no such file.
    """
    if not jid or not str(jid).endswith("@g.us"):
        return ""
    from artifact_runner import archive_has
    ui = f"{_AVATAR_DIR}/{jid}.j"
    return ui if archive_has(paths, ui) else ""


def _owner_label(reg, username):
    """Me line on Chat Groups. The phone is registration_jid. The username
    is a separate field (the You screen shows @name under the profile
    name). Neither is inferred from the other."""
    bits = []
    if reg:
        bits.append(f"+{reg}")
    if username:
        bits.append(username if str(username).startswith("@") else f"@{username}")
    if not bits:
        return "Me"
    # Chat Groups splits members on ", ", so the phone and username stay
    # inside one pair of parentheses.
    return "Me (" + " ".join(bits) + ")"


def _owner(paths):
    """(own jids, registration number or None, self_user_name or None,
    self_lid or None) from com.whatsapp_preferences_light.xml."""
    import re
    p = paths.get("prefs_light")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (TypeError, OSError):
        return set(), None, None, None
    def val(key):
        m = re.search(rf'<string name="{key}">([^<]+)<', text)
        return m.group(1) if m else None
    reg, lid, username = val("registration_jid"), val("self_lid"), val("self_user_name")
    jids = {j for j in (f"{reg}@s.whatsapp.net" if reg else None, lid) if j}
    return jids, reg, username, lid


def _username_for_lid(conn, self_lid):
    """Username stored on this phone's own @lid, when preferences have none.
    lid_display_name.lid_row_id is that jid. Empty if the table or the row
    is missing."""
    if not self_lid:
        return None
    try:
        row = conn.execute("""
            SELECT d.username
            FROM lid_display_name d
            JOIN jid j ON j._id = d.lid_row_id
            WHERE j.raw_string = ? AND d.username IS NOT NULL AND d.username != ''
        """, (self_lid,)).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def _fmt_jid(jid, jid_id):
    return f"{jid} ({jid_id})" if jid and jid_id is not None else ""


def _location_kind(latitude, longitude, place_name, place_address, url, live_duration):
    """Which kind of location message this was, from message_location.

    A current-location send stores coordinates and leaves the place name,
    address, and link empty. A place picked from the list stores those
    three. The coordinates alone do not say which one it was. A live
    share sets live_location_share_duration; that value was not present
    on the rows this was checked against. '' when there is no location.
    """
    if live_duration:
        return "Live location"
    if any((v or "").strip() for v in (place_name, place_address, url)):
        return "Selected place"
    if latitude is not None or longitude is not None:
        return "Current location"
    return ""


def _event_label(message_type, text, *, call=None, system=None, action=None,
                 timer=None, location_kind="", is_poll=False):
    """The Event column: what kind of message a row is, in this report's
    words. It is never copied into text, so text stays what the database
    stores and a search or a quote of text never finds these words.

    A call row is its call phrase. A type 7 row is its system phrase, or
    "Unknown system action N". Type 36 is the one-to-one timer line. A
    media kind confirmed on the test phone (photo, GIF, sticker), a
    location kind, and a poll are named. Any other type with no stored
    text is "Unknown message type N". Type 0 and type 15 (named in
    Deleted For Everyone) stay blank.
    """
    if call:
        return call
    if message_type == 7:
        return system or _unknown_label(7, action)
    if message_type == 36:
        span = _timer_span(timer)
        return f"Message timer updated, {span}" if span else "Message timer"
    parts = [p for p in (_kind_phrase(message_type), location_kind,
                         "Poll" if is_poll else "") if p]
    if parts:
        return ", ".join(parts)
    if (text is None or str(text).strip() == "") and message_type not in (0, 15):
        return _unknown_label(message_type, action)
    return ""


def _duration_clock(seconds):
    """call_log.duration is a whole number of seconds on this schema.
    Zero stays blank."""
    if not seconds:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min {secs} sec" if secs else f"{minutes} min"
    return f"{secs} sec"


def _call_phrase(video, result, duration):
    """Voice or video, then the outcome WhatsApp's own SQL names.

    call_result 5 is what the app counts as num_answered_outgoing_calls.
    call_result 2 with from_me 0 is the set it loads for missed calls;
    the same result on an outgoing call is the caller not being answered.
    Both are "Not answered" here. call_result 4 is a third code in the
    same column. The app counts it and does not name it, so neither does
    this phrase.
    """
    kind = {0: "Voice call", 1: "Video call"}.get(video, "Call")
    outcome = {5: "Answered", 2: "Not answered"}.get(result)
    parts = [kind]
    if outcome:
        parts.append(outcome)
    elif result is not None:
        parts.append(f"result {result}")
    clock = _duration_clock(duration)
    if clock:
        parts.append(clock)
    return ", ".join(parts)


def _timer_span(seconds):
    """The three timer choices this phone offered, in whole seconds."""
    return {86400: "24 hours", 604800: "7 days", 7776000: "90 days"}.get(seconds)


def _system_phrase(kind, blocked=None, action=None, joined=None,
                   privacy_type=None, text=None, duration=None):
    """Label for a type 7 row.

    Names used here were read off the linked phone on 27 October 2026,
    WhatsApp 2.26.42.71. An action we have not seen on that screen
    returns nothing, and the report says it is unknown.
    """
    if action == 118:
        return "Message pinned"
    if kind == "privacy" or (kind == "business" and privacy_type == 1):
        return "Encryption notice"
    if kind == "business" and privacy_type == 2:
        return "Business secure service"
    if kind == "blocked":
        return "Contact blocked" if blocked else "Contact unblocked"
    if kind == "number":
        return "Phone number changed"
    if kind == "username":
        return "Username changed"
    if kind == "photo":
        return "Group icon changed"
    if kind == "group" and action == 4 and joined:
        return "You were added"
    if kind == "group" and action == 5:
        return "Left the group"
    # A created-group row that stores a name was not shown as "created".
    # Who created it is not stored on the row, so the label does not say.
    if kind == "group" and action == 11 and not (text or "").strip():
        return "Group created"
    if kind == "group" and action == 14:
        return "Removed from the group"
    # Seen on this phone as "You're now an admin", but whether another
    # member's promotion uses the same action was not tested. The person
    # is taken from message_system_chat_participant (see _who_suffix).
    if kind == "group" and action == 15:
        return "Made an admin"
    # Action 56 is the timer line. Duration 0 was "You turned off
    # disappearing messages". A positive duration was "You updated the
    # message timer", and the screen named the 24-hour choice.
    if action == 56:
        return _timer_phrase(duration)
    return ""


def _timer_phrase(duration):
    """The disappearing-message line.

    Action 56 in a group, and message type 36 in a one-to-one chat, both
    showed this sentence. Duration 0 was only seen on action 56: "You
    turned off disappearing messages."
    """
    if duration == 0:
        return "Disappearing messages off"
    span = _timer_span(duration)
    if span:
        return f"Message timer updated, {span}"
    if duration:
        return "Message timer updated"
    return ""


def _unknown_label(message_type, action=None):
    """Visible marker for a code this phone stored and we have not named."""
    if message_type == 7:
        if action is None:
            return "Unknown system message"
        return f"Unknown system action {action}"
    if message_type is None:
        return "Unknown message"
    return f"Unknown message type {message_type}"


def _load_template_text(conn):
    """{message_id: text} for template messages.

    Type 27 leaves message.text_data empty. The wording is
    message_template.content_text_data, then the footer, then each
    button label in stored order. '' when this database has no
    template table.
    """
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        ("message_template",)).fetchone()
    if not found:
        return {}
    bodies = {}
    try:
        for mid, content, footer in conn.execute(
                "SELECT message_row_id, content_text_data, footer_text_data FROM message_template"):
            bodies[mid] = (content, footer)
    except Exception:
        return {}
    buttons = {}
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("message_template_button",)).fetchone():
        cols = {r[1] for r in conn.execute("PRAGMA table_info(message_template_button)")}
        if "message_row_id" in cols and "text_data" in cols:
            order = "_id" if "_id" in cols else "rowid"
            try:
                for mid, text in conn.execute(
                        f"SELECT message_row_id, text_data FROM message_template_button "
                        f"ORDER BY message_row_id, {order}"):
                    buttons.setdefault(mid, []).append(text)
            except Exception:
                buttons = {}
    out = {}
    ids = set(bodies) | set(buttons)
    for mid in ids:
        content, footer = bodies.get(mid, ("", ""))
        lines = [p.strip() for p in (content, footer) if p and str(p).strip()]
        lines.extend(b.strip() for b in buttons.get(mid, []) if b and str(b).strip())
        if lines:
            out[mid] = "\n".join(lines)
    return out


def _kind_phrase(message_type):
    """Short label for a media type confirmed on the same phone.
    Used only when the message has no caption."""
    return {1: "Photo", 13: "GIF", 20: "Sticker"}.get(message_type, "")


def _load_call_phrases(conn):
    """{message_id: phrase} for calls that are chat messages (type 90).
    {} if this msgstore has no call tables."""
    try:
        rows = conn.execute("""
            SELECT mcl.message_row_id, cl.video_call, cl.call_result, cl.duration
            FROM message_call_log mcl
            JOIN call_log cl ON cl._id = mcl.call_log_row_id
        """).fetchall()
    except Exception:
        return {}
    return {mid: _call_phrase(video, result, duration) for mid, video, result, duration in rows}


def _optional_system_join(conn, table, alias, expr):
    """(join SQL, select expression).

    Older msgstore builds omit a subtype table entirely. Joining a
    table that is not there used to throw away every system label,
    including the encryption notice. A missing table selects NULL."""
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    if not found:
        return "", "NULL"
    return (
        f"LEFT JOIN {table} {alias} ON {alias}.message_row_id = s.message_row_id",
        expr,
    )


def _load_system_phrases(conn):
    """{message_id: phrase} for type 7 rows we can name.
    Action 118 has no subtype table. A controlled pin on this phone
    wrote that action, so it is named from the action number."""
    joins = []
    selects = []
    for table, alias, expr in (
        ("message_system_block_contact", "b", "b.is_blocked"),
        ("message_system_initial_privacy_provider", "ip", "ip.message_row_id"),
        ("message_system_business_state", "bs", "bs.privacy_message_type"),
        ("message_system_number_change", "nc", "nc.message_row_id"),
        ("message_system_username_change", "uc", "uc.message_row_id"),
        ("message_system_photo_change", "pc", "pc.message_row_id"),
        ("message_system_group", "g", "g.is_me_joined"),
        ("message_ephemeral_setting", "e", "e.setting_duration"),
    ):
        join, selected = _optional_system_join(conn, table, alias, expr)
        if join:
            joins.append(join)
        selects.append(selected)
    # Column order consumed below: blocked, privacy, privacy type,
    # number, username, photo, joined, duration.
    blocked, privacy, privacy_type, number, username, photo, joined, duration = selects
    try:
        rows = conn.execute(f"""
            SELECT s.message_row_id, s.action_type,
                   {blocked},
                   {privacy},
                   {privacy_type},
                   {number},
                   {username},
                   {photo},
                   {joined},
                   m.text_data,
                   {duration}
            FROM message_system s
            LEFT JOIN message m ON m._id = s.message_row_id
            {' '.join(joins)}
        """).fetchall()
    except Exception:
        return {}
    out = {}
    for mid, action, blocked, privacy, privacy_type, number, username, photo, joined, text, duration in rows:
        if action == 118:
            kind, flag, ptype = "pinned", None, None
        elif privacy is not None:
            kind, flag, ptype = "privacy", None, None
        elif blocked is not None:
            kind, flag, ptype = "blocked", blocked, None
        elif number is not None:
            kind, flag, ptype = "number", None, None
        elif username is not None:
            kind, flag, ptype = "username", None, None
        elif photo is not None:
            kind, flag, ptype = "photo", None, None
        elif privacy_type is not None:
            kind, flag, ptype = "business", None, privacy_type
        elif joined is not None:
            kind, flag, ptype = "group", None, None
        else:
            kind, flag, ptype = "", None, None
        phrase = _system_phrase(
            kind, blocked=flag, action=action, joined=joined,
            privacy_type=ptype, text=text, duration=duration)
        if phrase:
            out[mid] = phrase
    return out


def _load_system_participants(conn):
    """{message_id: [user jid raw string, ...]} from
    message_system_chat_participant: the person a group line is about
    (who left, was removed, was made an admin). {} when the table is absent.
    """
    out = {}
    try:
        for mid, raw, server in conn.execute("""
            SELECT p.message_row_id, j.raw_string, j.server
            FROM message_system_chat_participant p
            JOIN jid j ON j._id = p.user_jid_row_id
            ORDER BY p.message_row_id, p.rowid
        """):
            out.setdefault(mid, []).append("lid_me" if server == "lid_me" else raw)
    except Exception:
        return {}
    return out


def _who_suffix(phrase, people, owner_jids, owner_label):
    """The group line with the person it is about, as stored. This phone's
    own address (or the lid_me row) is the Me label; anyone else is their
    stored address. No stored person leaves the line as it is."""
    names = []
    for raw in people or []:
        if raw == "lid_me" or raw in owner_jids:
            names.append(owner_label or "Me")
        elif raw:
            names.append(raw)
    return f"{phrase}: {', '.join(names)}" if names else phrase


def _addon_people(conn, owner_label, addon_type):
    """message_add_on rows of one type, each with the person who added it.

    This phone is from_me 1, or a sender whose server is lid_me. The label
    is the same Me line as Chat Groups. In a 1:1 chat an add-on with no
    sender address is the other person, named from the chat. In a group
    the sender is that person's own address, a LID mapped to a phone the
    same way as an incoming group message. An add-on whose message is
    gone is left out. Returns [] when the table is absent.
    """
    try:
        cur = conn.execute("""
            SELECT
                a._id AS addon_id,
                a.parent_message_row_id AS message_id,
                a.from_me AS from_me,
                a.timestamp AS sent_time,
                m.text_data AS question,
                c._id AS chat_id,
                c.subject AS chat_subject,
                chat_jid.raw_string AS chat_jid_raw,
                chat_jid._id AS chat_jid_id,
                chat_mapped.raw_string AS chat_mapped_raw,
                chat_mapped._id AS chat_mapped_id,
                COALESCE(chat_mapped_contact.display_name, chat_mapped_contact.wa_name,
                         chat_mapped_contact.given_name, chat_contact.display_name,
                         chat_contact.wa_name, chat_contact.given_name) AS chat_name,
                sender.raw_string AS sender_raw,
                sender._id AS sender_jid_id,
                sender.server AS sender_server,
                sender_mapped.raw_string AS sender_mapped_raw,
                sender_mapped._id AS sender_mapped_id,
                COALESCE(sender_mapped_contact.display_name, sender_mapped_contact.wa_name,
                         sender_mapped_contact.given_name, sender_contact.display_name,
                         sender_contact.wa_name, sender_contact.given_name) AS sender_name
            FROM message_add_on a
            JOIN message m ON m._id = a.parent_message_row_id
            LEFT JOIN chat c ON c._id = m.chat_row_id
            LEFT JOIN jid chat_jid ON chat_jid._id = c.jid_row_id
            LEFT JOIN jid_map chat_map ON chat_map.lid_row_id = c.jid_row_id
            LEFT JOIN jid chat_mapped ON chat_mapped._id = chat_map.jid_row_id
            LEFT JOIN jid sender
                   ON sender._id = a.sender_jid_row_id AND a.sender_jid_row_id > 0
            LEFT JOIN jid_map sender_map ON sender_map.lid_row_id = sender._id
            LEFT JOIN jid sender_mapped ON sender_mapped._id = sender_map.jid_row_id
            LEFT JOIN contacts_db.wa_contacts chat_contact
                   ON chat_contact.jid = chat_jid.raw_string
            LEFT JOIN contacts_db.wa_contacts chat_mapped_contact
                   ON chat_mapped_contact.jid = chat_mapped.raw_string
            LEFT JOIN contacts_db.wa_contacts sender_contact
                   ON sender_contact.jid = sender.raw_string
            LEFT JOIN contacts_db.wa_contacts sender_mapped_contact
                   ON sender_mapped_contact.jid = sender_mapped.raw_string
            WHERE a.message_add_on_type = ?
            ORDER BY a.timestamp, a._id
        """, (addon_type,))
        names = [d[0] for d in cur.description]
        addons = [dict(zip(names, row)) for row in cur.fetchall()]
    except Exception:
        return []
    me = owner_label or "Me"
    for a in addons:
        is_group = bool(a["chat_jid_raw"] and str(a["chat_jid_raw"]).endswith("@g.us"))
        chat_phone, chat_phone_id, chat_lid, chat_lid_id = _phone_and_lid(
            a["chat_jid_raw"], a["chat_jid_id"], a["chat_mapped_raw"], a["chat_mapped_id"])
        sender_phone, sender_phone_id, sender_lid, sender_lid_id = _phone_and_lid(
            a["sender_raw"], a["sender_jid_id"], a["sender_mapped_raw"], a["sender_mapped_id"])
        mine = a["from_me"] == 1 or a["sender_server"] == "lid_me"
        if mine:
            person, person_phone, person_lid = me, "", ""
        elif a["sender_raw"]:
            person = (a["sender_name"]
                      or _fmt_jid(sender_phone, sender_phone_id)
                      or _fmt_jid(sender_lid, sender_lid_id)
                      or _fmt_jid(a["sender_raw"], a["sender_jid_id"]))
            person_phone = _fmt_jid(sender_phone, sender_phone_id)
            person_lid = _fmt_jid(sender_lid, sender_lid_id)
        elif not is_group:
            person = (a["chat_name"]
                      or _fmt_jid(chat_phone, chat_phone_id)
                      or _fmt_jid(chat_lid, chat_lid_id)
                      or _fmt_jid(a["chat_jid_raw"], a["chat_jid_id"]))
            person_phone = _fmt_jid(chat_phone, chat_phone_id)
            person_lid = _fmt_jid(chat_lid, chat_lid_id)
        else:
            person, person_phone, person_lid = "", "", ""
        a["chat"] = (a["chat_subject"] or a["chat_name"]
                     or ("" if is_group else _fmt_jid(chat_phone, chat_phone_id))
                     or _fmt_jid(a["chat_jid_raw"], a["chat_jid_id"])
                     or "")
        a.update(mine=mine, is_group=is_group, person=person,
                 person_phone=person_phone, person_lid=person_lid,
                 chat_phone=_fmt_jid(chat_phone, chat_phone_id),
                 chat_lid=_fmt_jid(chat_lid, chat_lid_id))
    return addons


def poll_vote_records(conn, owner_label):
    """One row per selected poll option.

    message_add_on_type 67 is a vote on parent_message_row_id. The option
    is message_add_on_poll_vote_selected_option. A person who picks two
    options has two rows, and the count on the poll message is that row
    count. An add-on with no selected option is one row with a blank
    option. The voter is named as in _addon_people.
    """
    addons = _addon_people(conn, owner_label, 67)
    chosen = {}
    try:
        for addon_id, selected_id, option_name in conn.execute("""
            SELECT s.message_add_on_row_id, s._id, o.option_name
            FROM message_add_on_poll_vote_selected_option s
            JOIN message_poll_option o ON o._id = s.message_poll_option_id
            ORDER BY s.message_add_on_row_id, s._id
        """):
            chosen.setdefault(addon_id, []).append((selected_id, option_name or ""))
    except Exception:
        chosen = {}

    records = []
    for a in addons:
        picks = chosen.get(a["addon_id"]) or [(None, "")]
        for selected_id, option_name in picks:
            records.append({
                "chat": a["chat"],
                "question": a["question"] or "",
                "option": option_name or "",
                "voter": a["person"],
                "sent_time": a["sent_time"],
                "direction": "Sent" if a["mine"] else "Received",
                "chat_type": _chat_type(a["chat_jid_raw"]),
                "phone_jid": "" if a["is_group"] else a["chat_phone"],
                "lid": "" if a["is_group"] else a["chat_lid"],
                "chat_jid": _fmt_jid(a["chat_jid_raw"], a["chat_jid_id"]),
                "voter_phone_jid": a["person_phone"],
                "voter_lid": a["person_lid"],
                "message_id": a["message_id"],
                "addon_id": a["addon_id"],
                "selected_id": selected_id,
                "chat_id": a["chat_id"],
            })
    return records


def _reaction_time(ms):
    """UTC text for one reaction inside the Reactions cell.

    Text cannot follow the case's display time zone, so it says UTC.
    Last Reaction Time stays a raw value for the viewer to format.
    """
    from datetime import datetime, timezone
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S %d %b %Y UTC")


def reaction_summaries(conn, owner_label):
    """{message _id: (text, newest reaction time ms)} for reacted messages.

    message_add_on_type 56 is a reaction on parent_message_row_id, and
    message_add_on_reaction holds its emoji. The text is "emoji person
    time", joined with ", " in the order the reactions were stored. A
    blank emoji is skipped. The person is named as in _addon_people. The
    time is message_add_on.timestamp, the same clock as message.timestamp.
    """
    addons = _addon_people(conn, owner_label, 56)
    if not addons:
        return {}
    try:
        emoji = dict(conn.execute(
            "SELECT message_add_on_row_id, reaction FROM message_add_on_reaction"))
    except Exception:
        return {}
    parts = {}
    newest = {}
    for a in addons:
        mark = (emoji.get(a["addon_id"]) or "").strip()
        if not mark:
            continue
        mid = a["message_id"]
        when = a["sent_time"]
        parts.setdefault(mid, []).append(
            " ".join(p for p in (mark, a["person"], _reaction_time(when)) if p))
        if when is not None and (newest.get(mid) is None or when > newest[mid]):
            newest[mid] = when
    return {mid: (", ".join(items), newest.get(mid)) for mid, items in parts.items()}


def _group_members(conn):
    """{group jid row id: [(member jid row id, {jids}, label)]} from
    group_participant_user (membership at extraction, not at the time of a
    message). A member's privacy (@lid) jid is mapped to its phone jid via
    jid_map; the label is the contact name from wa.db, else the phone jid."""
    try:
        rows = conn.execute("""
            SELECT gpu.group_jid_row_id, u._id, u.server, u.raw_string, mapped.raw_string, mapped._id,
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
    for group, uid, server, raw, mapped, mapped_id, name in rows:
        # server lid_me is one sentinel row (raw string "lid_me"), not a
        # phone and not self_lid. On the linked companion it is how every
        # group that has members records this phone. Me is already added
        # from registration_jid. Hickman has no lid_me row.
        if server == "lid_me":
            continue
        jids = {j for j in (raw, mapped) if j}
        # the same form as phone_jid ("<phone jid> (<jid row id>)"), so
        # one person is written one way
        label = (name or (f"{mapped} ({mapped_id})" if mapped else None)
                 or (f"{raw} ({uid})" if raw else f"[jid row {uid}]"))
        out.setdefault(group, []).append((uid, jids, label))
    return out


def _chat_type(jid):
    """From the chat's jid server: @g.us group, @newsletter channel (A15
    follows several: Reuters, Chelsea FC...), @broadcast list, else 1:1.
    A 1:1 chat stored under a privacy LID (@lid) is still 1:1."""
    if not jid:
        return ""
    server = jid.rsplit("@", 1)[-1]
    return {"g.us": "Group", "newsletter": "Channel", "broadcast": "Broadcast",
            "status": "Status", "lid": "1:1"}.get(server, "1:1")


def _phone_and_lid(stored, stored_id, mapped, mapped_id):
    """(phone jid, phone id, lid, lid id) for one address.

    jid_map.lid_row_id is the @lid row and jid_map.jid_row_id is the
    @s.whatsapp.net row. That direction holds on the Android 14 test image,
    where chats themselves are still phone JIDs, and on a linked Android
    companion, where some 1:1 chats and the group members are stored as
    LIDs. A group (@g.us) or a channel is neither a phone JID nor a LID.
    """
    def server(jid):
        return jid.rsplit("@", 1)[-1] if jid and "@" in jid else ""

    phone = phone_id = lid = lid_id = None
    if server(stored) == "s.whatsapp.net":
        phone, phone_id = stored, stored_id
    elif server(stored) == "lid":
        lid, lid_id = stored, stored_id
        if server(mapped) == "s.whatsapp.net":
            phone, phone_id = mapped, mapped_id
    return phone, phone_id, lid, lid_id


def _party_addresses(is_group, is_sent, chat, sender):
    """(phone_jid, lid) for one message.

    The same two columns for a one-to-one chat and a group. One-to-one
    is the partner, on a sent row and a received row. An incoming group
    message is the sender. A group message this phone sent leaves both
    blank: a sender id on that row is this phone, or someone who did not
    send it. When both forms exist, both are returned. A phone with no
    LID leaves lid blank.
    """
    if is_group and is_sent:
        picked = (None, None, None, None)
    elif is_group:
        picked = sender
    else:
        picked = chat

    def fmt(jid, jid_id):
        return f"{jid} ({jid_id})" if jid and jid_id is not None else ""

    phone, phone_id, lid, lid_id = picked
    return fmt(phone, phone_id), fmt(lid, lid_id)

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
            message_location.longitude,
            message_location.place_name,
            message_location.place_address,
            message_location.url                                                AS location_url,
            message_location.live_location_share_duration                       AS live_duration
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
    owner_jids, reg, username, self_lid = _owner(paths)
    # self_user_name is what the You screen shows. Older extractions can
    # lack that key and still store the same name on this phone's own LID.
    if not username:
        username = _username_for_lid(conn, self_lid)
    owner_label = _owner_label(reg, username)
    polls = {}
    try:
        for mid, name in conn.execute("""
            SELECT message_row_id, option_name
            FROM message_poll_option
            WHERE option_name IS NOT NULL AND option_name != ''
            ORDER BY message_row_id, _id
        """):
            polls.setdefault(mid, []).append(name)
    except Exception:
        polls = {}
    members = _group_members(conn)
    call_phrases = _load_call_phrases(conn)
    system_phrases = _load_system_phrases(conn)
    participants = _load_system_participants(conn)
    templates = _load_template_text(conn)
    try:
        timer_durations = dict(conn.execute(
            "SELECT message_row_id, setting_duration FROM message_ephemeral_setting"))
    except Exception:
        timer_durations = {}
    try:
        system_actions = dict(conn.execute(
            "SELECT message_row_id, action_type FROM message_system"))
    except Exception:
        system_actions = {}
    vote_counts = {}
    for vote in poll_vote_records(conn, owner_label):
        vote_counts[vote["message_id"]] = vote_counts.get(vote["message_id"], 0) + 1
    reactions = reaction_summaries(conn, owner_label)
    conn.close()

    records = []
    for r in rows:
        is_sent  = r["from_me"] == 1
        is_group = bool(r["chat_jid_raw"] and r["chat_jid_raw"].endswith("@g.us"))

        def _fmt(jid, jid_id):
            return f"{jid} ({jid_id})" if jid and jid_id is not None else None

        # jid_map.lid_row_id -> jid_row_id is LID -> phone JID. On the
        # Android 14 test image the chats themselves are phone JIDs, so the
        # mapped side is empty and the stored jid is the phone. On a linked
        # companion some 1:1 chats, and the group senders, are stored as
        # LIDs and the phone is only on the mapped side. Never invent a
        # phone when that row is missing.
        chat_phone, chat_phone_id, chat_lid, chat_lid_id = _phone_and_lid(
            r["chat_jid_raw"], r["chat_jid_id"], r["chat_mapped_jid_raw"], r["chat_mapped_jid_id"])
        sender_phone, sender_phone_id, sender_lid, sender_lid_id = _phone_and_lid(
            r["sender_jid_raw"], r["sender_jid_id"], r["sender_mapped_jid_raw"], r["sender_mapped_jid_id"])
        phone_jid, lid = _party_addresses(
            is_group, is_sent,
            (chat_phone, chat_phone_id, chat_lid, chat_lid_id),
            (sender_phone, sender_phone_id, sender_lid, sender_lid_id))

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
        # text is what the database stores for the message, unchanged.
        # Type 27 keeps its wording on message_template, not text_data.
        text = r["text"]
        if text in (None, "") and r["message_id"] in templates:
            text = templates[r["message_id"]]
        system = system_phrases.get(r["message_id"]) if r["message_type"] == 7 else None
        # Left, removed, made an admin: name the person the row stores.
        if system and system_actions.get(r["message_id"]) in (5, 14, 15):
            system = _who_suffix(system, participants.get(r["message_id"]), owner_jids, owner_label)
        location_kind = _location_kind(
            r["latitude"], r["longitude"], r["place_name"], r["place_address"],
            r["location_url"], r["live_duration"])
        event = _event_label(
            r["message_type"], text,
            call=call_phrases.get(r["message_id"]),
            system=system,
            action=system_actions.get(r["message_id"]),
            timer=timer_durations.get(r["message_id"]),
            location_kind=location_kind,
            is_poll=r["message_id"] in polls)
        records.append({
            "message_id":       r["message_id"],
            "chat_id":          r["chat_id"],
            "chat_subject":     chat_subject,
            "chat_jid":         _fmt(r["chat_jid_raw"], r["chat_jid_id"]),
            # The other person. One-to-one: the partner. Incoming group
            # message: the sender. Both forms when the database has both.
            "phone_jid":        phone_jid,
            "lid":              lid,
            # A type 7 system row is not a message anyone sent. Its from_me
            # is 1 even for groups other people created years before this
            # phone (Pixel 7a image: all 165 system rows), so it is
            # "System", never "Sent". from_me stays visible as stored.
            "direction":        ("System" if r["message_type"] == 7
                                 else "Sent" if is_sent else "Received"),
            "from_me":          r["from_me"],
            "remote_name":      remote_name,
            "chat_members":     chat_members,
            "chat_type":        _chat_type(r["chat_jid_raw"]),
            # message_type 15 = "deleted for everyone" (a message_revoked
            # row for each one: 204/204 on A15, 2026-10-24)
            "deleted_for_everyone": "Deleted for everyone" if r["message_type"] == 15 else "",
            "sent_time":        r["sent_time"],
            "text":             text,
            # Written by this report, not stored as message text: a call,
            # a system line, a media kind, a location kind, a poll.
            "event":            event,
            "poll_options":     " | ".join(polls.get(r["message_id"]) or []),
            "poll_votes":       vote_counts.get(r["message_id"], 0) if r["message_id"] in polls else "",
            "reactions":        reactions.get(r["message_id"], ("", None))[0],
            "last_reaction_time": reactions.get(r["message_id"], ("", None))[1],
            "location_kind":    location_kind,
            "place_name":       r["place_name"] or "",
            "place_address":    r["place_address"] or "",
            "location_url":     r["location_url"] or "",
            "media_path":       r["media_path"],
            "attachment_path":  (f"{_MEDIA_BASE}/{r['media_path']}"
                                 if r["media_path"] else ''),
            "latitude":         r["latitude"],
            "longitude":        r["longitude"],
            # Plumbing only, for record_source below (see hidden_fields) --
            # an examiner already sees these resolved into chat_jid/
            # phone_jid/lid/remote_name above; the bare id adds nothing
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
    icons = {}
    for rec in records:
        jid = ""
        raw = rec.get("chat_jid") or ""
        # chat_jid is "jid (id)". The file is named with the bare jid.
        if raw.endswith("@g.us") or "@g.us " in raw:
            jid = raw.split(" ", 1)[0]
        if jid not in icons:
            icons[jid] = _avatar_ui_path(paths, jid)
        rec["group_icon"] = icons[jid]
        mid = rec.get("message_id")
        rec["thumbnail_link"] = (embedded_media_path(paths, "msgstore", "message_thumbnail",
                                                     "thumbnail", mid, thumbs.get(mid))
                                 if mid in thumbs else "")
    return records
