name = "LINE — Deleted & Changed Messages (iOS)"
description = (
    "LINE messages that were deleted, or changed after they were stored, recovered from "
    "Line.sqlite (iOS): rows removed from the message table (from the WAL, free space, "
    "freed pages), the unfinished write under a hot journal, and earlier versions of "
    "messages that are still there, listed only when the text, kind, sender, chat or "
    "location changed (the current value beside it, as \"(now)\"). Each row is the record "
    "as found in that one table, not joined to chats or contacts: chat_id and sender_id are "
    "the stored ids. Earlier versions of a chat's last-message preview are listed only when "
    "their text is in no message, live or recovered; most are app-generated (call "
    "durations, locations, official accounts), but one can be the only trace of a deleted "
    "message.\n\n"
    "How found says how each record was recovered and how sure that is; a record found by "
    "pattern has no row id. Direction (Outgoing / Incoming, as in the Android LINE report) "
    "comes from the sender column being empty for messages this phone sent, checked "
    "against documented actions on a test image; so are the content-type words. See the "
    "notes page. Live messages and calls are in LINE Messages (iOS)."
)

# Declaration-only report (report_type "recovered_records"): no run() here.
# The shared code in artifact_runner (recovered_records_report) recovers the
# records with sqlite_recovery and makes the rows; this file names the
# database and the columns that matter. Not joined to other tables.
#
# Line.sqlite: app group group.com.linecorp.line, under a per-account folder
# Library/Application Support/PrivateStore/P_<id>/Messages/ (Core Data).
# Checked 2026-11-17 on the Josh Hickman iOS 17 image against his
# documented actions (test_data/josh_hickman_ios17):
#   - ZMESSAGE.ZTIMESTAMP is Unix milliseconds: the two documented deleted
#     messages are at 16:42 and 10:11 US Eastern, the documented receive
#     times before the deletions at 16:43 and 10:17.
#   - ZSENDER empty = sent by this phone (Outgoing): 23 of 24 documented sent
#     messages; set for 21 of 22 received ones (the two others share their
#     minute with a message the other way).
#   - ZCONTENTTYPE 0 = text (37), 1 = picture (4), 100 = static location (3);
#     6 seen twice, not identified, so shown as stored.
#   - Both documented deleted messages are recovered from ZMESSAGE (WAL
#     frames, rows no longer in the table).
# Names follow the Android LINE report (artifacts/android/line.py):
# timestamp, message, direction Outgoing / Incoming.
category = "Chats & Messaging"

deleted_of = "line"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [
    {
        "file_key": "line",
        "table": "ZMESSAGE",
        "label": "Message",
        "fields": {
            "ZTIMESTAMP": "timestamp",
            "ZTEXT": "message",
            "ZCONTENTTYPE": "content_type",
            "ZLATITUDE": "latitude",
            "ZLONGITUDE": "longitude",
            "ZCHAT": "chat_id",
            "ZSENDER": "sender_id",
            "ZID": "line_message_id",
        },
        # A version matters when what was said, by whom, where or of what
        # kind changed. Not ZTIMESTAMP / ZSENDSTATUS / ZCONTENTMETADATA: they
        # change as a message is sent and delivered (27 of 31 earlier versions
        # on the iOS 17 image changed only those).
        "version_fields": ["ZTEXT", "ZCONTENTTYPE", "ZSENDER", "ZCHAT",
                           "ZLATITUDE", "ZLONGITUDE"],
        "labels": {"ZCONTENTTYPE": {0: "text", 1: "picture", 100: "location"}},
        "derived": {"direction": {"from": "ZSENDER", "values": {None: "Outgoing"},
                                  "else": "Incoming"}},
    },
    {
        "file_key": "line",
        "table": "ZCHAT",
        "label": "Chat preview (not a message row)",
        "fields": {"ZLASTMESSAGE": "message", "ZMID": "chat_mid"},
        "version_fields": ["ZLASTMESSAGE"],
        # only a preview whose text is in no message, live or recovered
        # (a comparison of values, not a join)
        "not_found_in": {"ZLASTMESSAGE": ["ZMESSAGE", "ZTEXT"]},
    },
]

timestamp_fields = {"timestamp": "ms"}

core_fields = ["status", "timestamp", "direction", "message", "changed_fields", "message (now)",
               "content_type", "record", "how_found", "copies"]
hidden_fields = ["recovered", "recovery_method", "recovery_version", "source_table",
                 "source_file_key", "raw_rowid", "raw_page", "raw_file", "raw_offset",
                 "raw_length", "source", "copy_locations"]
