name = "Chrome Bookmarks"
app_group_label = "Chrome"
group_sort_key = 5
description = (
    "Chrome bookmarks, from the plain-JSON Default/Bookmarks file -- "
    "walks all three root folders (Bookmarks bar, Other bookmarks, "
    "Mobile bookmarks) recursively, one row per url-type node, with "
    "`folder` showing the full nested folder path it lives in. "
    "No real bookmark exists anywhere on Joshua Hickman's documented "
    "Android 14 image across all three of its installed browsers "
    "(checked directly -- 0 url-type nodes in each), so this parser's "
    "extraction logic is only structurally verified against that real "
    "(empty) JSON schema plus a synthetic example matching Chromium's "
    "documented node shape -- not yet exercised against a real "
    "populated bookmark. No record_source -- this is a plain JSON file, "
    "not a SQLite table, so there is no on-disk cell for the Hex "
    "panel's Record mode to jump to; the raw file itself is still "
    "reachable via this report's own \"Source in ZIP\"/\"Exported "
    "Files\" tree entries."
)
app_path = "data/data/com.android.chrome"
# No required files -- Bookmarks is declared optional below and run()
# returns [] if it's missing, rather than the whole parser failing to
# run. `files` must still exist (even empty) for the multi-file API to
# activate at all -- see artifact_runner.py's hasattr(module, 'files')
# check.
files = {}
optional_files = {
    "bookmarks": "app_chrome/Default/Bookmarks",
}

timestamp_fields = {
    "date_added": "webkit_us",
    "date_modified": "webkit_us",
    "date_last_used": "webkit_us",
}
# What was bookmarked, its link, and when are the essentials; folder/
# date_modified/date_last_used are useful detail, not needed for a first
# pass.
core_fields = ["name", "url", "date_added"]


def _to_webkit_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _walk_bookmarks(node, folder_path, out):
    if node.get("type") == "url":
        out.append({
            "name": node.get("name"),
            "url": node.get("url"),
            "folder": " / ".join(folder_path),
            "date_added": _to_webkit_int(node.get("date_added")),
            "date_modified": _to_webkit_int(node.get("date_modified")),
            "date_last_used": _to_webkit_int(node.get("date_last_used")),
            "raw_guid": node.get("guid"),
            "raw_id": node.get("id"),
        })
        return
    for child in node.get("children", []):
        _walk_bookmarks(child, folder_path + [node.get("name", "")], out)


def run(paths):
    import json

    if "bookmarks" not in paths:
        return []

    with open(paths["bookmarks"], "r", encoding="utf-8") as f:
        doc = json.load(f)

    out = []
    for root in doc.get("roots", {}).values():
        _walk_bookmarks(root, [], out)
    return out
