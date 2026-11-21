name = 'GroupMe — Deleted & Changed Messages'
description = (
    "Deleted and changed GroupMe messages from groupme.db (table messages: a message's "
    'photo, video and location are in the same row). Live messages are in GroupMe.\n'
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
    'created_at and deleted_at are Unix seconds, as in the GroupMe report.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier '
    '(carried over from the GroupMe report): no WAL for this database, and its live pages '
    'held nothing recoverable.'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95, 2026-11-21: main reports hold live rows only). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "groupme"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'groupme',
  'table': 'messages',
  'label': 'Message',
  'fields': {'_id': 'row_id',
             'message_id': 'message_id',
             'conversation_id': 'conversation_id',
             'user_id': 'user_id',
             'sender_id': 'sender_id',
             'name': 'name',
             'created_at': 'created_at',
             'message_text': 'message',
             'is_system': 'is_system',
             'photo_url': 'photo_url',
             'video_url': 'video_url',
             'location_lat': 'location_lat',
             'location_lng': 'location_lng',
             'location_name': 'location_name',
             'deleted_at': 'deleted_at',
             'deletion_actor': 'deletion_actor'},
  'version_fields': ['message_text',
                     'photo_url',
                     'video_url',
                     'user_id',
                     'deleted_at',
                     'deletion_actor']}]

timestamp_fields = {'created_at': 's', 'deleted_at': 's'}

core_fields = ['status',
 'created_at',
 'name',
 'message',
 'photo_url',
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
