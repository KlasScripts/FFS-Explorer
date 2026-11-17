"""Timezone & time sources (iOS) — one document setting side by side
every source of "what zone" the case has, each labelled with what it
describes: the phone's own setting, the Acquisition PC (.ufd), and the
zip's DOS-vs-UT stamps. Device Info carries only the short phone-zone line;
this is the full story.

The Android counterpart is artifacts/android/timezone_report.py (different
sources, so a separate file, same precedent as device_info.py). Detection
lives in app/device_timezone.py so the Timestamp Display dialog, Device
Info and this report read the same file the same way.

Sources (verified 2026-11-16 on the iOS 17 Cellebrite image):
  - db/timezone/localtime: the extraction keeps the symlink as a small file
    whose content is the zoneinfo path (America/New_York there).
  - db/timed/Library/Preferences/com.apple.preferences.datetime.plist:
    holds only 'timezoneset' = True on that image. Its meaning is not
    verified, so it is shown raw and NOT reported as the automatic time
    zone switch. No verified iOS source for that switch is read.
  - No iOS weak-hint files are checked (none with a known, tested path).
"""

view_mode = "document"
device_wide = True
name = "Timezone & time sources"
category = "Device & App Info"
description = (
    "Every source of the case's time zone, side by side and labelled: the phone's own "
    "setting (db/timezone/localtime), the Acquisition PC's offset from the .ufd report, "
    "and the offset inferred from the zip's DOS-vs-UT file stamps, which is the extraction "
    "software's zone, usually the download computer's, not the phone's. Where the phone "
    "and the other sources disagree, the report says so. The automatic time zone switch "
    "is not determined on iOS: the one related setting found (timezoneset in "
    "com.apple.preferences.datetime.plist) is shown raw because its meaning is not "
    "verified.\n\n"
    "Everything here is a snapshot at extraction. No source on the phone records which "
    "zone it was in on earlier dates, so older evidence may come from another zone "
    "(travel, a manual change). Evidence times stay UTC; local time is display only."
)


def run(paths):
    import os
    from artifact_runner import open_db_readonly
    import device_timezone as dt

    ctx = paths["_case_context"]
    adapter, read = paths["_adapter"], paths["_read_zip_bytes"]

    db = open_db_readonly(os.path.join(ctx.case_dir, "caseresults.db"))
    try:
        settings = dict(db.execute("SELECT key, value FROM case_settings").fetchall())
    except Exception:
        settings = {}
    db.close()

    phone = dt.phone_zone_ios(read, adapter)
    zone = (phone or {}).get("zone", "")
    lines = [f"# {dt.REPORT_NAME}", "", dt.REPORT_WARNING, ""]

    lines.append("## Phone timezone (handset)")
    if phone:
        lines.append(f"- **Phone timezone:** {zone} — from `{phone['source']}` ({phone['setting']})")
        if not phone.get("known"):
            lines.append("- ⚠️ This name is not in this computer's zone database, so it cannot be "
                         "used for display.")
        lines.append("- *The setting when the phone was extracted. It does not record which zone "
                     "the phone was in on earlier dates. Timestamp Display's \"Handset local\" uses "
                     "this value.*")
    else:
        lines.append(f"- **Phone timezone:** Not found (no readable `{dt.IOS_LOCALTIME}`).")
    lines.append("")

    lines.append("## Automatic time zone")
    lines.append("- **Automatic time zone:** Not determined — no verified iOS source for this "
                 "switch is read.")
    prefs = dt.ios_datetime_prefs(read, adapter)
    if prefs and prefs.get("values"):
        shown = ", ".join(f"`{k}` = {v}" for k, v in sorted(prefs["values"].items()))
        lines.append(f"- Related setting found, meaning not verified: {shown} — from "
                     f"`{prefs['source']}`")
    lines.append("")

    lines.extend(dt.acquisition_pc_lines(settings, ctx.zip_path, zone))
    lines.append("")
    lines.extend(dt.archive_lines(dt.case_archive_detect(settings, ctx.zip_path, ctx.case_dir), zone))

    return [{"document_markdown": "\n".join(lines)}]
