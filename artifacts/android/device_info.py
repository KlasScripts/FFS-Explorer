"""Device Info (Android) — a one-page summary of the DEVICE itself, not app
content: make/model/OS, build/patch level, FFS acquisition type, timezone,
cellular (ICCID/phone/carrier/IMSI), the Google account signed into the
device, and any Contacts entry that email matches. Renders as a document
(view_mode="document"), not a table — see CLAUDE.md Conventions' "view_mode
= document" entry.

The iOS counterpart is artifacts/ios/device_info.py. Same report shape and
the same four sections, but every single source file below is different —
Android has no commcenter.plist, no Accounts3.sqlite and no
AddressBook.sqlitedb — so this is a separate file rather than one
cross-platform script with two branches, exactly the precedent
app_report.py already set for the same reason.

Deliberately kept short and flat, same as the iOS file: each field is a
couple of lines reading straight from its own real source, with the source
named right there. All sources below confirmed directly against this
project's own real Android 14 JoshHickman archive (2026-09-17),
cross-checked against ALEAPP's own parsers for the same files and against
that case's documented ground truth
(test_data/josh_hickman_android14/device_metadata.json):

  - system/build.prop — ro.build.id and ro.build.version.security_patch.
    Verified real on this archive: UQ1A.240105.004 and 2024-01-05, both
    matching ground truth's android_version.build/patch_level exactly.
    Make/model/Android version are NOT read from here — they're already
    parsed at case-open time into the case's own device_info table, same
    as the iOS file does. ro.serialno and ro.product.manufacturer/model
    are genuinely absent from every build.prop on this device (checked
    all five), so the serial in ground truth is not recoverable here —
    not an oversight, just not present in the extraction.
  - data/user_de/0/com.android.providers.telephony/databases/telephony.db,
    siminfo table — ICCID, phone number, carrier, IMSI, MCC/MNC. Source
    and column set confirmed against ALEAPP's own siminfo.py. Verified
    real on this archive: number 19199282177 matches ground truth's
    documented 919-928-2177 exactly, and display_name "Google Fi"
    matches its documented carrier_1.

    Two real, non-obvious things found by reading this device's actual
    rows rather than assuming ALEAPP's column list is enough: (1)
    carrier_name is EMPTY here while display_name holds "Google Fi", so
    carrier is read from display_name first and carrier_name only as a
    fallback — a report trusting carrier_name alone would show a blank
    carrier on this real device; (2) the legacy integer mcc/mnc columns
    are both 0 while mcc_string/mnc_string hold the real "310"/"240", so
    the string columns are the ones read.

    EVERY siminfo row is reported, not just the first — a dual-SIM
    device genuinely has several (ALEAPP's own sample data records 2-3
    rows on other real devices; this one has exactly 1). Note that
    ground truth documents a second carrier ("Visible") for this device
    that does NOT appear in siminfo at all — an honest limit of this
    source, not a parsing failure.
  - data/system_de/0/accounts_de.db, accounts table — type
    "com.google" is the Google account signed into the device. Source
    confirmed against ALEAPP's own accounts_de.py. Verified: name is
    "ldehner505@gmail.com", matching ground truth's documented
    google_account.email exactly. data/system_ce/0/accounts_ce.db is
    read as a fallback when the _de copy is missing — checked directly
    on this archive, the two files' accounts tables are row-for-row
    identical here (21 rows each).
  - data/data/com.android.providers.contacts/databases/contacts2.db —
    cross-referencing the Google account's own email against the
    email_v2 mimetype rows finds a matching contact, if the device owner
    also saved themselves as a contact. Verified honest-negative on this
    archive: the device's 29 raw contacts include exactly 2 email rows
    and NEITHER is the Google account's, so this section correctly does
    not appear at all for this device.

No IMEI. Unlike iOS — where commcenter.plist carries it — an Android FFS
extraction has no on-disk file holding the IMEI (it's read from the radio
at runtime), and ALEAPP doesn't recover one either. Stated plainly here
rather than left as an unexplained missing field.
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
    "make/model/Android version, build number and security patch level, "
    "FFS acquisition type, detected timezone, cellular identifiers per SIM "
    "(ICCID/phone number/carrier/IMSI/MCC-MNC), the Google account signed "
    "into the device, and any Contacts entry that account's own email "
    "matches. Every field states its own real source file inline in the "
    "report.\n\n"
    "Known limits, all real rather than theoretical. There is NO IMEI: an "
    "Android extraction has no on-disk file holding it (it comes from the "
    "radio at runtime), so unlike the iOS report this one cannot show "
    "one. Cellular details come from telephony.db's siminfo table and "
    "cover only SIMs that table knows about -- a carrier the device used "
    "at some point may be absent entirely, which is a limit of the source "
    "and not a parsing failure. Every siminfo row is listed, so a "
    "dual-SIM device shows more than one. The 'linked contact' match is a "
    "plain email-string match against Contacts, nothing more -- a "
    "coincidental match (two people sharing an email, which shouldn't "
    "happen for a real address but is worth knowing as a limitation) "
    "would show here too."
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


def _build_prop(read, adapter, ui_path):
    """Parse a build.prop into a plain dict. Android's own format: one
    key=value per line, '#' comments, no sections."""
    raw = _read_ui(read, adapter, ui_path)
    if not raw:
        return {}
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _sqlite_at(paths, ui_path):
    """Materialize one database out of the archive and open it READ-ONLY
    through artifact_runner.open_db_readonly (see CLAUDE.md's own
    WAL-checkpoint Conventions entry for why never a bare
    sqlite3.connect). Identical to artifacts/ios/device_info.py's own
    helper — see that file for the full reasoning, including the
    measured case where replaying a -wal changed what a real database
    contained.

    The -wal/-shm sidecars are copied alongside the main database,
    always, so the connection sees the database's REAL current state
    rather than its last-checkpointed one. ALEAPP's own siminfo.py
    collects the same sidecars via its path glob for exactly this
    reason. On THIS project's Android 14 JoshHickman archive
    accounts_de.db happens to ship no -wal at all (fully checkpointed) —
    which is precisely why a sidecar that isn't there is skipped rather
    than treated as an error.

    Written into this parser's own artifact_parser_files/ folder rather
    than a tempfile, so the exact bytes the report was built from stay
    on disk for inspection exactly like every files/optional_files
    parser's sources do (artifact_runner._parser_files_dir/_save_entry),
    and so no temp file is leaked per run. Any sidecar left behind by an
    earlier run is DELETED rather than left in place, since a stale -wal
    against freshly rewritten main-database bytes is worse than no -wal
    at all.
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
    import os
    from artifact_runner import open_db_readonly

    ctx = paths["_case_context"]
    adapter, read = paths["_adapter"], paths["_read_zip_bytes"]
    lines = ["# Device Info", ""]

    # Make/Model/Android Version — already parsed at case-open time, from
    # the device's own UFD/build.prop (see ffs-explorer.py's
    # _read_device_info) — no re-parsing needed here.
    db = open_db_readonly(os.path.join(ctx.case_dir, "caseresults.db"))
    device_fields = dict((k, v) for k, v, _src in
                         db.execute("SELECT field_name, data, source FROM device_info"))
    lines.append("## Device")
    for label in ("Make", "Model", "Android Version", "Hardware ID"):
        if device_fields.get(label):
            lines.append(f"- **{label}:** {device_fields[label]}")

    # system/build.prop — build number and security patch level. Not the
    # source for make/model/version above: those come from the case's own
    # device_info table, and on this project's real Pixel 7a archive
    # build.prop genuinely doesn't carry ro.product.manufacturer/model.
    props = _build_prop(read, adapter, "system/build.prop")
    if props.get("ro.build.id"):
        lines.append(f"- **Build Number:** {props['ro.build.id']}")
    if props.get("ro.build.version.security_patch"):
        lines.append(
            f"- **Security Patch Level:** {props['ro.build.version.security_patch']}")

    # FFS archive layout — see _LAYOUT_LABELS above for why this is
    # never phrased as an acquisition-tool identification. This script
    # only ever runs for an Android archive (see is_android() in
    # CLAUDE.md Conventions for why that check matters for a device_wide/
    # cross-platform caller, not needed here since this file IS the
    # Android-specific branch).
    lines.append("- **FFS Type:** "
                 f"{_LAYOUT_LABELS.get(adapter.format, adapter.format)} (Android)")

    # Detected handset timezone — case_settings, set best-effort at first
    # case-load (see timestamp_display.py / device_timezone.py
    # Conventions). Genuinely empty on some real cases, including this
    # project's own Android 14 JoshHickman one — hence the guard.
    tz_row = db.execute(
        "SELECT value FROM case_settings WHERE key='handset_timezone_name'").fetchone()
    if tz_row and tz_row[0]:
        lines.append(f"- **Detected Timezone:** {tz_row[0]}")
    db.close()
    lines.append("")

    # telephony.db siminfo — EVERY row, not just the first: a dual-SIM
    # device genuinely has several.
    lines.append("## Cellular")
    sim_db = _sqlite_at(
        paths, "data/user_de/0/com.android.providers.telephony/databases/telephony.db")
    sims = []
    if sim_db:
        try:
            sims = sim_db.execute(
                "SELECT icc_id, number, carrier_name, display_name, imsi, "
                "mcc_string, mnc_string, is_embedded FROM siminfo").fetchall()
        except Exception:
            sims = []
        sim_db.close()

    if not sims:
        lines.append("- *No SIM records found in telephony.db.*")
    for idx, sim in enumerate(sims, 1):
        if len(sims) > 1:
            lines.append(f"**SIM {idx}**")
        # display_name before carrier_name: on this project's real Pixel
        # 7a archive carrier_name is empty and display_name holds
        # "Google Fi" — see this module's docstring.
        carrier = sim["display_name"] or sim["carrier_name"]
        if sim["number"]:      lines.append(f"- **Phone Number:** {sim['number']}")
        if carrier:            lines.append(f"- **Carrier:** {carrier}")
        if sim["icc_id"]:      lines.append(f"- **ICCID:** {sim['icc_id']}")
        if sim["imsi"]:        lines.append(f"- **IMSI:** {sim['imsi']}")
        # mcc_string/mnc_string, never the legacy integer mcc/mnc columns
        # — both are 0 on this real device while the strings are correct.
        if sim["mcc_string"] and sim["mnc_string"]:
            lines.append(
                f"- **Network (MCC/MNC):** {sim['mcc_string']}/{sim['mnc_string']}")
        lines.append(f"- **SIM Type:** {'eSIM' if sim['is_embedded'] else 'Physical SIM'}")
        lines.append("")
    lines.append("- *No IMEI: an Android extraction has no on-disk file holding it.*")
    lines.append("")

    # accounts_de.db (accounts_ce.db as fallback) — type "com.google" is
    # the Google account signed into the device.
    acct_email = ""
    for ui_path in ("data/system_de/0/accounts_de.db", "data/system_ce/0/accounts_ce.db"):
        acct_db = _sqlite_at(paths, ui_path)
        if not acct_db:
            continue
        try:
            row = acct_db.execute(
                "SELECT name FROM accounts WHERE type='com.google' "
                "ORDER BY _id LIMIT 1").fetchone()
            acct_email = row["name"] if row else ""
        except Exception:
            acct_email = ""
        acct_db.close()
        if acct_email:
            break

    lines.append("## Device Account")
    if acct_email:
        lines.append(f"- **Google Account:** {acct_email}")
    else:
        lines.append("- *No Google account found.*")
    lines.append("")

    # contacts2.db — does the Google account's own email match a saved
    # contact? Verified honest-negative on this project's own Android 14
    # JoshHickman archive: it does not, so this section is absent there.
    if acct_email:
        ab_db = _sqlite_at(
            paths, "data/data/com.android.providers.contacts/databases/contacts2.db")
        if ab_db:
            try:
                row = ab_db.execute(
                    "SELECT r.display_name FROM data d "
                    "JOIN mimetypes m ON m._id = d.mimetype_id "
                    "JOIN raw_contacts r ON r._id = d.raw_contact_id "
                    "WHERE m.mimetype = 'vnd.android.cursor.item/email_v2' "
                    "AND d.data1 = ? LIMIT 1", (acct_email,)).fetchone()
            except Exception:
                row = None
            ab_db.close()
            if row:
                lines.append("## Linked Contact")
                lines.append(f"- **Name:** {row['display_name'] or '(no name on contact)'}")
                lines.append(f"- *Matched by email ({acct_email}) against Contacts.*")

    return [{"document_markdown": "\n".join(lines)}]
