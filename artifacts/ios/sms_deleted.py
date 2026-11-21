name = 'SMS & iMessage — Deleted & Changed Records'
description = (
    'Deleted and changed messages and attachment records from sms.db (tables message and '
    "attachment). Live messages, and the ones in iOS's own Recently Deleted window, are "
    "in SMS Messages. From iOS 16 a message's text is often only in attributedBody (a "
    'typedstream blob), shown here as stored, not decoded.\n'
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
    'message dates are Cocoa nanoseconds, as in SMS Messages; attachment.created_date is '
    'Cocoa seconds (checked 2026-11-21 on the Josh Hickman iOS 17 image: the earliest '
    'attachment is 2.5 s after the earliest message).\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 4 earlier versions of 2 '
    'messages (from the WAL and overflow pages) and 6 of attachments (from the WAL), all '
    "Changed; 2 more versions differ only in columns that don't matter here and are "
    'counted in the run log. Found earlier (carried over from SMS Messages): sms.db has '
    'PRAGMA secure_delete = FAST (freed ranges all zero); two messages deleted 6+ months '
    "before the extraction are gone, from this table and from Apple's own "
    "recoverable-message tables. A message deleted within iOS's ~30-40 day window is "
    'still live, and shown in SMS Messages. '
    'Also listed: attachment rows still live whose message is gone (status Orphaned): not '
    'deleted, so not recovered, and not in SMS Messages, which lists a row only with its message. '
    'On the Josh Hickman iOS 17 image (2026-11-21): 1 of 28 attachments, an outgoing '
    'image/jpeg with no message_attachment_join row.'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95, 2026-11-21: main reports hold live rows only). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "sms_messages"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'sms',
  'table': 'message',
  'label': 'Message',
  'fields': {'ROWID': 'message_id',
             'guid': 'guid',
             'handle_id': 'handle_id',
             'service': 'service',
             'account': 'account',
             'date': 'date',
             'date_read': 'date_read',
             'date_delivered': 'date_delivered',
             'date_edited': 'date_edited',
             'date_retracted': 'date_retracted',
             'is_from_me': 'is_from_me',
             'text': 'message',
             'attributedBody': 'attributed_body',
             'item_type': 'item_type',
             'group_title': 'group_title',
             'cache_has_attachments': 'has_attachments',
             'associated_message_guid': 'associated_message_guid'},
  'version_fields': ['text',
                     'attributedBody',
                     'handle_id',
                     'is_from_me',
                     'item_type',
                     'group_title']},
 {'file_key': 'sms',
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
  'version_fields': ['filename', 'transfer_name', 'mime_type']}]

timestamp_fields = {'date': 'cocoa_ns',
 'date_read': 'cocoa_ns',
 'date_delivered': 'cocoa_ns',
 'date_edited': 'cocoa_ns',
 'date_retracted': 'cocoa_ns',
 'created_date': 'cocoa_s'}

core_fields = ['status',
 'record',
 'date',
 'created_date',
 'message',
 'filename',
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
