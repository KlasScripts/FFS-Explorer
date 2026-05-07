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
import zipfile
from concurrent.futures import ThreadPoolExecutor

import msgpack

from . import graykey as _gk

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
        with open(zip_path, 'rb') as rf:
            for entry in meta_files:
                try:
                    info = _z.getinfo(entry)
                    rf.seek(info.header_offset + 26)
                    fname_len, extra_len = struct.unpack('<HH', rf.read(4))
                    data_offset = info.header_offset + 30 + fname_len + extra_len
                    guid = entry.split('/')[-2]
                    entries.append((guid, data_offset, info.file_size))
                except (KeyError, OSError):
                    pass
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
    batches = [entries[i:i + batch_size] for i in range(0, len(entries), batch_size)]
    out: dict = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        for batch_result in pool.map(_read_batch, batches):
            out.update(batch_result)
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
    def detect(cls, z: zipfile.ZipFile) -> "FfsAdapter":
        """Inspect an open ZipFile and return the matching adapter."""
        if _gk._is_graykey(z):
            return cls(cls.FORMAT_GRAYKEY, "private/var", "")
        names = frozenset(z.namelist())
        if any(n.startswith("Dump/") for n in names) and _gk._has_ut_extras(z):
            return cls(cls.FORMAT_ZIP_EXTRAS, "Dump", "")
        return cls.detect_from_names(names)

    @classmethod
    def detect_from_names(cls, zip_names: frozenset) -> "FfsAdapter":
        """Detect format from a set of zip entry names (no ZipFile required).

        Always returns a Cellebrite adapter — use this when the zip cannot
        be opened by zipfile (e.g. streaming zips with no central directory),
        where GrayKey can be ruled out."""
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
        pv = "private/var/" if self.format == self.FORMAT_GRAYKEY else ""
        return (
            pv + "mobile/Containers/Data/Application",
            pv + "mobile/Containers/Data/PluginKitPlugin",
            pv + "mobile/Containers/Shared/AppGroup",
        )

    def scan_folders(self) -> list[str]:
        """Return default header-scan folder prefixes for this format."""
        if self.format == self.FORMAT_GRAYKEY:
            return ['private/var/mobile/Containers/', 'data/data/']
        if self.format == self.FORMAT_ZIP_EXTRAS:
            return ['data/data/']
        return ['mobile/Containers/']

    def strip_display_prefix(self, path: str) -> str:
        """Strip the archive-format prefix for display.

        Removes the physical zip prefix (e.g. 'filesystem2/' or '/private/var/')
        so the path starts from the user-partition root."""
        p = path.lstrip('/')
        prefix = self.user_prefix + '/'
        if p.startswith(prefix):
            p = p[len(prefix):]
        if self.format == self.FORMAT_GRAYKEY and p.startswith('private/var/'):
            p = p[len('private/var/'):]
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
            for f in z.infolist():
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
                        raw = msgpack.unpack(f)
                except KeyError:
                    continue
                if self.old_layout:
                    # Strip the 'private/var/' prefix so ui_paths are bare
                    # (matching new-layout Cellebrite and GrayKey)
                    _PV = "private/var/"
                    return {
                        (k[len(_PV):] if k.startswith(_PV) else k): v
                        for k, v in raw.items()
                    }
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
        for entry in zip_names:
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
        streaming_index=None,
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

        # ── Streaming Cellebrite ──────────────────────────────────────────────
        if streaming_index is not None:
            _emit("Reading metadata.msgpack...")
            for mp_path in ("metadata2/metadata.msgpack", "metadata1/metadata.msgpack"):
                if mp_path in streaming_index:
                    raw: dict = msgpack.unpackb(
                        streaming_index.get_entry(mp_path).read(), raw=False)
                    break
            else:
                raise KeyError("metadata.msgpack not found in metadata1/ or metadata2/")
            if self.old_layout:
                _PV = "private/var/"
                raw = {(k[len(_PV):] if k.startswith(_PV) else k): v for k, v in raw.items()}
            return raw, {}, None

        assert z is not None

        # ── GrayKey iOS / Cellebrite Android (zip-extras) ────────────────────
        if self.format in (self.FORMAT_GRAYKEY, self.FORMAT_ZIP_EXTRAS):
            if self.format == self.FORMAT_GRAYKEY:
                _emit("Graykey archive detected — extracting metadata...")
                # GrayKey zip entries have a leading '/' so the prefix mirrors that.
                if guid_to_bundle is None:
                    _emit("Mapping Bundle IDs to GUIDs...")
                    guid_to_bundle = _build_guid_bundle_map(
                        zip_path, zip_names, z=z,
                        container_prefix="/private/var/mobile/Containers/")
                else:
                    _emit("Bundle ID map loaded from cache.")
            else:
                _emit("Cellebrite Android archive detected — reading metadata...")
                guid_to_bundle = {}
            ui_metadata = self.load_metadata(zip_path, z)
            return ui_metadata, guid_to_bundle, frozenset(ui_metadata.keys())

        # ── Cellebrite iOS non-streaming ──────────────────────────────────────
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
        for k, v in raw_data.items():
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
        ui_metadata: dict = {
            ui_path: (
                raw_data.get(ui_path)
                or (guid_meta.get(phys) if '-' in ui_path else None)
                or {}
            )
            for ui_path, phys in _zip_entries.items()
        }

        return ui_metadata, guid_to_bundle, zip_ui_paths
