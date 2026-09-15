"""Android App Report — a per-app inventory of every app found on the
device, not just ones with a parser.

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

Android-specific: there is no LaunchServices/app_registry equivalent on
Android at all (that's an iOS-only on-device registry), so
shared_data_folder/data_folder/plugins always come from
app_intelligence's own merged container list rather than a registry
lookup — see artifacts/ios/app_report.py's own docstring for the iOS
counterpart, which prefers app_registry first. category/permissions here
come from packages.xml/runtime-permissions.xml (real per-device files,
occasionally binary ABX-encoded rather than plain XML — handled
automatically) rather than iOS's Info.plist/iTunesMetadata.plist.
"""

device_wide = True
name = "App Report"
description = (
    "Every app found on this device, not just ones with a parser -- app "
    "name, package name, shared/data container paths, total size, "
    "last-activity timestamps, a deterministic 0-10 interest score, and "
    "category (from packages.xml's own categoryHint, when present). This "
    "is a full device-wide scan, not scoped to one app -- on a large case "
    "the first run can take up to a minute (subsequent runs against an "
    "unchanged case are much faster, via app_intelligence's own cache).\n\n"
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
    "own app list could have changed."
)
core_fields = ["display_name", "app_id", "category", "score", "has_parser", "total_bytes"]
byte_fields = ["total_bytes"]


def run(paths):
    import app_intelligence

    ctx = paths["_case_context"]
    rows = app_intelligence.scan_apps(ctx)
    app_ids = [r.get("app_id", "") for r in rows]
    # build_app_registry_lookup still safe to call on Android -- returns
    # ({}, plugins_by_bundle) since app_registry is iOS-only, matching
    # app_intelligence.py's own documented behavior for this case.
    registry_by_bundle, plugins_by_bundle = app_intelligence.build_app_registry_lookup(
        ctx.case_dir, app_ids)
    return [app_intelligence.flatten_row(r, registry_by_bundle, plugins_by_bundle)
           for r in rows]
