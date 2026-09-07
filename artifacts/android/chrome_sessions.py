name = "Chrome Sessions"
app_group_label = "Chrome"
group_sort_key = 18
description = (
    "Every real navigation recorded in Chrome's own tab-restore/session-"
    "restore files (`app_chrome/Default/Sessions/Session_*`/`Tabs_*`) -- "
    "the exact same SNSS binary container format desktop Chrome uses for "
    "its own \"Recently Closed\"/crash-restore history, confirmed on this "
    "device to hold real, richly-detailed per-navigation records: URL, "
    "page title, the transition type (a real click vs. a typed address vs. "
    "a redirect, and qualifiers like ForwardBack/FromApi), a real precise "
    "timestamp, and -- when the entry's own embedded PageState blob "
    "decodes -- a real referrer URL, even for a navigation whose OWN "
    "referrer_url field is empty. Parsed via the vendored `app/"
    "ccl_chromium_snss.py`/`app/ccl_chromium_pickle.py` (MIT, CCL "
    "Forensics, pinned to a specific commit of `ccl_chromium_reader` -- "
    "the same real library Hindsight, a genuine actively-maintained Chrome "
    "forensics tool, depends on for its own SNSS parsing) and `app/"
    "chrome_page_state.py` (Apache-2.0, vendored from Hindsight's own "
    "`pyhindsight/lib/page_state.py`) -- see those files' own docstrings "
    "for full provenance. `app/chrome_tabs.py` is this project's own "
    "original, Qt-free wiring code around them; no code in THIS file was "
    "copied from elsewhere. Verified directly against this project's real "
    "Android 14 JoshHickman data: one real `Tabs_*` file's own 4 real "
    "navigations exactly match Joshua Hickman's own independently "
    "documented ground-truth browsing sequence for this device (a "
    "'mobile phone forensics' Google search immediately followed by the "
    "Cellebrite mobile-forensics page, both recovered here with their "
    "real page titles and a real referrer chain confirming the second "
    "page was reached FROM the Google search results, not typed "
    "directly)."
)
warning = (
    "This report covers only Chrome's TAB-RESTORE files specifically -- "
    "not the same thing as ordinary browsing history (see the separate "
    "'Chrome Web History' report for that). A Session/Tab-restore file is "
    "written on an ordinary Chrome close/background and gets REPLACED the "
    "next time Chrome does the same -- there is no guarantee the file(s) "
    "present in this extraction still reflect every tab that was ever "
    "open on this device, only whatever was open the last time Chrome "
    "wrote one of these files before this extraction was taken. Every "
    "OTHER command type inside a real SNSS file (window/tab open-closed/"
    "pinned/grouped bookkeeping) is real signal too, but carries no url/"
    "title of its own -- this report only surfaces navigation records, "
    "not the full structural command stream."
)
app_path = "data/data/com.android.chrome"
files = {}
optional_files = {}
existence_check_paths = ["app_chrome/Default/Sessions/"]

timestamp_fields = {"timestamp_epoch_s": "s"}
core_fields = ["source_file", "nav_index", "url", "title", "timestamp_epoch_s", "transition", "referrer"]

# Each row already carries its own exact byte span (raw_offset/raw_length,
# computed in app/chrome_tabs.py from the record's own real SNSS framing)
# within a file this parser discovered dynamically -- no fixed files/
# optional_files entry exists for resolve_module_file_ui_path to key by
# (a different real file per row: Tabs_<timestamp>), so this uses the
# ui_path_field record_source shape (added 2026-09-05 alongside this
# parser) rather than the usual file_key one: the row's own raw_ui_path
# field IS the archive path to open, resolved directly, no live SQL
# lookup involved since there is no database here at all.
record_source = [
    {"label": "Navigation Entry", "ui_path_field": "raw_ui_path"},
]
hidden_fields = ["raw_ui_path", "raw_offset", "raw_length"]


def run(paths):
    import chrome_tabs

    zip_names = paths.get("_zip_names") or []
    read_bytes = paths.get("_read_zip_bytes")
    app_base = paths.get("_app_base_ui_path", "")
    adapter = paths.get("_adapter")
    if not zip_names or read_bytes is None or not app_base or adapter is None:
        return []

    sessions_ui_prefix = f"{app_base}/app_chrome/Default/Sessions/"
    sessions_physical_prefix = adapter.resolve(sessions_ui_prefix.rstrip("/")) + "/"
    entry_names = [n for n in zip_names
                  if n.startswith(sessions_physical_prefix) and not n.endswith("/")]

    out = []
    for physical_path in entry_names:
        basename = physical_path.rsplit("/", 1)[-1]
        if basename.startswith("Tabs_"):
            is_session_file = False
        elif basename.startswith("Session_"):
            is_session_file = True
        else:
            continue
        data = read_bytes(physical_path)
        if data is None:
            continue
        ui_path = f"{sessions_ui_prefix}{basename}"
        rows = chrome_tabs.parse_snss_file(data, is_session_file=is_session_file)
        for r in rows:
            r["source_file"] = basename
            r["raw_ui_path"] = ui_path
            out.append(r)
    return out
