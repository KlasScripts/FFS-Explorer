"""parser_versions.py — automatic version tracking for artifact parser
scripts.

A fast content hash of each parser's own .py source detects when it has
changed since the app last saw it, auto-incrementing a per-parser version
counter — so a report opened later (built by an OLDER version) can tell
the examiner a newer parser is available. The version NUMBER is always
derived purely from the hash, never hand-authored. A CHANGELOG entry is a
separate, optional, human-written note about why a specific version
changed (see record_changelog()) — a version bump with no matching
changelog entry means exactly that: the script changed and nobody
recorded why, surfaced honestly rather than guessed at.

Global (cross-case) JSON store, same dev/frozen-path convention and
mtime+size load cache as research_store.py, keyed "{platform}:{script_name}"
(matching validation_store.py, since e.g. ios:whatsapp/android:whatsapp
are different parsers sharing a filename).
"""

import hashlib
import json
import os
import sys

_cache = {"stat": None, "data": None}

# Hashed/version-checked at most once per (platform, script_name) per
# process — load_artifacts() is called far more often than that (every
# report open, every tree refresh), and re-hashing a file it already
# checked this session would be pure waste. Cleared only by restarting
# the app, which is exactly when a changed-on-disk script would need
# re-checking anyway (a running process already has the old module
# imported in memory regardless of what's on disk now).
_checked_this_session: set[str] = set()


def store_path() -> str:
    """Location of parser_versions.json (next to exe when frozen, else config/)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "parser_versions.json")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "config", "parser_versions.json")


def _load() -> dict:
    """Return {key: {hash, version, changelog}}; cached by file mtime+size."""
    path = store_path()
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache["stat"], _cache["data"] = None, {}
        return {}
    if _cache["stat"] == stat and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        parsers = raw.get("parsers", {})
        data = {k: v for k, v in parsers.items() if isinstance(v, dict)}
    except Exception:
        data = {}
    _cache["stat"], _cache["data"] = stat, data
    return data


def _save(parsers: dict) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = {
        "_note": ("Auto-generated: 'version'/'hash' are derived purely from "
                  "each parser script's own content and should not be hand-"
                  "edited. 'changelog' entries are the only part meant for "
                  "manual editing — see parser_versions.py."),
        "format_version": 1,
        "parsers": parsers,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    _cache["stat"] = None  # force re-read next _load()


def _hash_source(path: str) -> str | None:
    """Fast, non-cryptographic content hash of a parser script's raw bytes
    — change detection only, never a security use, so a short blake2b
    digest (stdlib, no dependency) is plenty. These are small (a few KB)
    source files; even hashing on every call would be cheap, but
    check_version() below still only ever does it once per script per
    process (see _checked_this_session) so this never touches startup
    time in any measurable way."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return hashlib.blake2b(data, digest_size=8).hexdigest()


def check_version(platform: str, script_name: str, source_path: str) -> None:
    """Called once per (platform, script_name) per process — see
    artifact_runner.load_artifacts(), the only real call site, which
    already has *source_path* on hand for every script it imports. A
    hash mismatch against the last-seen value (or no prior record at
    all) bumps the stored version; an unchanged hash does nothing."""
    key = f"{platform}:{script_name}"
    if key in _checked_this_session:
        return
    _checked_this_session.add(key)
    current_hash = _hash_source(source_path)
    if current_hash is None:
        return
    parsers = _load()
    entry = parsers.get(key)
    if entry is None:
        parsers[key] = {"hash": current_hash, "version": 1, "changelog": {}}
        _save(parsers)
        return
    if entry.get("hash") != current_hash:
        entry["version"] = int(entry.get("version", 1)) + 1
        entry["hash"] = current_hash
        _save(parsers)


def get_status(platform: str, script_name: str) -> dict | None:
    """{'hash': str, 'version': int, 'changelog': {"<version>": str}} for a
    known parser, or None if check_version() has never run for it (should
    only happen if load_artifacts() hasn't loaded this script yet)."""
    return _load().get(f"{platform}:{script_name}")


def get_current_version(platform: str, script_name: str) -> int | None:
    entry = get_status(platform, script_name)
    return entry.get("version") if entry else None


def get_changelog_entry(platform: str, script_name: str, version) -> str | None:
    entry = get_status(platform, script_name)
    if not entry:
        return None
    return entry.get("changelog", {}).get(str(version))


def record_changelog(platform: str, script_name: str, description: str) -> bool:
    """Attach a human-authored description to the CURRENT version of a
    parser. Call this deliberately right after intentionally editing a
    parser script — see the standing instruction in CLAUDE.md — so an
    examiner later offered the update sees WHY it changed, not just that
    it did. Never called automatically. Returns False (no-op) if the
    parser has no recorded version yet (check_version() hasn't run for it
    this session — call list_artifacts(platform) first)."""
    key = f"{platform}:{script_name}"
    parsers = _load()
    entry = parsers.get(key)
    if entry is None:
        return False
    entry.setdefault("changelog", {})[str(entry["version"])] = description
    _save(parsers)
    return True
