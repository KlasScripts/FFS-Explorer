name = "Chrome Site Settings"
app_group_label = "Chrome"
group_sort_key = 16
description = (
    "Every per-origin entry across ALL of Chrome's content-setting "
    "categories (app_chrome/Default/Preferences, the big JSON file's "
    "own profile.content_settings.exceptions object) -- one row per "
    "(category, origin) pair, generically, rather than one hand-built "
    "schema per category: Chromium has ~60 possible categories "
    "(geolocation/notifications/camera/mic/popups/... -- see "
    "Chromium's own content_settings_pattern.cc) and adds more over "
    "time, so a fixed per-category column set would go stale; every "
    "category's own real setting payload is instead kept as a single "
    "JSON-text column (setting_json) rather than flattened. "
    "Real, verified on this project's own Android 14 JoshHickman case: "
    "only 8 of the ~60 possible categories are populated (most, "
    "including notifications/geolocation/camera/mic, are real checked-"
    "empty negatives, not a parsing gap). Of the populated ones, THREE "
    "are directly meaningful, not just Chrome-internal bookkeeping: "
    "site_engagement (a real usage score Chrome computed per origin "
    "from actual visits, e.g. wickr-guest-access-pro-prod..., "
    "www.google.com, www.mlb.com -- corroborates real use, not just a "
    "single page load), media_engagement (per-origin visit counts + "
    "whether media ever played, 11 real origins including "
    "cellebrite.com), and fedcm_idp_signin (confirms accounts.google.com "
    "was used as a federated sign-in identity provider -- direct, "
    "independent corroboration of Chrome Login Data's own federated "
    "Pinterest entry). The other five populated categories on this case "
    "(app_banner/client_hints/cookie_controls_metadata/formfill_metadata/"
    "http_allowed) are real but lower-value browser-internal state -- "
    "shown rather than hidden, since \"process everything\" was the "
    "direct instruction, but not claimed to be equally significant."
)
warning = (
    "The origin's OWN presence here reflects Chrome having recorded "
    "SOME interaction with it under this category -- not necessarily a "
    "full page the user consciously visited (media_engagement records a "
    "visit even with zero actual media playback; site_engagement scores "
    "accrue from background activity too, not only foreground taps). "
    "Cross-reference against Chrome Web History/Chrome Cache before "
    "treating an origin found ONLY here as confirmed user browsing."
)
app_path = "data/data/com.android.chrome"
files = {
    "preferences": "app_chrome/Default/Preferences",
}

timestamp_fields = {"last_modified": "webkit_us", "expiration": "webkit_us"}
core_fields = ["category", "origin", "last_modified"]


def _clean_origin(key: str) -> str:
    """Chromium content-setting keys are "<primary_pattern>,<secondary_
    pattern>" -- secondary is almost always "*" (any embedding context);
    keep only the primary origin pattern, the meaningful part."""
    return key.split(",", 1)[0]


def _webkit_us_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run(paths):
    import json

    with open(paths["preferences"], "r", encoding="utf-8") as f:
        prefs = json.load(f)

    exceptions = (
        prefs.get("profile", {}).get("content_settings", {}).get("exceptions", {})
    )

    out = []
    for category, entries in exceptions.items():
        if not isinstance(entries, dict) or not entries:
            continue
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            out.append({
                "category": category,
                "origin": _clean_origin(key),
                "last_modified": _webkit_us_or_none(entry.get("last_modified")),
                "expiration": _webkit_us_or_none(entry.get("expiration")),
                "setting_json": json.dumps(entry.get("setting"), sort_keys=True),
                "raw_key": key,
            })
    return out
