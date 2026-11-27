name = 'WhatsApp — Deleted & Changed Messages (iOS)'
description = (
    'Deleted and changed WhatsApp messages from ChatStorage.sqlite (table ZWAMESSAGE; Core '
    'Data: Z_PK is the row id). Live messages are in WhatsApp Messages (iOS); deleted and '
    'changed media records (ZWAMEDIAITEM) are in WhatsApp — Deleted & Changed Media '
    '(iOS).\n'
    '\n'
    'Each row is one record as found in ZWAMESSAGE, never joined to another table '
    '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer in the '
    'table), Changed (an earlier version of a row still there, listed only when one of the '
    'columns that matter changed, with the current value beside it as "(now)") or an '
    'unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'Dates are Cocoa seconds, as in WhatsApp Messages (iOS).\n'
    '\n'
    "linked_media: where this message's media record was found. ZWAMESSAGE.ZMEDIAITEM holds "
    "the Z_PK of its ZWAMEDIAITEM row, so the link is made by comparing the two, in the table "
    'now (live) and in the records WhatsApp — Deleted & Changed Media (iOS) finds (recovered, '
    'with its row id); "no media id" for a message with none. The media row also names its '
    'message (ZWAMEDIAITEM.ZMESSAGE); on the test image both ways agree for all 19 pairs '
    '(2026-11-27). Only ids are compared: no value of the media row is brought in. '
    "How the link can be wrong: Z_PK comes from Core Data's own counter (Z_PRIMARYKEY.Z_MAX; checked 2026-11-27 on the Josh Hickman iOS 17 image: the highest Z_PK of each table is its Z_MAX), so an id is not normally handed out again, but a store rebuilt from a backup or a migration can put a different record under an old id; an earlier version found in the WAL and a live row with the same id were stored at different times; a record found by pattern has no row id and cannot be linked to; several copies of one record can carry the same id.\n"
    '\n'
    'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 0 records. Found earlier '
    '(carried over from WhatsApp Messages (iOS)): ChatStorage.sqlite has PRAGMA '
    'secure_delete = FAST; carving found nothing for a documented deleted message, and a '
    'byte search of the file for its text found no trace. The deletion itself shows as a '
    'one-row gap in ZWAMESSAGE.Z_PK (see that report).'
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
                     'ZMEDIAITEM'],
  'links': [{'name': 'linked_media', 'what': 'media', 'column': 'ZMEDIAITEM',
             'table': 'ZWAMEDIAITEM', 'key': 'Z_PK', 'report': 'whatsapp_deleted_media'}]}]

timestamp_fields = {'message_date': 'cocoa_s', 'sent_date': 'cocoa_s'}

core_fields = ['status',
 'message_date',
 'message',
 'linked_media',
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
