"""Files by Google Glide thumbnail cache — tiles only (not full-screen viewer).

Full-screen viewer thumbnails are *Full-screen Views*. Background scan,
Recents, grid and list thumbs stay here. Unlinked cache files are labelled
Unknown (W×H); kind is never guessed from pixels alone.
See app/files_by_google_glide.py.
"""

name = "Thumbnail Cache"
app_group_label = "Google Files"
group_sort_key = 5
warning = (
    "A thumbnail is not proof the user looked at that file. Background-scan "
    "thumbs are written without opening the app; Recents, grid and list "
    "thumbs mean a tile was drawn, not that it was noticed. Kind is only "
    "from a Glide-key match; unlinked rows are Unknown (size). Full-screen "
    "viewer thumbs are in Full-screen Views; inferred leftovers in "
    "Likely Full-screen Views. Read Report Notes."
)
description = (
    "Tile thumbnails in Files by Google's Glide disk cache (indexing scan, "
    "Recents, grid, list) when the Glide key reverses to a file, plus "
    "unlinked leftovers as Unknown (W×H). User Looked is a short flag — "
    "full meaning is in Report Notes."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {}
optional_files = {}
existence_check_paths = ["cache/image_manager_disk_cache/journal"]

core_fields = [
    "kind", "user_looked", "path", "thumbnail", "file_link", "linked", "size_px", "format",
    "journal_order", "link_source", "kind_source",
]
media_fields = ["thumbnail", "file_link"]
hidden_fields = [
    "cache_key", "media_store_id", "uri", "journal_reads",
]


def run(paths):
    import files_by_google_glide as glide
    from artifact_runner import log_note

    app_base = paths.get("_app_base_ui_path") or "data/data/com.google.android.apps.nbu.files"
    likely_keys = glide.likely_viewer_cache_keys(paths, app_base)
    rows = glide.thumbnail_rows(
        paths, app_base, kinds=glide.TILE_KINDS, include_unlinked=True,
        exclude_keys=likely_keys)
    if not rows and not glide.read_cache(paths, app_base):
        log_note("No Glide cache entries under cache/image_manager_disk_cache")
        return []
    for r in rows:
        r["user_looked"] = "No — not proof the user looked at it"
    linked_n = sum(1 for r in rows if r["linked"] == "Yes")
    unknown_n = sum(1 for r in rows if r["linked"] == "No")
    log_note(
        f"{len(rows)} tile/unknown thumbnails ({linked_n} Glide-linked, "
        f"{unknown_n} unlinked as Unknown); "
        f"{len(likely_keys)} claimed by Likely Full-screen Views")
    return rows
