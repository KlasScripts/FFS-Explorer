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
                               (e.g. "data/data/com.whatsapp")
    files     dict[str,str]  — {key: subpath} where subpath is relative to
                               app_path (e.g. {"msgstore": "databases/msgstore.db"})
    optional_files dict[str,str]
                             — like files, but extracted only when present in the
                               archive; a missing optional file is not an error
                               and its key is simply absent from paths
                               (e.g. {"wal": "Photos.sqlite-wal"})
    run(paths)               — receives dict[str, str] mapping each key to the
                               extracted file's path on disk; returns list[dict]

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

def list_artifacts(platform: str) -> list[tuple[str, object]]:
    """Return [(script_name, module), ...] for all valid scripts in artifacts/<platform>/."""
    plat_dir = os.path.join(_ARTIFACTS_DIR, platform)
    results = []
    if not os.path.isdir(plat_dir):
        return results
    for fname in sorted(os.listdir(plat_dir)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        path = os.path.join(plat_dir, fname)
        script_name = fname[:-3]
        try:
            spec = importlib.util.spec_from_file_location(script_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            results.append((script_name, mod))
        except Exception as exc:
            print(f"[artifact_runner] Could not load {fname}: {exc}")
    return results


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
    streaming_index,
) -> bool:
    """Try each candidate path and extract the first match to dest_path.

    Returns True if a file was found and saved (or already existed), False if
    none of the candidates exist in the archive.
    """
    if streaming_index is not None:
        for candidate in candidates:
            if candidate not in streaming_index:
                continue
            _save_entry(streaming_index.get_entry(candidate), dest_path)
            return True

    if zip_obj is not None:
        zip_names = set(zip_obj.namelist())
        for candidate in candidates:
            if candidate not in zip_names:
                continue
            zinfo = zip_obj.getinfo(candidate)
            _save_entry(ZipEntry(zip_path, candidate, zinfo), dest_path)
            return True

    return False


# ── Running ───────────────────────────────────────────────────────────────────

def run_artifact(
    script_name: str,
    module,
    zip_path: str,
    adapter,
    case_dir: str,
    zip_obj: zipfile.ZipFile | None = None,
    streaming_index=None,
) -> tuple[list[dict], str]:
    """Run one artifact parser against the open archive.

    Source files are saved to case_dir/artifact_parser_files/<name>/ and
    kept for direct inspection.  Extraction is skipped if the file exists.

    Returns (rows, error).  On success rows is a list[dict] and error is ''.
    On failure rows is [] and error describes what went wrong.
    """
    parser_name = getattr(module, 'name', script_name)
    dest_dir    = _parser_files_dir(case_dir, parser_name)

    # ── Multi-file API ────────────────────────────────────────────────────────
    if hasattr(module, 'app_path') and hasattr(module, 'files'):
        app_base = module.app_path.strip('/')
        paths: dict[str, str] = {}
        for key, subpath in module.files.items():
            ui_path    = f"{app_base}/{subpath.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(subpath))
            if not _extract_candidate(candidates, zip_path, dest_path, zip_obj, streaming_index):
                return [], f"{script_name}: file not found: {ui_path}"
            paths[key] = dest_path
        for key, subpath in getattr(module, 'optional_files', {}).items():
            ui_path    = f"{app_base}/{subpath.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(subpath))
            if _extract_candidate(candidates, zip_path, dest_path, zip_obj, streaming_index):
                paths[key] = dest_path
        try:
            return module.run(paths) or [], ''
        except Exception as exc:
            return [], f"{script_name}: {exc}"

    # ── Single-file API (legacy) ──────────────────────────────────────────────
    target_paths = getattr(module, 'target_paths', [])
    if not target_paths:
        return [], f"{script_name}: no target_paths or files defined"

    for ui_path in target_paths:
        candidates = adapter.user_candidates(ui_path)
        dest_path  = os.path.join(dest_dir, os.path.basename(ui_path))
        if not _extract_candidate(candidates, zip_path, dest_path, zip_obj, streaming_index):
            continue
        try:
            db   = sqlite3.connect(dest_path)
            rows = module.run(db)
            db.close()
            return rows or [], ''
        except Exception as exc:
            return [], f"{script_name}: {exc}"

    return [], f"{script_name}: target file not found in archive"
