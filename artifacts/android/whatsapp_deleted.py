name = 'WhatsApp — Deleted & Changed Messages'
description = (
    'Deleted and changed WhatsApp messages from msgstore.db (table message). Live '
    'messages are in WhatsApp Messages; deleted and changed media records (table '
    'message_media) are in WhatsApp — Deleted & Changed Media.\n'
    '\n'
    'Each row is one record as found in the message table, never joined to another table '
    '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer in the '
    'table), Changed (an earlier version of a row still there, listed only when one of the '
    'columns that matter changed, with the current value beside it as "(now)") or an '
    'unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'timestamp and received_timestamp are Unix milliseconds, as in the WhatsApp Messages '
    'report.\n'
    '\n'
    'linked_media: where the media record for this message was found. A message_media '
    "row's own row id (message_row_id) is the _id of its message, so the link is made by "
    "comparing this record's message_id with message_media.message_row_id, in the table "
    'now (live) and in the records WhatsApp — Deleted & Changed Media finds (recovered, '
    'with its row id). Only ids are compared: no value of the media row is brought in. '
    '"not found" is usual: a text message has no media row. How the link can be wrong: '
    "message._id is AUTOINCREMENT (checked 2026-11-27 on the Josh Hickman Android 14 "
    'image), so SQLite does not hand out an id again, but an app that re-inserts rows with '
    'their old ids (a restore from backup) can still put a different message under the same '
    'id; an earlier version found in the WAL and a live row with the same id were stored at '
    'different times; a record found by pattern has no row id and cannot be linked; '
    'several copies of one record can carry the same id.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier, '
    'on the extraction the WhatsApp report was built against (carried over from it): '
    'msgstore.db has PRAGMA secure_delete = FAST, which zeroes freed page bytes at delete '
    'time (the freed ranges were all zero apart from one 2-byte freeblock link) on a '
    'database with one confirmed-deleted message (a message._id gap); the same for '
    'message_ftsv2_content and the live WAL. A real negative for recovery there, not '
    'evidence nothing was deleted.'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95: main reports hold live rows only, 2026-11-21; one table per
# deleted report, 2026-11-27). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "whatsapp"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'msgstore',
  'table': 'message',
  'label': 'Message',
  'fields': {'_id': 'message_id',
             'chat_row_id': 'chat_id',
             'from_me': 'from_me',
             'key_id': 'key_id',
             'sender_jid_row_id': 'sender_jid_id',
             'status': 'message_status',
             'timestamp': 'timestamp',
             'received_timestamp': 'received_timestamp',
             'message_type': 'message_type',
             'text_data': 'message',
             'starred': 'starred'},
  'version_fields': ['text_data', 'message_type', 'chat_row_id', 'sender_jid_row_id', 'from_me'],
  'links': [{'name': 'linked_media', 'what': 'media', 'column': '_id',
             'table': 'message_media', 'key': 'message_row_id',
             'report': 'whatsapp_deleted_media'}]}]

timestamp_fields = {'timestamp': 'ms', 'received_timestamp': 'ms'}

core_fields = ['status',
 'timestamp',
 'message',
 'linked_media',
 'changed_fields',
 'message (now)',
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
