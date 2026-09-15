"""iOS App Report — a per-app inventory of every app found on the device,
not just ones with a parser.

Moved here 2026-09-15 from what used to be a hardcoded "Apps" tree node in
the Artifact Viewer (auto-populated the instant a case loaded), per direct
user request: "move the app table to be an artifact script instead...
[run] from the [Run Artifact Parsers] dialog and not when the case is
loaded." The underlying computation (app_intelligence.scan_apps) is
unchanged — this script is a thin wrapper matching every other parser's
own shape, not a rewrite of the logic itself. See CLAUDE.md's
app_intelligence.py Conventions entry for the full detail on how each
field is derived, and artifact_runner.py's own module docstring for the
`device_wide` API this uses (no single app_path/files — this scans every
app on the device in one pass).

iOS-specific: identity/location columns (shared_data_folder, data_folder,
plugins) are grounded in app_registry (the on-device LaunchServices
csstore) first, falling back to app_intelligence's own merged container
list only when no app_registry row exists (an unlinked App-Group, or a
PluginKit extension — app_registry does not reliably carry extension
bundle ids). See artifacts/android/app_report.py for the Android
counterpart, which has no app_registry/LaunchServices equivalent at all
and always uses the container-list fallback.
"""

device_wide = True
name = "App Report"
description = (
    "Every app found on this device, not just ones with a parser -- app "
    "name, bundle id, shared/data container paths, PluginKit extensions, "
    "total size, last-activity timestamps, a deterministic 0-10 interest "
    "score, and category. Identity/location columns are grounded in the "
    "device's own LaunchServices registry (app_registry) where available, "
    "falling back to the raw container list otherwise. This is a full "
    "device-wide scan, not scoped to one app -- on a large case the first "
    "run can take up to a minute (subsequent runs against an unchanged "
    "case are much faster, via app_intelligence's own cache).\n\n"
    "This report is a FIXED SNAPSHOT of the scan at the moment it was "
    "run -- per this project's own no-automatic-work design (see "
    "ProcessDialog and CLAUDE.md's Conventions), it is never re-run or "
    "refreshed on its own, including at case load. Re-run it manually "
    "(Tools -> Run Artifact Parsers) if you want it to reflect a newly "
    "added or updated parser script (has_parser/score are both derived "
    "from which parsers exist AT SCAN TIME, so an app parsed for the "
    "first time after this ran will still show has_parser=No here until "
    "re-run) -- the underlying device data itself never changes (a "
    "forensic archive is immutable once acquired), so a re-run only "
    "matters for keeping has_parser/score/category current against this "
    "project's own evolving parser coverage, not because the phone's "
    "own app list could have changed.\n\n"
    "What the four timestamp columns actually measure, and their limits: "
    "data_folder_created_utc/shared_folder_created_utc are each "
    "container's own creation time (filesystem birth time, not "
    "last-modified) -- confirmed against real ground-truth documentation "
    "to be an exact, reliable proxy for when the app was first "
    "installed/set up on this device. preferences_modified_utc is the "
    "mtime of the app's own Library/Preferences/<bundle id>.plist -- a "
    "file virtually every iOS app writes to during normal use -- "
    "confirmed to closely track real last use in most cases, but it can "
    "UNDERSHOOT the true last-used date if the app's final session never "
    "happened to rewrite its settings file. splash_snapshot_modified_utc "
    "is the mtime of the OS-generated app-switcher snapshot under "
    "Library/SplashBoard/Snapshots/ -- captured by iOS itself every time "
    "the app is foregrounded then backgrounded -- confirmed the closest "
    "single proxy for true last use across every app checked so far, but "
    "still a proxy: an app that was never backgrounded normally (a "
    "crash, a forced kill) may not get a fresh snapshot. An earlier "
    "version of this used a single Last Activity value per container (the "
    "maximum mtime across every file inside it) -- replaced after real "
    "casework showed multiple, unrelated apps' own third-party disk-cache "
    "libraries writing cache files with a deliberately fabricated "
    "far-future modification time (one app's cache uniformly stamped "
    "over a decade in the future), which a plain maximum has no way to "
    "tell apart from a genuine recent write. None of these four columns "
    "is a substitute for reviewing an app's own real content when a "
    "parser or manual database review is available -- they exist to "
    "help triage which unparsed apps are worth that closer look."
)
core_fields = ["display_name", "app_id", "category", "score", "has_parser", "total_bytes"]
byte_fields = ["total_bytes"]


def run(paths):
    import app_intelligence

    ctx = paths["_case_context"]
    rows = app_intelligence.scan_apps(ctx)
    app_ids = [r.get("app_id", "") for r in rows]
    registry_by_bundle, plugins_by_bundle = app_intelligence.build_app_registry_lookup(
        ctx.case_dir, app_ids)
    return [app_intelligence.flatten_row(r, registry_by_bundle, plugins_by_bundle)
           for r in rows]
