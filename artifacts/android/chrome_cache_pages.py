name = "Chrome Cache - Pages"
app_group_label = "Chrome"
group_sort_key = 8
description = (
    "Every text/html entry in Chrome's own HTTP disk cache (Simple Cache "
    "format, data/data/com.android.chrome/cache/Cache/Cache_Data/) that "
    "could be reconstructed into a real, openable .mhtml page (see the "
    "Attachment column) -- built by scanning that page's own markup for "
    "its referenced local resources (img/script/link src|href, CSS "
    "url()) and embedding whichever of those ALSO survive elsewhere in "
    "this same cache, verbatim -- resolved via each referenced URL's own "
    "Content-Location, the identical mechanism a real Chrome-generated "
    ".mhtml archive already uses (see chrome_offline_pages.py); it opens "
    "in the exact same in-app page viewer as a real Chrome Offline Pages "
    "snapshot, with a real rendered thumbnail (not just a filename). "
    "This is a RECONSTRUCTION, not a real Chrome-generated snapshot -- "
    "explicitly labeled as such in the archive's own From: header, and "
    "in this report's own name: 'Pages', not 'Web Pages', to keep "
    "reading as a set of reconstructions rather than files that existed "
    "on the device as such. "
    "Verified end to end against two real, independently-known pages in "
    "this project's own Android 14 JoshHickman casework: both decompress "
    "correctly and their own <title> matches Chrome History's title for "
    "that exact URL. "
    "Was previously one single 'Chrome Cache' report with three nested "
    "filtered views under one tree node, split into two genuinely "
    "separate top-level reports per direct instruction: 'changing "
    "chrome cache to two report rather than nested ... chrome cache - "
    "media and chrome cache - pages.' See 'Chrome Cache - Media' for "
    "every image/video entry (including this page's OWN embedded "
    "assets, cross-referenced back here via its own referenced_by_pages "
    "column) -- and shares its underlying entry-decode pass with that "
    "report (app/chrome_cache.py's own parse_all_entries) rather than "
    "re-implementing it. "
    "render_note flags, per row, whether opening this reconstruction "
    "will actually show anything -- prompted directly after a report "
    "that 'most of the mhtml do not work.' Investigated by actually "
    "rendering all 141 of this case's own real reconstructions in a "
    "real headless QWebEngineView with this project's own JS-disabled "
    "lockdown, not guessed at: NOT a reconstruction bug -- 118 of 141 "
    "genuinely have no content outside a <script> tag (ad-auction "
    "payloads, tracking-sync JS, React/SPA app shells like Discord/"
    "Calendly booking whose entire body is one empty <div id=\"root\">) "
    "and correctly render blank once JavaScript is off, the exact "
    "'JS-driven content is a gap' limitation this project's own "
    "reference-scan approach already accepts, just now surfaced per-row "
    "up front instead of discovered by opening each one. The other 23 "
    "have real static body content and all 23 were confirmed to render "
    "real matching text in that same real-render check. "
    "A SECOND, distinct render_note case, found the same way on a real "
    "GTD ground-truth pair (chrome-bbc-google-001, screenshots vs. this "
    "tool's own output): a page can have real, correct text content and "
    "STILL render solid black/unstyled, when its site ships zero "
    "<link rel=\"stylesheet\"> in the static markup at all -- confirmed "
    "on BBC's own site, whose real styling is CSS-in-JS, injected by the "
    "same JavaScript this viewer deliberately disables (a real MLB "
    "article with one ordinary resolved text/css part, by contrast, "
    "rendered correctly styled). render_note flags this too ('has real "
    "text content but may render unstyled') when zero text/css resources "
    "resolved for an otherwise-clean page -- a real, verified signal, "
    "not a guess, though a page styled entirely via inline <style> with "
    "no external stylesheet at all (e.g. some AMP pages, which forbid "
    "external CSS by spec) can false-positive here since they may render "
    "fine despite tripping the same zero-external-CSS check."
)
warning = (
    "A reconstructed page is NOT a faithful record of what the user saw. "
    "Only resources that (a) this page's own static markup actually "
    "references, AND (b) independently survive elsewhere in this SAME "
    "cache, are embedded -- a resource fetched by JavaScript at runtime, "
    "evicted before acquisition, blocked by an extension, or simply never "
    "cached at all is silently absent from the reconstruction the same "
    "way it would be from a real browser with no network access; absence "
    "here is not evidence the real page never had it. references_found/"
    "references_resolved on each row shows exactly how much of the page's "
    "own reference list could be recovered; render_note flags the more "
    "common case of nothing to recover at all -- a page whose entire "
    "real content was always JavaScript-driven, not something reference "
    "recovery could fix regardless of cache completeness. A text/html "
    "cache entry with "
    "no usable body (a tracking-pixel/redirect response with zero bytes, "
    "or a genuinely truncated/corrupted one -- see body_error) never "
    "appears here at all rather than as an empty reconstruction; check "
    "'Chrome Cache - Media' or the raw Cache_Data directory directly for "
    "that kind of entry."
)
app_path = "data/data/com.android.chrome"
files = {}
optional_files = {}
existence_check_paths = ["cache/Cache/Cache_Data/index"]

core_fields = ["url", "title", "content_type", "response_date_epoch", "render_note"]
media_fields = ["reconstructed_mhtml_path"]
# reconstructed_mhtml_path is an .mhtml PAGE, not a still image -- a plain
# image/video decode (media_viewer.ThumbnailWorker) can't produce a
# meaningful thumbnail for it. Declaring this routes it through an actual
# headless page-render instead (WebpageThumbnailRenderer, see
# artifact_media.py / artifact_viewer.py's _art_show_report) -- so the
# thumbnail shows what the page really looks like, not a filename box.
webpage_thumbnail_fields = ["reconstructed_mhtml_path"]
timestamp_fields = {"response_date_epoch": "s"}
hidden_fields = ["child_asset_urls"]


def run(paths):
    import chrome_cache

    rows = chrome_cache.parse_all_entries(paths)
    out = []
    for r in rows:
        ct = (r["content_type"] or "").lower()
        if "text/html" not in ct or not r["reconstructed_mhtml_path"]:
            continue
        out.append({
            "url": r["url"],
            "title": r["title"],
            "content_type": r["content_type"],
            "status_line": r["status_line"],
            "raw_body_length": r["raw_body_length"],
            "body_decoded_length": r["body_decoded_length"],
            "response_date": r["response_date"],
            "response_date_epoch": r["response_date_epoch"],
            "references_found": r["references_found"],
            "references_resolved": r["references_resolved"],
            "reconstructed_mhtml_path": r["reconstructed_mhtml_path"],
            "render_note": r["render_note"],
            "child_asset_urls": r["child_asset_urls"],
            "raw_ui_path": r["raw_ui_path"],
        })
    return out
