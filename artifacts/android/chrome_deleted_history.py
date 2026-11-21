name = 'Chrome — Deleted History Records'
description = (
    'Deleted and changed Chrome history records: History (tables visits and urls: a '
    "visit's url is the stored urls id) and, when the phone has it, "
    'segmentation_platform/ukm_db (table urls), Chrome\'s own URL log that survives "Clear '
    'browsing data". The main report recorded the ukm_db positive: 6 rows recovered via '
    'WAL history after a clear-history run on a GTLAB test image, none of it visible '
    'elsewhere; and a tested negative for History itself (a single deleted entry and a '
    'full clear left nothing; Chrome appears to rebuild the file). Live history is in '
    'Chrome Web History.\n'
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
    'Times are Chromium microseconds since 1601-01-01 UTC, as in Chrome Web History. '
    'transition is the stored number (core type plus qualifier bits); Chrome Web History '
    'decodes it for live visits.\n'
    '\n'
    'Checked 2026-11-21 on the Josh Hickman Android 14 image: 0 records (its History has '
    'no deleted records left; it has no ukm_db).'
)

# Declaration-only report (report_type "recovered_records", ASSUMPTIONS.md
# A-95, 2026-11-21: main reports hold live rows only). The shared code in
# artifact_runner.recovered_records_report recovers the records with
# sqlite_recovery and makes the rows. Never joined to other tables.
# Column names checked against the tables on the test images named in
# the description (2026-11-21); a column a version of the app lacks is
# left out and noted in the run log.

deleted_of = "chrome_web_history"   # its main report: the app location and database files come from there (A-95)

report_type = "recovered_records"
recovered_records = [{'file_key': 'history',
  'table': 'visits',
  'label': 'Visit (History)',
  'fields': {'id': 'visit_id',
             'url': 'url_id',
             'visit_time': 'visit_time',
             'from_visit': 'from_visit',
             'transition': 'transition',
             'visit_duration': 'visit_duration'},
  'version_fields': ['url', 'transition']},
 {'file_key': 'history',
  'table': 'urls',
  'label': 'URL (History)',
  'fields': {'id': 'url_id',
             'url': 'url',
             'title': 'title',
             'visit_count': 'visit_count',
             'typed_count': 'typed_count',
             'last_visit_time': 'last_visit_time',
             'hidden': 'hidden'},
  'version_fields': ['url', 'title']},
 {'file_key': 'ukm_db',
  'table': 'urls',
  'label': 'URL (Segmentation Platform)',
  'fields': {'url_id': 'url_id',
             'url': 'url',
             'title': 'title',
             'last_timestamp': 'last_timestamp',
             'counter': 'counter',
             'profile_id': 'profile_id'},
  'version_fields': ['url', 'title']}]

timestamp_fields = {'visit_time': 'webkit_us',
 'last_visit_time': 'webkit_us',
 'last_timestamp': 'webkit_us'}

core_fields = ['status',
 'record',
 'visit_time',
 'last_visit_time',
 'last_timestamp',
 'url',
 'title',
 'changed_fields',
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
