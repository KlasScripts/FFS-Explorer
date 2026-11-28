name = "Chrome Cookies"
app_group_label = "Chrome"
group_sort_key = 11
description = (
    "Every cookie Chrome currently holds (app_chrome/Default/Cookies, "
    "the `cookies` table) -- host_key/name/value/path/creation_utc/"
    "last_access_utc/expires_utc, one row per cookie. The single "
    "richest source of domain-level browsing evidence in this whole "
    "Chrome group that's genuinely INDEPENDENT of History: a page "
    "embedding third-party trackers, an OAuth/SSO redirect chain, or a "
    "site visited and never logged to History for any reason can still "
    "leave a cookie here. "
    "Real, verified property of THIS Android build's own data, checked "
    "directly rather than assumed from desktop Chrome's usual behavior: "
    "`value` is populated in PLAIN TEXT on every real cookie checked "
    "here, `encrypted_value` empty -- unlike Login Data's password_value "
    "(OS-encrypted, never decrypted by this parser), cookie values are "
    "NOT OS-encrypted on this platform/version, so `value` is reported "
    "as-is. If a future case's `encrypted_value` is ever populated "
    "instead (a real, possible platform/version difference), that "
    "column is reported honestly as an encrypted-blob byte length, the "
    "same 'never fabricate a decryption' rule this project's Login Data "
    "parser already follows. samesite_label/source_scheme_label decode "
    "Chromium's own net::CookieSameSite / net::CookieSourceScheme "
    "enums."
)
warning = (
    "A cookie's presence proves the browser exchanged it with that "
    "domain at some point -- for a THIRD-PARTY cookie (set by a domain "
    "embedded in a page the user visited, not the page's own domain) "
    "that does NOT mean the user directly navigated to host_key itself; "
    "cross-reference top_frame_site_key (the top-level site active when "
    "the cookie was set, when Chrome recorded one) before treating "
    "host_key alone as a site the user visited directly."
)
app_path = "data/data/com.android.chrome"
files = {
    "cookies_db": "app_chrome/Default/Cookies",
}
optional_files = {
    "cookies_db_journal": "app_chrome/Default/Cookies-journal",
}

timestamp_fields = {
    "creation_utc": "webkit_us", "last_access_utc": "webkit_us",
    "expires_utc": "webkit_us", "last_update_utc": "webkit_us",
}
core_fields = ["host_key", "name", "value", "creation_utc", "last_access_utc"]

record_source = {
    "label": "Cookie",
    "file_key": "cookies_db",
    "table": "cookies",
    "rowid_fields": ["raw_rowid"],
}

_SAMESITE_LABELS = {-1: "UNSPECIFIED", 0: "NO_RESTRICTION", 1: "LAX", 2: "STRICT"}
_SOURCE_SCHEME_LABELS = {0: "UNSET", 1: "NONSECURE", 2: "SECURE"}


def run(paths):
    import chrome_shared

    rows = chrome_shared.query_rows(paths["cookies_db"], """
        SELECT rowid AS rid, host_key, top_frame_site_key, name, value,
               length(encrypted_value) AS enc_len, path, is_secure,
               is_httponly, is_persistent, samesite, source_scheme,
               source_port, creation_utc, last_access_utc, expires_utc,
               has_expires, last_update_utc
        FROM cookies
    """)
    out = []
    for r in rows:
        out.append({
            "host_key": r["host_key"],
            "top_frame_site_key": r["top_frame_site_key"] or "",
            "name": r["name"],
            "value": r["value"] or "",
            "encrypted_value_bytes": r["enc_len"] or 0,
            "path": r["path"],
            "is_secure": bool(r["is_secure"]),
            "is_httponly": bool(r["is_httponly"]),
            "is_persistent": bool(r["is_persistent"]),
            "has_expires": bool(r["has_expires"]),
            "samesite_label": _SAMESITE_LABELS.get(r["samesite"], f"[unknown: {r['samesite']}]"),
            "source_scheme_label": _SOURCE_SCHEME_LABELS.get(r["source_scheme"], f"[unknown: {r['source_scheme']}]"),
            "source_port": r["source_port"],
            "creation_utc": r["creation_utc"],
            "last_access_utc": r["last_access_utc"],
            "expires_utc": r["expires_utc"],
            "last_update_utc": r["last_update_utc"],
            "raw_rowid": r["rid"],
        })
    return out


# Deleted and changed records, one report per table (ASSUMPTIONS.md A-95:
# declared here, 2026-11-27). Each entry is its own report, named by
# "script", made by artifact_runner._deleted_table_module and run
# whenever this report runs, on the files it extracted. The keys are
# recovered_records keys (artifact_runner, above recovered_records_report)
# plus the report's own: name, description, core_fields, timestamp_fields.
# Never joined to other tables; links compare ids only.
deleted_tables = [{'script': 'chrome_cookies_deleted',
  'name': 'Chrome Cookies — Deleted & Changed Cookies',
  'description': "Deleted and changed cookie records from Chrome's Cookies database (table "
                 'cookies): the site (host_key), the cookie name and value, when it was set, '
                 'last used and due to expire. A deleted cookie is a site visited and signed '
                 'in to, with times, after the cookies were cleared or expired; a changed one '
                 'shows an earlier value or expiry. Live cookies are in Chrome Cookies.\n'
                 '\n'
                 'Each row is one record as found in the cookies table, never joined to '
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
                 'Many records of this table are found in freed pages or earlier WAL frames, '
                 'where the page no longer says which table it belonged to. Such a record is '
                 "listed only when it fits this table's own record pattern and no other "
                 "table's (table_certain says which records these are); the run log counts the "
                 'ones left out.\n'
                 '\n'
                 'creation_utc, last_access_utc, expires_utc and last_update_utc are WebKit '
                 'microseconds, as in Chrome Cookies. value is as stored; encrypted_value is a '
                 'BLOB, shown as stored (not decrypted).\n'
                 '\n'
                 "This table has no id column of its own: SQLite's row id is used, and SQLite "
                 "gives a new row the highest row id + 1, so a deleted row's row id can be "
                 'handed to a new one. A record is therefore tied to a live row by the columns '
                 'Chrome keeps unique (host_key, top_frame_site_key, name, path, source_scheme '
                 "and source_port: Chrome's own unique key for a cookie), not by its row id: a "
                 'record with the same key as a live row is an earlier version of it ("tied to '
                 'live row" in how_found). Chrome often replaces a cookie by deleting and '
                 'inserting it, so many records are earlier versions of a live cookie: listed '
                 'when its value or expiry differs, otherwise counted in the run log.\n'
                 '\n'
                 'Checked 2026-11-28 on the Josh Hickman and CTF26 Android 14 and CTF25 '
                 'Android 15 images: 1 record on Josh Hickman, 20 on CTF25 (12 from freed '
                 "space in the table's own pages, 8 found by pattern whose table is not "
                 'certain), before the key '
                 'check. After it: 0 listed on Josh Hickman (its record is an earlier version of a live '
                 'cookie); 15 deleted and 3 changed on CTF25, 14 of the deleted ones partly overwritten '
                 '(zero bytes in their text, flagged in damaged) or cut off, all with their site and '
                 'creation time.',
  'timestamp_fields': {'creation_utc': 'webkit_us',
                       'last_access_utc': 'webkit_us',
                       'expires_utc': 'webkit_us',
                       'last_update_utc': 'webkit_us'},
  'core_fields': ['status',
                  'creation_utc',
                  'host_key',
                  'name',
                  'value',
                  'last_access_utc',
                  'changed_fields',
                  'value (now)',
                  'how_found',
                  'copies'],
  'file_key': 'cookies_db',
  'table': 'cookies',
  'label': 'Cookie',
  'fields': {'creation_utc': 'creation_utc',
             'host_key': 'host_key',
             'top_frame_site_key': 'top_frame_site_key',
             'name': 'name',
             'value': 'value',
             'encrypted_value': 'encrypted_value',
             'path': 'path',
             'expires_utc': 'expires_utc',
             'last_access_utc': 'last_access_utc',
             'last_update_utc': 'last_update_utc',
             'is_secure': 'is_secure',
             'is_httponly': 'is_httponly',
             'is_persistent': 'is_persistent',
             'samesite': 'samesite',
             'source_scheme': 'source_scheme',
             'source_port': 'source_port'},
  'identity': ['host_key',
               'top_frame_site_key',
               'name',
               'path',
               'source_scheme',
               'source_port'],
  'version_fields': ['host_key',
                     'name',
                     'value',
                     'encrypted_value',
                     'path',
                     'expires_utc',
                     'is_persistent'],
  'links': []}]
