name = "Life360 — Deleted & Changed Messages (iOS)"
description = (
    "Life360 chat messages recovered from Messaging.sqlite (iOS) that are no longer in its "
    "message table, or that changed after they were stored. On the test image the table "
    "held no live messages at all: every message is recovered, from WAL frames. So "
    "\"Deleted\" here means not in the table now, which is not by itself proof that the "
    "user deleted it; the app's own deleted flag (deleted_in_app) is what marks a message "
    "the app recorded as deleted, and both documented deletions carry it. Each row is the "
    "record as found, not joined to members or threads: sender_id and thread_id are the "
    "stored ids. Direction is Outgoing when the sender is a member the app marks as the "
    "logged-in user.\n\n"
    "Checked against documented actions on a test image; see the notes page."
)

# Declaration-only report (report_type "recovered_records", see
# artifact_runner.recovered_records_report): no run() here.
#
# Messaging.sqlite: Life360's own container (com.life360.safetymap),
# Library/Application Support/ (Core Data). Checked 2026-11-17 on the Josh
# Hickman iOS 17 image against his documented actions
# (test_data/josh_hickman_ios17):
#   - ZCHATMESSAGE holds no live rows; 247 records recovered from WAL frames
#     are 33 messages under 46 distinct ZMESSAGEID values (13 records of
#     sent messages hold the client id, ZCLIENTMESSAGEID, before the
#     server's id replaces it).
#   - ZDATE is a Core Data time (seconds since 2001): the messages fall on
#     the documented dates; 30 of 31 documented sent/received messages are
#     recovered at their documented minute (not: the last one, 09:57).
#   - Direction: Outgoing when ZSENDER is a ZCHATMEMBER row with
#     ZISLOGGEDINUSER = 1 (one such member per circle): right on all 30.
#   - ZISLOCALLYDELETED = 1 is the app's deleted flag: it is set in a later
#     version of exactly the two messages documented as deleted (received
#     15:54 deleted 15:56, received 09:46 deleted 09:50, US Eastern) and of
#     no other message.
category = "Chats & Messaging"
app_bundle_id = "com.life360.safetymap"
files = {
    "messaging": "Library/Application Support/Messaging.sqlite",
}
# The -wal and -journal are extracted beside it by the runner (A-96).

report_type = "recovered_records"
recovered_records = [
    {
        "file_key": "messaging",
        "table": "ZCHATMESSAGE",
        "label": "Message",
        "fields": {
            "ZDATE": "timestamp",
            "ZMESSAGETEXT": "message",
            "ZISLOCALLYDELETED": "deleted_in_app",
            "ZMESSAGETYPE": "message_type",
            "ZSENDER": "sender_id",
            "ZTHREAD": "thread_id",
            "ZPHOTO": "photo_id",
            "ZLOCATION": "location_id",
            "ZACTION": "action",
            "ZMESSAGEID": "life360_message_id",
            # the app's own id before the server's: a message's records can
            # carry either (13 of the 46 ids on the test image are these)
            "ZCLIENTMESSAGEID": "client_message_id",
        },
        # A version matters when the text, the sender, the thread, the kind
        # or the app's deleted flag changed. Not ZISREAD / ZSENTSTATUSASINTEGER:
        # they change as a message is read and sent.
        "version_fields": ["ZMESSAGETEXT", "ZSENDER", "ZTHREAD", "ZMESSAGETYPE",
                           "ZISLOCALLYDELETED"],
        "labels": {"ZISLOCALLYDELETED": {1: "Yes", 0: "No"}},
        "derived": {"direction": {"from": "ZSENDER",
                                  "in": {"table": "ZCHATMEMBER", "column": "Z_PK",
                                         "where": "ZISLOGGEDINUSER = 1"},
                                  "then": "Outgoing", "else": "Incoming"}},
    },
]

timestamp_fields = {"timestamp": "cocoa_s"}

core_fields = ["status", "timestamp", "direction", "message", "deleted_in_app",
               "changed_fields", "how_found"]
hidden_fields = ["recovered", "recovery_method", "recovery_version", "source_table",
                 "source_file_key", "raw_rowid", "raw_page", "raw_file", "raw_offset",
                 "raw_length", "source"]
