"""Files in Android's shared trash, from MediaProvider's external.db.

A file moved to the trash stays where it was and is renamed
.trashed-<expiry>-<name>. The files table keeps is_trashed=1 and
date_expires, the same number as the name, in Unix seconds. AOSP sets the
expiry to the trash time plus 30 days (FileUtils DEFAULT_DURATION_TRASHED),
so the trash time is the expiry minus 30 days. A .pending- file is an
unfinished write and expires 7 days after it was started.

The expiry can be moved. MediaProvider's idle maintenance renames an item
that expired more than a week ago to now plus 7 days. The media store
recovery log (data/media/0/.transforms/recovery/leveldb-<volume>) records
each name a file had, with the media store _id in the value, so an earlier
.trashed- name for the same _id shows the extension, and the first name of
that run gives the original trash time.

Three other sources are matched by time or file:
- Google Photos' action_queue (gphotos-1.db): a
  ...trash.delete.move_to_trash_optimistic_action row is written when the
  user moves items to the trash in Photos. Its entity_blob lists each
  item's dedup_key, which is the base64url SHA-1 of the file, and
  local_media gives the dedup_key of a media store id.
- Files by Google's activityTimeLogStoreName.pb: start and end of each
  session the app was open.
- Usage Stats: which app's activity was on screen at the trash time.

Verified on the lab emulator (Android 16, Photos group trash, Files by
Google group trash, idle maintenance extension and purge) and on the
Xiaomi Poco X7 Pro image (Android 15).
"""

import base64
import bisect
import hashlib
import os
import re

name = "Media Trash"
warning = (
    "Experimental. Trashed Time is worked out from the expiry, not stored. "
    "It assumes Android's 30-day trash period; a phone maker can change "
    "it. Photos, Files by Google and Usage Stats are matched by time or "
    "file and are supporting evidence, not proof of who trashed a file. "
    "A blank column means nothing was found, not that nothing happened."
)
description = (
    "Every media store row with an expiry date: files in the trash and "
    "unfinished (pending) writes, plus trashed files Google Photos or the "
    "media store recovery log still lists after the media store dropped "
    "them. Trashed Time is the expiry minus 30 days, or the first expiry "
    "of the current trash in the recovery log when Android extended it. "
    "Photos Trash Action is the time Photos queued a move to trash that "
    "lists this file. Files Open From/To is a Files by Google session "
    "that covers the trash time. App On Screen is the app whose screen "
    "Usage Stats shows at that time."
)
app_path = "data/data/com.google.android.providers.media.module"
files = {
    "external_db": "databases/external.db",
}
optional_files = {
    "external_db_wal": "databases/external.db-wal",
    "external_db_shm": "databases/external.db-shm",
}
timestamp_fields = {
    "trashed_time": "s",
    "photos_action_time": "ms",
    "files_open_from": "ms",
    "files_open_to": "ms",
    "expires": "s",
    "date_added": "s",
    "date_modified": "s",
}
core_fields = [
    "trashed_time", "status", "file_name", "original_path", "expiry_extended",
    "photos_action_time", "photos_action_files", "app_on_screen",
    "files_open_from", "files_open_to", "created_by",
]
media_fields = ["file_link"]
hidden_fields = ["raw_files_id"]
record_source = [
    {"label": "Media store row", "file_key": "external_db", "table": "files",
     "rowid_fields": ["raw_files_id"], "presence_fields": ["raw_files_id"]},
]

_TRASH_DAYS_S = 30 * 86400
_NAME_RE = re.compile(r"^\.(trashed|pending)-(\d+)-(.+)$")
_PHOTOS_DB = "com.google.android.apps.photos/databases/gphotos-1.db"
_FILES_LOG = "com.google.android.apps.nbu.files/files/activityTimeLogStoreName.pb"
_RECOVERY = "/.transforms/recovery/leveldb-"
_HASH_LIMIT = 512 * 1024 * 1024


def _split_name(device_path: str) -> tuple[str, str, int | None, str]:
    """(folder, kind, expiry, original name) for a media path."""
    folder, _, base = (device_path or "").rpartition("/")
    m = _NAME_RE.match(base)
    if m:
        return folder, m.group(1), int(m.group(2)), m.group(3)
    return folder, "", None, base


def _original_path(device_path: str) -> str:
    folder, kind, _, base = _split_name(device_path)
    if not kind:
        return device_path or ""
    return f"{folder}/{base}" if folder else base


def _pick(names: list[str], suffix: str) -> str:
    from artifact_runner import is_capture_alias
    found = [n for n in names if n.endswith(suffix) and not is_capture_alias(n)]
    found.sort(key=lambda n: (0 if "/data/data/" in n or n.startswith("data/data/") else 1, len(n)))
    return found[0] if found else ""


def _copy_out(paths: dict, entry: str, subdir: str, sidecars=("",)) -> str:
    read = paths.get("_read_zip_bytes")
    base = paths.get("_parser_files_dir")
    if not entry or read is None:
        return ""
    if not base:
        import tempfile
        base = tempfile.mkdtemp(prefix="ffs_media_trash_")
    dest_dir = os.path.join(base, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    first = ""
    for sfx in sidecars:
        data = read(entry + sfx)
        if data is None:
            continue
        dest = os.path.join(dest_dir, entry.rsplit("/", 1)[-1] + sfx)
        if not os.path.exists(dest):
            with open(dest, "wb") as f:
                f.write(data)
        if sfx == "":
            first = dest
    return first


# ── Recovery log ──────────────────────────────────────────────────────────────

def _recovery_records(paths: dict) -> list[tuple[int, str, int]] | None:
    """(seq, device path, media store _id) for every live recovery-log record
    whose value names an _id. None when the archive has no recovery log."""
    from artifact_runner import is_capture_alias, log_note
    names = paths.get("_zip_names") or []
    read = paths.get("_read_zip_bytes")
    dirs: dict[str, list[str]] = {}
    for n in names:
        if _RECOVERY not in n or n.endswith("/") or is_capture_alias(n):
            continue
        folder = n.rsplit("/", 1)[0]
        label = folder.rsplit("/", 1)[-1]
        if label == "leveldb-internal":
            continue
        dirs.setdefault(folder, []).append(n)
    if not dirs or read is None:
        return None
    chosen: dict[str, str] = {}
    for folder in sorted(dirs, key=lambda f: (0 if "data/media/0/" in f else 1, len(f))):
        chosen.setdefault(folder.rsplit("/", 1)[-1], folder)
    base = paths.get("_parser_files_dir")
    if not base:
        import tempfile
        base = tempfile.mkdtemp(prefix="ffs_media_trash_")
    import ccl_leveldb
    out: list[tuple[int, str, int]] = []
    for label, folder in chosen.items():
        dest = os.path.join(base, "recovery", label)
        os.makedirs(dest, exist_ok=True)
        for n in dirs[folder]:
            target = os.path.join(dest, n.rsplit("/", 1)[-1])
            if os.path.exists(target):
                continue
            data = read(n)
            if data is not None:
                with open(target, "wb") as f:
                    f.write(data)
        try:
            db = ccl_leveldb.RawLevelDb(dest)
        except Exception as exc:
            log_note(f"{folder}: recovery log not opened ({exc}); Expiry Extended is blank for it")
            continue
        try:
            for rec in db.iterate_records_raw():
                if not rec.value:
                    continue
                parts = rec.value.decode("utf-8", "replace").split("::")
                if len(parts) < 2 or not parts[1].isdigit():
                    continue
                out.append((rec.seq, rec.user_key.decode("utf-8", "replace"), int(parts[1])))
        except Exception as exc:
            log_note(f"{folder}: recovery log not fully read ({exc})")
        finally:
            db.close()
    return out


def _trash_history(records: list[tuple[int, str, int]]) -> dict[int, dict]:
    """Per _id: the expiries of the latest unbroken run of .trashed- names
    (an extension renames trashed to trashed), and how many earlier runs
    ended in a plain name (a restore)."""
    by_id: dict[int, list[tuple[int, str]]] = {}
    for seq, key, file_id in records:
        by_id.setdefault(file_id, []).append((seq, key))
    history: dict[int, dict] = {}
    for file_id, recs in by_id.items():
        recs.sort()
        runs: list[list[int]] = []
        last_kind = None
        for _, key in recs:
            folder, kind, expiry, base = _split_name(key)
            if kind == "trashed":
                if last_kind != "trashed":
                    runs.append([])
                if not runs[-1] or runs[-1][-1] != expiry:
                    runs[-1].append(expiry)
            last_kind = kind or "plain"
            history.setdefault(file_id, {})["last_path"] = key
        if runs:
            history[file_id]["run"] = runs[-1]
            history[file_id]["earlier_runs"] = len(runs) - 1
            history[file_id]["still_trashed"] = last_kind == "trashed"
    return history


# ── Google Photos ─────────────────────────────────────────────────────────────

def _protobuf_fields(data: bytes) -> list[tuple[int, object]]:
    out = []
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            length, i = _varint(data, i)
            if i + length > len(data):
                raise ValueError("truncated length")
            value = data[i:i + length]
            i += length
        elif wire == 1:
            value = data[i:i + 8]
            i += 8
        elif wire == 5:
            value = data[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((field, value))
    return out


def _varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while i < len(data) and shift <= 70:
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7
    raise ValueError("truncated varint")


def _photos(paths: dict) -> tuple[dict, dict, list]:
    """(local_media by media store id, trash actions by dedup key,
    trashed local_media rows) from gphotos-1.db."""
    from artifact_runner import log_note, open_db_readonly
    entry = _pick(paths.get("_zip_names") or [], _PHOTOS_DB)
    db_path = _copy_out(paths, entry, "photos", ("", "-wal", "-shm"))
    if not db_path:
        return {}, {}, []
    try:
        conn = open_db_readonly(db_path)
    except Exception as exc:
        log_note(f"{entry}: not opened ({exc}); Photos columns are blank")
        return {}, {}, []
    by_store_id: dict[int, dict] = {}
    actions: dict[str, list[dict]] = {}
    trashed: list[dict] = []
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "local_media" in tables:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(local_media)")}
            want = ["media_store_id", "dedup_key", "trash_timestamp", "purge_timestamp", "filepath", "filename"]
            if {"media_store_id", "dedup_key"} <= cols:
                sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
                for r in conn.execute(f"SELECT {sel} FROM local_media WHERE media_store_id IS NOT NULL"):
                    row = dict(r)
                    by_store_id[row["media_store_id"]] = row
                    if row["trash_timestamp"]:
                        trashed.append(row)
        if "action_queue" in tables:
            for r in conn.execute(
                    "SELECT rowid, entity_type, entity_blob, creation_timestamp FROM action_queue "
                    "WHERE entity_type LIKE '%trash%'"):
                try:
                    keys = [v.decode("ascii") for f, v in _protobuf_fields(r["entity_blob"] or b"")
                            if f == 2 and isinstance(v, bytes)]
                except (ValueError, UnicodeDecodeError) as exc:
                    log_note(f"Photos action_queue row {r['rowid']}: not decoded ({exc})")
                    continue
                action = {"time": r["creation_timestamp"], "type": r["entity_type"], "count": len(keys)}
                for k in keys:
                    actions.setdefault(k, []).append(action)
    finally:
        conn.close()
    return by_store_id, actions, trashed


def _dedup_key(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha1(data).digest()).decode("ascii").rstrip("=")


# ── Files by Google ───────────────────────────────────────────────────────────

def _files_sessions(paths: dict) -> list[tuple[int, int]]:
    """(start ms, end ms) for each Files by Google session, sorted."""
    from artifact_runner import log_note
    entry = _pick(paths.get("_zip_names") or [], _FILES_LOG)
    read = paths.get("_read_zip_bytes")
    if not entry or read is None:
        return []
    data = read(entry) or b""

    def stamp(raw) -> int | None:
        if not isinstance(raw, bytes):
            return None
        parts = dict(_protobuf_fields(raw))
        seconds = parts.get(1)
        if not isinstance(seconds, int):
            return None
        nanos = parts.get(2) if isinstance(parts.get(2), int) else 0
        return seconds * 1000 + nanos // 1_000_000

    sessions = []
    try:
        for field, value in _protobuf_fields(data):
            if field != 1 or not isinstance(value, bytes):
                continue
            inner = dict(_protobuf_fields(value))
            start, end = stamp(inner.get(1)), stamp(inner.get(2))
            if start is not None and end is not None:
                sessions.append((start, end))
    except ValueError as exc:
        log_note(f"{entry}: not decoded ({exc}); Files Open From/To is blank")
        return []
    return sorted(sessions)


# ── Usage Stats ───────────────────────────────────────────────────────────────

def _usage_stats_module():
    import importlib.util
    import sys
    from pathlib import Path
    key = "ffs_android_usagestats"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name("usagestats.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


_RESUMED = "ACTIVITY_RESUMED"
_ENDED = ("ACTIVITY_PAUSED", "ACTIVITY_STOPPED")


class _Screen:
    """Which app's activity Usage Stats shows on screen at a time."""

    def __init__(self, events: list[dict]):
        self.events = sorted(
            (e["event_time"], e["event_type"], e.get("package") or "", e.get("class_name") or "")
            for e in events
            if isinstance(e.get("event_time"), int) and e.get("event_type") in (_RESUMED,) + _ENDED)
        self.times = [e[0] for e in self.events]

    def at(self, start: int, end: int) -> str:
        if not self.events:
            return "[no Usage Stats activity events]"
        if start < self.times[0]:
            return "[before the first Usage Stats event]"
        if start > self.times[-1]:
            return "[after the last Usage Stats event]"
        stop = bisect.bisect_right(self.times, start)
        shown: list[str] = []
        for i in range(stop - 1, -1, -1):
            when, kind, pkg, cls = self.events[i]
            if kind != _RESUMED:
                continue
            ended = any(k in _ENDED and (p, c) == (pkg, cls) for _, k, p, c in self.events[i + 1:stop])
            if not ended:
                shown.append(pkg)
            break
        later: list[str] = []
        for when, kind, pkg, _ in self.events[stop:bisect.bisect_right(self.times, end)]:
            if kind == _RESUMED and pkg not in shown and pkg not in later:
                later.append(pkg)
        if not shown and not later:
            return "[no app on screen]"
        text = ", ".join(shown) if shown else "[no app on screen]"
        if later:
            text += ", then " + ", then ".join(later)
        return text


# ── Report ────────────────────────────────────────────────────────────────────

def _row(**values) -> dict:
    row = {
        "trashed_time": None, "status": "", "file_name": "", "original_path": "",
        "trashed_time_basis": "", "expiry_extended": "", "earlier_expiries": "",
        "trashed_before": None, "photos_action_time": None, "photos_action": "",
        "photos_action_files": None, "photos_match": "", "app_on_screen": "",
        "files_open_from": None,
        "files_open_to": None, "expires": None, "created_by": "", "mime_type": "",
        "size": None, "date_added": None, "date_modified": None, "current_path": "",
        "file_link": "", "media_store_id": None, "source": "", "raw_files_id": None,
    }
    row.update(values)
    return row


def _apply_history(row: dict, hist: dict | None, expiry: int | None) -> None:
    run = (hist or {}).get("run") or []
    if expiry not in run:
        row["trashed_time"] = expiry - _TRASH_DAYS_S if expiry else None
        row["trashed_time_basis"] = "Expiry minus 30 days" if expiry else ""
        if hist is not None:
            row["expiry_extended"] = "[current name not in the recovery log]"
        return
    earlier = [n for n in run if n != expiry]
    row["trashed_time"] = run[0] - _TRASH_DAYS_S
    row["expiry_extended"] = "Yes" if earlier else "No"
    row["earlier_expiries"] = ", ".join(str(n) for n in earlier)
    row["trashed_before"] = hist.get("earlier_runs") or None
    row["trashed_time_basis"] = (
        "First expiry in the recovery log minus 30 days (expiry extended)"
        if earlier else "Expiry minus 30 days")


def _match_photos_action(row: dict, keys: list[tuple[str, str]], actions: dict) -> None:
    target = (row["trashed_time"] or 0) * 1000
    found = [(a, how) for k, how in keys for a in actions.get(k, [])]
    if not found:
        seen = {id(a): a for group in actions.values() for a in group}
        found = [(a, "Same second only") for a in seen.values()
                 if a["time"] is not None and target <= a["time"] < target + 1000]
    if not found:
        return
    best, how = min(found, key=lambda pair: abs((pair[0]["time"] or 0) - target))
    row["photos_action_time"] = best["time"]
    row["photos_action"] = (best["type"] or "").rsplit(".", 1)[-1]
    row["photos_action_files"] = best["count"]
    row["photos_match"] = how


def run(paths):
    from artifact_runner import (archive_has, device_path_to_ui_path, log_note,
                                 open_db_readonly)

    conn = open_db_readonly(paths["external_db"])
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        want = ["_id", "_data", "_display_name", "is_trashed", "is_pending", "date_expires",
                "owner_package_name", "mime_type", "_size", "date_added", "date_modified"]
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
        store_rows = [dict(r) for r in conn.execute(
            f"SELECT {sel} FROM files WHERE date_expires IS NOT NULL ORDER BY _id")]
        all_ids = {r[0] for r in conn.execute("SELECT _id FROM files")}
    finally:
        conn.close()

    records = _recovery_records(paths)
    history = _trash_history(records) if records else {}
    if records is None:
        log_note("No media store recovery log in this archive; Expiry Extended is blank")
    by_store_id, actions, photos_trashed = _photos(paths)

    rows: list[dict] = []
    for r in store_rows:
        device_path = r["_data"] or ""
        _, kind, name_expiry, base = _split_name(device_path)
        expiry = r["date_expires"]
        if r["is_trashed"]:
            status = "In trash"
        elif r["is_pending"]:
            status = "Pending"
        else:
            status = "Expiry set"
        ui = device_path_to_ui_path(device_path)
        row = _row(
            status=status,
            file_name=r["_display_name"] or base,
            original_path=_original_path(device_path),
            expires=expiry,
            created_by=r["owner_package_name"] or "",
            mime_type=r["mime_type"] or "",
            size=r["_size"],
            date_added=r["date_added"],
            date_modified=r["date_modified"],
            current_path=device_path,
            file_link=ui if archive_has(paths, ui) else "",
            media_store_id=r["_id"],
            source="Media store",
            raw_files_id=r["_id"],
        )
        if status == "In trash":
            hist = history.get(r["_id"]) if records else None
            _apply_history(row, hist, expiry)
            if records is not None and hist is None:
                row["expiry_extended"] = "[no recovery log record]"
        rows.append(row)

    listed = {row["media_store_id"] for row in rows}
    for p in photos_trashed:
        store_id = p["media_store_id"]
        if store_id in all_ids or store_id in listed:
            continue
        device_path = p["filepath"] or ""
        _, _, _, base = _split_name(device_path)
        row = _row(
            status="Gone from media store",
            file_name=base or (p["filename"] or ""),
            original_path=_original_path(device_path),
            trashed_time=p["trash_timestamp"] // 1000,
            trashed_time_basis="Photos local_media trash_timestamp",
            expires=(p["purge_timestamp"] // 1000) if p["purge_timestamp"] else None,
            current_path=device_path,
            media_store_id=store_id,
            source="Google Photos",
        )
        rows.append(row)
        listed.add(store_id)

    for file_id, hist in history.items():
        if file_id in all_ids or file_id in listed or not hist.get("still_trashed"):
            continue
        last = hist.get("last_path", "")
        _, _, _, base = _split_name(last)
        row = _row(
            status="Gone from media store",
            file_name=base,
            original_path=_original_path(last),
            current_path=last,
            media_store_id=file_id,
            source="Media store recovery log",
        )
        _apply_history(row, hist, hist["run"][-1])
        row["expires"] = hist["run"][-1]
        rows.append(row)

    trashed_rows = [row for row in rows if row["trashed_time"] is not None]
    if trashed_rows and actions:
        read = paths.get("_read_zip_bytes")
        known_keys = {p["dedup_key"] for p in by_store_id.values() if p.get("dedup_key")}
        unmatched = set(actions) - known_keys
        adapter = paths.get("_adapter")
        for row in trashed_rows:
            keys = []
            p = by_store_id.get(row["media_store_id"])
            if p and p.get("dedup_key"):
                keys.append((p["dedup_key"], "Photos media store id"))
            if unmatched and not keys and row["file_link"] and read is not None:
                if (row["size"] or 0) > _HASH_LIMIT:
                    log_note(f"{row['file_link']}: over 512 MB, not hashed for the Photos match")
                else:
                    data = read(adapter.resolve(row["file_link"]) if adapter is not None
                                else row["file_link"])
                    if data is not None:
                        keys.append((_dedup_key(data), "File SHA-1"))
            _match_photos_action(row, keys, actions)

    if trashed_rows:
        sessions = _files_sessions(paths)
        starts = [s for s, _ in sessions]
        try:
            screen = _Screen(_usage_stats_module().run(paths))
        except Exception as exc:
            log_note(f"Usage Stats could not be read ({exc}); App On Screen is blank")
            screen = None
        for row in trashed_rows:
            if row["photos_action_time"]:
                start = end = row["photos_action_time"]
            else:
                start = row["trashed_time"] * 1000
                end = start + 999
            i = bisect.bisect_right(starts, end)
            for s, e in reversed(sessions[:i]):
                if e >= start:
                    row["files_open_from"], row["files_open_to"] = s, e
                    break
            if screen is not None:
                row["app_on_screen"] = screen.at(start, end)

    rows.sort(key=lambda row: (row["trashed_time"] is None, row["trashed_time"] or 0,
                               row["media_store_id"] or 0))
    return rows
