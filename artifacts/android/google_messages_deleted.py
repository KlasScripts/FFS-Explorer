name = 'Google Messages — Deleted & Changed Messages'
description = (
    'Deleted and changed Google Messages message records from bugle_db (table messages). '
    "A message's text and attachments are not in this table but in parts rows: deleted and "
    'changed parts are in Google Messages — Deleted & Changed Parts. Live messages are in '
    'Google Messages (SMS/MMS/RCS); deleted conversations from the audit tables are in '
    'Google Messages — Recovered (deleted conversations).\n'
    '\n'
    'Each row is one record as found in the messages table, never joined to another table '
    '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer in the '
    'table), Changed (an earlier version of a row still there, listed only when one of the '
    'columns that matter changed, with the current value beside it as "(now)") or an '
    'unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'Times are Unix milliseconds, as in the Google Messages report.\n'
    '\n'
    "linked_parts: where this message's parts (its text and attachments) were found. Each "
    "parts row names its message in parts.message_id, so the link is made by finding the "
    "parts rows whose message_id is this record's message_id, in the table now (live, with "
    'their row ids) and in the records Google Messages — Deleted & Changed Parts finds '
    '(recovered, with their row ids); a message can have several (up to 3 on the test '
    'images) or none (178 of 1,278 live messages on the Josh Hickman image). Only ids are '
    'compared: no value of a part is brought in. How the link can be wrong: messages._id and parts._id are AUTOINCREMENT (checked 2026-11-27 on the Josh Hickman and CTF26 Android 14 images), so SQLite does not hand out an id again, but an app that re-inserts rows with their old ids (a restore from backup) can still put a different record under the same id; an earlier version found in the WAL and a live row with the same id were stored at different times; a record found by pattern has no row id and cannot be linked to by id; several copies of one record can carry the same id.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier '
    '(carried over from the Google Messages report): bugle_db has PRAGMA secure_delete = '
    'FAST; the freed page ranges were all zero on a database known to have a deleted '
    'conversation. A real negative for recovery, not evidence nothing was deleted.'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95: main reports hold live rows only, 2026-11-21; one table per
# deleted report, 2026-11-27). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "google_messages"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'bugle_db',
  'table': 'messages',
  'label': 'Message',
  'fields': {'_id': 'message_id',
             'conversation_id': 'conversation_id',
             'sender_id': 'sender_id',
             'sent_timestamp': 'sent_timestamp',
             'received_timestamp': 'received_timestamp',
             'message_protocol': 'message_protocol',
             'message_status': 'message_status',
             'mms_subject': 'mms_subject',
             'sms_message_uri': 'sms_message_uri',
             'rcs_message_id': 'rcs_message_id'},
  'version_fields': ['conversation_id', 'sender_id', 'message_protocol', 'mms_subject'],
  'links': [{'name': 'linked_parts', 'what': 'parts', 'column': '_id',
             'table': 'parts', 'key': 'message_id', 'report': 'google_messages_deleted_parts'}]}]

timestamp_fields = {'sent_timestamp': 'ms', 'received_timestamp': 'ms'}

core_fields = ['status',
 'received_timestamp',
 'sent_timestamp',
 'mms_subject',
 'linked_parts',
 'changed_fields',
 'how_found', "copies"]
hidden_fields = ['record',
 'recovered',
 'recovery_method',
 'recovery_version',
 'source_table',
 'source_file_key',
 'raw_rowid',
 'raw_page',
 'raw_file',
 'raw_offset',
 'raw_length',
 'source',
 'table_certain', "copy_locations"]
