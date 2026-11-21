name = 'Viber — Deleted & Changed Messages'
description = (
    "Deleted and changed Viber messages from viber_messages (table messages: a message's "
    'media fields are in the same row). Live messages are in Viber.\n'
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
    'msg_date is Unix milliseconds, as in the Viber report.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 3 deleted message records, '
    'each found by a rebuilt record header (no row id: How found says so; treat them as '
    'weaker than a record with a row id). Found earlier (carried over from the Viber '
    "report): a known 'sent, then deleted' test message on the extraction it was built "
    'against is not present as bytes anywhere in the main database, WAL or SHM.'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95, 2026-11-21: main reports hold live rows only). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "viber"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'viber_messages',
  'table': 'messages',
  'label': 'Message',
  'fields': {'_id': 'message_id',
             'conversation_id': 'conversation_id',
             'participant_id': 'participant_id',
             'msg_date': 'msg_date',
             'send_type': 'send_type',
             'body': 'message',
             'description': 'description',
             'extra_uri': 'extra_uri',
             'extra_mime': 'extra_mime',
             'location_lat': 'location_lat',
             'location_lng': 'location_lng',
             'deleted': 'deleted',
             'token': 'token',
             'message_global_id': 'message_global_id'},
  'version_fields': ['body',
                     'send_type',
                     'participant_id',
                     'conversation_id',
                     'extra_uri',
                     'deleted']}]

timestamp_fields = {'msg_date': 'ms'}

core_fields = ['status', 'msg_date', 'message', 'extra_mime', 'changed_fields', 'message (now)', 'how_found', "copies"]
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
