"""Files in Android's shared trash, from MediaProvider's external.db.

A file moved to the trash stays where it was and is renamed
.trashed-<expiry>-<name>. The files table keeps is_trashed=1 and
date_expires, the same number as the name, in Unix seconds. AOSP sets the
expiry to the trash time plus 30 days (FileUtils DEFAULT_DURATION_TRASHED),
so the trash time is the expiry minus 30 days. A .pending- file is an
unfinished write and expires 7 days after it was started.

The expiry can be moved. MediaProvider's idle maintenance
(deleteOrExtendExpiredItems) renames an item that expired a week or more
ago to now plus 7 days. The media store recovery log
(data/media/0/.transforms/recovery/leveldb-<volume>) records each name a
file had, with the media store _id in the value, so an earlier .trashed-
name for the same _id shows the extension, and the first name of that run
gives the original trash time. Compaction can drop the earlier name, so an
expiry that is exactly 7 days after an idle maintenance run in app_logs is
also treated as probably extended, and its trash time as unknown.

Three other sources are matched by time or file:
- Google Photos' action_queue (gphotos-1.db, and gphotos<N>.db for each
  signed-in account): a ...trash.delete.move_to_trash_optimistic_action
  row is written when the user moves items to the trash in Photos. Its
  entity_blob lists each item's dedup_key, the base64url SHA-1 of the
  file's content, and local_media gives the dedup_key of a media store
  id. A key names content, not one file, so an action is matched only
  when it was queued in the trash second or the next.
- Files by Google's activityTimeLogStoreName.pb: the onResume and onPause
  time of each Files screen.
- Usage Stats: which apps' activities were resumed at the trash time.
  A clock change moves every earlier Usage Stats time (AOSP
  UsageStatsDatabase.onTimeChanged renames the files), so a row says when
  Usage Stats disagrees with Photos' or Files' own time.

Verified on the lab emulator (Android 16, Photos group trash, Files by
Google group trash, idle maintenance extension and purge) and on the
Xiaomi Poco X7 Pro image (Android 15).
"""

import base64
import bisect
import calendar
import datetime
import hashlib
import os
import re

name = "Media Trash"
warning = (
    "Experimental. Trashed Time is worked out from the expiry, assuming "
    "Android's 30-day trash period; a phone maker can change it. Trashed "
    "By is a conclusion from the evidence in its reason: only a Photos "
    "trash action that lists the file's content is firm, everything marked "
    "Probably is supporting evidence. Every row says which file and field "
    "each answer came from."
)
description = (
    "Files moved to Android's shared trash: when, whether Android extended "
    "the expiry, and which app did it, each with its source. Lists the files "
    "still in the trash (media store external.db) and trashed files the "
    "media store has since dropped but its recovery log or Google Photos "
    "still lists. Detail columns hold the evidence: the Photos trash "
    "action, the Files by Google screen, and the apps Usage Stats shows on "
    "screen."
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
    "trashed_time", "trashed_time_source", "file_link", "file_name", "trashed_by",
    "trashed_by_reason", "expiry_extended", "extended_source", "status", "original_path",
]
media_fields = ["file_link"]
hidden_fields = ["raw_files_id"]
record_source = [
    {"label": "Media store row", "file_key": "external_db", "table": "files",
     "rowid_fields": ["raw_files_id"], "presence_fields": ["raw_files_id"]},
]

_TRASH_DAYS_S = 30 * 86400
_EXTENDED_S = 7 * 86400          # FileUtils.DEFAULT_DURATION_EXTENDED
_NAME_RE = re.compile(r"^\.(trashed|pending)-(\d+)-(.+)$")
# gphotos-1.db is Photos without an account; gphotos0.db, gphotos1.db, ...
# one per signed-in account.
_PHOTOS_DB_RE = re.compile(r"/com\.google\.android\.apps\.photos/databases/(gphotos(?:-1|\d+)\.db)$")
_PHOTOS_PKG = "com.google.android.apps.photos"
_FILES_PKG = "com.google.android.apps.nbu.files"
_FILES_LOG = _FILES_PKG + "/files/activityTimeLogStoreName.pb"
_RECOVERY = "/.transforms/recovery/leveldb-"
_APP_LOGS = "com.google.android.providers.media.module/app_logs/"
_HASH_LIMIT = 512 * 1024 * 1024
_GONE = "Gone (trash emptied, or expired)"
# Photos queued its action 51-81 ms after the rename in every lab trash, so
# the trash second or the next.
_ACTION_WINDOW_MS = 2000


def _utc_s(seconds: int) -> str:
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _utc_ms(ms: int) -> str:
    t = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    return f"{t:%Y-%m-%d %H:%M:%S}.{ms % 1000:03d} UTC"


def _prefer_data_data(n: str):
    return (0 if "/data/data/" in n or n.startswith("data/data/") else 1, len(n))


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
    found.sort(key=_prefer_data_data)
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

def _recovery_records(paths: dict) -> list[tuple[int, str, int, str]] | None:
    """(seq, device path, media store _id, log folder) for every live
    recovery-log record whose value names an _id. None when the archive has
    no recovery log."""
    from artifact_runner import is_capture_alias, log_note
    names = paths.get("_zip_names") or []
    read = paths.get("_read_zip_bytes")
    dirs: dict[str, list[str]] = {}
    for n in names:
        if _RECOVERY not in n or n.endswith("/") or is_capture_alias(n):
            continue
        folder = n.rsplit("/", 1)[0]
        label = folder.rsplit("/", 1)[-1]
        # internal is the internal volume's log; ownership maps packages.
        if label in ("leveldb-internal", "leveldb-ownership"):
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
    out: list[tuple[int, str, int, str]] = []
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
            log_note(f"{folder}: recovery log not opened ({exc}); Extended is not known for its files")
            continue
        try:
            for rec in db.iterate_records_raw():
                if not rec.value:
                    continue
                parts = rec.value.decode("utf-8", "replace").split("::")
                if len(parts) < 2 or not parts[1].isdigit():
                    continue
                out.append((rec.seq, rec.user_key.decode("utf-8", "replace"), int(parts[1]), folder))
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
    logs: dict[int, str] = {}
    for seq, key, file_id, *log in records:
        by_id.setdefault(file_id, []).append((seq, key))
        if log:
            logs[file_id] = log[0]
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
            history.setdefault(file_id, {"log": logs.get(file_id, "")})["last_path"] = key
        if runs:
            history[file_id]["run"] = runs[-1]
            history[file_id]["earlier_runs"] = len(runs) - 1
            history[file_id]["still_trashed"] = last_kind == "trashed"
    return history


# ── Media store app_logs ──────────────────────────────────────────────────────

_LOG_TIME = rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) "
_FIRST_LINE_RE = re.compile(rb"^" + _LOG_TIME)
_IDLE_RE = re.compile(_LOG_TIME + rb"Idle maintenance on external, \d+ items taking (\d+)ms")


def _log_ms(text: bytes) -> int:
    t = datetime.datetime.strptime(text.decode("ascii"), "%Y-%m-%d %H:%M:%S.%f")
    return calendar.timegm(t.timetuple()) * 1000 + t.microsecond // 1000


def _idle_runs(paths: dict) -> list[tuple[int, int, str]]:
    """(start ms, end ms, log file) UTC of each idle maintenance run in the media
    store's app_logs. Lines are in the phone's local time (Logging.java,
    SimpleDateFormat with the default zone); each file is named with the
    UTC ms it was started at, so its first line gives the offset."""
    from artifact_runner import is_capture_alias, log_note
    read = paths.get("_read_zip_bytes")
    if read is None:
        return []
    runs = set()
    for n in paths.get("_zip_names") or []:
        base = n.rsplit("/", 1)[-1]
        if _APP_LOGS not in n or not base.isdigit() or is_capture_alias(n):
            continue
        data = read(n) or b""
        first = _FIRST_LINE_RE.match(data)
        if not first:
            continue
        offset = round((_log_ms(first.group(1)) - int(base)) / 900_000) * 900_000
        if abs(offset) > 14 * 3600_000:
            log_note(f"{n}: first line is {offset // 60_000} minutes from the file's name; "
                     "its idle maintenance runs are not used")
            continue
        for m in _IDLE_RE.finditer(data):
            end = _log_ms(m.group(1)) - offset
            # The line is written when the run ends; "taking" is its length.
            runs.add((end - int(m.group(2)) - 1000, end, n))
    return sorted(runs)


def _idle_extension(expiry: int | None, idle_runs: list[tuple[int, int, str]]) -> tuple[int, str] | None:
    """(end ms, log file) of an idle maintenance run that this expiry is 7
    days after: an extension sets the expiry to the run's time plus 7 days."""
    if not expiry:
        return None
    when = (expiry - _EXTENDED_S) * 1000
    for start, end, log in idle_runs:
        if start <= when <= end:
            return end, log
    return None


def _mark_extended(row: dict, expiry: int, idle: tuple[int, str]) -> None:
    # An item is extended only when its expiry was a week or more before the
    # run, so its old expiry is at least 14 days before the new one, and the
    # trash at least 44.
    end, log = idle
    row["trashed_time"] = None
    row["expiry_extended"] = "Probably"
    row["extended_source"] = (
        f"{log}: idle maintenance ran at {_utc_ms(end)}, 7 days before the expiry "
        f"{expiry} ({_utc_s(expiry)}); an extension sets the expiry to that run plus 7 days")
    row["trashed_time_source"] = (
        "[Unknown: Android probably extended the expiry, and the earlier one is not in "
        f"the recovery log. Trashed before {_utc_s(expiry - 44 * 86400)}]")


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


def _photos_dbs(names: list[str]) -> list[str]:
    """Each Photos database once (gphotos-1.db and one per account), the
    data/data copy where the archive has several."""
    from artifact_runner import is_capture_alias
    by_name: dict[str, list[str]] = {}
    for n in names:
        m = _PHOTOS_DB_RE.search(n)
        if m and not is_capture_alias(n):
            by_name.setdefault(m.group(1), []).append(n)
    return [min(found, key=_prefer_data_data) for _, found in sorted(by_name.items())]


def _photos(paths: dict) -> tuple[dict, dict, list]:
    """(local_media by media store id, trash actions by dedup key,
    trashed local_media rows) from every Photos database."""
    from artifact_runner import log_note, open_db_readonly
    by_store_id: dict[int, dict] = {}
    actions: dict[str, list[dict]] = {}
    trashed: dict[int, dict] = {}
    seen_actions: set = set()
    for entry in _photos_dbs(paths.get("_zip_names") or []):
        db_name = entry.rsplit("/", 1)[-1]
        db_path = _copy_out(paths, entry, "photos", ("", "-wal", "-shm"))
        if not db_path:
            continue
        try:
            conn = open_db_readonly(db_path)
        except Exception as exc:
            log_note(f"{entry}: not opened ({exc}); its Photos rows are not used")
            continue
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "local_media" in tables:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(local_media)")}
                want = ["media_store_id", "dedup_key", "trash_timestamp", "purge_timestamp", "filepath", "filename"]
                if {"media_store_id", "dedup_key"} <= cols:
                    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
                    for r in conn.execute(f"SELECT {sel} FROM local_media WHERE media_store_id IS NOT NULL"):
                        row = dict(r)
                        by_store_id.setdefault(row["media_store_id"], row)
                        if row["trash_timestamp"]:
                            found = trashed.setdefault(row["media_store_id"], dict(row, dbs=[]))
                            found["dbs"].append(db_name)
            if "action_queue" in tables:
                for r in conn.execute(
                        "SELECT rowid, entity_type, entity_blob, creation_timestamp FROM action_queue "
                        "WHERE entity_type LIKE '%trash%'"):
                    try:
                        keys = [v.decode("ascii") for f, v in _protobuf_fields(r["entity_blob"] or b"")
                                if f == 2 and isinstance(v, bytes)]
                    except (ValueError, UnicodeDecodeError) as exc:
                        log_note(f"{db_name} action_queue row {r['rowid']}: not decoded ({exc})")
                        continue
                    ident = (r["creation_timestamp"], r["entity_type"], tuple(keys))
                    if ident in seen_actions:
                        continue
                    seen_actions.add(ident)
                    action = {"time": r["creation_timestamp"], "type": r["entity_type"],
                              "count": len(keys), "keys": keys, "db": db_name}
                    for k in keys:
                        actions.setdefault(k, []).append(action)
        finally:
            conn.close()
    return by_store_id, actions, list(trashed.values())


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
# Nothing is resumed after a boot. DEVICE_SHUTDOWN is not used: it can be
# logged minutes before the phone goes off (usagestats.py).
_STARTUP = "DEVICE_STARTUP"


class _Screen:
    """Which apps' activities Usage Stats shows resumed at a time. More
    than one can be: split screen, or a switch in progress (on the Poco X7
    Pro the next activity is often resumed before the last is paused)."""

    def __init__(self, events: list[dict]):
        self.events = sorted(
            (e["event_time"], e["event_type"], e.get("package") or "", e.get("class_name") or "")
            for e in events
            if isinstance(e.get("event_time"), int)
            and e.get("event_type") in (_RESUMED, _STARTUP) + _ENDED)
        self.times = [e[0] for e in self.events]
        # The packages resumed after each event, latest first.
        self.resumed: list[tuple[str, ...]] = []
        open_: dict[tuple[str, str], int] = {}
        for when, kind, pkg, cls in self.events:
            if kind == _RESUMED:
                open_[(pkg, cls)] = when
            elif kind == _STARTUP:
                open_.clear()
            else:
                open_.pop((pkg, cls), None)
            latest = sorted(open_.items(), key=lambda item: -item[1])
            self.resumed.append(tuple(dict.fromkeys(p for (p, _), _ in latest)))

    def at(self, start: int, end: int) -> str:
        if not self.events:
            return "[no Usage Stats activity events]"
        if start < self.times[0]:
            return "[before the first Usage Stats event]"
        if start > self.times[-1]:
            return "[after the last Usage Stats event]"
        stop = bisect.bisect_right(self.times, start)
        shown = list(self.resumed[stop - 1]) if stop else []
        later: list[str] = []
        for when, kind, pkg, _ in self.events[stop:bisect.bisect_right(self.times, end)]:
            if kind == _RESUMED and pkg not in shown and pkg not in later:
                later.append(pkg)
        if not shown and not later:
            return "[no app on screen]"
        text = " + ".join(shown) if shown else "[no app on screen]"
        if later:
            text += ", then " + ", then ".join(later)
        return text

    def on_screen(self, when: int) -> tuple[str, ...] | None:
        """Packages resumed at *when*, latest first; None when Usage Stats
        does not cover that time."""
        if not self.events or when < self.times[0] or when > self.times[-1]:
            return None
        stop = bisect.bisect_right(self.times, when)
        return self.resumed[stop - 1] if stop else ()

    def shows(self, pkg: str, start: int, end: int) -> bool | None:
        """Whether *pkg* is resumed at some point in [start, end]; None when
        Usage Stats does not cover that time."""
        if not self.events or start < self.times[0] or start > self.times[-1]:
            return None
        stop = bisect.bisect_right(self.times, start)
        if stop and pkg in self.resumed[stop - 1]:
            return True
        return any(pkg in state for state in self.resumed[stop:bisect.bisect_right(self.times, end)])


# ── Report ────────────────────────────────────────────────────────────────────

def _row(**values) -> dict:
    row = {
        "trashed_time": None, "trashed_time_source": "", "file_link": "", "file_name": "",
        "trashed_by": "", "trashed_by_reason": "", "expiry_extended": "", "extended_source": "",
        "status": "", "original_path": "", "earlier_expiries": "",
        "trashed_before": None, "photos_action_time": None, "photos_action": "",
        "photos_action_files": None, "photos_match": "", "photos_db": "", "app_on_screen": "",
        "files_open_from": None,
        "files_open_to": None, "expires": None, "created_by": "", "mime_type": "",
        "size": None, "date_added": None, "date_modified": None, "current_path": "",
        "media_store_id": None, "source": "", "raw_files_id": None,
    }
    row.update(values)
    return row


def _apply_history(row: dict, hist: dict | None, expiry: int | None,
                   idle_runs: list[tuple[int, int, str]] = (), log_read: bool = True,
                   expiry_from: str = "external.db files.date_expires",
                   app_logs_read: bool = True) -> None:
    """Trashed Time and Extended, each with its source, from the expiry
    (read from *expiry_from*), the recovery log's names for this file
    (*hist*, None when it has none) and the idle maintenance runs."""
    run = (hist or {}).get("run") or []
    log = (hist or {}).get("log") or "the media store recovery log"
    if expiry in run:
        first = run[0]
        earlier = [n for n in run if n != expiry]
        row["earlier_expiries"] = ", ".join(str(n) for n in earlier)
        row["trashed_before"] = hist.get("earlier_runs") or None
        if earlier:
            row["expiry_extended"] = "Yes"
            row["extended_source"] = (
                f"{log}: the file was named .trashed-{first}- before .trashed-{expiry}-, "
                "with no plain name between")
            row["trashed_time_source"] = (
                f"{log}: first trash name .trashed-{first}- ({_utc_s(first)}) minus 30 days")
        else:
            row["expiry_extended"] = "No sign of it"
            row["extended_source"] = f"{log} has no earlier .trashed- name for this file" + (
                ", and app_logs no idle maintenance run 7 days before the expiry"
                if app_logs_read else "; this archive has no media store app_logs to check")
    else:
        first = expiry
        if not log_read:
            row["expiry_extended"] = "Not known"
            row["extended_source"] = "No media store recovery log in this archive"
        elif hist is None:
            row["expiry_extended"] = "Not known"
            row["extended_source"] = f"{log} has no record for media store id {row['media_store_id']}"
        else:
            row["expiry_extended"] = "Not known"
            row["extended_source"] = f"{log} does not have this file's current name"
    if first and not row["trashed_time_source"]:
        row["trashed_time_source"] = f"{expiry_from} {first} ({_utc_s(first)}) minus 30 days"
    row["trashed_time"] = first - _TRASH_DAYS_S if first else None
    # The log can have lost an earlier name (compaction): an expiry set by
    # an idle maintenance run is itself an extension.
    idle = _idle_extension(first, idle_runs)
    if idle is not None:
        _mark_extended(row, first, idle)


def _match_photos_action(row: dict, keys: list[tuple[str, str]], actions: dict,
                         unexplained: set[str]) -> None:
    """Link the Photos trash action queued in the trash second or the next.
    By key when this file's content key is known; else, weakly, by time
    alone, and only to an action listing a key no known file has."""
    second = (row["trashed_time"] or 0) * 1000

    def near(a):
        return a["time"] is not None and second <= a["time"] < second + _ACTION_WINDOW_MS

    by_key = [(a, how) for k, how in keys for a in actions.get(k, [])]
    found = [(a, how) for a, how in by_key if near(a)]
    if not found and not keys:
        seen = {id(a): a for group in actions.values() for a in group}
        found = [(a, "Time only (file content unknown)") for a in seen.values()
                 if near(a) and set(a["keys"]) & unexplained]
    if not found:
        other_times = sorted({a["time"] for a, _ in by_key if a["time"] is not None})
        if other_times:
            listed = ", ".join(_utc_ms(t) for t in other_times[:3])
            more = f" and {len(other_times) - 3} more" if len(other_times) > 3 else ""
            row["photos_match"] = (f"[a Photos trash action at {listed}{more} lists the same "
                                   "content, not at this trash]")
        return
    best, how = min(found, key=lambda pair: pair[0]["time"] - second)
    row["photos_action_time"] = best["time"]
    row["photos_action"] = (best["type"] or "").rsplit(".", 1)[-1]
    row["photos_action_files"] = best["count"]
    row["photos_match"] = how
    row["photos_db"] = best["db"]


_APP_NAMES = {
    _PHOTOS_PKG: "Google Photos",
    _FILES_PKG: "Files by Google",
    "com.miui.gallery": "Xiaomi Gallery",
    "com.sec.android.gallery3d": "Samsung Gallery",
    "com.google.android.documentsui": "Android Files",
}


def _not_an_app(pkg: str) -> bool:
    # The home screen and system UI do not trash files themselves.
    return "launcher" in pkg or pkg == "com.android.systemui"


def _decide_trashed_by(row: dict, screen, start: int, end: int) -> None:
    """Trashed By and its reason, from the strongest evidence there is:
    Photos' own trash action listing this file, then a Files by Google
    screen open at the trash time, then the one app Usage Stats shows on
    screen. Each reason names the file and field it came from."""
    t, how = row["photos_action_time"], row["photos_match"]
    action = (f"Google Photos {row['photos_db']} action_queue: a move to trash queued at "
              f"{_utc_ms(t)}" if t else "")
    if t and how == "Photos media store id":
        row["trashed_by"] = "Google Photos"
        row["trashed_by_reason"] = (
            f"{action} lists this file (the dedup_key Photos holds for media store id "
            f"{row['media_store_id']})")
        return
    if t and how == "File SHA-1":
        row["trashed_by"] = "Google Photos"
        row["trashed_by_reason"] = f"{action} lists this file (its SHA-1)"
        return
    if t:
        row["trashed_by"] = "Probably Google Photos"
        row["trashed_by_reason"] = (
            f"{action}, in the trash second; the file is gone, so its content cannot be "
            "checked against the action")
        return
    if row["files_open_from"] is not None:
        row["trashed_by"] = "Probably Files by Google"
        why = (f"Files by Google activityTimeLogStoreName.pb: a Files screen was open "
               f"{_utc_ms(row['files_open_from'])} to {_utc_ms(row['files_open_to'])}")
        if screen is not None and screen.shows(_FILES_PKG, start, end):
            why += "; Usage Stats also shows Files on screen"
        row["trashed_by_reason"] = why + "; no Photos trash action"
        return
    shown = screen.on_screen(start) if screen is not None else None
    apps = [p for p in shown or () if not _not_an_app(p)]
    if len(apps) == 1:
        row["trashed_by"] = f"Probably {_APP_NAMES.get(apps[0], apps[0])}"
        row["trashed_by_reason"] = (
            f"Usage Stats: {apps[0]} was on screen at the trash time; no Photos trash action "
            "or Files by Google screen found")
        return
    row["trashed_by"] = "Unknown"
    row["trashed_by_reason"] = (
        "No Photos trash action and no Files by Google screen at the trash time; Usage Stats: "
        + (row["app_on_screen"] or "not read"))


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
            f"SELECT {sel} FROM files WHERE is_trashed = 1 ORDER BY _id")]
        all_ids = {r[0] for r in conn.execute("SELECT _id FROM files")}
    finally:
        conn.close()

    records = _recovery_records(paths)
    history = _trash_history(records) if records else {}
    if records is None:
        log_note("No media store recovery log in this archive; Extended is 'Not known' unless app_logs show it")
    idle_runs = _idle_runs(paths)
    app_logs_read = any(_APP_LOGS in n for n in paths.get("_zip_names") or [])
    by_store_id, actions, photos_trashed = _photos(paths)

    rows: list[dict] = []
    for r in store_rows:
        device_path = r["_data"] or ""
        _, kind, name_expiry, base = _split_name(device_path)
        expiry = r["date_expires"]
        ui = device_path_to_ui_path(device_path)
        row = _row(
            status="In trash",
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
        hist = history.get(r["_id"]) if records else None
        _apply_history(row, hist, expiry, idle_runs, log_read=bool(records),
                       app_logs_read=app_logs_read)
        if not expiry:
            row["trashed_time_source"] = "[no expiry stored, so no trash time]"
        rows.append(row)

    listed = {row["media_store_id"] for row in rows}
    for p in photos_trashed:
        store_id = p["media_store_id"]
        if store_id in all_ids or store_id in listed:
            continue
        device_path = p["filepath"] or ""
        _, _, _, base = _split_name(device_path)
        expires = (p["purge_timestamp"] // 1000) if p["purge_timestamp"] else None
        row = _row(
            status=_GONE,
            file_name=base or (p["filename"] or ""),
            original_path=_original_path(device_path),
            trashed_time=p["trash_timestamp"] // 1000,
            trashed_time_source=(f"Google Photos {' and '.join(p['dbs'])} local_media.trash_timestamp "
                                 f"{p['trash_timestamp']} for media store id {store_id}"),
            expiry_extended="Not known",
            extended_source=("Only Google Photos lists this file; the media store and its "
                             "recovery log no longer have it"),
            expires=expires,
            current_path=device_path,
            media_store_id=store_id,
            source="Google Photos (" + ", ".join(p["dbs"]) + ")",
        )
        # Photos' trash_timestamp is the media store expiry minus 30 days,
        # so an extended expiry makes it wrong too.
        idle_end = _idle_extension(expires, idle_runs)
        if idle_end is not None:
            _mark_extended(row, expires, idle_end)
        rows.append(row)
        listed.add(store_id)

    for file_id, hist in history.items():
        if file_id in all_ids or file_id in listed or not hist.get("still_trashed"):
            continue
        last = hist.get("last_path", "")
        _, _, _, base = _split_name(last)
        row = _row(
            status=_GONE,
            file_name=base,
            original_path=_original_path(last),
            current_path=last,
            media_store_id=file_id,
            source="Media store recovery log",
        )
        _apply_history(row, hist, hist["run"][-1], idle_runs,
                       expiry_from=f"{hist['log']}: expiry in the last trash name,",
                       app_logs_read=app_logs_read)
        row["expires"] = hist["run"][-1]
        rows.append(row)

    trashed_rows = [row for row in rows if row["trashed_time"] is not None]
    if trashed_rows and actions:
        read = paths.get("_read_zip_bytes")
        known_keys = {p["dedup_key"] for p in by_store_id.values() if p.get("dedup_key")}
        unmatched = set(actions) - known_keys
        adapter = paths.get("_adapter")
        row_keys = []
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
            row_keys.append(keys)
        # Action keys no file in this report or in Photos accounts for: only
        # these can be a file whose content is unknown (gone from the phone).
        unexplained = unmatched - {k for keys in row_keys for k, _ in keys}
        for row, keys in zip(trashed_rows, row_keys):
            _match_photos_action(row, keys, actions, unexplained)

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
                # Photos' action time and Files' own log are written with
                # the clock of the moment; Usage Stats times move with every
                # later clock change. Say when they disagree.
                checks = []
                if row["photos_action_time"]:
                    checks.append((_PHOTOS_PKG, "Photos' trash action"))
                if row["files_open_from"] is not None:
                    checks.append((_FILES_PKG, "Files by Google's own log"))
                for pkg, source in checks:
                    if screen.shows(pkg, start, end) is False:
                        row["app_on_screen"] += (
                            f" [Usage Stats does not show {pkg} here, though {source} does: "
                            "its times may have been moved by a clock change]")
            _decide_trashed_by(row, screen, start, end)

    for row in rows:
        if row["trashed_time"] is None and not row["trashed_by"]:
            row["trashed_by"] = "Unknown"
            row["trashed_by_reason"] = "No trash time to match against"

    rows.sort(key=lambda row: (row["trashed_time"] is None, row["trashed_time"] or 0,
                               row["media_store_id"] or 0))
    return rows
