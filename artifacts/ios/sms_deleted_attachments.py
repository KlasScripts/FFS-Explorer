name = 'SMS & iMessage — Deleted & Changed Attachments'
description = (
    'Deleted and changed attachment records from sms.db (table attachment). Live '
    'attachments are in SMS Messages, with their message; deleted and changed messages '
    '(table message) are in SMS & iMessage — Deleted & Changed Messages.\n'
    '\n'
    'Each row is one record as found in the attachment table, never joined to another '
    'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer in '
    'the table), Changed (an earlier version of a row still there, listed only when one of '
    'the columns that matter changed, with the current value beside it as "(now)") or an '
    'unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'created_date is Cocoa seconds (checked 2026-11-21 on the Josh Hickman iOS 17 image: '
    'the earliest attachment is 2.5 s after the earliest message).\n'
    '\n'
    "linked_message: where the message this attachment belongs to was found. message_attachment_join holds one row per (message_id, attachment_id) pair, both the ROWIDs of their rows, so the link goes through it: the join rows holding this record's id are taken live or as recovered records of message_attachment_join, and the other id in each is looked up live and in the records the other deleted report finds. Each part of the cell says whether its join row was live or recovered. Only ids are compared: no value of the other row is brought in. On the Josh Hickman iOS 17 image (2026-11-27): 27 join rows, up to 3 attachments per message, every join row names a live message. How the link can be wrong: message.ROWID and attachment.ROWID are AUTOINCREMENT, so SQLite does not hand out an id again, but an app that re-inserts rows with their old ids (a restore from backup) can still put a different record under the same id; when a message goes, its join rows go with it, and sms.db's own trigger then deletes an attachment no other join row names, so a deleted record is linked only when its join row was recovered too, and a recovered join row may be from a different time than the rows it names; an earlier version found in the WAL and a live row with the same id were stored at different times; a record found by pattern has no row id and cannot be linked; several copies of one record can carry the same id.\n"
    '\n'
    'Also listed: attachment rows still live whose message is gone (status Orphaned): not '
    'deleted, so not recovered, and not in SMS Messages, which lists a row only with its '
    'message. On the Josh Hickman iOS 17 image (2026-11-21): 1 of 28 attachments, an '
    'outgoing image/jpeg with no message_attachment_join row.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 6 earlier versions of '
    'attachments (from the WAL), all Changed. sms.db has PRAGMA secure_delete = FAST '
    '(carried over from SMS Messages).'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95: main reports hold live rows only, 2026-11-21; one table per
# deleted report, 2026-11-27). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "sms_messages"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'sms',
  'table': 'attachment',
  'label': 'Attachment',
  'fields': {'ROWID': 'attachment_id',
             'guid': 'guid',
             'created_date': 'created_date',
             'filename': 'filename',
             'transfer_name': 'transfer_name',
             'mime_type': 'mime_type',
             'uti': 'uti',
             'total_bytes': 'total_bytes',
             'is_outgoing': 'is_outgoing',
             'is_sticker': 'is_sticker'},
  'orphan_of': {'column': 'ROWID', 'via': ['message_attachment_join', 'attachment_id', 'message_id'], 'in': ['message', 'ROWID']},
  'version_fields': ['filename', 'transfer_name', 'mime_type'],
  'links': [{'name': 'linked_message', 'what': 'message', 'column': 'ROWID',
             'via': ['message_attachment_join', 'attachment_id', 'message_id'],
             'table': 'message', 'key': 'ROWID', 'report': 'sms_deleted'}]}]

timestamp_fields = {'created_date': 'cocoa_s'}

core_fields = ['status',
 'created_date',
 'filename',
 'mime_type',
 'linked_message',
 'changed_fields',
 'filename (now)',
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
