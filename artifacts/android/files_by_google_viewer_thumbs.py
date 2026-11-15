"""Full-screen viewer thumbnails from Files by Google's Glide disk cache.

Only Glide-key matches for the FitCenter full-screen recipe (index, media
store, Safe folder, or size/mtime carved from media-store WAL/free pages).
Never inferred from pixel size alone. Companion to Opened Files (open time)
and Thumbnail Cache (tiles). See app/files_by_google_glide.py.
"""

name = "Full-screen Views"
app_group_label = "Google Files"
group_sort_key = 2
warning = (
    "Every row is a Glide-key match for Files by Google's full-screen viewer "
    "recipe: the picture was shown full screen at least once. It does not "
    "record when or for how long — use Opened Files for the open time. "
    "Rows are never labelled from image size alone. Safe folder moves can "
    "delete open records while leaving these thumbnails. A carved media-store "
    "link needs leftover size and modified time in external.db WAL or free "
    "pages; many older deletes leave nothing to carve."
)
description = (
    "Full-screen thumbnails from Files by Google's Glide disk cache, each "
    "tied to a file by reversing the viewer ResourceCacheKey (or carving "
    "size/mtime from the media store when the live row is gone). Unlinked "
    "or size-guessed images are not listed."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {}
optional_files = {}
existence_check_paths = ["cache/image_manager_disk_cache/journal"]

core_fields = [
    "path", "thumbnail", "file_link", "linked", "size_px", "format", "journal_order", "link_source",
]
media_fields = ["thumbnail", "file_link"]
hidden_fields = [
    "kind", "cache_key", "kind_source", "media_store_id", "uri", "journal_reads",
]


def run(paths):
    import files_by_google_glide as glide
    from artifact_runner import log_note

    app_base = paths.get("_app_base_ui_path") or "data/data/com.google.android.apps.nbu.files"
    rows = glide.thumbnail_rows(
        paths, app_base, kinds={glide.VIEWER}, linked_only=True)
    if not rows:
        log_note(
            "No Glide-proven full-screen viewer thumbnails "
            "(size-only guesses are not listed)")
        return []
    by_src = {}
    for r in rows:
        by_src[r["link_source"] or "?"] = by_src.get(r["link_source"] or "?", 0) + 1
    log_note(
        f"{len(rows)} full-screen viewer thumbnails (Glide-proven): "
        + ", ".join(f"{k}={v}" for k, v in sorted(by_src.items())))
    return rows
