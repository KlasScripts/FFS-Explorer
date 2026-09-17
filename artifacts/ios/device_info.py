"""Device Info (iOS) — a one-page summary of the DEVICE itself, not app
content: make/model/OS, FFS acquisition type, timezone, cellular (IMEI/
ICCID/phone/carrier), the Apple ID signed into the device, and any
Contacts entry that email matches. Renders as a document
(view_mode="document"), not a table — see CLAUDE.md Conventions' "view_mode
= document" entry. See artifacts/android/device_info.py for the Android
counterpart — different real sources, so a separate file per app_report.py's
own precedent.

Deliberately kept short and flat: each field below is a couple of lines
reading straight from its own real source file, with the source named
right there — so adding the next field means finding the right two lines
to copy, not learning a framework. All real sources below, not guessed —
confirmed directly against this project's own real IOS17 JoshHickman
archive (2026-09-16), cross-checked against that case's own documented
ground truth (test_data/josh_hickman_ios17/device_metadata.json) where it
has one:

  - wireless/Library/Preferences/com.apple.commcenter.plist — IMEI, ICCID,
    phone number, serving MCC/MNC. Source confirmed via iLEAPP's own
    imeiImsi.py/celWireless.py. Verified real on this archive: phone
    number 19195794674 matches ground truth's documented 919-579-4674
    exactly; IMEI/ICCID are real values with no ground truth to check
    them against (Josh didn't record those specifically).
  - mobile/Library/Accounts/Accounts3.sqlite, ZACCOUNT table — the Apple
    ID. ZACCOUNTTYPE=26 is specifically
    com.apple.account.AppleIDAuthentication (confirmed via this same
    file's own ZACCOUNTTYPE table) — the one row to trust when several
    ZACCOUNTTYPE rows share the same ZUSERNAME (this device's real
    account genuinely appears under 10 different account types on this
    archive). Verified: ZUSERNAME is "thisisdfir@gmail.com", matching
    ground truth's documented apple_account.email exactly.
  - mobile/Library/AddressBook/AddressBook.sqlitedb — cross-referencing
    the Apple ID's own email against ABMultiValue (property=4 is email)
    finds a matching ABPerson row, if the device owner also saved
    themselves as a contact. Verified real on this archive: the Apple ID
    email matched a real contact, "This Is DFIR" — no ground truth entry
    for this (not part of Josh's documented action log), but confirmed
    directly in the real database.
"""

# The archive LAYOUT this project's own detector recognized — deliberately
# never stated as "acquired with <tool>". FORMAT_GRAYKEY/FORMAT_CELLEBRITE
# name the shape of the archive, not the acquiring tool, and this project
# has a real counter-example in its own test data: the "Android 14 CTF26
# Magnet" archive is a Magnet acquisition that detects as FORMAT_GRAYKEY.
# See CLAUDE.md Conventions' FORMAT_GRAYKEY/FORMAT_CELLEBRITE ambiguity
# entry. Reporting the detected layout as a tool identification would be
# an evidentiary claim this project cannot actually stand behind.
_LAYOUT_LABELS = {
    "graykey":    "GrayKey-style archive layout",
    "cellebrite": "Cellebrite-style archive layout",
    "zip_extras": "Generic Android FFS zip layout",
}

view_mode = "document"
device_wide = True
name = "Device Info"
description = (
    "A one-page summary of the device that produced this extraction -- "
    "make/model/iOS version, FFS acquisition type (GrayKey/Cellebrite), "
    "detected timezone, cellular identifiers (IMEI/ICCID/phone number/"
    "carrier), the Apple ID signed into the device, and any Contacts "
    "entry that account's own email matches. Every field states its own "
    "real source file inline in the report. IMEI/ICCID/carrier come from "
    "wireless/Library/Preferences/com.apple.commcenter.plist -- absent on "
    "a device with no cellular service ever configured, not an error. "
    "The 'linked contact' match is a plain email-string match against "
    "Contacts, nothing more -- a coincidental match (two people sharing "
    "an email, which shouldn't happen for a real address but is worth "
    "knowing as a limitation) would show here too."
    "'FFS Type' names the archive LAYOUT this tool's own detector recognized -- NOT a verified identification of the acquisition tool. The two do not always agree (a Magnet acquisition in this project's own test data detects as a GrayKey-style layout), so do not cite this field as evidence of which tool produced the extraction."
)


def _read_ui(read, adapter, ui_path):
    """Archive bytes for one UI path, or None. adapter.resolve() raises
    for a path shape this particular archive doesn't have, so it's
    guarded here rather than at each call site — the same best-effort
    guard artifact_runner._read_ui_path_bytes already uses."""
    try:
        return read(adapter.resolve(ui_path))
    except Exception:
        return None


def _plist_at(read, adapter, ui_path):
    raw = _read_ui(read, adapter, ui_path)
    if not raw:
        return {}
    import plistlib
    try:
        return plistlib.loads(raw)
    except Exception:
        return {}


def _sqlite_at(paths, ui_path):
    """Materialize one database out of the archive and open it READ-ONLY
    through artifact_runner.open_db_readonly (see CLAUDE.md's own
    WAL-checkpoint Conventions entry for why never a bare
    sqlite3.connect).

    The -wal/-shm sidecars are copied alongside the main database,
    always, so the connection sees the database's REAL current state
    rather than its last-checkpointed one. That is not a theoretical
    concern on real evidence: on this project's own IOS17 JoshHickman
    archive, AddressBook.sqlitedb ships a 1.2 MB -wal against a 1.4 MB
    main database, and Accounts3.sqlite a 918 KB -wal against a 256 KB
    one. Measured directly (2026-09-17): replaying AddressBook's WAL
    surfaces 7 ABMultiValue email rows where the main database alone
    shows 6. The Apple-ID match below happens to resolve identically
    either way on THIS archive, but a device whose owner-contact row —
    or whose matching email address — exists only in the WAL would
    silently report "no linked contact" without this.

    Written into this parser's own artifact_parser_files/ folder rather
    than a tempfile, so the exact bytes the report was built from stay
    on disk for inspection exactly like every files/optional_files
    parser's sources do (artifact_runner._parser_files_dir/_save_entry),
    and so no temp file is leaked per run. A sidecar the archive doesn't
    have is skipped — a fully checkpointed database has none — and any
    sidecar left behind by an earlier run is DELETED rather than left in
    place, since a stale -wal against freshly rewritten main-database
    bytes is worse than no -wal at all.
    """
    import os
    from artifact_runner import open_db_readonly
    read, adapter = paths["_read_zip_bytes"], paths["_adapter"]

    raw = _read_ui(read, adapter, ui_path)
    if not raw:
        return None
    db_path = os.path.join(paths["_parser_files_dir"], os.path.basename(ui_path))
    with open(db_path, "wb") as f:
        f.write(raw)
    for suffix in ("-wal", "-shm"):
        side = _read_ui(read, adapter, ui_path + suffix)
        if side:
            with open(db_path + suffix, "wb") as f:
                f.write(side)
        elif os.path.exists(db_path + suffix):
            os.remove(db_path + suffix)
    return open_db_readonly(db_path)


def run(paths):
    ctx = paths["_case_context"]
    adapter, read = paths["_adapter"], paths["_read_zip_bytes"]
    lines = ["# Device Info", ""]

    # Make/Model/iOS Version — already parsed at case-open time, from the
    # device's own UFD/MobileGestalt/SystemVersion plist (see
    # ffs-explorer.py's _read_device_info) — no re-parsing needed here.
    import os
    from artifact_runner import open_db_readonly
    db = open_db_readonly(os.path.join(ctx.case_dir, "caseresults.db"))
    device_fields = dict((k, v) for k, v, _src in
                         db.execute("SELECT field_name, data, source FROM device_info"))
    lines.append("## Device")
    for label in ("Make", "Model", "iOS Version", "Hardware ID"):
        if device_fields.get(label):
            lines.append(f"- **{label}:** {device_fields[label]}")

    # FFS archive layout — see _LAYOUT_LABELS above for why this is
    # never phrased as an acquisition-tool identification. This script
    # only ever runs for an iOS archive (see is_android() in CLAUDE.md
    # Conventions for why that check matters for a device_wide/
    # cross-platform caller, not needed here since this file IS the
    # iOS-specific branch).
    lines.append("- **FFS Type:** "
                 f"{_LAYOUT_LABELS.get(adapter.format, adapter.format)} (iOS)")

    # Detected handset timezone — case_settings, set best-effort at first
    # case-load (see timestamp_display.py / device_timezone.py Conventions).
    tz_row = db.execute(
        "SELECT value FROM case_settings WHERE key='handset_timezone_name'").fetchone()
    if tz_row and tz_row[0]:
        lines.append(f"- **Detected Timezone:** {tz_row[0]}")
    db.close()
    lines.append("")

    # wireless/Library/Preferences/com.apple.commcenter.plist
    cc = _plist_at(read, adapter, "wireless/Library/Preferences/com.apple.commcenter.plist")
    iccid   = cc.get("LastKnownICCID")
    phone   = cc.get("PhoneNumber")
    carrier = cc.get("com.apple.carrier_1") or cc.get("CarrierBundleName")
    mcc, mnc = cc.get("LastKnownServingMcc"), cc.get("LastKnownServingMnc")
    imei = ""
    for sim in (cc.get("PersonalWallet") or {}).values():
        imei = (sim.get("CarrierEntitlements") or {}).get(
            "kEntitlementsSelfRegistrationUpdateImei", "")
        if imei:
            break

    lines.append("## Cellular")
    if imei:    lines.append(f"- **IMEI:** {imei}")
    if iccid:   lines.append(f"- **ICCID:** {iccid}")
    if phone:   lines.append(f"- **Phone Number:** {phone}")
    if carrier: lines.append(f"- **Carrier Bundle:** {carrier}")
    if mcc and mnc:
        lines.append(f"- **Serving Network (MCC/MNC):** {mcc}/{mnc}")
    if not (imei or iccid or phone):
        lines.append("- *No cellular service configured on this device.*")
    lines.append("")

    # mobile/Library/Accounts/Accounts3.sqlite -- ZACCOUNTTYPE=26 is
    # specifically com.apple.account.AppleIDAuthentication.
    acct_email = ""
    acct_db = _sqlite_at(paths, "mobile/Library/Accounts/Accounts3.sqlite")
    if acct_db:
        row = acct_db.execute(
            "SELECT ZUSERNAME FROM ZACCOUNT WHERE ZACCOUNTTYPE=26 "
            "AND ZACTIVE=1 LIMIT 1").fetchone()
        acct_email = row["ZUSERNAME"] if row else ""
        acct_db.close()

    lines.append("## Device Account")
    if acct_email:
        lines.append(f"- **Apple ID:** {acct_email}")
    else:
        lines.append("- *No active Apple ID found.*")
    lines.append("")

    # mobile/Library/AddressBook/AddressBook.sqlitedb -- does the Apple
    # ID's own email match a saved contact? property=4 is email in
    # ABMultiValue (confirmed against this real database's own rows).
    if acct_email:
        ab_db = _sqlite_at(paths, "mobile/Library/AddressBook/AddressBook.sqlitedb")
        if ab_db:
            row = ab_db.execute(
                "SELECT p.First, p.Last FROM ABMultiValue m "
                "JOIN ABPerson p ON p.ROWID = m.record_id "
                "WHERE m.property = 4 AND m.value = ? LIMIT 1",
                (acct_email,)).fetchone()
            ab_db.close()
            if row:
                name_str = " ".join(x for x in (row["First"], row["Last"]) if x)
                lines.append("## Linked Contact")
                lines.append(f"- **Name:** {name_str or '(no name on contact)'}")
                lines.append(f"- *Matched by email ({acct_email}) against Contacts.*")

    return [{"document_markdown": "\n".join(lines)}]
