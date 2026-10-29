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
segment of uri. The path is reported even when that file was not pulled.
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
    from artifact_runner import open_db_readonly

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
        out.append({
            "clip_id": row["_id"],
            "timestamp": row["timestamp"],
            "text": text,
            "html_text": row["html_text"] or "",
            "uri": uri,
            "image_path": _image_path(app_base, uri),
            "item_type": row["item_type"],
            "entity_type": row["entity_type"],
            "raw_ui_path": ui_path,
        })
    return out
