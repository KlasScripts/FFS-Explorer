name = 'WhatsApp — Deleted & Changed Records'
description = (
    'Deleted and changed WhatsApp messages and media records from msgstore.db (tables '
    'message and message_media). Live messages are in WhatsApp Messages.\n'
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
    'timestamp and received_timestamp are Unix milliseconds, as in the WhatsApp Messages '
    'report.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier, '
    'on the extraction the WhatsApp report was built against (carried over from it): '
    'msgstore.db has PRAGMA secure_delete = FAST, which zeroes freed page bytes at delete '
    'time (the freed ranges were all zero apart from one 2-byte freeblock link) on a '
    'database with one confirmed-deleted message (a message._id gap); the same for '
    'message_ftsv2_content and the live WAL. A real negative for recovery there, not '
    'evidence nothing was deleted.'
    '\n\nAlso listed: live message_media rows whose message is gone (status Orphaned): '
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
  'version_fields': ['text_data', 'message_type', 'chat_row_id', 'sender_jid_row_id', 'from_me']},
 {'file_key': 'msgstore',
  'table': 'message_media',
  'label': 'Media',
  'fields': {'message_row_id': 'message_id',
             'chat_row_id': 'chat_id',
             'file_path': 'media_path',
             'file_size': 'file_size',
             'mime_type': 'mime_type',
             'media_name': 'media_name',
             'media_caption': 'media_caption',
             'file_hash': 'file_hash',
             'media_duration': 'media_duration',
             'width': 'width',
             'height': 'height'},
  'orphan_of': {'column': 'message_row_id', 'in': ['message', '_id']},
  'version_fields': ['file_path', 'media_caption', 'mime_type', 'file_hash']}]

timestamp_fields = {'timestamp': 'ms', 'received_timestamp': 'ms'}

core_fields = ['status',
 'record',
 'timestamp',
 'message',
 'media_path',
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
