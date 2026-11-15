"""Likely full-screen viewer leftovers — picture match, not Glide-proven.

When Glide/carving cannot reverse a cache name to the viewer recipe, but an
unlinked cache image fills this case's proven viewer screen box and matches
the Glide-proven background-scan thumb of a file whose media-store id is no
longer in the media store (and no other file's scan comes close), list it
here as inferred. Never mixed into Full-screen Views. See
app/files_by_google_glide.likely_viewer_rows.
"""

name = "Likely Full-screen Views"
app_group_label = "Google Files"
group_sort_key = 3
warning = (
    "Every row is INFERRED: a leftover Glide cache image that looks like the "
    "same picture as the background-scan thumb of a file whose id is no "
    "longer in the media store. It is NOT a Glide-key proof that Files "
    "showed it full screen, and it has no open time. If the same picture "
    "existed as another file that is also gone, the id may be the other "
    "copy's. An empty report is not evidence nothing was viewed. Do not read "
    "the media store id as proof the file was deleted. Read Report Notes."
)
description = (
    "Unlinked Glide cache images that fill this case's proven full-screen "
    "box and picture-match the Glide-proven scan thumb of a file whose id is "
    "not in the current media store. Weaker than Full-screen Views; use when "
    "size/mtime for the viewer hash are gone."
)
app_path = "data/data/com.google.android.apps.nbu.files"
files = {}
optional_files = {}
existence_check_paths = ["cache/image_manager_disk_cache/journal"]

core_fields = [
    "user_looked", "confidence", "path", "thumbnail", "paired_scan_thumb",
    "match_distance",
    "runner_up_distance", "same_file_rows", "size_px", "scan_link_source",
    "link_method",
]
media_fields = ["thumbnail", "paired_scan_thumb", "file_link"]
# Rows are a picture comparison: the full-size viewer opens side by side.
media_compare = True
hidden_fields = [
    "kind", "kind_source", "cache_key", "media_store_id", "uri",
    "journal_order", "journal_reads", "format",
]


def run(paths):
    import files_by_google_glide as glide
    from artifact_runner import log_note

    app_base = paths.get("_app_base_ui_path") or "data/data/com.google.android.apps.nbu.files"
    rows = glide.likely_viewer_rows(paths, app_base)
    if not rows:
        entries, linked = glide.link_all(paths, app_base)
        has_viewer = any(k == glide.VIEWER for k, _f, _d in linked.values())
        if not has_viewer:
            log_note(
                "No Glide-proven full-screen thumbs in this case — "
                "screen-size gate cannot run; nothing listed")
        else:
            log_note(
                "No inferred full-screen leftovers "
                "(unlinked images filling the proven viewer box that match the "
                "scan thumb of a file whose id is not in the current media "
                "store, with no other file's scan close)")
        return []
    log_note(
        f"{len(rows)} inferred likely full-screen leftovers, each paired to "
        f"the scan thumb of a file whose id is not in the current media store")
    return rows
