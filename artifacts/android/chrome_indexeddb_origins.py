name = "Chrome IndexedDB Origins"
app_group_label = "Chrome"
group_sort_key = 15
description = (
    "Every origin Chrome has an IndexedDB store for (app_chrome/Default/"
    "IndexedDB/) -- each subdirectory is named directly after the "
    "origin that owns it (\"<scheme>_<host>_<port>.indexeddb.leveldb\" "
    "or \"...indexeddb.blob\" for large attached blobs), so the mere "
    "PRESENCE of a directory is real evidence Chrome interacted with "
    "that origin's own web app code deeply enough to open a database -- "
    "independent of History, and independent of Chrome Cache too. "
    "Directory-name-only: this parser does NOT read the LevelDB content "
    "inside each directory (SSTable format, no parsing library "
    "currently in this project's dependencies -- see CLAUDE.md) -- only "
    "confirms the origin exists, not the actual stored data. "
    "Real, verified on this project's own Android 14 JoshHickman case: "
    "3 real origins -- cellebrite.com, www.mlb.com, www.npr.org -- all "
    "three corroborate real activity already visible elsewhere in this "
    "Chrome group (Chrome Web History and/or Chrome Cache's own "
    "reconstructed articles on the same exact sites, checked directly "
    "rather than assumed) rather than surfacing a site invisible "
    "everywhere else on this specific case; still worth its own report, "
    "since that correlation won't hold on every case."
)
warning = (
    "IndexedDB is a persistent per-origin database that a site's own "
    "JavaScript opens explicitly -- its presence proves Chrome loaded "
    "that origin's own script and that script chose to use IndexedDB "
    "(common for PWAs, offline support, some analytics/consent-"
    "management scripts), not that the user necessarily saw or acted on "
    "a full page there. A directory an origin used and later cleared/"
    "evicted is simply gone -- absence here is not proof of no contact."
)
app_path = "data/data/com.android.chrome"
files = {}
optional_files = {}
existence_check_paths = ["app_chrome/Default/IndexedDB"]

core_fields = ["origin", "storage_kind"]


def _parse_origin_dir(dirname: str):
    """"https_www.example.com_0.indexeddb.leveldb" ->
    ("https://www.example.com", "leveldb"). Returns (None, None) for
    anything not matching this exact real naming convention (Chrome's
    own storage::GetIdentifierFromOrigin encoding) rather than guessing."""
    import re

    m = re.match(r'^(https?|file|chrome-extension)_(.+)_(\d+)\.indexeddb\.(leveldb|blob)$', dirname)
    if not m:
        return None, None
    scheme, host, port, kind = m.groups()
    origin = f"{scheme}://{host}" + (f":{port}" if port not in ("0", "") else "")
    return origin, kind


def run(paths):
    zip_names = paths.get("_zip_names") or []
    app_base = paths.get("_app_base_ui_path", "")
    adapter = paths.get("_adapter")
    if not zip_names or not app_base or adapter is None:
        return []

    indexeddb_ui_prefix = f"{app_base}/app_chrome/Default/IndexedDB/"
    indexeddb_physical_prefix = adapter.resolve(indexeddb_ui_prefix.rstrip("/")) + "/"

    dirnames: set[str] = set()
    for n in zip_names:
        if not n.startswith(indexeddb_physical_prefix):
            continue
        rest = n[len(indexeddb_physical_prefix):]
        if not rest:
            continue
        dirnames.add(rest.split("/")[0])

    out = []
    for dirname in sorted(dirnames):
        origin, kind = _parse_origin_dir(dirname)
        if origin is None:
            continue
        out.append({
            "origin": origin,
            "storage_kind": kind,
            "raw_directory_name": dirname,
        })
    return out
