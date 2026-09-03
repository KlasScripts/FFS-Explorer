name = "Chrome Login Data"
app_group_label = "Chrome"
group_sort_key = 10
description = (
    "Chrome's saved-password manager (app_chrome/Default/Login Data, "
    "the `logins` table) -- genuinely different from Chrome Autofill: "
    "this table DOES carry a real site column (origin_url/signon_realm), "
    "no inference needed. Every row is one of three real, distinct "
    "meanings, decoded here into login_type rather than left for the "
    "examiner to work out from raw flags -- confirmed against this "
    "project's own real Android 14 JoshHickman data, where all three "
    "actually occur: "
    "'Blacklisted (never save)' -- blacklisted_by_user=1, meaning "
    "Chrome's own save-password prompt appeared on this site and the "
    "user explicitly declined; NOT a saved credential at all, but still "
    "real evidence the user encountered a login form there. "
    "'Federated login' -- federation_url is set (e.g. \"Sign in with "
    "Google\"): the account IS real and username_value is often "
    "populated, but there is no local password to store, by design -- "
    "an empty password_value here is expected, not a decryption gap. "
    "'Saved password' -- password_value actually has bytes. "
    "password_value itself is NEVER decrypted or shown here: on every "
    "Chrome platform it's OS-encrypted (Android Keystore-backed), and "
    "that key material isn't part of a filesystem-level FFS extraction "
    "-- has_password just reports whether a real encrypted blob is "
    "present, honestly stating what could and couldn't be recovered "
    "rather than guessing or fabricating a plaintext value. "
    "origin_url for an app-affiliated credential (not a website at all) "
    "reads \"android://<hash>@<package.name>/\" -- Chrome's own Digital "
    "Asset Links affiliation encoding, confirmed on real data (this "
    "case's own com.facebook.orca and com.pinterest rows) -- shown "
    "as-is rather than decoded further, since the hash itself carries "
    "no recoverable meaning. scheme_label is decoded from Chromium's own "
    "password_manager::PasswordForm::Scheme enum (kHtml/kBasic/kDigest/"
    "kOther/kUsernameOnly)."
)
app_path = "data/data/com.android.chrome"
files = {
    "login_data": "app_chrome/Default/Login Data",
}
optional_files = {
    "login_data_journal": "app_chrome/Default/Login Data-journal",
}

timestamp_fields = {
    "date_created": "webkit_us",
    "date_last_used": "webkit_us",
    "date_password_modified": "webkit_us",
}
core_fields = ["login_type", "origin_url", "username_value", "date_created"]

record_source = {
    "label": "Login Entry",
    "file_key": "login_data",
    "table": "logins",
    "rowid_fields": ["raw_rowid", "id"],
}

_SCHEME_LABELS = {
    0: "kHtml", 1: "kBasic", 2: "kDigest", 3: "kOther", 4: "kUsernameOnly",
}


def _login_type(blacklisted, federation_url, has_password) -> str:
    if blacklisted:
        return "Blacklisted (never save)"
    if federation_url:
        return "Federated login"
    if has_password:
        return "Saved password"
    return "Saved login (no password stored)"


def run(paths):
    import chrome_shared

    rows = chrome_shared.query_rows(paths["login_data"], """
        SELECT rowid AS rid, id, origin_url, signon_realm, username_value,
               federation_url, blacklisted_by_user, scheme, times_used,
               date_created, date_last_used, date_password_modified,
               length(password_value) AS pwlen
        FROM logins
    """)
    out = []
    for r in rows:
        has_password = bool(r["pwlen"])
        out.append({
            "login_type": _login_type(r["blacklisted_by_user"], r["federation_url"], has_password),
            "origin_url": r["origin_url"],
            "signon_realm": r["signon_realm"],
            "username_value": r["username_value"],
            "federation_url": r["federation_url"],
            "has_password": has_password,
            "scheme_label": _SCHEME_LABELS.get(r["scheme"], f"[unknown scheme: {r['scheme']}]"),
            "times_used": r["times_used"],
            "date_created": r["date_created"],
            "date_last_used": r["date_last_used"],
            "date_password_modified": r["date_password_modified"],
            "raw_rowid": r["rid"],
            "id": r["id"],
        })
    return out
