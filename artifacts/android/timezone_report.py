"""Timezone & time sources (Android) — one document setting side by side
every source of "what zone" the case has, each labelled with what it
describes: the phone's own setting, the automatic time zone switch, the
Acquisition PC (.ufd), and the zip's DOS-vs-UT stamps. Device Info carries
only the short phone-zone and automatic-zone lines; this is the full story.

The iOS counterpart is artifacts/ios/timezone_report.py (different
sources, so a separate file, same precedent as device_info.py). Detection
lives in app/device_timezone.py so the Timestamp Display dialog, Device
Info and this report read the same file the same way.

Sources (verified 2026-11-16 on the Pixel 7a, Galaxy A53 (GrayKey-style
layout) and Poco X7 Pro images, all America/New_York, confidence 100,
automatic on; and on the Android 16 lab emulator, Europe/London):
  - data/property/persistent_properties, persist.sys.timezone (protobuf).
    vendor.qemu.timezone (emulators only) is ignored.
  - data/system/users/0/settings_global.xml (ABX), auto_time_zone and
    auto_time. Lab: switching "Set time zone automatically" off changed
    auto_time_zone 1 -> 0.
  - Weak hints, allow-listed only: data/system/netpolicy.xml (a
    ZonedDateTime such as the data-cycle cycleStart) and Settings'
    battery-usage-db-v*. Never tzdata/APK/apex files or geotimezone.pb.
"""

view_mode = "document"
device_wide = True
name = "Timezone & time sources"
category = "Device & App Info"
description = (
    "Every source of the case's time zone, side by side and labelled: the phone's own "
    "setting (persist.sys.timezone in data/property/persistent_properties), the automatic "
    "time zone switch (auto_time_zone in settings_global.xml), the Acquisition PC's offset "
    "from the .ufd report, and the offset inferred from the zip's DOS-vs-UT file stamps, "
    "which is the extraction software's zone, usually the download computer's, not the "
    "phone's. Where the phone and the other sources disagree, the report says so. Up to "
    "three weak hints from allow-listed system files are listed as incidental only.\n\n"
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

    phone = dt.phone_zone_android(read, adapter)
    zone = (phone or {}).get("zone", "")
    lines = [f"# {dt.REPORT_NAME}", "", dt.REPORT_WARNING, ""]

    lines.append("## Phone timezone (handset)")
    if phone:
        conf = f", confidence {phone['confidence']}" if phone.get("confidence") else ""
        lines.append(f"- **Phone timezone:** {zone}{conf} — from `{phone['source']}` "
                     f"(`{phone['setting']}`)")
        if not phone.get("known"):
            lines.append("- ⚠️ This name is not in this computer's zone database, so it cannot be "
                         "used for display.")
        lines.append("- *The setting when the phone was extracted. It does not record which zone "
                     "the phone was in on earlier dates. Timestamp Display's \"Handset local\" uses "
                     "this value.*")
    else:
        lines.append("- **Phone timezone:** Not found (`persist.sys.timezone` not in "
                     f"`{dt.ANDROID_PROPS}`, and no `{dt.ANDROID_OLD_PROP}` file).")
    lines.append("")

    lines.append("## Automatic time zone")
    auto = dt.auto_time_zone_android(read, adapter)
    if auto and auto.get("auto_time_zone") is not None:
        on = auto["auto_time_zone"]
        lines.append(f"- **Automatic time zone:** {'On' if on else 'Off'} — from `{auto['source']}` "
                     "(`auto_time_zone`)")
        if on:
            lines.append("- *On: the phone sets its zone from the mobile network or location, so "
                         "the zone follows where the phone is. Travel would have changed it.*")
        else:
            lines.append("- *Off: the zone was set by hand and stays put until someone changes it.*")
        if auto.get("auto_time") is not None:
            lines.append(f"- **Automatic date and time:** {'On' if auto['auto_time'] else 'Off'} "
                         "(`auto_time`)")
    else:
        lines.append(f"- **Automatic time zone:** Not found (`auto_time_zone` not in "
                     f"`{dt.ANDROID_SETTINGS_GLOBAL}`).")
    lines.append("")

    lines.extend(dt.acquisition_pc_lines(settings, ctx.zip_path, zone))
    lines.append("")
    lines.extend(dt.archive_lines(dt.case_archive_detect(settings, ctx.zip_path, ctx.case_dir), zone))
    lines.append("")

    ui_paths = list((ctx.get_ui_metadata() or {}).keys())
    lines.extend(dt.hint_lines(dt.weak_hints_android(read, adapter, ui_paths)))
    lines.append("")
    lines.append("*Not used: tzdata files in apps, APEX and system images (they list every zone "
                 "and are not travel history), and Google Play services' geotimezone.pb.*")

    return [{"document_markdown": "\n".join(lines)}]
