"""ffs.py — format-agnostic adapter for iOS Full File System (FFS) extractions.

FfsAdapter is the single place where Cellebrite and GrayKey differences
are handled: format detection, path resolution (ui_path → physical zip path),
candidate-path generation for plist lookups, and metadata loading.

Usage
-----
    with zipfile.ZipFile(zip_path) as z:
        adapter = FfsAdapter.detect(z)

    # Resolve a ui_path to its physical zip entry
    adapter.resolve("mobile/Library/SMS/sms.db")

    # Generate candidate paths for a user-partition file
    adapter.user_candidates("preferences/SystemConfiguration/preferences.plist")

    # Generate candidate paths for a system-partition file
    adapter.system_candidates("System/Library/CoreServices/SystemVersion.plist")

    # Load the full metadata dict
    raw = adapter.load_metadata(zip_path, z)
"""

import plistlib
import re
import struct
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from itertools import batched

import msgpack

import csstore as _cs
from zip_cd_cache import compute_data_offsets
from . import graykey as _gk

# GIL-handoff cadence for O(n)-over-all-entries loops.  These may run inside
# the GUI process's worker QThread (in-process fallback paths), where a long
# pure-Python loop starves the main thread; time.sleep(0) yields the GIL.
_YIELD_EVERY = 4096

_FS_RE = re.compile(r'^(filesystem\d+)/')

# ── Cellebrite partition detection ────────────────────────────────────────────

def _has_prefix(zip_names: frozenset, prefix: str) -> bool:
    """Return True if any entry in zip_names starts with *prefix* (with or without
    a trailing slash), or is exactly *prefix*.  Handles zips that omit explicit
    directory entries by checking file entries that live under the prefix."""
    slash = prefix if prefix.endswith("/") else prefix + "/"
    bare  = prefix.rstrip("/")
    if bare in zip_names or slash in zip_names:
        return True
    return any(n.startswith(slash) for n in zip_names)


def _detect_user_prefix(zip_names: frozenset) -> tuple[str, bool]:
    """Return (user_prefix, old_layout) for the Cellebrite user partition.

    Two Cellebrite layouts exist:
    - New: filesystemN/mobile/  — msgpack keys are bare (mobile/...)
           → ('filesystemN', False)
    - Old: filesystemN/private/var/mobile/  — msgpack keys include private/var/
           → ('filesystemN', True)

    Falls back to ('filesystem2', False).

    New-layout is checked across ALL filesystem numbers first, because some
    iOS system partitions (filesystem1) contain a small number of stub entries
    under private/var/mobile/ that would otherwise trigger a false old-layout
    match before the real user partition (filesystem2) is examined."""
    # Pass 1: new layout — mobile/wireless directly under filesystemN
    for n in range(1, 10):
        prefix = f"filesystem{n}"
        for root in ("mobile", "wireless"):
            if _has_prefix(zip_names, f"{prefix}/{root}"):
                return prefix, False
    # Pass 2: old layout — private/var/mobile under filesystemN
    for n in range(1, 10):
        prefix = f"filesystem{n}"
        for root in ("mobile", "wireless"):
            if _has_prefix(zip_names, f"{prefix}/private/var/{root}"):
                return prefix, True
    return "filesystem2", False


def _detect_system_prefix(zip_names: frozenset) -> str:
    """Return the filesystemN folder that contains the iOS system partition.
    Identified by the presence of System/Library, which is unique to the
    system partition.  Falls back to 'filesystem1'."""
    for n in range(1, 10):
        prefix = f"filesystem{n}"
        if (
            f"{prefix}/System/Library/CoreServices/SystemVersion.plist" in zip_names
            or _has_prefix(zip_names, f"{prefix}/System/Library")
            or _has_prefix(zip_names, f"{prefix}/System")
        ):
            return prefix
    return "filesystem1"


# ── GUID → bundle-ID mapping ─────────────────────────────────────────────────

def _build_guid_bundle_map(zip_path: str, zip_names: frozenset,
                           z: zipfile.ZipFile | None = None,
                           container_prefix: str = "") -> dict:
    """Return {guid: bundle_id} by reading MCM metadata plists in parallel.

    Uses the already-open ZipFile *z* (if provided) solely to look up data
    offsets from the central directory — a single cheap scan.  Each worker
    thread then reads raw bytes directly without opening ZipFile at all,
    avoiding repeated central-directory parses on large archives.

    *container_prefix*, when set, limits the scan to entries under that path
    (e.g. 'filesystem2/mobile/Containers/') so that system-partition and other
    unrelated entries are rejected by a cheap startswith before the 57-char
    endswith is evaluated."""
    meta_name = ".com.apple.mobile_container_manager.metadata.plist"
    if container_prefix:
        meta_files = [f for f in zip_names
                      if f.startswith(container_prefix) and f.endswith(meta_name)]
    else:
        meta_files = [f for f in zip_names if f.endswith(meta_name)]
    if not meta_files:
        return {}

    # Collect (guid, data_offset, file_size) using the already-open ZipFile
    # (or open one temporarily if z was not supplied).
    entries: list[tuple[str, int, int]] = []
    _close = z is None
    _z = zipfile.ZipFile(zip_path, 'r') if _close else z
    assert _z is not None
    try:
        # Data offsets via compute_data_offsets (same probe-once-then-pure-
        # arithmetic approach ZipEntry itself uses) rather than a per-entry
        # seek+unpack hand-rolled here separately -- one shared
        # implementation of "where does this entry's data actually start".
        infos = []
        guid_by_filename: dict[str, str] = {}
        for entry in meta_files:
            try:
                info = _z.getinfo(entry)
            except KeyError:
                continue
            infos.append(info)
            guid_by_filename[info.filename] = entry.split('/')[-2]
        offsets = compute_data_offsets(zip_path, infos)
        for info in infos:
            data_offset = offsets.get(info.filename)
            if data_offset is not None:
                entries.append((guid_by_filename[info.filename], data_offset, info.file_size))
    finally:
        if _close:
            _z.close()

    if not entries:
        return {}

    def _read_batch(batch: list[tuple[str, int, int]]) -> dict:
        result: dict = {}
        try:
            with open(zip_path, 'rb') as rf:
                for guid, data_offset, file_size in batch:
                    try:
                        rf.seek(data_offset)
                        bid = plistlib.loads(rf.read(file_size)).get("MCMMetadataIdentifier")
                        if bid:
                            result[guid] = bid
                    except Exception:
                        pass
        except OSError:
            pass
        return result

    n = min(8, len(entries))
    batch_size = max(1, (len(entries) + n - 1) // n)
    out: dict = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        for batch_result in pool.map(_read_batch, batched(entries, batch_size)):
            out.update(batch_result)
    return out


# ── LaunchServices csstore: richer, independent GUID/App-Group source ────────
#
# Apple's proprietary, undocumented LaunchServices cache
# (com.apple.LaunchServices-<version>-v2.csstore, `bdsl` magic — see vendored
# app/csstore.py) turned out, on real casework 2026-08-23/24, to be a single
# central registry covering bundle id, both container GUIDs, and code-signing
# App Group entitlements for the large majority of installed apps — all from
# one file, independent of the per-container metadata plists
# _build_guid_bundle_map() above reads (confirmed on real GrayKey extractions
# to sometimes be missing per-container). See CLAUDE.md for the full
# verification evidence (exact GUIDs/paths cross-checked against three real
# apps). Field offsets below are reverse-engineered against real data, not
# from any format documentation — Apple's own layout, undocumented.

_LS_CSSTORE_RE = re.compile(r'com\.apple\.LaunchServices-(\d+)-v2\.csstore$')
_GUID_RE = re.compile(
    r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')


def _find_launchservices_csstore(zip_names: frozenset, container_prefix: str = "") -> str | None:
    """Locate the LaunchServices cache file. The version number in the
    filename varies per iOS build (5019, 6012 confirmed on two real
    images) so this matches by pattern, not a fixed name. A
    'SystemDataOnly-' prefixed sibling (a smaller subset) can also exist —
    the plain, unprefixed file is preferred when both do.

    A device that's been through multiple OS updates can leave several
    superseded generations of this file behind (confirmed on the iOS 16.5
    CTF23 Cellebrite image: versions 4031/4033/4035 of both the plain and
    SystemDataOnly- variant all still present under the same
    InternalDaemon container) — picking an arbitrary one (the previous
    `non_system[0]` over an unordered frozenset) could silently return a
    stale snapshot, confirmed on that same image to report a WhatsApp
    data-container GUID that no longer exists anywhere in the archive.
    The highest version number is the most recent build's registry, so
    candidates are ranked by that first, then by the plain/SystemDataOnly-
    preference within the winning version."""
    candidates = [n for n in zip_names
                 if (not container_prefix or n.startswith(container_prefix))
                 and 'Library/Caches/' in n and _LS_CSSTORE_RE.search(n)]
    if not candidates:
        return None

    def _rank(n: str) -> tuple[int, int]:
        version = int(_LS_CSSTORE_RE.search(n).group(1))
        return (version, 0 if 'SystemDataOnly-' not in n else -1)

    return max(candidates, key=_rank)


def _load_launchservices_store(zip_path: str, zip_names: frozenset,
                               z: zipfile.ZipFile | None = None,
                               container_prefix: str = "") -> "_cs.CSStore | None":
    """Locate and parse the LaunchServices csstore file once; callers share
    this single parse. Returns None if the file isn't present or fails to
    parse (e.g. a corrupted/partial extraction) — never raises, matching
    this module's existing resilience convention for optional metadata
    sources."""
    entry = _find_launchservices_csstore(zip_names, container_prefix)
    if entry is None:
        return None
    _close = z is None
    _z = zipfile.ZipFile(zip_path, 'r') if _close else z
    try:
        raw = _z.read(entry)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    finally:
        if _close:
            _z.close()
    try:
        return _cs.CSStore.from_bytes(raw)
    except Exception:
        return None


def _ls_string(store: "_cs.CSStore", key: int) -> str | None:
    try:
        return store.strings.get_string(key)
    except Exception:
        return None


def _guid_from_path(path: str | None) -> str | None:
    m = _GUID_RE.search(path or '')
    return m.group(0) if m else None


def _resolve_app_group_paths(store: "_cs.CSStore") -> dict:
    """{app_group_id: guid} from PropertyList records whose keys are ALL
    'group.'-prefixed — confirmed present as plain (non-NSKeyedArchiver)
    bplist blobs for both a single-group app (Threema) and a five-group
    app (WhatsApp — every declared group correctly paired with its own
    container path). See CLAUDE.md for the verification evidence."""
    out: dict = {}
    try:
        pl = store.get_table('PropertyList')
    except KeyError:
        return out
    for unit in pl.hashmap.values():
        d = unit.data
        if d[:8] != b'bplist00':
            continue
        try:
            result = plistlib.loads(d)
        except Exception:
            continue
        if not isinstance(result, dict) or not result:
            continue
        if not all(isinstance(k, str) and k.startswith('group.') for k in result):
            continue
        for gid, path in result.items():
            guid = _guid_from_path(path)
            if guid:
                out[gid] = guid
    return out


def _resolve_app_group_entitlements(store: "_cs.CSStore", bundle_record_data: bytes) -> list:
    """Every 'group.'-prefixed App Group id this bundle's own code-signing
    entitlements declare — read via the Bundle record's own cached
    entitlements PropertyList (offset 35*4 — see
    _extract_app_registry_from_launchservices), not by reading the app's
    compiled binary (a Mach-O tail-read approach was investigated and
    verified working, then found unnecessary once this was confirmed).
    This record type has a literal 4-byte 'lnch' prefix before its real
    bplist00 magic that the App-Group-PATH record type
    (_resolve_app_group_paths) does not — confirmed on real data. Some key
    names in the decoded dict come out as bplist string-sharing artifacts
    (e.g. 'Yb' instead of the real entitlement key name) — the *values*
    are unaffected, so this scans values for the 'group.' prefix rather
    than trusting any specific key name."""
    if len(bundle_record_data) < 36 * 4:
        return []
    key = struct.unpack_from('<I', bundle_record_data, 35 * 4)[0]
    try:
        pl = store.get_table('PropertyList')
        raw = pl.hashmap[key].data
    except (KeyError, struct.error):
        return []
    if raw[:4] == b'lnch':
        raw = raw[4:]
    try:
        result = plistlib.loads(raw)
    except Exception:
        return []
    groups = set()
    if isinstance(result, dict):
        for v in result.values():
            if isinstance(v, list):
                groups.update(x for x in v if isinstance(x, str) and x.startswith('group.'))
    return sorted(groups)


def _extract_app_registry_from_launchservices(store: "_cs.CSStore") -> list:
    """Walk the Bundle table, returning one dict per resolvable app:
    {bundle_id, display_name, team_id, bundle_container_path,
    data_container_path, app_group_ids}. Field offsets confirmed stable
    across three different real apps (WhatsApp, Signal, Discord) — see
    CLAUDE.md. A record that doesn't resolve a plausible bundle id
    (roughly 5% on real data — likely malformed/incomplete registrations)
    is skipped, not guessed. PluginKit extensions (e.g. a Share/Notification
    extension) show up as their OWN rows here, with their own bundle id
    (a dotted suffix of the host app's, e.g.
    'ch.threema.iapp.ThreemaShareExtension') — no separate handling
    needed; a caller wanting "every container for app X" matches on
    bundle_id == X OR bundle_id LIKE 'X.%'."""
    by_id: dict = {}
    try:
        bundle_table = store.get_table('Bundle')
        alias_table = store.get_table('Alias')
    except KeyError:
        return []
    for unit in bundle_table.hashmap.values():
        d = unit.data
        if len(d) < 100:
            continue
        try:
            bundle_alias_key = struct.unpack_from('<I', d, 0)[0]
            name_key        = struct.unpack_from('<I', d, 8)[0]
            bundle_id_key   = struct.unpack_from('<I', d, 12)[0]
            team_key        = struct.unpack_from('<I', d, 16)[0]
            data_alias_key  = struct.unpack_from('<I', d, 96)[0]
        except struct.error:
            continue
        bundle_id = _ls_string(store, bundle_id_key)
        if not bundle_id or '.' not in bundle_id or bundle_id[0].isdigit():
            continue  # not a plausible reverse-DNS bundle id
        bundle_path = (alias_table.hashmap[bundle_alias_key].data.decode('utf-8', 'replace')
                      if bundle_alias_key in alias_table.hashmap else None)
        data_path = (alias_table.hashmap[data_alias_key].data.decode('utf-8', 'replace')
                    if data_alias_key in alias_table.hashmap else None)
        row = {
            'bundle_id': bundle_id,
            'display_name': _ls_string(store, name_key),
            'team_id': _ls_string(store, team_key),
            'bundle_container_path': bundle_path,
            'data_container_path': data_path,
            'app_group_ids': _resolve_app_group_entitlements(store, d),
        }
        # Apple's own built-in apps are confirmed registered TWICE: once as
        # the real installed app (real Data container), once as an
        # always-present '/System/Library/AppPlaceholders/<Name>.app/' stub
        # (no data container) — this dedup keeps whichever entry actually
        # has a data_container_path, confirmed correct for all 33 real
        # duplicate pairs found on the iOS 17 test image, not guessed.
        existing = by_id.get(bundle_id)
        if existing is None or (not existing.get('data_container_path') and data_path):
            by_id[bundle_id] = row
    return list(by_id.values())


def _guid_map_from_app_registry_rows(rows: list) -> dict:
    """{guid: bundle_id} derived from _extract_app_registry_from_launchservices()'s
    output — lets the LaunchServices pass contribute to guid_to_bundle
    without a second file read/parse."""
    out = {}
    for r in rows:
        for path in (r.get('bundle_container_path'), r.get('data_container_path')):
            guid = _guid_from_path(path)
            if guid:
                out[guid] = r['bundle_id']
    return out


# ── Adapter ───────────────────────────────────────────────────────────────────

class FfsAdapter:
    """Encapsulates all format-specific behaviour for a single FFS extraction."""

    FORMAT_GRAYKEY    = "graykey"
    FORMAT_CELLEBRITE = "cellebrite"
    FORMAT_ZIP_EXTRAS = "zip_extras"   # Android zip with UT/UX extra fields, no msgpack

    def __init__(self, fmt: str, user_prefix: str, sys_prefix: str,
                 old_layout: bool = False) -> None:
        self.format      = fmt
        self.user_prefix = user_prefix   # e.g. 'filesystem2', 'private/var', or 'Dump'
        self.sys_prefix  = sys_prefix    # e.g. 'filesystem1' (Cellebrite only)
        self.old_layout  = old_layout    # True = old Cellebrite: keys include private/var/

    # ── Detection ─────────────────────────────────────────────────────────────

    @classmethod
    def detect(cls, z: zipfile.ZipFile, names: frozenset | None = None) -> "FfsAdapter":
        """Inspect an open ZipFile and return the matching adapter.

        Pass *names* when a frozenset of entry names is already built — it
        saves a second full namelist scan on 500k-entry archives."""
        if _gk._is_graykey(z):
            return cls(cls.FORMAT_GRAYKEY, "private/var", "")
        if names is None:
            names = frozenset(z.namelist())
        if any(n.startswith("Dump/") for n in names) and _gk._has_ut_extras(z):
            return cls(cls.FORMAT_ZIP_EXTRAS, "Dump", "")
        return cls.detect_from_names(names)

    @classmethod
    def detect_from_names(cls, zip_names: frozenset) -> "FfsAdapter":
        """Detect format from a set of zip entry names (no ZipFile required).

        Always returns a Cellebrite adapter — used by detect() once GrayKey
        and zip-extras have been ruled out."""
        user_prefix, old_layout = _detect_user_prefix(zip_names)
        return cls(
            cls.FORMAT_CELLEBRITE,
            user_prefix,
            _detect_system_prefix(zip_names),
            old_layout,
        )

    # ── Path resolution ───────────────────────────────────────────────────────

    def resolve(self, ui_path: str) -> str:
        """Map a ui_path (as stored in metadata) to its physical zip entry path.

        For GrayKey this is a simple prefix prepend.
        For Cellebrite, GUID-style path segments (containing a 32-char hex
        suffix after the last '-') are reduced to that suffix alone, matching
        the physical layout inside the zip.
        """
        if self.format == self.FORMAT_GRAYKEY:
            return "/" + ui_path

        if self.format == self.FORMAT_ZIP_EXTRAS:
            return f"{self.user_prefix}/{ui_path}"

        # Fast path: no '-' means no GUID segments (covers ~90% of paths)
        if '-' not in ui_path:
            if self.old_layout:
                return f"{self.user_prefix}/private/var/{ui_path}"
            return f"{self.user_prefix}/{ui_path}"

        parts = []
        for part in ui_path.split("/"):
            if "-" in part:
                suffix = part.split("-")[-1]
                if len(suffix) >= 32 and all(
                    c in "0123456789abcdefABCDEF" for c in suffix
                ):
                    parts.append(suffix)
                    continue
            parts.append(part)
        if self.old_layout:
            return f"{self.user_prefix}/private/var/{'/'.join(parts)}"
        return f"{self.user_prefix}/{'/'.join(parts)}"

    # ── Candidate-path generators ─────────────────────────────────────────────

    def user_candidates(self, *suffixes: str) -> list[str]:
        """Return ordered candidate zip paths for user-partition files.

        For each suffix the list covers the most-likely path first so that
        _read_plist_from_zip() finds the entry on the first hit.

        Old-layout Cellebrite archives store files under
        filesystemN/private/var/<suffix>, so both the plain and the
        private/var-infixed paths are tried."""
        candidates: list[str] = []
        if self.format == self.FORMAT_GRAYKEY:
            for s in suffixes:
                candidates.append(f"private/var/{s}")
                candidates.append(f"/private/var/{s}")
                candidates.append(s)
                candidates.append(f"/{s}")
        elif self.format == self.FORMAT_ZIP_EXTRAS:
            for s in suffixes:
                candidates.append(f"{self.user_prefix}/{s}")
                candidates.append(s)
        elif self.old_layout:
            for s in suffixes:
                candidates.append(f"{self.user_prefix}/private/var/{s}")
                candidates.append(s)
                candidates.append(f"/{s}")
        else:
            for s in suffixes:
                candidates.append(f"{self.user_prefix}/{s}")
                candidates.append(s)
                candidates.append(f"/{s}")
        return candidates

    def system_candidates(self, *suffixes: str) -> list[str]:
        """Return ordered candidate zip paths for system-partition files.

        GrayKey extractions do not have a separate system-partition prefix so
        bare and leading-slash paths are tried directly."""
        candidates: list[str] = []
        if self.format == self.FORMAT_ZIP_EXTRAS:
            for s in suffixes:
                candidates.append(f"{self.user_prefix}/{s}")
                candidates.append(s)
        elif self.format == self.FORMAT_GRAYKEY:
            for s in suffixes:
                candidates.append(s)
                candidates.append(f"/{s}")
        else:
            for s in suffixes:
                candidates.append(f"{self.sys_prefix}/{s}")
                candidates.append(s)
                candidates.append(f"/{s}")
        return candidates

    def container_parents(self) -> tuple[str, ...]:
        """Return ui_path prefixes for app container parent folders."""
        if self.format == self.FORMAT_ZIP_EXTRAS:
            return ("data/data",)
        pv = "private/var/" if self.format == self.FORMAT_GRAYKEY else ""
        return (
            pv + "mobile/Containers/Data/Application",
            pv + "mobile/Containers/Data/PluginKitPlugin",
            pv + "mobile/Containers/Shared/AppGroup",
        )

    def build_app_registry(self, zip_path: str, zip_names: frozenset,
                           z: zipfile.ZipFile | None = None) -> tuple[list, dict]:
        """Build app_registry rows AND a {guid: bundle_id} map from the
        LaunchServices csstore (see the module-level _extract_app_registry_from_launchservices
        etc. above) — a single central file, independent of the
        per-container metadata plists _build_guid_bundle_map() reads,
        confirmed on real casework to sometimes be missing on GrayKey
        extractions. Android (FORMAT_ZIP_EXTRAS) has no LaunchServices
        csstore and no GUID indirection to resolve at all — returns
        ([], {}) immediately. Also returns ([], {}) if the csstore file
        isn't present in this archive or fails to parse — never raises;
        this is a bonus source, not a required one. Call this AFTER
        build_ui_metadata(), not instead of it — this method computes its
        own container-path prefix independently rather than requiring
        build_ui_metadata's internal call sequence to change."""
        if self.format == self.FORMAT_ZIP_EXTRAS:
            return [], {}
        if self.format == self.FORMAT_GRAYKEY:
            container_prefix = "/private/var/mobile/Containers/"
        else:
            pv = "private/var/" if self.old_layout else ""
            container_prefix = f"{self.user_prefix}/{pv}mobile/Containers/"
        store = _load_launchservices_store(zip_path, zip_names, z=z,
                                           container_prefix=container_prefix)
        if store is None:
            return [], {}
        rows = _extract_app_registry_from_launchservices(store)
        guid_map = _guid_map_from_app_registry_rows(rows)
        group_paths = _resolve_app_group_paths(store)
        for r in rows:
            r['app_group_paths'] = {gid: group_paths[gid] for gid in r['app_group_ids']
                                    if gid in group_paths}
        return rows, guid_map

    def container_bundle_id(self, child_path: str, guid_map: dict) -> str | None:
        """Resolve one container_parents() child to its bundle/package id.

        Android's data/data/<package> layout needs no indirection — the
        folder name already *is* the package id. iOS containers are
        GUID-named; resolve through the guid->bundle map built at
        metadata-parse time."""
        name = child_path.rsplit('/', 1)[-1]
        if self.format == self.FORMAT_ZIP_EXTRAS:
            return name
        return guid_map.get(name)

    def bundle_id_for_path(self, path: str, guid_map: dict) -> str | None:
        """Resolve the owning app's bundle/package id for an arbitrary path
        that falls under one of container_parents(), or None if it doesn't."""
        for parent in self.container_parents():
            prefix = parent + '/'
            if path.startswith(prefix):
                child_name = path[len(prefix):].split('/', 1)[0]
                return self.container_bundle_id(f'{parent}/{child_name}', guid_map)
        return None

    def scan_folders(self) -> list[str]:
        """Return default header-scan folder prefixes for this format."""
        if self.format == self.FORMAT_GRAYKEY:
            pv = 'private/var/'
            return [
                pv + 'mobile/Containers/',
                pv + 'mobile/Library/Mobile Documents/',
                pv + 'mobile/Library/Mail/',
                pv + 'mobile/Library/SMS/Attachments/',
                pv + 'mobile/Library/Biome/streams/',
                'data/data/',
                'data/media/',
            ]
        if self.format == self.FORMAT_ZIP_EXTRAS:
            return ['data/data/', 'data/media/']
        return [
            'mobile/Containers/',
            'mobile/Library/Mobile Documents/',
            'mobile/Library/Mail/',
            'mobile/Library/SMS/Attachments/',
            'mobile/Library/Biome/streams/',
        ]

    def archive_discovery_folders(self) -> list[str]:
        """Paths to scan for archive discovery — containers plus high-value Library locations.

        For GrayKey the zip may hold an Android device (data/data/, data/media/)
        rather than iOS, so both sets of paths are included."""
        if self.format == self.FORMAT_ZIP_EXTRAS:
            return ['data/data/', 'data/media/']
        pv = 'private/var/' if self.format == self.FORMAT_GRAYKEY else ''
        base = pv + 'mobile/'
        paths = [
            base + 'Containers/',
            base + 'Library/Mobile Documents/',
            base + 'Library/Mail/',
            base + 'Library/SMS/Attachments/',
        ]
        if self.format == self.FORMAT_GRAYKEY:
            paths += ['data/data/', 'data/media/']
        return paths

    def strip_display_prefix(self, path: str) -> str:
        """Strip the archive-format prefix for display.

        Removes the physical zip prefix (e.g. 'filesystem2/' or '/private/var/')
        so the path starts from the user-partition root."""
        p = path.lstrip('/').removeprefix(self.user_prefix + '/')
        if self.format == self.FORMAT_GRAYKEY:
            p = p.removeprefix('private/var/')
        return p

    def prefix_shortcut(self, path: str) -> str:
        """Add the format-appropriate prefix to a bare shortcut path.

        GrayKey iOS ui_paths start with 'private/var/'; shortcut definitions
        omit this prefix for readability, so it must be re-added at use time."""
        if self.format == self.FORMAT_GRAYKEY and not path.startswith('private/'):
            return 'private/var/' + path
        return path

    # ── Metadata loading ──────────────────────────────────────────────────────

    def load_metadata(self, zip_path: str, z: zipfile.ZipFile) -> dict:
        """Return the raw metadata dict for this extraction.

        For GrayKey the metadata is parsed from the zip's extra fields.
        For Cellebrite it is unpacked from metadata2/metadata.msgpack.
        In both cases the returned keys are ui_paths with the
        '/private/var/' prefix stripped."""
        if self.format == self.FORMAT_GRAYKEY:
            raw = _gk.extract_metadata(zip_path, z)
            return {k.lstrip("/"): v for k, v in raw.items()}
        if self.format == self.FORMAT_ZIP_EXTRAS:
            # Central directory ZipInfo objects are already in memory — no
            # local-header seeks needed.  atime and ctime are always zero in
            # this format; mtime is read from the UT extra block (already in
            # f.extra) and file_size comes straight from the ZipInfo.
            slash      = self.user_prefix.rstrip('/') + '/'
            slash_len  = len(slash)
            _unpack    = struct.unpack_from
            _S_TO_NS   = _gk._S_TO_NS
            _find_block = _gk._find_block
            _TAG_UT    = _gk._TAG_UT
            result = {}
            for i, f in enumerate(z.infolist()):
                if i % _YIELD_EVERY == 0:
                    time.sleep(0)
                if f.filename.endswith('/'):
                    continue
                name = f.filename.rstrip('/')
                if not name.startswith(slash):
                    continue
                ui_path = name[slash_len:]
                if not ui_path:
                    continue
                # Fast path: UT block is almost always the first extra field in
                # Cellebrite Android zips — check tag bytes directly before
                # falling back to the full TLV scan.
                extra = f.extra
                if (len(extra) >= 9
                        and extra[0] == 0x55 and extra[1] == 0x54  # tag 0x5455 = UT
                        and extra[4] & 1):                          # mtime flag
                    mtime = _unpack('<I', extra, 5)[0] * _S_TO_NS
                else:
                    ut = _find_block(extra, _TAG_UT)
                    mtime = (_unpack('<I', ut, 1)[0] * _S_TO_NS
                             if ut and len(ut) >= 5 and (ut[0] & 1) else 0)
                result[ui_path] = {
                    'atime': 0, 'btime': 0, 'ctime': 0, 'mtime': mtime,
                    'uid': 0, 'gid': 0, 'inode': 0,
                    'links': None, 'mode': None, 'prot': None,
                    'size': f.file_size,
                    'xattr': {},
                }
            return result
        else:
            for candidate in ("metadata2/metadata.msgpack", "metadata1/metadata.msgpack"):
                try:
                    with z.open(candidate) as f:
                        # Single GIL hold for the whole unpack (~1-2 s for a large
                        # archive): a top-level msgpack map cannot be chunk-decoded
                        # without changing the metadata format.  The dominant path
                        # avoids this via subprocess parse + chunked snapshot.
                        raw = msgpack.unpack(f)
                except KeyError:
                    continue
                if self.old_layout:
                    # Strip the 'private/var/' prefix so ui_paths are bare
                    # (matching new-layout Cellebrite and GrayKey)
                    return {k.removeprefix("private/var/"): v for k, v in raw.items()}
                return raw
            raise KeyError("metadata.msgpack not found in metadata1/ or metadata2/")

    # ── Unified metadata builder ──────────────────────────────────────────────

    def build_zip_entries(self, zip_names: frozenset) -> dict[str, str]:
        """Map ui_path → physical zip entry name for every user-partition entry.

        Strips the format prefix (e.g. 'filesystem2/') and optionally the
        old-layout 'private/var/' infix.  Called by ZipMetadataWorker when a
        local .zcd cache is available so the scan runs over in-memory data
        rather than requiring a network seek.
        """
        prefix    = self.user_prefix + '/'
        old_strip = (prefix + 'private/var/') if self.old_layout else None

        result: dict[str, str] = {}
        for i, entry in enumerate(zip_names):
            if i % _YIELD_EVERY == 0:
                time.sleep(0)  # runs in the worker QThread — yield the GIL
            phys = entry.rstrip('/')
            if not phys.startswith(prefix):
                continue
            ui_path = (phys.removeprefix(old_strip) if old_strip and phys.startswith(old_strip)
                       else phys.removeprefix(prefix))
            if ui_path:
                result[ui_path] = phys
        return result

    def build_ui_metadata(
        self,
        zip_path: str,
        zip_names: frozenset,
        z: zipfile.ZipFile | None = None,
        status_cb=None,
        guid_to_bundle: dict | None = None,
        zip_entries: dict | None = None,
    ) -> tuple[dict, dict, frozenset | None]:
        """Return (ui_metadata, guid_to_bundle, zip_ui_paths).

        ui_metadata      — {ui_path: metadata_dict} ready for the folder tree.
        guid_to_bundle   — {guid: bundle_id}; empty for non-Cellebrite formats.
        zip_ui_paths     — frozenset of ui_paths that have a physical zip entry
                           (None when the metadata dict is the sole source).
        """
        def _emit(msg):
            if status_cb:
                status_cb(msg)

        assert z is not None

        # ── GrayKey iOS / Cellebrite Android (zip-extras) ────────────────────
        if self.format in (self.FORMAT_GRAYKEY, self.FORMAT_ZIP_EXTRAS):
            if self.format == self.FORMAT_GRAYKEY:
                _emit("Graykey archive detected — extracting metadata...")
                _ios_containers = "/private/var/mobile/Containers/"
                _is_gk_ios = any(n.startswith(_ios_containers) for n in zip_names)
                if not _is_gk_ios:
                    guid_to_bundle = {}
                elif guid_to_bundle is None:
                    _emit("Mapping Bundle IDs to GUIDs...")
                    guid_to_bundle = _build_guid_bundle_map(
                        zip_path, zip_names, z=z,
                        container_prefix=_ios_containers)
                else:
                    _emit("Bundle ID map loaded from cache.")
            else:
                _emit("Cellebrite Android archive detected — reading metadata...")
                guid_to_bundle = {}
            ui_metadata = self.load_metadata(zip_path, z)
            return ui_metadata, guid_to_bundle, frozenset(ui_metadata.keys())

        # ── Cellebrite iOS ────────────────────────────────────────────────────
        if guid_to_bundle is None:
            _emit("Mapping Bundle IDs to GUIDs...")
            _pv = "private/var/" if self.old_layout else ""
            _cpfx = f"{self.user_prefix}/{_pv}mobile/Containers/"
            guid_to_bundle = _build_guid_bundle_map(zip_path, zip_names, z=z,
                                                    container_prefix=_cpfx)
        else:
            _emit("Bundle ID map loaded from cache.")

        _emit("Reading metadata.msgpack...")
        raw_data = self.load_metadata(zip_path, z)

        _emit("Building GUID enrichment index…")
        # resolve() is only needed for GUID-containing paths (those with a
        # 32-char hex suffix after the last '-').  That's ~10k out of 500k
        # entries.  All other ui_paths are identical to the msgpack key so
        # raw_data can be queried directly, eliminating ~490k resolve() calls
        # and one full in-memory copy of the metadata dict.
        guid_meta: dict = {}
        for i, (k, v) in enumerate(raw_data.items()):
            if i % _YIELD_EVERY == 0:
                time.sleep(0)
            if '-' in k:
                guid_meta[self.resolve(k)] = v

        # Pass 1 — discover: the zip central directory is the single source of
        # truth for what exists.  Directory records (entry[-1]=='/') are normalised
        # by stripping the trailing slash so they contribute folder metadata from
        # the msgpack just like file entries do.
        if zip_entries is not None:
            _zip_entries = zip_entries
        else:
            _emit(f"Scanning user partition ({self.user_prefix})…")
            _zip_entries = self.build_zip_entries(zip_names)

        zip_ui_paths = frozenset(_zip_entries)

        # Pass 2 — enrich: msgpack is a read-only lookup; it never introduces new
        # keys.  Anything in the msgpack but absent from the zip simply doesn't
        # exist as far as the browser is concerned — no "Not in Zip" entries.
        _emit("Merging zip and msgpack metadata…")
        ui_metadata: dict = {}
        for i, (ui_path, phys) in enumerate(_zip_entries.items()):
            if i % _YIELD_EVERY == 0:
                time.sleep(0)
            ui_metadata[ui_path] = (
                raw_data.get(ui_path)
                or (guid_meta.get(phys) if '-' in ui_path else None)
                or {}
            )

        return ui_metadata, guid_to_bundle, zip_ui_paths
