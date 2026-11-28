name = "Chrome Downloads"
app_group_label = "Chrome"
group_sort_key = 3
description = (
    "Chrome's own download manager records, from the downloads table in "
    "the same History database as the Web History Summary/Full reports. "
    "Tab URL is downloads.tab_url, the page the download was started "
    "from -- not the download's own source URL, which Chrome stores "
    "separately in downloads_url_chains (not parsed by this report; same "
    "distinction ALEAPP's own chrome.py notes). state/danger_type/"
    "interrupt_reason enum labels are ported from Chromium's own source "
    "(download_danger_type.h, download_interrupt_reason_values.h, via "
    "ALEAPP's chrome.py) -- source-verified, not data-proven: no download "
    "was ever produced across either ground truth available while "
    "building this parser (Joshua Hickman's documented Android 14 image "
    "has 0 rows in this table, and neither GTLAB coach run performed a "
    "download), so this report's actual row-producing path, and every "
    "one of the enum labels below, is code-present but genuinely "
    "UNEXERCISED against real data. Treat a populated report from this "
    "parser with the same care as any newly-written, unvalidated parser "
    "until it has actually been checked against a real download. "
    "last_access_time/tab_url are read defensively (checked for column "
    "existence first) since ALEAPP's own chrome.py notes older Chrome "
    "database versions lack one or both -- this has not been exercised "
    "against an actual old-version database either, only confirmed the "
    "columns are both present on the one real schema available "
    "(Chrome 121)."
)
app_path = "data/data/com.android.chrome"
files = {
    "history": "app_chrome/Default/History",
}
optional_files = {
    "history_journal": "app_chrome/Default/History-journal",
}

timestamp_fields = {"start_time": "webkit_us", "end_time": "webkit_us", "last_access_time": "webkit_us"}
# When, what file, and whether it completed are the essentials of a
# download record; tab_url/danger_type/interrupt_reason/byte counts are
# useful detail, not needed for a first pass.
core_fields = ["start_time", "target_path", "state"]
# Linked Report: the downloaded file itself, target_path mapped from the
# device path (/storage/emulated/0/...) to the archive's data/media/0/...,
# when it is still in the archive. NOT verified on real data: the four test
# Android images have no rows in downloads (2026-10-23).
link_fields = ["download_link"]
hidden_fields = ["download_link"]

# downloads.id is confirmed "INTEGER PRIMARY KEY" in the real schema (a
# genuine rowid alias).
record_source = {
    "label": "Download",
    "file_key": "history",
    "table": "downloads",
    "rowid_fields": ["raw_download_id"],
}

_STATE = {0: "In Progress", 1: "Complete", 2: "Canceled", 3: "Interrupted", 4: "Interrupted"}

_DANGER_TYPE = {
    0: "",
    1: "Dangerous",
    2: "Dangerous URL",
    3: "Dangerous Content",
    4: "Content May Be Malicious",
    5: "Uncommon Content",
    6: "Dangerous But User Validated",
    7: "Dangerous Host",
    8: "Potentially Unwanted",
    9: "Allowlisted by Policy",
    10: "Pending Scan",
    11: "Blocked - Password Protected",
    12: "Blocked - Too Large",
    13: "Warning - Sensitive Content",
    14: "Blocked - Sensitive Content",
    15: "Safe - Deep Scanned",
    16: "Dangerous, But User Opened",
    17: "Prompt For Scanning",
    18: "Blocked - Unsupported Type",
    19: "Dangerous - Account Compromise",
    20: "Deep Scan Failed",
    21: "Encrypted - Prompt User for Password for Local Scanning",
    22: "Encrypted - Pending Detailed Verdict after Local Scanning",
    23: "Blocked - Scan Failed",
}

_INTERRUPT_REASON = {
    0: "",
    1: "File Error",
    2: "Access Denied",
    3: "Disk Full",
    5: "Path Too Long",
    6: "File Too Large",
    7: "Virus",
    10: "Temporary Problem",
    11: "Blocked",
    12: "Security Check Failed",
    13: "Resume Error",
    14: "File Hash Mismatch",
    15: "File Same as Source",
    20: "Network Error",
    21: "Operation Timed Out",
    22: "Connection Lost",
    23: "Server Down",
    30: "Server Error",
    31: "Range Request Error",
    32: "Server Precondition Error",
    33: "Unable To Get File",
    34: "Server Unauthorized",
    35: "Server Certificate Problem",
    36: "Server Access Forbidden",
    37: "Server Unreachable",
    38: "Content Length Mismatch",
    39: "Cross Origin Redirect",
    40: "Canceled",
    41: "Browser Shutdown",
    50: "Browser Crashed",
}


def _column_exists(conn, table, column):
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def run(paths):
    from artifact_runner import open_db_readonly

    conn = open_db_readonly(paths["history"])

    has_last_access = _column_exists(conn, "downloads", "last_access_time")
    has_tab_url = _column_exists(conn, "downloads", "tab_url")

    select_extra = ""
    if has_last_access:
        select_extra += ", last_access_time"
    if has_tab_url:
        select_extra += ", tab_url"

    rows = conn.execute(f"""
        SELECT id, start_time, end_time, target_path, state, danger_type,
               interrupt_reason, opened, received_bytes, total_bytes
               {select_extra}
        FROM downloads
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        keys = r.keys()
        out.append({
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "last_access_time": r["last_access_time"] if "last_access_time" in keys else None,
            "tab_url": r["tab_url"] if "tab_url" in keys else "",
            "target_path": r["target_path"],
            "state": _STATE.get(r["state"], f"[unknown state: {r['state']}]"),
            "danger_type": _DANGER_TYPE.get(r["danger_type"], f"[unknown danger_type: {r['danger_type']}]"),
            "interrupt_reason": _INTERRUPT_REASON.get(
                r["interrupt_reason"], f"[unknown interrupt_reason: {r['interrupt_reason']}]"
            ),
            "opened": bool(r["opened"]),
            "received_bytes": r["received_bytes"],
            "total_bytes": r["total_bytes"],
            "raw_download_id": r["id"],
            "download_link": _download_link(paths, r["target_path"]),
        })
    return out


def _download_link(paths, target_path):
    from artifact_runner import archive_has, device_path_to_ui_path
    ui = device_path_to_ui_path(target_path)
    return ui if archive_has(paths, ui) else ""


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'chrome_downloads_deleted',
  'name': 'Chrome Downloads — Deleted & Changed Downloads',
  'description': "Deleted and changed download records from Chrome's History (table "
                 'downloads): the file saved (target_path), the page it came from (tab_url, '
                 'referrer), when, how much and whether it finished. A deleted record is a '
                 "download removed from Chrome's list. Live downloads are in Chrome Downloads; "
                 'the URL each download was fetched from is in downloads_url_chains.\n'
                 '\n'
                 'Each row is one record as found in the downloads table, never joined to '
                 'another table (ASSUMPTIONS.md A-95): ids are the stored ids. Status says '
                 'Deleted (no longer in the table), Changed (an earlier version of a row still '
                 'there, listed only when one of the columns that matter changed, with the '
                 'current value beside it as "(now)") or an unfinished write/delete under a '
                 'hot journal. How found says how each record was recovered and how sure that '
                 'is; a record found by pattern has no row id. Values are as stored: no code '
                 'is turned into a word here unless it was checked against a test image.\n'
                 '\n'
                 'Chrome writes this database with a rollback journal, not a WAL: records come '
                 'from freed space and freed pages, and from the -journal file when one was '
                 'extracted.\n'
                 '\n'
                 'start_time, end_time and last_access_time are WebKit microseconds, as in '
                 'Chrome Downloads. state, danger_type and interrupt_reason are shown as '
                 'stored (Chrome Downloads explains the codes).\n'
                 '\n'
                 'id is a plain INTEGER PRIMARY KEY: SQLite gives a new row the highest id + '
                 "1, so a deleted download's id can be handed to a new one. guid is unique per "
                 'download, so a record with the guid of a live download is an earlier version '
                 'of it ("tied to live row" in how_found).\n'
                 '\n'
                 'linked_url_chain: where the downloads_url_chains rows of this download (id) '
                 'were found, live or recovered (that table has no deleted report of its own; '
                 'only ids are compared; the id can have been reused, as above).\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman and CTF26 Android 14 and CTF25 '
                 'Android 15 images: no download records, live or deleted, on any of the '
                 'three. Value in principle; nothing to check it against yet.',
  'timestamp_fields': {'start_time': 'webkit_us',
                       'end_time': 'webkit_us',
                       'last_access_time': 'webkit_us'},
  'core_fields': ['status',
                  'start_time',
                  'target_path',
                  'tab_url',
                  'state',
                  'linked_url_chain',
                  'changed_fields',
                  'how_found',
                  'copies'],
  'file_key': 'history',
  'table': 'downloads',
  'label': 'Download',
  'fields': {'id': 'download_id',
             'guid': 'guid',
             'target_path': 'target_path',
             'current_path': 'current_path',
             'start_time': 'start_time',
             'end_time': 'end_time',
             'received_bytes': 'received_bytes',
             'total_bytes': 'total_bytes',
             'state': 'state',
             'danger_type': 'danger_type',
             'interrupt_reason': 'interrupt_reason',
             'opened': 'opened',
             'last_access_time': 'last_access_time',
             'referrer': 'referrer',
             'site_url': 'site_url',
             'tab_url': 'tab_url',
             'tab_referrer_url': 'tab_referrer_url',
             'mime_type': 'mime_type',
             'original_mime_type': 'original_mime_type'},
  'identity': ['guid'],
  'stable_id': ['guid'],
  'version_fields': ['guid',
                     'target_path',
                     'current_path',
                     'state',
                     'tab_url',
                     'referrer',
                     'site_url',
                     'total_bytes',
                     'opened'],
  'links': [{'name': 'linked_url_chain',
             'what': 'url chain',
             'column': 'id',
             'table': 'downloads_url_chains',
             'key': 'id'}]}]
