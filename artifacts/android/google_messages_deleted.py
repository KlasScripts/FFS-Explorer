name = 'Google Messages — Deleted & Changed Records'
description = (
    'Deleted and changed Google Messages records from bugle_db (tables messages and '
    "parts: a message's text and attachments are parts rows). Live messages are in Google "
    'Messages (SMS/MMS/RCS); deleted conversations from the audit tables are in Google '
    'Messages — Recovered (deleted conversations).\n'
    '\n'
    'Each row is one record as found in one table, never joined to another table '
    '(ASSUMPTIONS.md A-95): ids are the stored ids, and a message and its media are '
    'separate rows. Status says Deleted (no longer in the table), Changed (an earlier '
    'version of a row still there, listed only when one of the columns that matter '
    'changed, with the current value beside it as "(now)") or an unfinished write/delete '
    'under a hot journal. How found says how each record was recovered and how sure that '
    'is; a record found by pattern has no row id. Values are as stored: no code is turned '
    'into a word here unless it was checked against a test image.\n'
    '\n'
    'Times are Unix milliseconds, as in the Google Messages report.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier '
    '(carried over from the Google Messages report): bugle_db has PRAGMA secure_delete = '
    'FAST; the freed page ranges were all zero on a database known to have a deleted '
    'conversation. A real negative for recovery, not evidence nothing was deleted.'
    '\n\nAlso listed: live parts rows whose message is gone (status Orphaned): '
    'not deleted, so not recovered, and not in the main report, which lists a row only '
    'with its message. None on the test image checked (2026-11-21).'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95, 2026-11-21: main reports hold live rows only). The shared code in
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
  'version_fields': ['conversation_id', 'sender_id', 'message_protocol', 'mms_subject']},
 {'file_key': 'bugle_db',
  'table': 'parts',
  'label': 'Part (text or attachment)',
  'fields': {'_id': 'part_id',
             'message_id': 'message_id',
             'conversation_id': 'conversation_id',
             'text': 'message',
             'content_type': 'content_type',
             'uri': 'uri',
             'file_name': 'file_name',
             'file_size_bytes': 'file_size',
             'timestamp': 'timestamp',
             'latitude': 'latitude',
             'longitude': 'longitude',
             'local_cache_path': 'local_cache_path'},
  'orphan_of': {'column': 'message_id', 'in': ['messages', '_id']},
  'version_fields': ['text', 'content_type', 'uri', 'file_name']}]

timestamp_fields = {'sent_timestamp': 'ms', 'received_timestamp': 'ms', 'timestamp': 'ms'}

core_fields = ['status',
 'record',
 'received_timestamp',
 'timestamp',
 'message',
 'content_type',
 'changed_fields',
 'message (now)',
 'how_found', "copies"]
hidden_fields = ['recovered',
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
