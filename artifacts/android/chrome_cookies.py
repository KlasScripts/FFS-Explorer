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
