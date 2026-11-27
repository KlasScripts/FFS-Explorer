name = 'SMS & iMessage — Deleted & Changed Messages'
description = (
    'Deleted and changed messages from sms.db (table message). Live messages, and the ones '
    "in iOS's own Recently Deleted window, are in SMS Messages; deleted and changed "
    'attachment records (table attachment) are in SMS & iMessage — Deleted & Changed '
    "Attachments. From iOS 16 a message's text is often only in attributedBody (a "
    'typedstream blob), shown here as stored, not decoded.\n'
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
    'message dates are Cocoa nanoseconds, as in SMS Messages.\n'
    '\n'
    "linked_attachments: where this message's attachments were found. message_attachment_join holds one row per (message_id, attachment_id) pair, both the ROWIDs of their rows, so the link goes through it: the join rows holding this record's id are taken live or as recovered records of message_attachment_join, and the other id in each is looked up live and in the records the other deleted report finds. Each part of the cell says whether its join row was live or recovered. Only ids are compared: no value of the other row is brought in. On the Josh Hickman iOS 17 image (2026-11-27): 27 join rows, up to 3 attachments per message, every join row names a live message. How the link can be wrong: message.ROWID and attachment.ROWID are AUTOINCREMENT, so SQLite does not hand out an id again, but an app that re-inserts rows with their old ids (a restore from backup) can still put a different record under the same id; when a message goes, its join rows go with it, and sms.db's own trigger then deletes an attachment no other join row names, so a deleted record is linked only when its join row was recovered too, and a recovered join row may be from a different time than the rows it names; an earlier version found in the WAL and a live row with the same id were stored at different times; a record found by pattern has no row id and cannot be linked; several copies of one record can carry the same id.\n"
    '\n'
    'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 4 earlier versions of 2 '
    'messages (from the WAL and overflow pages), all Changed; more versions that differ '
    "only in columns that don't matter here are counted in the run log. Found earlier "
    '(carried over from SMS Messages): sms.db has PRAGMA secure_delete = FAST (freed '
    'ranges all zero); two messages deleted 6+ months before the extraction are gone, from '
    "this table and from Apple's own recoverable-message tables. A message deleted within "
    "iOS's ~30-40 day window is still live, and shown in SMS Messages."
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
                     'group_title'],
  'links': [{'name': 'linked_attachments', 'what': 'attachment', 'column': 'ROWID',
             'via': ['message_attachment_join', 'message_id', 'attachment_id'],
             'table': 'attachment', 'key': 'ROWID', 'report': 'sms_deleted_attachments'}]}]

timestamp_fields = {'date': 'cocoa_ns',
 'date_read': 'cocoa_ns',
 'date_delivered': 'cocoa_ns',
 'date_edited': 'cocoa_ns',
 'date_retracted': 'cocoa_ns'}

core_fields = ['status',
 'date',
 'message',
 'linked_attachments',
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
