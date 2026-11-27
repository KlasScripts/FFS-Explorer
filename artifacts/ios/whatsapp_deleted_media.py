name = 'WhatsApp — Deleted & Changed Media (iOS)'
description = (
    'Deleted and changed WhatsApp media records from ChatStorage.sqlite (table '
    'ZWAMEDIAITEM; Core Data: Z_PK is the row id, ZMESSAGE the message it belongs to). '
    'Live media are in WhatsApp Messages (iOS); deleted and changed messages (ZWAMESSAGE) '
    'are in WhatsApp — Deleted & Changed Messages (iOS).\n'
    '\n'
    'Each row is one record as found in ZWAMEDIAITEM, never joined to another table '
    '(ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no longer in the '
    'table), Changed (an earlier version of a row still there, listed only when one of the '
    'columns that matter changed, with the current value beside it as "(now)") or an '
    'unfinished write/delete under a hot journal. How found says how each record was '
    'recovered and how sure that is; a record found by pattern has no row id. Values are '
    'as stored: no code is turned into a word here unless it was checked against a test '
    'image.\n'
    '\n'
    'media_url_date is Cocoa seconds, as in WhatsApp Messages (iOS).\n'
    '\n'
    'linked_message: where the message this media belongs to was found. ZMESSAGE holds the '
    'Z_PK of its ZWAMESSAGE row, so the link is made by comparing the two, in the table now '
    '(live: the message is in WhatsApp Messages (iOS)) and in the records WhatsApp — '
    'Deleted & Changed Messages (iOS) finds (recovered, with its row id). The message also '
    'names its media (ZWAMESSAGE.ZMEDIAITEM); on the test image both ways agree for all 19 '
    'pairs (2026-11-27). Only ids are compared: no value of the message is brought in. '
    "How the link can be wrong: Z_PK comes from Core Data's own counter "
    '(Z_PRIMARYKEY.Z_MAX; checked 2026-11-27 on the Josh Hickman iOS 17 image: the highest '
    'Z_PK of each table is its Z_MAX), so an id is not normally handed out again, but a '
    'store rebuilt from a backup or a migration can put a different record under an old id; '
    'an earlier version found in the WAL and a live row with the same id were stored at '
    'different times; a record found by pattern has no row id and cannot be linked to; '
    'several copies of one record can carry the same id.\n'
    '\n'
    'Also listed: live ZWAMEDIAITEM rows whose message is gone (status Orphaned): not '
    'deleted, so not recovered, and not in the main report, which lists a row only with '
    'its message. linked_message says whether that message was recovered. None on the test '
    'image checked (2026-11-21).\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman iOS 17 image: 0 records. ChatStorage.sqlite '
    'has PRAGMA secure_delete = FAST (carried over from WhatsApp Messages (iOS)).'
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
  'version_fields': ['ZMEDIALOCALPATH', 'ZTITLE', 'ZMEDIAURL', 'ZLATITUDE', 'ZLONGITUDE'],
  'links': [{'name': 'linked_message', 'what': 'message', 'column': 'ZMESSAGE',
             'table': 'ZWAMESSAGE', 'key': 'Z_PK', 'report': 'whatsapp_deleted'}]}]

timestamp_fields = {'media_url_date': 'cocoa_s'}

core_fields = ['status',
 'media_path',
 'title',
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
