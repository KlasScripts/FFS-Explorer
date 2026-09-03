name = "Chrome Cache - Media"
app_group_label = "Chrome"
group_sort_key = 7
description = (
    "Every image/video entry in Chrome's own HTTP disk cache (Simple Cache "
    "format, data/data/com.android.chrome/cache/Cache/Cache_Data/ -- NOT "
    "the app_chrome/Default database directory every other Chrome report "
    "here reads from, a genuinely different location): favicons, page "
    "images, ad/tracking pixels, byte-range-cached video, all mixed "
    "together exactly as Chrome stored them, unsorted by page. Each "
    "entry's already-decompressed body is written out as a real local "
    "file (see the Attachment column) so it thumbnails/opens the normal "
    "way -- the RAW cache entry (raw_ui_path) is not itself image/video-"
    "decodable, still wrapped in Simple Cache's own container plus "
    "whatever Content-Encoding Chrome applied. "
    "referenced_by_pages lists every page (by URL) whose own markup "
    "referenced this exact resource, resolved the same way as this "
    "case's 'Chrome Cache - Pages' report's own references_found/"
    "references_resolved -- empty means this file was never referenced "
    "by any successfully-parsed page's own static markup (evicted "
    "referencing page, JS-driven reference, or genuinely orphaned/"
    "standalone fetch). referenced_by_page_titles is that SAME page "
    "list's own <title> text, positionally parallel line-for-line (not "
    "combined into one string, per direct request -- 'give the page "
    "title of the page that the cache belongs to') so an examiner can "
    "recognize a referencing page at a glance ('MLB.com | The Official "
    "Site...') without first cross-referencing its bare URL. Companion "
    "to 'Chrome Cache - Pages': when an "
    "image here looks worth investigating, its referenced_by_pages value "
    "is the page URL to go search for over there -- was previously one "
    "single 'Chrome Cache' report with three nested filtered views under "
    "one tree node (All Media/Orphaned Media/Reconstructed Web Pages), "
    "split into two genuinely separate top-level reports per direct "
    "instruction: 'changing chrome cache to two report rather than "
    "nested ... chrome cache - media and chrome cache - pages.' "
    "Shares its underlying entry-decode pass with 'Chrome Cache - Pages' "
    "(app/chrome_cache.py's own parse_all_entries) rather than "
    "re-implementing it -- each report's own run() just filters/projects "
    "that same full parse down to its own content-type."
)
warning = (
    "referenced_by_pages only reflects what THIS project's own static "
    "reference scan of a successfully-decoded page's markup could find "
    "-- a resource fetched by JavaScript at runtime, or referenced by a "
    "page whose own body failed to decompress (see 'Chrome Cache - "
    "Pages' report's body_error column), can be genuinely linked to a "
    "real page without showing up here. Empty is not proof this file "
    "was never part of any page a user saw."
)
app_path = "data/data/com.android.chrome"
files = {}
optional_files = {}
# files is deliberately empty (this parser enumerates the whole Cache_Data
# directory itself via _zip_names/_read_zip_bytes -- see chrome_cache.py's
# own parse_all_entries), so the "select parsers to run" dialog's own
# existence check (ArtifactRunnerDialog._mod_matches in artifact_viewer.py)
# has nothing in files.values() to test -- existence_check_paths gives it
# something: Simple Cache's own index file, present whenever the cache
# directory genuinely exists at all.
existence_check_paths = ["cache/Cache/Cache_Data/index"]

core_fields = ["url", "content_type", "referenced_by_page_titles", "referenced_by_pages", "response_date_epoch"]
media_fields = ["decoded_media_path"]
timestamp_fields = {"response_date_epoch": "s"}


def run(paths):
    import chrome_cache

    rows = chrome_cache.parse_all_entries(paths)
    out = []
    for r in rows:
        ct = (r["content_type"] or "").lower()
        if not (ct.startswith("image/") or ct.startswith("video/")):
            continue
        out.append({
            "url": r["url"],
            "content_type": r["content_type"],
            "content_encoding": r["content_encoding"],
            "raw_body_length": r["raw_body_length"],
            "body_decoded_length": r["body_decoded_length"],
            "body_error": r["body_error"],
            "response_date": r["response_date"],
            "response_date_epoch": r["response_date_epoch"],
            "referenced_by_pages": r["referenced_by_pages"],
            "referenced_by_page_titles": r["referenced_by_page_titles"],
            "decoded_media_path": r["decoded_media_path"],
            "raw_ui_path": r["raw_ui_path"],
        })
    return out
