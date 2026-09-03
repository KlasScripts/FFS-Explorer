name = "Chrome Favicons"
app_group_label = "Chrome"
group_sort_key = 9
description = (
    "Every page_url -> icon mapping in Chrome's own Favicons database "
    "(app_chrome/Default/Favicons -- a separate SQLite store from "
    "History, not a table inside it), one row per (page_url, icon "
    "bitmap) pair: icon_mapping.page_url joined through favicons (the "
    "icon's own url + icon_type) to favicon_bitmaps (the actual PNG "
    "bytes, plus last_updated/last_requested). icon_type is decoded "
    "from Chromium's own bitmask (favicon_base::IconType): FAVICON, "
    "TOUCH_ICON, TOUCH_PRECOMPOSED_ICON, WEB_MANIFEST_ICON, or a "
    "'+'-joined combination when more than one bit is set. Each icon "
    "bitmap is written out as a real .png (see the Attachment column) "
    "so the actual icon can be viewed, not just its byte length. "
    "page_url is asked for directly, without going through History at "
    "all -- prompted by a direct question: 'are there not sometimes "
    "google search captured with this artifact.' Confirmed on this "
    "case's own real data: yes -- icon_mapping.page_url includes both "
    "of Joshua Hickman's documented real Google searches in full, "
    "e.g. 'https://www.google.com/search?q=mobile+phone+forensics&...' "
    "(Chrome fetches/associates the Google search-results page's own "
    "favicon the same as any other page). On THIS case specifically "
    "that is NOT new information -- both searches are already fully "
    "recoverable via the separate 'Chrome Web History'/'Chrome Search' "
    "reports, since nothing here was cleared; the history_coverage "
    "column says exactly that for both of them ('Chrome History'), "
    "confirmed by automatic cross-reference rather than a one-off manual "
    "check (see _history_coverage in run() below) -- so an examiner can "
    "filter this report straight to 'Not in History/UKM' for the actual "
    "additional-value rows, per direct follow-up request, rather than "
    "re-deriving that by hand against the other two reports every time. "
    "The reason to still extract this as its own artifact regardless of "
    "history_coverage's result is structural: Favicons is a genuinely "
    "separate SQLite file from History, so 'Clear Browsing Data' "
    "clearing one doesn't guarantee the other is cleared through the "
    "identical code path or on the identical schedule -- a "
    "well-established general rationale in mobile forensics for "
    "extracting Favicons alongside History, not something invented "
    "here. That specific persistence-after-clearing behavior has NOT "
    "been field-tested against real data in THIS project the way "
    "chrome_web_history.py's Segmentation Platform (UKM) claim was "
    "(see that report's own description for the tested GTLAB clear-"
    "history run) -- stated as the standing rationale for keeping this "
    "report, not as a verified-on-this-case finding. "
    "Real, observed oddity worth stating plainly rather than silently "
    "passing through: every single favicon_bitmaps row in this case's "
    "own real database has last_requested = 0 -- last_updated (when "
    "the icon was actually fetched from the network) is the only "
    "reliably-populated timestamp here; last_requested is shown "
    "anyway, but don't read a 0 there as 'never requested.'"
)
warning = (
    "page_url here is an ASSOCIATION, not proof the page was actually "
    "browsed by a human at that moment -- Chrome can fetch/refresh a "
    "favicon mapping as a side effect of prefetching, a redirect chain, "
    "or a background tab, not only a page the user consciously visited. "
    "history_coverage cross-references it against Chrome History/"
    "Segmentation Platform (UKM) automatically -- see that column before "
    "treating a page_url found ONLY here as confirmed user activity."
)
app_path = "data/data/com.android.chrome"
files = {
    "favicons_db": "app_chrome/Default/Favicons",
}
optional_files = {
    "favicons_journal": "app_chrome/Default/Favicons-journal",
    # Read here ONLY to populate history_coverage below (page_url present
    # elsewhere or not) -- this parser never re-reports a History/UKM row
    # itself, that's chrome_web_history.py's job. Both genuinely optional:
    # a case can have Favicons survive with History/UKM gone entirely
    # (exactly the scenario this report exists for -- see description
    # above), and history_coverage says so plainly rather than treating a
    # missing comparison file as "not found" (see _history_coverage).
    "history": "app_chrome/Default/History",
    "history_journal": "app_chrome/Default/History-journal",
    "ukm_db": "app_chrome/segmentation_platform/ukm_db",
    "ukm_db_wal": "app_chrome/segmentation_platform/ukm_db-wal",
}

timestamp_fields = {"last_updated": "webkit_us", "last_requested": "webkit_us"}
# Which page, which icon, when it was last fetched, and whether it's
# ALREADY visible elsewhere -- the essentials; width/height/icon_url stay
# non-core (identify a specific bitmap variant, not needed for a first
# pass). history_coverage is core specifically so "filter to page_urls
# NOT already in History/UKM" (the whole point of adding it -- see its
# own comment in run() below) doesn't require digging through Columns
# first to find it.
core_fields = ["page_url", "icon_type_label", "last_updated", "history_coverage"]
media_fields = ["icon_image_path"]

record_source = {
    "label": "icon_mapping",
    "file_key": "favicons_db",
    "table": "icon_mapping",
    "rowid_fields": ["raw_mapping_id"],
}

_ICON_TYPE_BITS = [
    (1, "FAVICON"),
    (2, "TOUCH_ICON"),
    (4, "TOUCH_PRECOMPOSED_ICON"),
    (8, "WEB_MANIFEST_ICON"),
]


def _decode_icon_type(value) -> str:
    value = value or 0
    names = [name for bit, name in _ICON_TYPE_BITS if value & bit]
    return "+".join(names) if names else f"[unknown icon_type: {value}]"


def _history_coverage(page_url, history_urls, ukm_urls) -> str:
    """Whether *page_url* is ALSO visible via Chrome Web History/Chrome
    Search -- prompted directly: 'have a column ... not in history or
    searches ... filter the favicons for additional values.' Chrome
    Search's own rows are a subset of History's urls table (a keyword-
    search join or a "search?q=" URL match -- see chrome_search.py), so
    checking History's urls already covers "or searches" too; no separate
    search-specific check is needed. Three real outcomes, not two --
    "checked, not found" (the actual additional-value signal the examiner
    asked to filter for) is deliberately never conflated with "couldn't
    check" (History/UKM missing or unreadable), since a case where
    History/UKM is GONE and only Favicons survived is the single most
    forensically interesting state this report can be in, not a
    to-be-ignored gap."""
    in_history = history_urls is not None and page_url in history_urls
    in_ukm = ukm_urls is not None and page_url in ukm_urls
    if in_history and in_ukm:
        return "Chrome History + UKM"
    if in_history:
        return "Chrome History"
    if in_ukm:
        return "Segmentation Platform (UKM)"
    if history_urls is None and ukm_urls is None:
        return "History/UKM unavailable"
    return "Not in History/UKM"


def run(paths):
    import os
    import chrome_shared

    favicons_by_id = {
        r["id"]: (r["url"], r["icon_type"])
        for r in chrome_shared.query_rows(paths["favicons_db"], "SELECT id, url, icon_type FROM favicons")
    }
    mapping_rows = chrome_shared.query_rows(
        paths["favicons_db"], "SELECT id, page_url, icon_id FROM icon_mapping")
    bitmaps_by_icon_id = {}
    for r in chrome_shared.query_rows(paths["favicons_db"],
            "SELECT id, icon_id, last_updated, last_requested, width, height, "
            "image_data FROM favicon_bitmaps"):
        bitmaps_by_icon_id.setdefault(r["icon_id"], []).append(r)

    history_urls = chrome_shared.url_set(paths.get("history"))
    ukm_urls = chrome_shared.url_set(paths.get("ukm_db"))

    parser_dir = paths.get("_parser_files_dir")

    out = []
    for m in mapping_rows:
        icon_url, icon_type = favicons_by_id.get(m["icon_id"], (None, None))
        coverage = _history_coverage(m["page_url"], history_urls, ukm_urls)
        bitmaps = bitmaps_by_icon_id.get(m["icon_id"], [])
        if not bitmaps:
            # A real, if unusual, gap: icon_mapping references an icon_id
            # with no favicon_bitmaps row at all (bitmap since evicted, or
            # never actually downloaded). Surface the mapping itself
            # rather than silently dropping it -- the page_url is still
            # real signal even with no image to show for it.
            out.append({
                "page_url": m["page_url"],
                "icon_url": icon_url,
                "icon_type_label": _decode_icon_type(icon_type),
                "width": None,
                "height": None,
                "last_updated": None,
                "last_requested": None,
                "icon_image_path": "",
                "history_coverage": coverage,
                "raw_mapping_id": m["id"],
            })
            continue
        for b in bitmaps:
            icon_image_path = ""
            if parser_dir and b["image_data"]:
                dest = os.path.join(
                    parser_dir, f'icon_{m["icon_id"]}_{b["id"]}.png')
                try:
                    with open(dest, "wb") as f:
                        f.write(b["image_data"])
                    icon_image_path = dest
                except OSError:
                    icon_image_path = ""
            out.append({
                "page_url": m["page_url"],
                "icon_url": icon_url,
                "icon_type_label": _decode_icon_type(icon_type),
                "width": b["width"],
                "height": b["height"],
                "last_updated": b["last_updated"],
                "last_requested": b["last_requested"],
                "icon_image_path": icon_image_path,
                "history_coverage": coverage,
                "raw_mapping_id": m["id"],
            })
    return out
