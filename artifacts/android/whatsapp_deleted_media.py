name = 'WhatsApp — Deleted & Changed Media'
description = (
    'Deleted and changed WhatsApp media records from msgstore.db (table message_media). '
    'Live media are in WhatsApp Messages; deleted and changed messages (table message) '
    'are in WhatsApp — Deleted & Changed Messages.\n'
    '\n'
    'Each row is one record as found in the message_media table, never joined to another '
    'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer '
    'in the table), Changed (an earlier version of a row still there, listed only when one '
    'of the columns that matter changed, with the current value beside it as "(now)") or '
    'an unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'linked_message: where the message this media belongs to was found. message_row_id '
    "(this table's own row id) is the _id of its message, so the link is made by comparing "
    'it with message._id, in the table now (live: the message is in WhatsApp Messages) and '
    'in the records WhatsApp — Deleted & Changed Messages finds (recovered, with its row '
    'id). Only ids are compared: no value of the message is brought in. How the link can be '
    "wrong: message._id is AUTOINCREMENT (checked 2026-11-27 on the Josh Hickman Android 14 "
    'image), so SQLite does not hand out an id again, but an app that re-inserts rows with '
    'their old ids (a restore from backup) can still put a different message under the same '
    'id; an earlier version found in the WAL and a live row with the same id were stored at '
    'different times; a record found by pattern has no row id and cannot be linked, and its '
    'message_row_id, being the row id, is lost with it; several copies of one record can '
    'carry the same id.\n'
    '\n'
    'Also listed: live message_media rows whose message is gone (status Orphaned): not '
    'deleted, so not recovered, and not in the main report, which lists a row only with '
    'its message. linked_message says whether that message was recovered. None on the test '
    'image checked (2026-11-21).\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records. Found earlier '
    '(carried over from WhatsApp Messages): msgstore.db has PRAGMA secure_delete = FAST, '
    'which zeroes freed page bytes at delete time. A real negative for recovery there, '
    'not evidence nothing was deleted.'
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
  'version_fields': ['file_path', 'media_caption', 'mime_type', 'file_hash'],
  'links': [{'name': 'linked_message', 'what': 'message', 'column': 'message_row_id',
             'table': 'message', 'key': '_id', 'report': 'whatsapp_deleted'}]}]

core_fields = ['status',
 'media_path',
 'mime_type',
 'media_caption',
 'linked_message',
 'changed_fields',
 'media_path (now)',
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
