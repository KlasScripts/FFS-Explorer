"""Gboard clipboard history.

Rows come from the clips table in
data/data/com.google.android.inputmethod.latin/databases/gboard_clipboard.db.
ALEAPP's get_gboardCache reads the same table and the same columns
(timestamp, text, html_text, uri, item_type, entity_type, _id). The
database exists only when Gboard's clipboard feature has been turned on.
The four instruction clips Gboard writes when that screen is first opened
are left out. item_type and entity_type are reported as stored. Their
meanings are not established.

An image path is the archive path of the clipboard file named by the last
segment of uri, given only when that file is in the extraction. When it is
not, image_missing names the expected path.
"""

name = "Gboard Clipboard"
description = (
    "Text and images Gboard kept in its clipboard history. This is not a "
    "copy of an app's messages. A clip is here only if it was copied while "
    "Gboard's clipboard history was on. Incoming messages that were never "
    "copied are absent. The four instruction lines Gboard writes when the "
    "clipboard screen is first opened are left out. item_type and "
    "entity_type are the stored numbers; what those numbers mean is not "
    "established."
)
app_path = "data/data/com.google.android.inputmethod.latin"
files = {
    "clipboard_db": "databases/gboard_clipboard.db",
}
optional_files = {
    "clipboard_db_wal": "databases/gboard_clipboard.db-wal",
    "clipboard_db_shm": "databases/gboard_clipboard.db-shm",
}
timestamp_fields = {"timestamp": "ms"}
media_fields = ["image_path"]
core_fields = ["timestamp", "text", "uri", "image_path"]
record_source = [
    {"label": "Gboard clipboard", "ui_path_field": "raw_ui_path"},
]

# Written together, in one second, the first time the clipboard screen is
# opened. They are Gboard's own help text, not something the user copied.
_SEEDED_CLIPS = frozenset({
    "Use the edit icon to pin, add or delete clips.",
    "Touch and hold a clip to pin it. Unpinned clips will be deleted after 1 hour.",
    "Tap on a clip to paste it in the text box.",
    "Welcome to Gboard clipboard, any text you copy will be saved here.",
})


def _image_path(app_base: str, uri: str) -> str:
    if not uri:
        return ""
    tail = uri.split("?", 1)[0].rstrip("/").split("/")[-1]
    if not tail or tail in {".", ".."} or any(c in tail for c in "/\\:"):
        return ""
    return f"{app_base}/files/clipboard_image/{tail}"


def run(paths):
    from artifact_runner import archive_has, open_db_readonly

    conn = open_db_readonly(paths["clipboard_db"])
    try:
        rows = conn.execute(
            "SELECT _id, timestamp, text, html_text, uri, item_type, entity_type "
            "FROM clips ORDER BY timestamp, _id"
        ).fetchall()
    finally:
        conn.close()

    app_base = paths["_app_base_ui_path"]
    ui_path = f"{app_base}/databases/gboard_clipboard.db"
    out = []
    for row in rows:
        text = row["text"] or ""
        if text in _SEEDED_CLIPS:
            continue
        uri = row["uri"] or ""
        expected = _image_path(app_base, uri)
        present = archive_has(paths, expected)
        out.append({
            "clip_id": row["_id"],
            "timestamp": row["timestamp"],
            "text": text,
            "html_text": row["html_text"] or "",
            "uri": uri,
            "image_path": expected if present else "",
            "image_missing": "" if present or not expected else f"not in extraction: {expected}",
            "item_type": row["item_type"],
            "entity_type": row["entity_type"],
            "raw_ui_path": ui_path,
        })
    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'gboard_clipboard_deleted',
  'name': 'Gboard Clipboard — Deleted & Changed Clips',
  'description': "Deleted and changed clipboard records from Gboard's gboard_clipboard.db "
                 '(table clips): text the user copied, with when. Gboard drops clips after a '
                 'while and on request, so a deleted clip is copied text the phone no longer '
                 'shows. Live clips are in Gboard Clipboard.\n'
                 '\n'
                 'Each row is one record as found in the clips table, never joined to another '
                 'table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says Deleted (no '
                 'longer in the table), Changed (an earlier version of a row still there, '
                 'listed only when one of the columns that matter changed, with the current '
                 'value beside it as "(now)") or an unfinished write/delete under a hot '
                 'journal. How found says how each record was recovered and how sure that is; '
                 'a record found by pattern has no row id. Values are as stored: no code is '
                 'turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'Gboard writes this database with a rollback journal: records come from freed '
                 'space and freed pages, and from the -journal file when one was extracted.\n'
                 '\n'
                 'timestamp is Unix milliseconds, as in Gboard Clipboard. _id is a time too '
                 '(milliseconds, set by Gboard), so it is not reused the way a counter can be. '
                 'item_type and entity_type are shown as stored; sensitive marks a clip '
                 'Android flagged as sensitive (a password).\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman Android 14 and CTF25 Android 15 images: no deleted or changed clip (4 live on Josh Hickman and '
                 'CTF25).',
  'timestamp_fields': {'timestamp': 'ms'},
  'core_fields': ['status',
                  'timestamp',
                  'text',
                  'sensitive',
                  'changed_fields',
                  'how_found',
                  'copies'],
  'file_key': 'clipboard_db',
  'table': 'clips',
  'label': 'Clip',
  'fields': {'_id': 'clip_id',
             'text': 'text',
             'html_text': 'html_text',
             'item_type': 'item_type',
             'entity_type': 'entity_type',
             'timestamp': 'timestamp',
             'sensitive': 'sensitive',
             'uri': 'uri',
             'group_id': 'group_id'},
  'version_fields': ['text', 'html_text', 'uri', 'item_type', 'sensitive'],
  'links': []}]
