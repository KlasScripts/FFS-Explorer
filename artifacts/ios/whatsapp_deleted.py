name = 'WhatsApp — Deleted & Changed Records (iOS)'
description = (
    'Deleted and changed WhatsApp messages and media records from ChatStorage.sqlite '
    '(tables ZWAMESSAGE and ZWAMEDIAITEM; Core Data: Z_PK is the row id, '
    'ZWAMEDIAITEM.ZMESSAGE the message it belongs to). Live messages are in WhatsApp '
    'Messages (iOS).\n'
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
    'Dates are Cocoa seconds, as in WhatsApp Messages (iOS).\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 0 records. Found earlier '
    '(carried over from WhatsApp Messages (iOS)): ChatStorage.sqlite has PRAGMA '
    'secure_delete = FAST; carving found nothing for a documented deleted message, and a '
    'byte search of the file for its text found no trace. The deletion itself shows as a '
    'one-row gap in ZWAMESSAGE.Z_PK (see that report).'
    '\n\nAlso listed: live ZWAMEDIAITEM rows whose message is gone (status Orphaned): '
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
recovered_records = [{'file_key': 'chatstorage',
  'table': 'ZWAMESSAGE',
  'label': 'Message',
  'fields': {'Z_PK': 'message_id',
             'ZCHATSESSION': 'chat_session_id',
             'ZGROUPMEMBER': 'group_member_id',
             'ZISFROMME': 'is_from_me',
             'ZMESSAGEDATE': 'message_date',
             'ZSENTDATE': 'sent_date',
             'ZTEXT': 'message',
             'ZMESSAGETYPE': 'message_type',
             'ZFROMJID': 'from_jid',
             'ZTOJID': 'to_jid',
             'ZPUSHNAME': 'push_name',
             'ZMEDIAITEM': 'media_item_id',
             'ZSTANZAID': 'stanza_id',
             'ZMESSAGESTATUS': 'message_status',
             'ZSTARRED': 'starred'},
  'version_fields': ['ZTEXT',
                     'ZMESSAGETYPE',
                     'ZCHATSESSION',
                     'ZFROMJID',
                     'ZTOJID',
                     'ZISFROMME',
                     'ZMEDIAITEM']},
 {'file_key': 'chatstorage',
  'table': 'ZWAMEDIAITEM',
  'label': 'Media',
  'fields': {'Z_PK': 'media_item_id',
             'ZMESSAGE': 'message_id',
             'ZMEDIALOCALPATH': 'media_path',
             'ZTHUMBNAILLOCALPATH': 'thumbnail_path',
             'ZMEDIAURL': 'media_url',
             'ZTITLE': 'title',
             'ZFILESIZE': 'file_size',
             'ZMOVIEDURATION': 'duration',
             'ZLATITUDE': 'latitude',
             'ZLONGITUDE': 'longitude',
             'ZAUTHORNAME': 'author_name',
             'ZVCARDNAME': 'vcard_name',
             'ZMEDIAURLDATE': 'media_url_date'},
  'orphan_of': {'column': 'ZMESSAGE', 'in': ['ZWAMESSAGE', 'Z_PK']},
  'version_fields': ['ZMEDIALOCALPATH', 'ZTITLE', 'ZMEDIAURL', 'ZLATITUDE', 'ZLONGITUDE']}]

timestamp_fields = {'message_date': 'cocoa_s', 'sent_date': 'cocoa_s', 'media_url_date': 'cocoa_s'}

core_fields = ['status',
 'record',
 'message_date',
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
