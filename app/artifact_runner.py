"""artifact_runner.py — loads and runs artifact parser scripts.

Artifact scripts live in artifacts/ios/ or artifacts/android/.
Each script exposes:
    name         (str)        — human-readable label
    target_paths (list[str])  — ui_paths of the target file(s), relative to
                                the user partition (e.g. "mobile/Library/SMS/sms.db")
    run(db)                   — receives a sqlite3.Connection; returns list[dict]

Source files are saved to:
    case_dir/artifact_parser_files/<Parser Name>/original_filename.db

Files are kept so investigators can open them directly, and re-running a
parser skips extraction if the file is already present.
"""

import importlib.util
import os
import re
import sqlite3
import zipfile

from zip_entry import ZipEntry

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


# ── Opening saved files as SQLite connections ─────────────────────────────────

def _open_db(file_path: str) -> sqlite3.Connection:
    return sqlite3.connect(file_path)


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
    target_paths = getattr(module, 'target_paths', [])
    if not target_paths:
        return [], f"{script_name}: no target_paths defined"

    parser_name = getattr(module, 'name', script_name)
    dest_dir    = _parser_files_dir(case_dir, parser_name)

    for ui_path in target_paths:
        candidates  = adapter.user_candidates(ui_path)
        filename    = os.path.basename(ui_path)
        dest_path   = os.path.join(dest_dir, filename)

        # ── Streaming index ───────────────────────────────────────────────────
        if streaming_index is not None:
            for candidate in candidates:
                if candidate not in streaming_index:
                    continue
                entry = streaming_index.get_entry(candidate)
                try:
                    _save_entry(entry, dest_path)
                    db   = _open_db(dest_path)
                    rows = module.run(db)
                    db.close()
                    return rows or [], ''
                except Exception as exc:
                    return [], f"{script_name}: {exc}"

        # ── Regular zipfile ───────────────────────────────────────────────────
        if zip_obj is not None:
            zip_names = set(zip_obj.namelist())
            for candidate in candidates:
                if candidate not in zip_names:
                    continue
                try:
                    zinfo = zip_obj.getinfo(candidate)
                    entry = ZipEntry(zip_path, candidate, zinfo)
                    _save_entry(entry, dest_path)
                    db    = _open_db(dest_path)
                    rows  = module.run(db)
                    db.close()
                    return rows or [], ''
                except Exception as exc:
                    return [], f"{script_name}: {exc}"

    return [], f"{script_name}: target file not found in archive"
