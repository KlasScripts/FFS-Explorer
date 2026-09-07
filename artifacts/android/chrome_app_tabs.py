name = "Chrome App Tabs"
app_group_label = "Chrome"
group_sort_key = 19
description = (
    "Chrome-for-Android's own per-tab persistence files under `app_tabs/"
    "<tab_id>/` -- what tabs were open, their real navigation history, "
    "and real tab metadata (theme color, tab group membership, pinned/"
    "sensitive-content flags, parent/root tab id), independent of "
    "ordinary browsing history and of the 'Chrome Sessions' report's own "
    "Session/Tab-restore files (a genuinely different real location and "
    "format). TWO real, distinct on-disk formats coexist here, confirmed "
    "against this project's own real Android 14 JoshHickman data -- the "
    "`format` column names which one produced a given row, never "
    "conflated into one shape:\n\n"
    "`TabState` -- files literally named `tab<N>` (no underscore), the "
    "current, real Chrome-for-Android tab-persistence format, confirmed "
    "directly against real Chromium source fetched while building this "
    "parser (`chrome/browser/tabpersistence/android/java/.../"
    "TabStateFileManager.java`'s `readState()` for the outer per-tab "
    "wrapper -- real wall-clock save/last-navigation timestamps, opener "
    "app id, tab group id, pinned/sensitive-content flags -- and `chrome/"
    "browser/tab/web_contents_state.cc` + `components/sessions/core/"
    "serialized_navigation_entry.cc` for the embedded native "
    "WebContentsState blob, which is the SAME real per-entry record "
    "format the 'Chrome Sessions' report's own SNSS files use, decoded "
    "here via the identical vendored `ccl_chromium_snss.NavigationEntry."
    "from_pickle` -- so a `TabState`-format row gets the full real url/"
    "title/transition/referrer/timestamp detail that format provides, "
    "not just a bare URL. Verified end to end against 6 independent real "
    "files on this device (a real 2024-02-08 ad-redirect tab correctly "
    "decoding its own real title 'Dulcetty' and a real 'Link; FromApi' "
    "transition; a real multi-step Wickr/Cognito OAuth sign-in flow "
    "recovered across 4 chained real navigations in one tab).\n\n"
    "`Legacy` -- files literally named `tab_state<N>` (WITH the "
    "underscore), a genuinely SIMPLER, DIFFERENT format found alongside "
    "the modern one on this same real device under DIFFERENT (stale) tab "
    "ids -- most likely leftover files from a Chrome version this device "
    "ran before an update changed the on-disk format, never cleaned up. "
    "Reverse-engineered PURELY from this project's own real data -- "
    "unlike the `TabState` format above, no current OR historical "
    "Chromium source could be found naming this exact file convention, "
    "so its handling is stated honestly as unverified-against-source, "
    "even though it decoded cleanly (zero leftover/unaccounted bytes) "
    "across all 6 independent real examples checked. Recovers only a "
    "bare url per navigation step -- no title, timestamp, or transition "
    "type is recoverable from this format at all (genuinely absent from "
    "its own byte layout, not merely undecoded here)."
)
warning = (
    "A `Legacy`-format row's `url` is the only real content this format "
    "offers -- there is no title, timestamp, referrer, or transition type "
    "to cross-check it against, unlike a `TabState`-format row. Do not "
    "read a `Legacy` row's tab_directory/tab_file as necessarily "
    "describing the SAME real-world tab session as a `TabState` row "
    "sharing that same tab_directory -- the numeric tab ids in each "
    "format's own filenames are independent (this device's real "
    "`custom_tabs` directory has `TabState` ids 0/2/9/11/25 alongside "
    "`Legacy` ids 16/54/1666/1366/2486 -- never the same id twice), so "
    "there is no real correspondence to draw between a `TabState` row and "
    "a `Legacy` row beyond both having been found under the same parent "
    "directory."
)
app_path = "data/data/com.android.chrome"
files = {}
optional_files = {}
existence_check_paths = ["app_tabs/"]

timestamp_fields = {
    "timestamp_epoch_s": "s",
    "timestamp_millis": "ms",
    "last_navigation_committed_timestamp_millis": "ms",
}
core_fields = ["tab_directory", "tab_file", "format", "nav_index", "url", "title", "timestamp_epoch_s"]

# Two independent shapes per real format, per direct instruction that a
# "join" (here: the outer per-tab wrapper fields and the specific
# navigation entry decoded from within it, two logically different pieces
# of the SAME physical file, at different byte ranges) needs every piece
# listed, not just the one that produced the row's own most-visible
# content. Each entry uses the ui_path_field shape (see chrome_sessions.py
# for why: a dynamically-discovered file per row, no fixed files/
# optional_files key to resolve through). "Navigation Entry" is dropped
# for a row with no decoded navigation at all (a file with zero entries)
# via presence_fields — "Tab Metadata"/"Legacy Header" always apply for
# their own format, since those fields are set on every row regardless of
# whether a navigation was also decoded. source_match keys off the row's
# own `source` field, set below to the same value as the visible `format`
# column (record_source's own source_match convention hard-codes reading
# `source`, not a parser-chosen field name).
record_source = [
    {"label": "Navigation Entry", "ui_path_field": "raw_ui_path",
     "source_match": ["TabState"], "presence_fields": ["raw_offset"]},
    {"label": "Tab Metadata", "ui_path_field": "raw_ui_path",
     "offset_field": "metadata_raw_offset", "length_field": "metadata_raw_length",
     "source_match": ["TabState"]},
    {"label": "Navigation Entry", "ui_path_field": "raw_ui_path",
     "source_match": ["Legacy"], "presence_fields": ["raw_offset"]},
    {"label": "Legacy Header", "ui_path_field": "raw_ui_path",
     "offset_field": "header_raw_offset", "length_field": "header_raw_length",
     "source_match": ["Legacy"]},
]
hidden_fields = [
    "raw_ui_path", "raw_offset", "raw_length",
    "metadata_raw_offset", "metadata_raw_length",
    "header_raw_offset", "header_raw_length",
    "source",
]


def _tab_state_row(base, res):
    row = dict(base)
    row["timestamp_millis"] = res.get("timestamp_millis")
    row["parent_id"] = res.get("parent_id")
    row["root_id"] = res.get("root_id")
    row["is_pinned"] = res.get("is_pinned")
    row["tab_has_sensitive_content"] = res.get("tab_has_sensitive_content")
    row["tab_group_id"] = res.get("tab_group_id")
    row["current_url"] = res.get("current_url")
    row["opener_app_id"] = res.get("opener_app_id")
    row["last_navigation_committed_timestamp_millis"] = res.get(
        "last_navigation_committed_timestamp_millis")
    row["metadata_raw_offset"] = res.get("metadata_raw_offset")
    row["metadata_raw_length"] = res.get("metadata_raw_length")
    return row


def run(paths):
    import chrome_tabs

    zip_names = paths.get("_zip_names") or []
    read_bytes = paths.get("_read_zip_bytes")
    app_base = paths.get("_app_base_ui_path", "")
    adapter = paths.get("_adapter")
    if not zip_names or read_bytes is None or not app_base or adapter is None:
        return []

    tabs_ui_prefix = f"{app_base}/app_tabs/"
    tabs_physical_prefix = adapter.resolve(tabs_ui_prefix.rstrip("/")) + "/"
    entry_names = [n for n in zip_names
                  if n.startswith(tabs_physical_prefix) and not n.endswith("/")]

    out = []
    for physical_path in entry_names:
        basename = physical_path.rsplit("/", 1)[-1]
        rel = physical_path[len(tabs_physical_prefix):]
        tab_directory = rel.rsplit("/", 1)[0] if "/" in rel else ""

        if basename.startswith("tab_state"):
            fmt = "Legacy"
        elif basename.startswith("tab"):
            fmt = "TabState"
        else:
            continue

        data = read_bytes(physical_path)
        if data is None:
            continue

        ui_path = f"{tabs_ui_prefix}{rel}"
        base = {
            "tab_directory": tab_directory, "tab_file": basename, "format": fmt,
            "source": fmt, "raw_ui_path": ui_path,
        }

        if fmt == "Legacy":
            res = chrome_tabs.parse_android_tab_state_legacy(data)
            if res is None:
                out.append({**base, "parse_error": "Failed to decode this file's structure"})
                continue
            base["header_raw_offset"] = res.get("header_raw_offset")
            base["header_raw_length"] = res.get("header_raw_length")
            navigations = res.get("navigations") or []
            if not navigations:
                out.append(base)
                continue
            for nav in navigations:
                row = dict(base)
                row.update(nav)
                out.append(row)
        else:
            res = chrome_tabs.parse_android_tab_state(data)
            if res is None:
                out.append({**base, "parse_error": "Failed to decode this file's structure"})
                continue
            row_with_meta = _tab_state_row(base, res)
            navigations = res.get("navigations") or []
            if not navigations:
                out.append(row_with_meta)
                continue
            for nav in navigations:
                row = dict(row_with_meta)
                row.update(nav)
                out.append(row)
    return out
