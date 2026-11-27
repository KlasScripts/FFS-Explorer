name = 'Google Messages — Deleted & Changed Parts'
description = (
    'Deleted and changed Google Messages parts records from bugle_db (table parts: a '
    "message's text and each of its attachments is a parts row). Live messages are in "
    'Google Messages (SMS/MMS/RCS); deleted and changed message records (table messages) '
    'are in Google Messages — Deleted & Changed Messages.\n'
    '\n'
    'Each row is one record as found in the parts table, never joined to another table '
    '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer in the '
    'table), Changed (an earlier version of a row still there, listed only when one of the '
    'columns that matter changed, with the current value beside it as "(now)") or an '
    'unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'timestamp is Unix milliseconds, as in the Google Messages report.\n'
    '\n'
    'linked_message: where the message this part belongs to was found. parts.message_id '
    'holds the _id of its messages row, so the link is made by comparing the two, in the '
    'table now (live: the message is in Google Messages) and in the records Google Messages '
    '— Deleted & Changed Messages finds (recovered, with its row id). Every live part names '
    'a live message on the test images (2026-11-27). Only ids are compared: no value of the '
    'message is brought in. How the link can be wrong: messages._id and parts._id are AUTOINCREMENT (checked 2026-11-27 on the Josh Hickman and CTF26 Android 14 images), so SQLite does not hand out an id again, but an app that re-inserts rows with their old ids (a restore from backup) can still put a different record under the same id; an earlier version found in the WAL and a live row with the same id were stored at different times; a record found by pattern has no row id and cannot be linked to by id; several copies of one record can carry the same id.\n'
    '\n'
    'Also listed: live parts rows whose message is gone (status Orphaned): not deleted, so '
    'not recovered, and not in the main report, which lists a row only with its message. '
    'linked_message says whether that message was recovered. None on the test image '
    'checked (2026-11-21).\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. bugle_db has '
    'PRAGMA secure_delete = FAST (carried over from the Google Messages report).'
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
  'version_fields': ['text', 'content_type', 'uri', 'file_name'],
  'links': [{'name': 'linked_message', 'what': 'message', 'column': 'message_id',
             'table': 'messages', 'key': '_id', 'report': 'google_messages_deleted'}]}]

timestamp_fields = {'timestamp': 'ms'}

core_fields = ['status',
 'timestamp',
 'message',
 'content_type',
 'file_name',
 'linked_message',
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
