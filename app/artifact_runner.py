"""artifact_runner.py — loads and runs artifact parser scripts.

Artifact scripts live in artifacts/ios/ or artifacts/android/.

Two script APIs are supported:

Single-file (legacy, iOS-style):
    name         str        — human-readable label
    target_paths list[str]  — one or more ui_paths tried in order
                              (e.g. "mobile/Library/SMS/sms.db")
    run(db)                 — receives a sqlite3.Connection; returns list[dict]

Multi-file (new, for scripts that need several databases):
    name      str            — human-readable label
    app_path  str            — base path of the app within the user partition
                               (e.g. "data/data/com.whatsapp"). Android
                               package dirs and fixed iOS OS-level paths
                               (e.g. "mobile/Library/SMS") only — a
                               THIRD-PARTY iOS app's own container is
                               GUID-named per install, not usable here;
                               such a module declares app_group instead
                               (see below), never both.
    app_group str            — alternative to app_path, third-party iOS
                               apps only: the app's stable App Group
                               identifier (e.g.
                               "group.net.whatsapp.WhatsApp.shared"),
                               resolved to this device's actual GUID-named
                               container via the case's own guid_to_bundle
                               map (see _resolve_app_group_base). Confirm
                               the exact string against a real
                               extraction's guid_to_bundle first — it is
                               NOT the app's bundle id, and not guessable
                               (e.g. WhatsApp's own bundle id is
                               net.whatsapp.WhatsApp, a different string
                               from its App Group above).
    files     dict[str,str]  — {key: subpath} where subpath is relative to
                               app_path/app_group (e.g. {"msgstore": "databases/msgstore.db"})
    optional_files dict[str,str]
                             — like files, but extracted only when present in the
                               archive; a missing optional file is not an error
                               and its key is simply absent from paths
                               (e.g. {"wal": "Photos.sqlite-wal"})
    run(paths)               — receives dict[str, str] mapping each key to the
                               extracted file's path on disk; returns list[dict]

Optional module-level attributes, read by app/artifact_viewer.py:
    description str — where this data comes from and any known reliability
                       caveats; shown on the report's "Report Notes/Warning"
                       tree entry (not inline on the report itself — a long
                       description there used to push the table down/off-
                       screen).
    warning     str — separate from description; something the examiner
                       must read before trusting the report (e.g. a
                       carving/recovery limitation, not a routine note).
                       Styled in bold red on the same Notes/Warning page,
                       above description. Most scripts don't need one —
                       reserve it for a genuine "read this or you'll
                       misinterpret the data" caveat, not general notes.
    timestamp_fields dict[str, str] — {field_name: unit_code} for every
                       timestamp-shaped output field (unit_code one of
                       "s"/"ms"/"cocoa_s"/"cocoa_ns"); the field must hold
                       the raw numeric value, never a pre-formatted string
                       — the Report table formats it per the case's
                       UTC/handset/acquisition setting at display time (see
                       CLAUDE.md's timestamp conventions).
    recoverable_tables list[str] — declarative only, names tables this
                       script's recovery pass (sqlite_carve) can carve from;
                       no recovery code belongs on the module itself.

Source files are saved to:
    case_dir/artifact_parser_files/<Parser Name>/original_filename

Files are kept so investigators can open them directly, and re-running a
parser skips extraction if the file is already present.
"""

import importlib.util
import os
import re
import sqlite3
import sys
import zipfile

from zip_entry import ZipEntry

if getattr(sys, 'frozen', False):
    # PyInstaller bundle: data files land in sys._MEIPASS
    _ARTIFACTS_DIR = os.path.join(sys._MEIPASS, 'artifacts')
else:
    # Source layout: artifacts/ sits two levels above app/artifact_runner.py
    _ARTIFACTS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'artifacts'
    )


# ── Loading ───────────────────────────────────────────────────────────────────

def load_artifacts(platform: str) -> tuple[list[tuple[str, object]], list[tuple[str, str]]]:
    """Load all artifact scripts for a platform.

    Returns (modules, errors):
      modules — [(script_name, module), ...] for scripts that imported cleanly.
      errors  — [(filename, message), ...] for scripts that failed to load,
                e.g. a stdlib dependency that wasn't bundled into the frozen
                build.  These are reported so the user knows a parser is
                unavailable instead of it silently vanishing from the list.
    """
    plat_dir = os.path.join(_ARTIFACTS_DIR, platform)
    modules: list[tuple[str, object]] = []
    errors:  list[tuple[str, str]]    = []
    if not os.path.isdir(plat_dir):
        return modules, errors
    for fname in sorted(os.listdir(plat_dir)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        path = os.path.join(plat_dir, fname)
        script_name = fname[:-3]
        try:
            spec = importlib.util.spec_from_file_location(script_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            modules.append((script_name, mod))
        except Exception as exc:
            errors.append((fname, f"{type(exc).__name__}: {exc}"))
            print(f"[artifact_runner] Could not load {fname}: {exc}")
    return modules, errors


def list_artifacts(platform: str) -> list[tuple[str, object]]:
    """Return [(script_name, module), ...] for all valid scripts in artifacts/<platform>/.

    Thin wrapper over load_artifacts() for callers that don't need load errors."""
    return load_artifacts(platform)[0]


# ── File saving ───────────────────────────────────────────────────────────────

def safe_folder_name(name: str) -> str:
    """Convert a parser name to a safe folder name e.g. 'SMS Messages' → 'SMS_Messages'."""
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


def _parser_files_dir(case_dir: str, parser_name: str) -> str:
    """Return (and create) case_dir/artifact_parser_files/<Parser_Name>/."""
    folder = os.path.join(case_dir, 'artifact_parser_files', safe_folder_name(parser_name))
    os.makedirs(folder, exist_ok=True)
    return folder


def _save_entry(zip_entry: ZipEntry, dest_path: str) -> None:
    """Write zip entry bytes to dest_path (skipped if file already exists)."""
    if os.path.exists(dest_path):
        return
    data = zip_entry.read()
    with open(dest_path, 'wb') as f:
        f.write(data)


def _extract_candidate(
    candidates: list[str],
    zip_path: str,
    dest_path: str,
    zip_obj: zipfile.ZipFile | None,
) -> bool:
    """Try each candidate path and extract the first match to dest_path.

    Returns True if a file was found and saved (or already existed), False if
    none of the candidates exist in the archive.
    """
    if zip_obj is not None:
        zip_names = set(zip_obj.namelist())
        for candidate in candidates:
            if candidate not in zip_names:
                continue
            zinfo = zip_obj.getinfo(candidate)
            _save_entry(ZipEntry(zip_path, candidate, zinfo), dest_path)
            return True

    return False


def _recover_deleted_rows(paths: dict, table: str, field_notes: dict) -> list[dict]:
    """One entry of a parser's `recoverable_tables` -> extra rows from
    app/sqlite_carve.py's freeblock/freed-page/WAL-history scan, or [] if
    nothing survived, the module can't be imported, or anything about the
    attempt failed. A parser declares only the table name (see viber.py /
    burner.py for the one-line convention) — no recovery code belongs in
    an artifact script; this is the only call site. *field_notes* is the
    matching entry (if any) of a parser's optional `recovery_field_notes`
    — also declarative, a caveat string per column the parser's own
    cross-checking found unreliable, attached to the recovered row rather
    than silently dropped or (worse) "corrected" to a guess."""
    try:
        import sqlite_carve
    except ImportError:
        return []
    return sqlite_carve.recover_deleted_rows(paths, table, field_notes)


# ── Running ───────────────────────────────────────────────────────────────────

def _resolve_app_group_base(app_group: str, guid_to_bundle: dict) -> str | None:
    """iOS third-party app data lives under a GUID-named container
    (Containers/Data/Application/<GUID>/… for the app's own sandbox, or
    Containers/Shared/AppGroup/<GUID>/… for data an app explicitly shares
    via an App Group entitlement — the GUID is random per install, never
    the same across extractions, so a module can't hardcode it the way
    Android's package-name-based app_path works. A module declares the
    App Group's own stable bundle-style identifier instead (e.g.
    "group.net.whatsapp.WhatsApp.shared", confirmed against a real
    extraction's guid_to_bundle map — see artifacts/ios/whatsapp.py) and
    this reverse-looks-up which GUID that resolved to on THIS device.
    guid_to_bundle is {guid: bundle_id}, already built once at case-load
    (FastZipBrowser.guid_to_bundle) from Cellebrite's own MCM metadata —
    no new resolution mechanism, just reusing it here."""
    for guid, bundle_id in (guid_to_bundle or {}).items():
        if bundle_id == app_group:
            return f"mobile/Containers/Shared/AppGroup/{guid}"
    return None


def resolve_module_file_ui_path(module, file_key: str, guid_to_bundle: dict | None) -> str | None:
    """Rebuild the archive ui_path for one of a module's declared
    `files`/`optional_files` entries, without extracting or touching disk —
    used by the Artifact Viewer's "jump to record in hex" feature
    (`record_source` on a module) to re-read a source database's CURRENT
    archive bytes at click time, long after the parser run that populated
    the report table. Deliberately never returns a path to the cached
    extracted copy in artifact_parser_files/ — that copy was opened by the
    parser's own (non-read-only) sqlite3.connect() and could have been
    checkpointed since, where the archive entry itself never changes.
    Mirrors run_artifact's own app_base/ui_path join; returns None if the
    module uses the single-file `target_paths` API instead (nothing to key
    by there) or the key isn't declared."""
    has_app_path  = hasattr(module, 'app_path')  and hasattr(module, 'files')
    has_app_group = hasattr(module, 'app_group') and hasattr(module, 'files')
    if not (has_app_path or has_app_group):
        return None
    if has_app_path:
        app_base = module.app_path.strip('/')
    else:
        app_base = _resolve_app_group_base(module.app_group, guid_to_bundle or {})
        if app_base is None:
            return None
    subpath = module.files.get(file_key)
    if subpath is None:
        subpath = getattr(module, 'optional_files', {}).get(file_key)
    if subpath is None:
        return None
    return f"{app_base}/{subpath.lstrip('/')}"


def run_artifact(
    script_name: str,
    module,
    zip_path: str,
    adapter,
    case_dir: str,
    zip_obj: zipfile.ZipFile | None = None,
    guid_to_bundle: dict | None = None,
) -> tuple[list[dict], str]:
    """Run one artifact parser against the open archive.

    Source files are saved to case_dir/artifact_parser_files/<name>/ and
    kept for direct inspection.  Extraction is skipped if the file exists.

    Returns (rows, error). On success rows is a list[dict] — the parser's
    own query result plus whatever its `recoverable_tables` declaration
    turned up, appended after. A script wanting recovered/carved content
    kept as its own separate, independently-trusted report (rather than
    blended into a normal query's output) should be its own separate
    script whose `run()` returns [] and whose entire contribution is its
    `recoverable_tables` declaration — see e.g.
    artifacts/android/burner_messageentity.py. On failure rows is [] and
    error describes what went wrong.
    """
    parser_name = getattr(module, 'name', script_name)
    dest_dir    = _parser_files_dir(case_dir, parser_name)

    # ── Multi-file API ────────────────────────────────────────────────────────
    has_app_path  = hasattr(module, 'app_path')  and hasattr(module, 'files')
    has_app_group = hasattr(module, 'app_group') and hasattr(module, 'files')
    if has_app_path or has_app_group:
        if has_app_path:
            app_base = module.app_path.strip('/')
        else:
            app_base = _resolve_app_group_base(module.app_group, guid_to_bundle)
            if app_base is None:
                return [], f"{script_name}: app group '{module.app_group}' not present on this device"
        paths: dict[str, str] = {}
        # Reserved key (leading underscore — never a real `files`/
        # `optional_files` entry, whose keys are chosen by the module
        # author): the container's own ui_path, for a parser that needs to
        # build a *reference* to some other file inside the container
        # rather than extract it directly — e.g. an attachment path stored
        # in a database column, which artifact_media.py resolves later at
        # display time via the app's normal zip-reading path, never copied
        # to disk by the parser itself. See media_fields on the module and
        # artifacts/ios/whatsapp.py's `attachment_path` for the convention.
        paths['_app_base_ui_path'] = app_base
        for key, subpath in module.files.items():
            ui_path    = f"{app_base}/{subpath.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(subpath))
            if not _extract_candidate(candidates, zip_path, dest_path, zip_obj):
                return [], f"{script_name}: file not found: {ui_path}"
            paths[key] = dest_path
        for key, subpath in getattr(module, 'optional_files', {}).items():
            ui_path    = f"{app_base}/{subpath.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(subpath))
            if _extract_candidate(candidates, zip_path, dest_path, zip_obj):
                paths[key] = dest_path
        try:
            rows = module.run(paths) or []
        except Exception as exc:
            return [], f"{script_name}: {exc}"
        all_notes = getattr(module, 'recovery_field_notes', {})
        for table in getattr(module, 'recoverable_tables', ()):
            rows = rows + _recover_deleted_rows(paths, table, all_notes.get(table, {}))
        return rows, ''

    # ── Single-file API (legacy) ──────────────────────────────────────────────
    target_paths = getattr(module, 'target_paths', [])
    if not target_paths:
        return [], f"{script_name}: no target_paths or files defined"

    for ui_path in target_paths:
        candidates = adapter.user_candidates(ui_path)
        dest_path  = os.path.join(dest_dir, os.path.basename(ui_path))
        if not _extract_candidate(candidates, zip_path, dest_path, zip_obj):
            continue
        try:
            db   = sqlite3.connect(dest_path)
            rows = module.run(db)
            db.close()
            return rows or [], ''
        except Exception as exc:
            return [], f"{script_name}: {exc}"

    return [], f"{script_name}: target file not found in archive"
