name = 'LINE — Deleted & Changed Messages'
description = (
    'Deleted and changed LINE messages from naver_line (table chat_history: the message '
    'and its attachment fields are in the same row). Live messages are in Line; messages '
    "kept only in LINE's full-text search index are in LINE — Recovered (full-text search "
    'index).\n'
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
    'created_time and delivered_time are Unix milliseconds, as in the Line report.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier '
    '(carried over from the Line report): on the extraction it was built against '
    'naver_line has no WAL and its live pages held nothing recoverable; the one deleted '
    'message there survived only in the full-text-search index (see LINE — Recovered '
    '(full-text search index)).'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95, 2026-11-21: main reports hold live rows only). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "line"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'naver_line',
  'table': 'chat_history',
  'label': 'Message',
  'fields': {'id': 'message_id',
             'server_id': 'server_id',
             'chat_id': 'chat_id',
             'from_mid': 'from_mid',
             'type': 'type',
             'content': 'message',
             'created_time': 'created_time',
             'delivered_time': 'delivered_time',
             'status': 'message_status',
             'attachement_type': 'attachment_type',
             'attachement_local_uri': 'attachment_local_uri',
             'location_name': 'location_name',
             'location_latitude': 'location_latitude',
             'location_longitude': 'location_longitude'},
  'version_fields': ['content', 'from_mid', 'chat_id', 'type', 'attachement_local_uri']}]

timestamp_fields = {'created_time': 'ms', 'delivered_time': 'ms'}

core_fields = ['status',
 'created_time',
 'message',
 'attachment_type',
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
