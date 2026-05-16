import sys
import os
import re
import io
import time
import atexit
import zipfile
import gzip
import json
import hashlib
import struct
import plistlib
import xml.dom.minidom
import pathlib
import subprocess
import configparser
from concurrent.futures import ThreadPoolExecutor

_APP_DIR             = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app')    # adapters, db_utils, etc.
_CONFIG_DIR          = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config') # JSON config files
FFS_ARCHIVES_FILE    = os.path.join(_CONFIG_DIR, 'ffs_archives.json')                     # recently opened archives
HARDWARE_MODELS_FILE = os.path.join('config', 'hardware_models.json')                      # device make/model lookup tables (relative — used with resource_path() for PyInstaller)
sys.path.insert(0, _APP_DIR)

from adapters import FfsAdapter
from db_utils import (_open_cache_db, _open_results_db, OldSchemaError,
                      check_cache_schema, check_results_schema,
                      save_header_types, load_header_types, clear_header_types,
                      save_guid_bundle_map, load_guid_bundle_map,
                      save_folder_counts, save_folder_sizes, load_folder_data,
                      save_device_info, load_device_info,
                      upsert_device_info_field,
                      start_run_log, complete_run_log, load_last_run,
                      save_nested_archive, save_nested_archive_entries,
                      load_nested_archives, load_nested_archive_entries)
import header_scan
from hex_viewer import HexViewerMixin
from media_viewer import MediaViewerMixin, MEDIA_EXTENSIONS
from keyword_search import KeywordSearchMixin
from artifact_viewer import ArtifactViewerMixin
from streaming_zip import StreamingZipIndex
from zip_cd_cache import (CachedZipView, is_valid as _cd_cache_valid,
                          save as _cd_cache_save, load as _cd_cache_load,
                          probe_delta as _probe_delta,
                          compute_data_offsets as _compute_data_offsets,
                          sidecar_hash as _cd_sidecar_hash,
                          extract_cd_hash as _cd_extract_hash)
from datetime import datetime, timezone
from PySide6.QtWidgets import (QApplication, QMainWindow, QTreeView, QTableView, QVBoxLayout,
                              QHBoxLayout, QWidget, QHeaderView, QPushButton,
                              QFileDialog, QProgressBar, QMenu, QDialog,
                              QComboBox, QSplitter, QStatusBar,
                              QLineEdit, QLabel,
                              QTabWidget, QSizePolicy, QStackedWidget,
                              QMessageBox, QCheckBox, QAbstractItemView)
from PySide6.QtGui import (QStandardItemModel, QStandardItem, QAction, QFont,
                           QCursor, QColor, QIcon)
from PySide6.QtCore import (Qt, QThread, Signal, QSortFilterProxyModel, QTimer,
                             QModelIndex, QPersistentModelIndex, QAbstractTableModel,
                             qInstallMessageHandler, QtMsgType)

FRAME_BUDGET_SECS = 0.016   # max seconds per UI batch — keeps the interface responsive at 60 fps while the tree builds

_TREE_PLACEHOLDER = "__placeholder__"
# Minimum tree column width (px).  The column always fills the panel, but never
# shrinks below this so deeply-indented items can be reached via horizontal scroll.
_TREE_COL_MIN = 1000

# iOS container metadata files: a folder that contains only these (and nothing else)
# is labelled "Folder - Metadata Only" rather than a regular folder.
_SYSTEM_METADATA_NAMES: frozenset = frozenset({
    ".com.apple.mobile_container_manager.metadata.plist",
    ".com.apple.FairPlay.MachineIdentifier",
    ".com.apple.springboard.shortcuts",
})

_UUID_RE = re.compile(
    r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
)

FORENSIC_SHORTCUTS = [
    ("App Data", "mobile/Containers/Data/Application"),
    ("App Data Plugins", "mobile/Containers/Data/PluginKitPlugin"),
    ("App Data Shared", "mobile/Containers/Shared/AppGroup"),
    None,  # separator
    ("Communications", [
        ("SMS / iMessage",          "mobile/Library/SMS"),
        ("Call History",            "wireless/Library/CallHistoryDatabase"),
        ("Contacts",                "mobile/Library/AddressBook"),
        ("Mail",                    "mobile/Library/Mail"),
        ("Voicemail",               "wireless/Library/Voicemail"),
    ]),
    ("Media", [
        ("Photos (DCIM)",           "mobile/Media/DCIM"),
        ("Photo Library Data",      "mobile/Media/PhotoData"),
        ("Voice Memos",             "mobile/Media/Recordings"),
    ]),
    ("Browsing & Web", [
        ("Safari",                  "mobile/Library/Safari"),
        ("Safari Downloads — iOS 13+","mobile/Library/Safari/Downloads"),
        ("Safari Reading List",     "mobile/Library/Safari/ReadingListArchives"),
    ]),
    ("System & Device", [
        ("Biome — iOS 14+",         "mobile/Library/Biome"),
        ("KnowledgeC — iOS 9+",     "mobile/Library/CoreDuet/Knowledge"),
        ("Health",                  "mobile/Library/Health"),
        ("Calendar",                "mobile/Library/Calendar"),
        ("Notes",                   "mobile/Library/NoteStore"),
        ("Reminders",               "mobile/Library/Reminders"),
        ("Location History",        "mobile/Library/Caches/com.apple.routined"),
        ("Keychain",                "private/var/Keychains"),
        ("Wi-Fi Networks",          "wireless/Library/Preferences"),
        ("Device Logs",             "mobile/Library/Logs"),
        ("Unified Logs — iOS 10+",          "private/var/db/diagnostics"),
        ("Unified Logs UUID Text — iOS 10+","private/var/db/uuidtext"),
    ]),
    ("Installed Apps", [
        ("App Executables",             "mobile/Containers/Bundle/Application"),
        ("Crash Reports",           "mobile/Library/Logs/CrashReporter"),
    ]),
]

PICTURE_EXTENSIONS = {
    '.jpg', '.jpeg', '.heic', '.heif', '.png', '.gif',
    '.bmp', '.tiff', '.tif', '.webp', '.raw', '.cr2', '.nef', '.dng',
}
VIDEO_EXTENSIONS = {
    '.mp4', '.mov', '.avi', '.mkv', '.m4v', '.wmv',
    '.flv', '.webm', '.3gp', '.mts', '.m2ts', '.mpg', '.mpeg',
}
DATABASE_EXTENSIONS = {
    '.db', '.sqlite', '.sqlite3', '.db3',
    '.db-wal', '.db-shm', '.db-journal',
    '.sqlite-wal', '.sqlite-shm', '.sqlite-journal',
}
LOG_EXTENSIONS = {
    '.log', '.ips', '.crash', '.clslog', '.logarchive',
}
DOCUMENT_EXTENSIONS = {
    '.pdf', '.txt', '.rtf', '.csv',
    '.doc', '.docx', '.pages', '.numbers', '.key',
    '.xls', '.xlsx', '.ppt', '.pptx',
}
AUDIO_EXTENSIONS = {
    '.mp3', '.m4a', '.aac', '.wav', '.flac', '.aiff', '.aif',
    '.caf', '.opus', '.ogg', '.amr', '.wma',
}
ARCHIVE_EXTENSIONS = {
    '.zip', '.gz', '.tar', '.bz2', '.xz',
    '.tgz', '.tbz', '.zst', '.aar',
}
APPLICATION_EXTENSIONS = {'.ipa', '.apk', '.apks', '.jar'}
JSON_EXTENSIONS = {'.json', '.jsonl', '.ndjson'}
XML_EXTENSIONS  = {'.xml', '.xsl', '.xslt', '.xsd', '.gpx', '.kml', '.rss', '.svg'}
WEB_DATA_EXTENSIONS = {
    '.html', '.htm', '.js', '.css', '.ts',
    '.yaml', '.yml', '.toml',
}
CERTIFICATE_EXTENSIONS = {
    '.pem', '.cer', '.crt', '.der', '.p12', '.p7s', '.p8', '.pfx',
}



def resource_path(relative):
    """Works both in development and when bundled by PyInstaller."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


_TEXT_RENDERABLE_EXTENSIONS = frozenset({
    'json', 'xml', 'plist', 'html', 'htm', 'xhtml',
    'txt', 'log', 'csv', 'md', 'yaml', 'yml', 'ini', 'conf', 'cfg',
    'js', 'ts', 'css', 'py', 'java', 'kt', 'swift', 'm', 'h', 'c', 'cpp',
})


def _get_file_type(name):
    ext = os.path.splitext(name)[1].lower()
    # Check multi-part extensions first (.db-wal etc.)
    lower_name = name.lower()
    for db_ext in ('.db-wal', '.db-shm', '.db-journal',
                   '.sqlite-wal', '.sqlite-shm', '.sqlite-journal',
                   '-wal', '-shm'):
        if lower_name.endswith(db_ext):
            return 'Database'
    if ext in PICTURE_EXTENSIONS:       return 'Picture'
    if ext in VIDEO_EXTENSIONS:         return 'Video'
    if ext in DATABASE_EXTENSIONS:      return 'Database'
    if ext == '.plist':                 return 'Property List'
    if ext in LOG_EXTENSIONS:           return 'Log'
    if ext in DOCUMENT_EXTENSIONS:      return 'Document'
    if ext in AUDIO_EXTENSIONS:         return 'Audio'
    if ext in ARCHIVE_EXTENSIONS:       return 'Archive'
    if ext in APPLICATION_EXTENSIONS:   return 'Application'
    if ext in JSON_EXTENSIONS:          return 'JSON'
    if ext in XML_EXTENSIONS:           return 'XML'
    if ext in WEB_DATA_EXTENSIONS:      return 'Web / Data'
    if ext in CERTIFICATE_EXTENSIONS:   return 'Certificate'
    return 'Other'



def _load_json_file(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


_hw = _load_json_file(resource_path(HARDWARE_MODELS_FILE), {})
_HW_BOARD_NAMES: dict   = _hw.get('apple_board_ids', {})
_HW_MODEL_NAMES: dict   = _hw.get('apple_model_ids', {})
_HW_ANDROID_MODELS: dict = _hw.get('android_model_ids', {})


def _read_text_from_zip(z, *candidates) -> str:
    """Return the text content of the first matching candidate path, or ''."""
    names = frozenset(z.namelist())
    for path in candidates:
        if path in names:
            try:
                return z.open(path).read().decode('utf-8', errors='replace')
            except Exception:
                pass
    return ''


def _parse_build_prop(content: str) -> dict:
    """Parse an Android build.prop file and return a key→value dict."""
    props = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, _, v = line.partition('=')
            props[k.strip()] = v.strip()
    return props


def _read_plist_from_zip(z: zipfile.ZipFile, *candidates) -> dict:
    """Try each candidate path in the zip and return the first successfully
    parsed plist as a dict, or {} if none found."""
    names = frozenset(z.namelist())
    for path in candidates:
        if path in names:
            try:
                return plistlib.loads(z.read(path))
            except Exception:
                pass
    return {}


def _plist_find(d, key, _depth=0):
    """Recursively search a plist dict for the first occurrence of key."""
    if not isinstance(d, dict) or _depth > 6:
        return None
    if key in d:
        v = d[key]
        return v if isinstance(v, str) and v else None
    for v in d.values():
        result = _plist_find(v, key, _depth + 1)
        if result:
            return result
    return None


def _read_device_info_from_ufd(zip_path: str) -> tuple[list[tuple[str,str,str]], str]:
    """Return (fields, label) from a companion .ufd file, or ([], '') if none.

    fields — [(field_name, data, source), ...] where source is 'UFD'.
    label  — short display string suitable for the archive dropdown.
    """
    ufd_path = os.path.splitext(zip_path)[0] + '.ufd'
    if not os.path.isfile(ufd_path):
        return [], ''
    try:
        cp = configparser.ConfigParser(strict=False)
        cp.read(ufd_path, encoding='utf-8-sig')

        def _get(*args):
            for section, key in args:
                try:
                    v = cp.get(section, key).strip()
                    if v:
                        return v
                except (configparser.NoSectionError, configparser.NoOptionError):
                    pass
            return ''

        vendor   = _get(('DeviceInfo', 'Vendor'),  ('General', 'Vendor'))
        os_str   = _get(('DeviceInfo', 'OS'),       ('General', 'OS'))
        friendly = _get(('General', 'FullName'), ('DeviceInfo', 'DeviceModel'),
                        ('General', 'Model'))
        hw_id    = _get(('DeviceInfo', 'Model'), ('General', 'Model'))

        if not os_str:
            return [], ''

        is_android = os_str.lower().startswith('android')
        if is_android:
            version  = os_str[len('android'):].strip() or os_str
            os_field = 'Android Version'
            platform = 'Android'
        else:
            version  = os_str.split()[0]   # "17.5.1 (21F90)" → "17.5.1"
            os_field = 'iOS Version'
            platform = 'iOS'

        make  = vendor.title() if vendor else ''
        model = friendly or hw_id

        fields: list[tuple[str,str,str]] = []
        if make:    fields.append(('Make',        make,    'UFD'))
        if model:   fields.append(('Model',       model,   'UFD'))
        if version: fields.append((os_field,      version, 'UFD'))
        if hw_id:   fields.append(('Hardware ID', hw_id,   'UFD'))

        label = _build_label(f'{make} {model}'.strip(), version, platform)
        return fields, label
    except Exception:
        return [], ''


_MG_PLIST_SUFFIX = ('containers/Shared/SystemGroup/'
                    'systemgroup.com.apple.mobilegestaltcache/Library/Caches/'
                    'com.apple.MobileGestalt.plist')


def _lookup_hw_id(hw_id: str) -> tuple[str, str]:
    """Return (make, model) for an Apple board or model identifier, or ('', hw_id) if unknown."""
    entry = _HW_BOARD_NAMES.get(hw_id) or _HW_MODEL_NAMES.get(hw_id)
    if entry:
        return entry[0], entry[1]
    return '', hw_id if hw_id else ''


def _build_label(model_name: str, version: str, platform: str) -> str:
    """Return a short display label e.g. 'Apple iPhone 14 Pro · iOS 17.4'."""
    if model_name and version:
        return f'{model_name} · {platform} {version}'
    return model_name or (f'{platform} {version}' if version else '')


def _read_ios_info(z: zipfile.ZipFile, ffs_adapter) -> tuple[list[tuple[str,str,str]], str]:
    """Extract iOS device info from MobileGestalt, SystemVersion, and preferences plists.

    Returns (fields, label) where fields are (field_name, data, source) triples.
    Returns ([], '') if no usable data is found.
    """
    mg_plist = _read_plist_from_zip(z, *ffs_adapter.user_candidates(_MG_PLIST_SUFFIX))

    ios_version = _plist_find(mg_plist, 'ProductVersion') or ''
    ios_version_source = 'MobileGestalt.plist' if ios_version else ''
    if not ios_version:
        sv = _read_plist_from_zip(z,
            *ffs_adapter.system_candidates('System/Library/CoreServices/SystemVersion.plist'),
            *ffs_adapter.user_candidates('run/SystemVersion.plist'),
        )
        ios_version = sv.get('ProductVersion', '')
        if ios_version:
            ios_version_source = 'SystemVersion.plist'

    pref = _read_plist_from_zip(z,
        *ffs_adapter.user_candidates('preferences/SystemConfiguration/preferences.plist'))

    hw_id        = ''
    hw_id_source = ''
    if pref.get('Model'):
        hw_id        = pref['Model']
        hw_id_source = 'preferences.plist'
    elif _plist_find(mg_plist, 'ProductType'):
        hw_id        = _plist_find(mg_plist, 'ProductType')
        hw_id_source = 'MobileGestalt.plist'
    elif _plist_find(mg_plist, 'HardwareModel'):
        hw_id        = _plist_find(mg_plist, 'HardwareModel')
        hw_id_source = 'MobileGestalt.plist'

    make, model = _lookup_hw_id(hw_id) if hw_id else ('', '')
    model_name  = f'{make} {model}'.strip()
    label       = _build_label(model_name, ios_version, 'iOS')
    if not label:
        return [], ''

    hw_db_src = f'HW DB ({hw_id} via {hw_id_source})' if hw_id else 'Hardware model database'
    fields: list[tuple[str,str,str]] = []
    if make:        fields.append(('Make',        make,        hw_db_src))
    if model:       fields.append(('Model',       model,       hw_db_src))
    if ios_version: fields.append(('iOS Version', ios_version, ios_version_source))
    if hw_id:       fields.append(('Hardware ID', hw_id,       hw_id_source))
    return fields, label


def _read_android_info(z: zipfile.ZipFile, ffs_adapter) -> tuple[list[tuple[str,str,str]], str]:
    """Extract Android device info by merging system/vendor/product build.prop files.

    Returns (fields, label) where fields are (field_name, data, source) triples.
    Returns ([], '') if no usable data is found.
    """
    bp: dict[str, tuple[str, str]] = {}   # key → (value, source_file)
    for prop_file in ('system/build.prop', 'vendor/build.prop', 'product/build.prop'):
        content = _read_text_from_zip(z, *ffs_adapter.system_candidates(prop_file))
        if content:
            for k, v in _parse_build_prop(content).items():
                if v and k not in bp:
                    bp[k] = (v, prop_file)
    if not bp:
        return [], ''

    def _first(*keys: str) -> tuple[str, str]:
        for k in keys:
            v, src = bp.get(k, ('', ''))
            if v:
                return v, src
        return '', ''

    raw_model, raw_model_src = _first(
        'ro.product.model', 'ro.product.vendor.model', 'ro.product.system.model')
    version, version_src = _first(
        'ro.build.version.release', 'ro.vendor.build.version.release')

    hw_entry = _HW_ANDROID_MODELS.get(raw_model)
    if hw_entry:
        make, model = hw_entry[0], hw_entry[1]
        hw_db_src = f'HW DB ({raw_model} via {raw_model_src})'
        make_src  = hw_db_src
        model_src = hw_db_src
    else:
        raw_make, make_src = _first(
            'ro.product.manufacturer', 'ro.product.vendor.manufacturer',
            'ro.product.system.manufacturer', 'ro.product.brand',
            'ro.product.system.brand')
        make      = raw_make.title() if raw_make else ''
        model     = raw_model
        model_src = raw_model_src

    model_name = f'{make} {model}'.strip()
    label      = _build_label(model_name, version, 'Android')
    if not label:
        return [], ''

    fields: list[tuple[str,str,str]] = []
    if make:    fields.append(('Make',            make,    make_src))
    if model:   fields.append(('Model',           model,   model_src))
    if version: fields.append(('Android Version', version, version_src))
    return fields, label


def _read_device_info(zip_path: str) -> tuple[list[tuple[str,str,str]], str]:
    """Return (fields, label) extracted from the FFS zip.

    fields — [(field_name, data, source), ...]
    label  — short display string like 'Apple iPhone 14 Pro · iOS 17.4.1'
    Returns ([], '') on failure.
    """
    fields, label = _read_device_info_from_ufd(zip_path)
    if fields:
        return fields, label
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            ffs_adapter = FfsAdapter.detect(z)
            result = _read_ios_info(z, ffs_adapter)
            if not result[0]:
                result = _read_android_info(z, ffs_adapter)
            return result
    except Exception:
        return [], ''


class ExtractorWorker(QThread):
    file_count = Signal(int)          # total files, emitted once before loop
    progress   = Signal(int, int)     # (current, total)
    status     = Signal(str)
    finished   = Signal(bool, str, str)

    def __init__(self, zip_path, export_tasks, dest_dir, folder_map, path_resolver):
        super().__init__()
        self.zip_path = zip_path
        self.export_tasks = export_tasks
        self.dest_dir = dest_dir
        self.folder_map = folder_map
        self.path_resolver = path_resolver
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        _CHUNK = 1024 * 1024  # 1 MB copy buffer
        try:
            self.status.emit("Initializing extraction...")

            # Build the file queue and pre-fetch all ZipInfo in one pass
            with zipfile.ZipFile(self.zip_path, 'r') as z:
                final_queue = []
                for ui_logical_path, base_parent in self.export_tasks:
                    if ui_logical_path in self.folder_map:
                        children = self._get_all_children(ui_logical_path)
                        for child in children:
                            rel = os.path.relpath(child, start=base_parent)
                            final_queue.append((child, rel))
                    else:
                        rel = os.path.basename(ui_logical_path)
                        final_queue.append((ui_logical_path, rel))

                total = len(final_queue)
                if total == 0:
                    self.finished.emit(False, "No files found to export.", "")
                    return

                # Pre-fetch ZipInfo for every file (one central-directory lookup each)
                zip_infos = {}
                for ui_path, _ in final_queue:
                    phys = self.path_resolver(ui_path)
                    try:
                        zip_infos[phys] = z.getinfo(phys)
                    except KeyError:
                        pass

            self.file_count.emit(total)

            # Single raw file handle stays open for all STORED reads
            with open(self.zip_path, 'rb') as raw_f, \
                 zipfile.ZipFile(self.zip_path, 'r') as z:

                for i, (ui_path, rel_path) in enumerate(final_queue):
                    if self._cancelled:
                        self.finished.emit(False, "Export cancelled.", "")
                        return

                    physical_path = self.path_resolver(ui_path)
                    path_segments = list(os.path.split(rel_path))
                    if path_segments[1].startswith('.'):
                        path_segments[1] = '_' + path_segments[1][1:]
                    sanitized_rel = os.path.join(*path_segments)
                    dest_path = os.path.join(self.dest_dir, sanitized_rel)
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

                    if i % 50 == 0 or i == total - 1:
                        self.status.emit(sanitized_rel)

                    zinfo = zip_infos.get(physical_path)
                    if zinfo is None:
                        continue

                    try:
                        if zinfo.compress_type == zipfile.ZIP_STORED:
                            raw_f.seek(zinfo.header_offset + 26)
                            fname_len, extra_len = struct.unpack('<HH', raw_f.read(4))
                            raw_f.seek(zinfo.header_offset + 30 + fname_len + extra_len)
                            remaining = zinfo.file_size
                            with open(dest_path, 'wb') as target:
                                while remaining > 0:
                                    chunk = raw_f.read(min(_CHUNK, remaining))
                                    if not chunk:
                                        break
                                    target.write(chunk)
                                    remaining -= len(chunk)
                        else:
                            with z.open(physical_path) as source, \
                                 open(dest_path, 'wb') as target:
                                while chunk := source.read(_CHUNK):
                                    target.write(chunk)
                    except KeyError:
                        continue

                    self.progress.emit(i + 1, total)

            self.finished.emit(True, f"Successfully exported {total} items.", self.dest_dir)
        except Exception as e:
            self.finished.emit(False, f"Export Failed: {str(e)}", "")

    def _get_all_children(self, path, visited=None):
        if visited is None:
            visited = set()
        if path in visited:
            return []
        visited.add(path)
        children = []
        if path in self.folder_map:
            for sub_path in self.folder_map[path]:
                if sub_path in self.folder_map:
                    children.extend(self._get_all_children(sub_path, visited))
                else:
                    children.append(sub_path)
        else:
            children.append(path)
        return children


def _compute_folder_sizes(
    folder_map: dict,
    zip_names: frozenset,
    zip_sizes: dict,
    ffs_adapter,
    zip_ui_paths: frozenset | None = None,
    progress_cb=None,
) -> dict:
    """Compute total bytes per folder using a bottom-up (leaf-first) algorithm.

    Returns {folder_path: total_bytes} for every folder except the root ('').

    Step 1 accumulates direct file sizes into each immediate parent.
    Step 2 propagates child totals upward sorted deepest-first so every
    subtotal is resolved before its parent is visited.  The root ('') is
    intentionally excluded — it would just be the sum of every file and
    provides no actionable insight in the UI.
    """
    folder_bytes: dict[str, int] = {fp: 0 for fp in folder_map}
    total = len(folder_map)
    done = [0]
    _REPORT_EVERY = 2000

    # Step 1: direct file contributions into each immediate parent.
    for fp, children in folder_map.items():
        for child in children:
            if child in folder_map:
                continue  # subfolder — handled in step 2
            if zip_ui_paths is not None:
                in_zip = child in zip_ui_paths
            else:
                resolved = ffs_adapter.resolve(child)
                in_zip = resolved in zip_names or resolved.lstrip('/') in zip_names
            if not in_zip or not zip_sizes:
                continue
            raw = ffs_adapter.resolve(child)
            # Bare directory entries are keyed with a trailing slash in zip_names.
            if (raw + '/') in zip_names or (raw.lstrip('/') + '/') in zip_names:
                continue
            size = zip_sizes.get(raw, zip_sizes.get(raw.lstrip('/'), 0))
            folder_bytes[fp] += size

        done[0] += 1
        if progress_cb and done[0] % _REPORT_EVERY == 0:
            progress_cb(done[0], total)
            time.sleep(0)

    # Step 2: propagate child totals up to parents, deepest folders first.
    for fp in sorted(folder_bytes, key=lambda p: p.count('/'), reverse=True):
        if not fp:
            continue  # never propagate from root
        parent = fp.rsplit('/', 1)[0] if '/' in fp else ''
        if parent and parent in folder_bytes:
            folder_bytes[parent] += folder_bytes[fp]

    folder_bytes.pop('', None)  # remove root
    if progress_cb:
        progress_cb(total, total)
    return folder_bytes


def _find_metadata_only_folders(
    folder_map: dict,
    zip_names: frozenset,
    zip_ui_paths: frozenset | None,
    ffs_adapter,
) -> set:
    """Return the set of iOS folder paths whose only in-zip content is Apple container metadata.

    A folder qualifies if every file it (recursively) contains is in
    _SYSTEM_METADATA_NAMES and at least one such file is present.  Truly
    empty subfolders are ignored — they do not disqualify the parent.
    Only call this for iOS archives (Graykey / Cellebrite FFS), not Android.
    """
    cache: dict[str, str] = {}  # fp -> "metadata_only" | "empty" | "content"

    def _check(fp: str) -> str:
        if fp in cache:
            return cache[fp]
        cache[fp] = "empty"  # cycle guard
        has_metadata = False
        for child in folder_map.get(fp, []):
            if child in folder_map:
                cs = _check(child)
                if cs == "content":
                    cache[fp] = "content"
                    return "content"
                if cs == "metadata_only":
                    has_metadata = True
                # "empty" subfolder — does not disqualify
            else:
                if zip_ui_paths is not None:
                    in_zip = child in zip_ui_paths
                else:
                    resolved = ffs_adapter.resolve(child)
                    in_zip = resolved in zip_names or resolved.lstrip('/') in zip_names
                if not in_zip:
                    continue
                raw = ffs_adapter.resolve(child)
                if (raw + '/') in zip_names or (raw.lstrip('/') + '/') in zip_names:
                    continue  # bare directory entry — skip
                if child.split('/')[-1] in _SYSTEM_METADATA_NAMES:
                    has_metadata = True
                else:
                    cache[fp] = "content"
                    return "content"
        result = "metadata_only" if has_metadata else "empty"
        cache[fp] = result
        return result

    for fp in folder_map:
        if fp:  # skip root
            _check(fp)

    return {fp for fp, v in cache.items() if v == "metadata_only"}


def _build_folder_tree(ui_metadata: dict, status_cb=None) -> dict:
    """Build and return folder_map from ui_metadata paths."""
    if status_cb:
        status_cb(f"Building folder tree ({len(ui_metadata):,} entries)...")
    folder_map_sets: dict[str, set] = {}
    for i, ui_path in enumerate(ui_metadata):
        if not ui_path:
            continue  # skip empty-string keys from malformed archives
        parent = ui_path.rsplit('/', 1)[0] if '/' in ui_path else ""
        folder_map_sets.setdefault(parent, set()).add(ui_path)
        if i % 20_000 == 0:
            time.sleep(0)   # yield GIL so the main thread can process events
    if status_cb:
        status_cb("Resolving folder hierarchy...")
    for i, path in enumerate(list(folder_map_sets.keys())):
        current = path
        while current:
            parent = current.rsplit('/', 1)[0] if '/' in current else ""
            if parent not in folder_map_sets:
                folder_map_sets[parent] = {current}
            elif current not in folder_map_sets[parent]:
                folder_map_sets[parent].add(current)
            else:
                break
            current = parent
        if i % 20_000 == 0:
            time.sleep(0)
    return {k: list(v) for k, v in folder_map_sets.items()}



def _count_header_candidates(ui_metadata: dict, ffs_adapter) -> int:
    """Count 'Other'-typed files in scan_folders — no I/O, used for dialog display."""
    scan_folders = ffs_adapter.scan_folders()
    return sum(
        1 for ui_path in ui_metadata
        if _get_file_type(ui_path.rsplit('/', 1)[-1]) == 'Other'
        and any(ui_path.startswith(f) for f in scan_folders)
    )


_EXTRACTABLE_TYPES = {'Archive', 'Compressed'}

def _collect_nested_archive_candidates(
    ui_metadata: dict,
    ffs_adapter,
    header_type_overrides: dict,
) -> list[str]:
    """Return ui_paths of Archive- or Compressed-type files in app data scan folders.

    Covers iOS (mobile/Containers/) and Android (data/data/) paths via
    scan_folders(). Type is determined by extension (_get_file_type) or,
    for extensionless files, by the header scan result in header_type_overrides.
    'Archive' = ZIP containers; 'Compressed' = gzip/bzip2/xz streams.
    """
    scan_roots = ffs_adapter.scan_folders()
    results = []
    for ui_path in ui_metadata:
        if not any(ui_path.startswith(p) for p in scan_roots):
            continue
        name = ui_path.rsplit('/', 1)[-1]
        ft   = _get_file_type(name)
        if ft == 'Other':
            ft = header_type_overrides.get(ui_path, 'Other')
        if ft not in _EXTRACTABLE_TYPES:
            continue
        results.append(ui_path)
    return results


def _collect_header_candidates(
    zip_path: str,
    ui_metadata: dict,
    ffs_adapter,
    z=None,
    streaming_index=None,
    delta: int | None = None,
) -> list[tuple[str, int, int]]:
    """Return [(ui_path, data_offset, file_size)] for 'Other'-typed files in scan_folders."""
    scan_folders = ffs_adapter.scan_folders()
    targets = [
        ui_path for ui_path in ui_metadata
        if _get_file_type(ui_path.rsplit('/', 1)[-1]) == 'Other'
        and any(ui_path.startswith(f) for f in scan_folders)
    ]
    if not targets:
        return []

    candidates: list[tuple[str, int, int]] = []

    if streaming_index is not None:
        entries = streaming_index._entries
        for ui_path in targets:
            row = entries.get(ffs_adapter.resolve(ui_path))
            if row:
                candidates.append((ui_path, row[0], row[2]))
    else:
        assert z is not None
        resolve = ffs_adapter.resolve
        target_infos = []
        target_meta: dict[str, tuple[str, int]] = {}  # filename → (ui_path, file_size)
        for ui_path in targets:
            try:
                info = z.getinfo(resolve(ui_path))
                target_infos.append(info)
                target_meta[info.filename] = (ui_path, info.file_size)
            except KeyError:
                pass
        offsets = _compute_data_offsets(zip_path, target_infos, delta=delta)
        for filename, data_offset in offsets.items():
            ui_path, file_size = target_meta[filename]
            candidates.append((ui_path, data_offset, file_size))

    return candidates


def _resolve_file_candidates(
    zip_path: str,
    ui_paths: list,
    ffs_adapter,
    streaming_index=None,
    delta: int | None = None,
) -> list[tuple[str, int, int]]:
    """Return [(ui_path, data_offset, file_size)] for explicit ui_paths."""
    candidates: list[tuple[str, int, int]] = []
    if streaming_index is not None:
        entries = streaming_index._entries
        for ui_path in ui_paths:
            row = entries.get(ffs_adapter.resolve(ui_path))
            if row:
                candidates.append((ui_path, row[0], row[2]))
    else:
        try:
            z = zipfile.ZipFile(zip_path, 'r')
        except Exception:
            return []
        try:
            resolve = ffs_adapter.resolve
            target_infos = []
            target_meta: dict[str, tuple[str, int]] = {}
            for ui_path in ui_paths:
                try:
                    info = z.getinfo(resolve(ui_path))
                    target_infos.append(info)
                    target_meta[info.filename] = (ui_path, info.file_size)
                except KeyError:
                    pass
            offsets = _compute_data_offsets(zip_path, target_infos, delta=delta)
            for filename, data_offset in offsets.items():
                ui_path, file_size = target_meta[filename]
                candidates.append((ui_path, data_offset, file_size))
        finally:
            z.close()
    return candidates


class ZipMetadataWorker(QThread):
    """Background thread that loads and processes an FFS archive. Steps:
      1. Open the zip (falls back to StreamingZipIndex if no central directory)
      2. Detect the archive format (Graykey / Cellebrite iOS / Cellebrite Android)
      3. Load the GUID→bundle-ID map from cache or build it from the archive
      4. Build ui_metadata (path → timestamps/size dict) via the format adapter
      5. Build the folder tree hierarchy
      6. Identify UUID app-container folders with missing plist metadata
      7. Compute folder sizes (from cache if available, otherwise calculated)
      8. Emit metadata_ready so the UI tree can appear immediately
      9. Persist GUID map and folder sizes to the SQLite case database
     10. If scan_headers is set, read magic bytes of unknown-extension files
         and emit header_scan_done with the detected type overrides
    """
    status_update        = Signal(str)    # message
    metadata_ready       = Signal(dict, dict, dict, object, object, object, dict, object)  # ui_metadata, folder_map, guid_to_bundle, zip_names, adapter, missing_plist_paths, folder_sizes, zip_ui_paths
    header_scan_progress = Signal(int)   # remaining (decreasing)
    header_scan_done     = Signal(dict)  # {ui_path: detected_type}

    def __init__(self, zip_path: str, scan_headers: bool = False, case_dir: str | None = None):
        super().__init__()
        self.zip_path = zip_path
        self.scan_headers = scan_headers
        self.case_dir = case_dir

    def run(self):
        try:
            self.status_update.emit("Opening Archive...")
            self._streaming_index    = None
            self._local_extra_delta: int | None = None

            if self.case_dir:
                try:
                    check_cache_schema(self.case_dir)
                except OldSchemaError:
                    self.status_update.emit(
                        "Old casecache.db detected — rebuilding cache database…")
                    try:
                        os.remove(os.path.join(self.case_dir, 'casecache.db'))
                    except OSError:
                        pass
                try:
                    check_results_schema(self.case_dir)
                except OldSchemaError as e:
                    self.status_update.emit(
                        f"Warning: {e}")

            # ── Open the archive ──────────────────────────────────────────────
            if self.case_dir:
                # Ensure local .zcd exists — copy on first open, always use local copy
                if not _cd_cache_valid(self.zip_path, self.case_dir):
                    self.status_update.emit(
                        "Copying central directory to case folder (first open — may be slow on network)…")
                    _t0 = time.monotonic()
                    _saved = _cd_cache_save(
                        self.zip_path, self.case_dir,
                        progress_cb=lambda done, total: self.status_update.emit(
                            f"Copying central directory… {done / max(total, 1):.0%}"),
                    )
                    if not _saved:
                        # No seekable CD — fall back to streaming index
                        self.status_update.emit("Streaming zip detected — building index...")
                        self._streaming_index = StreamingZipIndex.open(
                            self.zip_path,
                            progress_cb=lambda done, total: self.status_update.emit(
                                f"Indexing archive: {done / max(total, 1):.0%}"),
                        )
                    else:
                        _elapsed = time.monotonic() - _t0
                        self.status_update.emit(
                            f"Central directory copied to case folder in {_elapsed:.1f}s.")

                if self._streaming_index is None:
                    self.status_update.emit("Loading central directory from local copy…")
                    _cached_infos = _cd_cache_load(self.zip_path, self.case_dir)
                    z_ctx = CachedZipView(self.zip_path, _cached_infos) if _cached_infos else None
                    if z_ctx is None:
                        raise RuntimeError("Failed to load central directory from local copy")
                else:
                    z_ctx = None
            else:
                # No case dir — open the network file directly
                try:
                    z_ctx = zipfile.ZipFile(self.zip_path, 'r')
                    z_ctx.__enter__()
                except zipfile.BadZipFile:
                    self.status_update.emit("Streaming zip detected — building index...")
                    self._streaming_index = StreamingZipIndex.open(
                        self.zip_path,
                        progress_cb=lambda done, total: self.status_update.emit(
                            f"Indexing archive: {done / max(total, 1):.0%}"),
                    )
                    z_ctx = None

            try:
                self.status_update.emit("Reading zip directory...")
                if self._streaming_index is not None:
                    zip_names = frozenset(self._streaming_index.namelist())
                    zip_sizes: dict[str, int] = {}
                    ffs_adapter = FfsAdapter.detect_from_names(zip_names)
                else:
                    assert z_ctx is not None
                    _infolist = z_ctx.infolist()
                    zip_names = frozenset(info.filename for info in _infolist)
                    zip_sizes = {info.filename: info.file_size for info in _infolist}
                    ffs_adapter = FfsAdapter.detect(z_ctx)
                    _stored = [i for i in _infolist
                               if i.compress_type == zipfile.ZIP_STORED and i.file_size > 0]
                    self._local_extra_delta = _probe_delta(self.zip_path, _stored)
                time.sleep(0)   # yield GIL after C-level frozenset build

                cached_guid_bundle: dict | None = None
                if self.case_dir:
                    try:
                        _db = _open_cache_db(self.case_dir)
                        _cached = load_guid_bundle_map(_db)
                        _db.close()
                        if _cached:
                            cached_guid_bundle = _cached
                    except Exception:
                        pass

                # When using the local .zcd cache zip_names is already in memory,
                # so build_zip_entries runs at pure-Python speed with no network seeks.
                # Only Cellebrite iOS uses Pass 1; GrayKey/Android return early.
                _precomputed_zip_entries = (
                    ffs_adapter.build_zip_entries(zip_names)
                    if isinstance(z_ctx, CachedZipView)
                    and ffs_adapter.format == FfsAdapter.FORMAT_CELLEBRITE
                    else None
                )

                ui_metadata, guid_to_bundle, zip_ui_paths = ffs_adapter.build_ui_metadata(
                    self.zip_path, zip_names,
                    z=z_ctx,
                    streaming_index=self._streaming_index,
                    status_cb=self.status_update.emit,
                    guid_to_bundle=cached_guid_bundle,
                    zip_entries=_precomputed_zip_entries,
                )
                time.sleep(0)   # yield GIL after msgpack/metadata build

                _need_save_guid = cached_guid_bundle is None and bool(guid_to_bundle)
                header_candidates: list = []
                if self.scan_headers:
                    self.status_update.emit(
                        "Collecting header scan candidates from archive (may be slow on network)…")
                    header_candidates = _collect_header_candidates(
                        self.zip_path, ui_metadata, ffs_adapter,
                        z=z_ctx, streaming_index=self._streaming_index,
                        delta=self._local_extra_delta,
                    )
            finally:
                if z_ctx is not None:
                    z_ctx.__exit__(None, None, None)

            folder_map = _build_folder_tree(ui_metadata, status_cb=self.status_update.emit)
            time.sleep(0)   # yield GIL after folder tree build

            _CONTAINER_PARENTS = ffs_adapter.container_parents()
            missing_plist_paths = [
                p for p in folder_map
                if p.rsplit('/', 1)[0] in _CONTAINER_PARENTS
                and _UUID_RE.match(p.split('/')[-1])
                and p.split('/')[-1] not in guid_to_bundle
            ]

            _cached_sizes: dict | None = None
            if self.case_dir:
                try:
                    _db = _open_cache_db(self.case_dir)
                    _, _fs = load_folder_data(_db)
                    _db.close()
                    if _fs:
                        _cached_sizes = _fs
                except Exception:
                    pass

            if _cached_sizes is not None:
                self.status_update.emit("Folder sizes loaded from cache.")
                folder_sizes = _cached_sizes
            else:
                self.status_update.emit("Computing folder sizes...")
                folder_sizes = _compute_folder_sizes(
                    folder_map, zip_names, zip_sizes, ffs_adapter,
                    zip_ui_paths=zip_ui_paths,
                    progress_cb=lambda done, total: self.status_update.emit(
                        f"Computing folder sizes… {done:,}/{total:,}"))

            # Emit first — UI tree appears immediately. DB and cache writes follow.
            self.metadata_ready.emit(
                ui_metadata, folder_map, guid_to_bundle, zip_names,
                ffs_adapter, missing_plist_paths, folder_sizes, zip_ui_paths)

            if self.case_dir:
                try:
                    _db = _open_cache_db(self.case_dir)
                    if _need_save_guid:
                        save_guid_bundle_map(_db, guid_to_bundle)
                    if _cached_sizes is None:
                        save_folder_sizes(_db, folder_sizes)
                    _db.close()
                except Exception:
                    pass

            if header_candidates:
                self.status_update.emit(
                    f"Scanning headers: {len(header_candidates):,} unknown files...")
                self.header_scan_progress.emit(len(header_candidates))
                results = header_scan.scan_entries(
                    self.zip_path, header_candidates,
                    progress_cb=self.header_scan_progress.emit,
                )
                self.header_scan_done.emit(results)
                self.status_update.emit(
                    f"Header scan complete — {len(results):,} types identified")
        except Exception as e:
            self.status_update.emit(f"Error: {str(e)}")



_FILTER_CHUNK = 8000   # rows processed per frame during incremental filter

_HEADER_TOOLTIPS: dict[str, str] = {
    'Created': 'btime — file birth time (when first created)',
    'Changed': 'ctime — last metadata change (permissions, ownership, or content write)',
    'Modified': 'mtime — last content write',
}


class FileTableModel(QAbstractTableModel):
    """Lightweight table model backed by a plain Python list.
    Dramatically faster than QStandardItemModel for large row counts:
    no per-cell QStandardItem allocation, sort() runs as a native
    Python list sort, and filtering is incremental (chunked per frame)
    so the visible row count decreases live while filtering runs."""

    filter_progress = Signal(int, int)   # (visible, total)
    filter_done     = Signal(int, int)   # (visible, total)

    _GREY_COLOR        = QColor(Qt.GlobalColor.darkGray)
    _BOLD_FONT         = QFont("Arial", weight=QFont.Weight.ExtraBold)
    _BOLD_ITALIC_FONT  = QFont("Arial", weight=QFont.Weight.ExtraBold, italic=True)

    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self._headers  = list(headers)
        self._all_rows = []   # all rows, never modified by filtering
        self._rows     = []   # currently visible (filtered) rows
        self._files_col = self._headers.index('Files') if 'Files' in self._headers else -1
        self._size_col  = self._headers.index('Size (Bytes)') if 'Size (Bytes)' in self._headers else -1
        self._filter_gen = 0  # bump to cancel an in-flight filter
        self._sort_col: int = -1
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder
        self._show_files   = True
        self._show_folders = True

    # ── required overrides ──────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()):
        return 0 if parent.isValid() else len(self._headers)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            cols = row[0]
            return cols[col] if col < len(cols) else None
        if role == Qt.ItemDataRole.UserRole:
            return row[1]
        if role == Qt.ItemDataRole.UserRole + 1:
            return row[2]
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._GREY_COLOR if row[4] else None
        if role == Qt.ItemDataRole.FontRole:
            if row[3]:
                return self._BOLD_ITALIC_FONT if (len(row) > 5 and row[5]) else self._BOLD_FONT
            return None
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._headers):
            if role == Qt.ItemDataRole.DisplayRole:
                return self._headers[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                return _HEADER_TOOLTIPS.get(self._headers[section])
        return None

    # ── bulk mutation ────────────────────────────────────────────────────────

    def append_rows_batch(self, rows):
        """Append rows to _all_rows; only add to _rows those passing the type filter."""
        if not rows:
            return
        self._all_rows.extend(rows)
        visible = [r for r in rows if (self._show_folders if r[2] >= 0 else self._show_files)]
        if visible:
            first = len(self._rows)
            last  = first + len(visible) - 1
            self.beginInsertRows(QModelIndex(), first, last)
            self._rows.extend(visible)
            self.endInsertRows()

    # ── filtering ────────────────────────────────────────────────────────────

    def set_filter(self, text, col):
        """Start an incremental filter. Clears visible rows immediately, then
        adds matching rows in chunks so the UI count updates live."""
        self._filter_gen += 1
        my_gen = self._filter_gen
        needle = text.lower()
        ncols  = self.columnCount()
        check_cols = list(range(ncols)) if col < 0 else [col]
        total  = len(self._all_rows)

        # Clear visible rows immediately
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

        show_files   = self._show_files
        show_folders = self._show_folders

        def _type_ok(r):
            is_folder = r[2] >= 0  # fc == -1 for files, >= 0 for folders
            return (show_folders if is_folder else show_files)

        if not needle:
            visible = [r for r in self._all_rows if _type_ok(r)]
            self.beginInsertRows(QModelIndex(), 0, len(visible) - 1)
            self._rows = visible
            self.endInsertRows()
            self.filter_done.emit(len(visible), total)
            return

        source = self._all_rows
        idx = [0]

        def _chunk():
            if self._filter_gen != my_gen:
                return  # superseded
            end = min(idx[0] + _FILTER_CHUNK, total)
            matched = [
                r for r in source[idx[0]:end]
                if _type_ok(r) and any(r[0][c].lower().find(needle) != -1
                       for c in check_cols if c < len(r[0]))
            ]
            if matched:
                first = len(self._rows)
                last  = first + len(matched) - 1
                self.beginInsertRows(QModelIndex(), first, last)
                self._rows.extend(matched)
                self.endInsertRows()
            idx[0] = end
            self.filter_progress.emit(len(self._rows), total)
            if idx[0] < total:
                QTimer.singleShot(0, _chunk)
            else:
                self.filter_done.emit(len(self._rows), total)

        QTimer.singleShot(0, _chunk)

    def set_type_filter(self, show_files: bool, show_folders: bool, current_text: str, current_col: int):
        self._show_files   = show_files
        self._show_folders = show_folders
        self.set_filter(current_text, current_col)

    # ── sorting ──────────────────────────────────────────────────────────────

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """Sort both _all_rows and _rows so sort survives re-filtering."""
        self.layoutAboutToBeChanged.emit()
        self._sort_col = column
        self._sort_order = order
        reverse = (order == Qt.SortOrder.DescendingOrder)
        if column == self._files_col:
            key = lambda r: r[2]
        elif column == self._size_col:
            key = lambda r: int(r[0][column].replace(',', '')) if column < len(r[0]) and r[0][column] else 0
        else:
            key = lambda r: r[0][column].lower() if column < len(r[0]) and r[0][column] else ''
        self._all_rows.sort(key=key, reverse=reverse)
        self._rows.sort(key=key, reverse=reverse)
        self.layoutChanged.emit()


class MultiColumnFilterProxy(QSortFilterProxyModel):
    """Thin proxy used only for sort delegation — filtering is now handled
    inside FileTableModel so filterAcceptsRow always returns True."""

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        src = self.sourceModel()
        if src is not None:
            src.sort(column, order)
            self.invalidate()

    def set_filter(self, text, col):
        src = self.sourceModel()
        if src is not None:
            assert isinstance(src, FileTableModel)
            src.set_filter(text, col)


class ExportProgressDialog(QDialog):
    cancel_requested = Signal()

    def __init__(self, dest_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Exporting Files")
        self.setMinimumWidth(540)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._running = True

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        dest_label = QLabel(f"<b>Destination:</b> {dest_dir}")
        dest_label.setWordWrap(True)
        layout.addWidget(dest_label)

        self._count_label = QLabel("Preparing export…")
        layout.addWidget(self._count_label)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)   # indeterminate until file_count arrives
        layout.addWidget(self._bar)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        self._ok_btn = QPushButton("OK")
        self._ok_btn.setVisible(False)
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

    # --- slots wired to ExtractorWorker signals ---

    def on_file_count(self, total: int):
        self._count_label.setText(f"Exporting {total:,} files…")
        self._bar.setRange(0, total)

    def on_progress(self, current: int, total: int):
        self._bar.setValue(current)
        self._count_label.setText(f"Exporting {current:,} of {total:,} files…")

    def on_status(self, msg: str):
        self._status_label.setText(msg)

    def on_finished(self, success: bool, message: str, _dest: str):
        self._running = False
        self._bar.setRange(0, max(self._bar.maximum(), 1))
        self._bar.setValue(self._bar.maximum())
        self._cancel_btn.setVisible(False)
        self._ok_btn.setVisible(True)
        if success:
            self._count_label.setText(f"Export complete — {message}")
            self._status_label.setText(
                "Note: Any hidden files whose names began with '.' have been "
                "renamed with a leading '_' so they remain visible on all platforms."
            )
        else:
            self._count_label.setText(message)
            self._status_label.setText("")

    def _on_cancel(self):
        self._cancel_btn.setEnabled(False)
        self._count_label.setText("Cancelling…")
        self._status_label.setText("")
        self.cancel_requested.emit()

    def reject(self):
        # Block Esc / window-close while export is in progress
        if self._running:
            self._on_cancel()
        else:
            super().reject()




class CaseSettingsDialog(QDialog):
    """Shown the first time an FFS zip is opened.

    Lets the user choose the base folder that will hold the per-zip case folder.
    The case folder is created as  <base>/<zip_stem>/  and stores:
      • casedata.db    — thumbnails and search results
      • Export/        — extracted files
    """

    def __init__(self, zip_path: str, last_base: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Case Folder Settings")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._accepted_dir: str | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(
            "<b>Choose where to store the cache and exports for this FFS archive.</b><br>"
            "A subfolder will be created inside the base location you select."
        ))

        # Base folder row
        base_row = QHBoxLayout()
        base_row.addWidget(QLabel("Base folder:"))
        self._base_edit = QLineEdit()
        self._base_edit.setPlaceholderText("Select a folder…")
        if last_base and os.path.isdir(last_base):
            self._base_edit.setText(last_base)
        self._base_edit.textChanged.connect(self._update_preview)
        base_row.addWidget(self._base_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        base_row.addWidget(browse_btn)
        layout.addLayout(base_row)

        # Case folder name row
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Case folder name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setText(pathlib.Path(zip_path).stem)
        self._name_edit.textChanged.connect(self._update_preview)
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)

        # Preview
        self._preview_label = QLabel()
        self._preview_label.setWordWrap(True)
        layout.addWidget(self._preview_label)

        # Header scan option
        self._scan_cb = QCheckBox("Scan unknown file headers for precise type detection")
        self._scan_cb.setChecked(False)
        layout.addWidget(self._scan_cb)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._save_btn = QPushButton("Save Settings")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._save_btn)
        layout.addLayout(btn_row)

        # Populate preview now that _save_btn exists
        self._update_preview()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Base Folder",
                                                  self._base_edit.text())
        if folder:
            self._base_edit.setText(folder)

    def _update_preview(self):
        base = self._base_edit.text().strip()
        name = self._name_edit.text().strip()
        if not base or not name:
            self._preview_label.setText("<i>No folder selected.</i>")
            self._save_btn.setEnabled(False)
            return
        case_dir  = os.path.join(base, name)
        cache_loc = os.path.join(case_dir, 'casedata.db')
        export_loc = os.path.join(case_dir, 'Export', '')
        exists_note = " <b>(already exists)</b>" if os.path.isdir(case_dir) else ""
        self._preview_label.setText(
            f"<b>Case folder:</b> {case_dir}{exists_note}<br>"
            f"<b>Thumbnail cache:</b> {cache_loc}<br>"
            f"<b>Export folder:</b> {export_loc}"
        )
        self._save_btn.setEnabled(True)

    def _on_save(self):
        base = self._base_edit.text().strip()
        name = self._name_edit.text().strip()
        if not base or not name:
            return
        case_dir = os.path.join(base, name)
        if os.path.isdir(case_dir):
            from PySide6.QtWidgets import QMessageBox
            ans = QMessageBox.question(
                self,
                "Case Folder Already Exists",
                f"A folder named <b>{name}</b> already exists at this location.<br><br>"
                f"<b>{case_dir}</b><br><br>"
                "Use the existing folder? (Cache and exports will be shared.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._accepted_dir = case_dir
        self.accept()

    @property
    def case_dir(self) -> str | None:
        return self._accepted_dir

    @property
    def scan_headers(self) -> bool:
        return self._scan_cb.isChecked()


class HeaderScanWorker(QThread):
    """Standalone worker for re-running a header scan after initial load."""
    progress = Signal(int)   # remaining count
    done     = Signal(dict)  # {ui_path: detected_type}

    def __init__(self, zip_path, ui_metadata, ffs_adapter, streaming_index=None,
                 delta=None):
        super().__init__()
        self._zip_path        = zip_path
        self._ui_metadata     = ui_metadata
        self._adapter         = ffs_adapter
        self._streaming_index = streaming_index
        self._delta           = delta

    def run(self):
        z = None
        try:
            if self._streaming_index is None:
                z = zipfile.ZipFile(self._zip_path, 'r')
            candidates = _collect_header_candidates(
                self._zip_path, self._ui_metadata, self._adapter,
                z=z, streaming_index=self._streaming_index,
                delta=self._delta,
            )
        finally:
            if z:
                z.close()

        if not candidates:
            self.done.emit({})
            return

        self.progress.emit(len(candidates))
        results = header_scan.scan_entries(
            self._zip_path, candidates,
            progress_cb=self.progress.emit,
            cancel_check=self.isInterruptionRequested,
        )
        self.done.emit(results)


class SingleFileScanWorker(QThread):
    """Scan file headers for a specific list of ui_paths (not just 'Other' files)."""
    done = Signal(dict)   # {ui_path: detected_type}

    def __init__(self, zip_path, ui_paths, ffs_adapter,
                 streaming_index=None, delta=None):
        super().__init__()
        self._zip_path        = zip_path
        self._ui_paths        = ui_paths
        self._ffs_adapter     = ffs_adapter
        self._streaming_index = streaming_index
        self._delta           = delta

    def run(self):
        candidates = _resolve_file_candidates(
            self._zip_path, self._ui_paths, self._ffs_adapter,
            self._streaming_index, self._delta,
        )
        if not candidates:
            self.done.emit({})
            return
        results = header_scan.scan_entries(self._zip_path, candidates)
        self.done.emit(results)


class NestedArchiveWorker(QThread):
    """Extracts Archive-type files from the FFS zip, repacks as ZIP_STORED,
    saves to case_dir/nested_archives/, and records each in casecache.db."""

    progress      = Signal(int, int)      # (done, total)
    item_done     = Signal(str, bool)    # (ui_path, success)
    item_error    = Signal(str, str)     # (ui_path, error_message)
    types_updated = Signal(dict)         # {ui_path: "JSON — gzip", ...}
    finished      = Signal(int, int)     # (success_count, error_count)

    _MEM_LIMIT = 100 * 1024 * 1024   # 100 MB — larger files written to temp first

    def __init__(self, zip_path, case_dir, candidates, ui_metadata,
                 ffs_adapter, streaming_index=None, delta=None, parent=None):
        super().__init__(parent)
        self._zip_path        = zip_path
        self._case_dir        = case_dir
        self._candidates      = candidates      # list of ui_path strings
        self._ui_metadata     = ui_metadata
        self._adapter         = ffs_adapter
        self._streaming_index = streaming_index
        self._delta           = delta

    def _load_completed(self, out_dir: str) -> set[str]:
        """Return ui_paths already fully extracted: DB entry + file on disk + entry count match."""
        try:
            db   = _open_cache_db(self._case_dir)
            rows = load_nested_archives(db)
            completed: set[str] = set()
            for row in rows:
                ui_path        = row['ui_path']
                stored_filename = row['stored_filename']
                expected_count  = row['entry_count']
                disk_path       = os.path.join(out_dir, stored_filename)
                if not os.path.isfile(disk_path):
                    continue
                actual_count = len(load_nested_archive_entries(db, ui_path))
                if actual_count == expected_count:
                    completed.add(ui_path)
            db.close()
            return completed
        except Exception:
            return set()

    def run(self):
        out_dir  = os.path.join(self._case_dir, 'nested_archives')
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(self._case_dir, 'nested_archive_errors.log')
        try:
            os.remove(log_path)
        except OSError:
            pass
        completed  = self._load_completed(out_dir)
        ok = err = skipped = 0
        total       = len(self._candidates)
        gzip_types: dict[str, str] = {}
        for i, ui_path in enumerate(self._candidates):
            if self.isInterruptionRequested():
                break
            self.progress.emit(i, total)
            if ui_path in completed:
                skipped += 1
                ok += 1
                self.item_done.emit(ui_path, True)
                continue
            success, gzip_type = self._process_one(ui_path, out_dir)
            if success:
                ok += 1
                if gzip_type:
                    gzip_types[ui_path] = gzip_type
            else:
                err += 1
            self.item_done.emit(ui_path, success)
        self.progress.emit(total, total)
        if gzip_types:
            self.types_updated.emit(gzip_types)
        self.finished.emit(ok, err)

    def _process_one(self, ui_path: str, out_dir: str) -> tuple[bool, str | None]:
        """Process one candidate.  Returns (success, compound_type_or_None).

        compound_type is set for gzip files where the decompressed content
        type is known, e.g. 'JSON — gzip'.  None for zip files or unknown.
        """
        try:
            physical  = self._adapter.resolve(ui_path)
            meta      = self._ui_metadata.get(ui_path, {})
            file_size = meta.get('size', 0)

            # ── Read raw bytes from FFS zip ───────────────────────────────────
            if self._streaming_index is not None:
                raw = self._streaming_index.get_entry(physical).read()
            else:
                with zipfile.ZipFile(self._zip_path, 'r') as zf:
                    raw = zf.read(physical)

            key             = hashlib.sha1(ui_path.encode()).hexdigest()[:12]
            basename        = os.path.basename(ui_path) or 'content'
            stored_filename = f"{key}_{basename}"
            out_path        = os.path.join(out_dir, stored_filename)

            if raw[:2] == b'\x1f\x8b':
                # ── Gzip: decompress and store the raw decompressed file ──────
                decompressed = gzip.decompress(raw)
                with open(out_path, 'wb') as f:
                    f.write(decompressed)
                child_name = (basename[:-3] if basename.lower().endswith('.gz')
                              else basename)
                ft = _get_file_type(child_name)
                if ft == 'Other' and decompressed:
                    ft = header_scan.classify_magic(decompressed[:16]) or 'Other'
                gz_mtime = struct.unpack_from('<I', raw, 4)[0] if len(raw) >= 8 else 0
                gz_mdate = (datetime.fromtimestamp(gz_mtime, tz=timezone.utc)
                            .strftime('%Y-%m-%d %H:%M:%S') if gz_mtime else None)
                content_rows  = [(child_name, gz_mdate, len(decompressed), ft)]
                entry_count   = 1
                compound_type = f"{ft} — gzip" if ft != 'Other' else None

            else:
                # ── ZIP: repack as ZIP_STORED ─────────────────────────────────
                tmp_path = out_path + '.tmp'
                if file_size > self._MEM_LIMIT:
                    with open(tmp_path, 'wb') as f:
                        f.write(raw)
                    src_arg = tmp_path
                else:
                    src_arg = None

                entry_count   = 0
                content_rows  = []
                compound_type = None
                src_handle   = open(src_arg, 'rb') if src_arg else io.BytesIO(raw)
                try:
                    with zipfile.ZipFile(src_handle, 'r') as src_zip, \
                         zipfile.ZipFile(out_path, 'w',
                                         compression=zipfile.ZIP_STORED) as dst_zip:
                        for info in src_zip.infolist():
                            if info.filename.endswith('/'):
                                continue   # skip directory entries
                            data = src_zip.read(info.filename)
                            dst_zip.writestr(info, data,
                                             compress_type=zipfile.ZIP_STORED)
                            entry_count += 1
                            name = info.filename
                            ft   = _get_file_type(name.rsplit('/', 1)[-1])
                            if ft == 'Other' and data:
                                ft = header_scan.classify_magic(data[:16]) or 'Other'
                            dt    = info.date_time
                            mdate = (f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d} "
                                     f"{dt[3]:02d}:{dt[4]:02d}:{dt[5]:02d}"
                                     if any(dt) else None)
                            content_rows.append((info.filename, mdate,
                                                 info.file_size, ft))
                finally:
                    if src_arg:
                        src_handle.close()
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass

            # ── Record in casecache.db ────────────────────────────────────────
            db = _open_cache_db(self._case_dir)
            save_nested_archive(db, ui_path, stored_filename,
                                file_size, entry_count)
            save_nested_archive_entries(db, ui_path, content_rows)
            db.close()
            return True, compound_type

        except Exception as exc:
            msg = str(exc)
            self.item_error.emit(ui_path, msg)
            try:
                log_path = os.path.join(self._case_dir, 'nested_archive_errors.log')
                ts = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[{ts}] FAILED: {ui_path}\n  {msg}\n")
            except OSError:
                pass
            return False, None


class ProcessDialog(QDialog):
    """Lets the user view header-scan history and re-run the file header scan."""
    header_scan_done       = Signal(dict)
    header_types_cleared   = Signal()   # emitted before a rescan so the main window can reset
    nested_extraction_done = Signal()   # emitted after NestedArchiveWorker finishes

    _RUN_TYPE = 'header_scan'

    def __init__(self, zip_path, case_dir, ffs_adapter, ui_metadata,
                 streaming_index=None, delta=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Process Archive")
        self.setModal(True)
        self.setMinimumWidth(540)
        self._zip_path        = zip_path
        self._case_dir        = case_dir
        self._adapter         = ffs_adapter
        self._ui_metadata     = ui_metadata
        self._streaming_index = streaming_index
        self._delta           = delta
        self._candidate_count  = _count_header_candidates(ui_metadata, ffs_adapter)
        self._scan_worker:   HeaderScanWorker   | None = None
        self._nested_worker: NestedArchiveWorker | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        layout.addWidget(QLabel(f"<b>Archive:</b> {os.path.basename(zip_path)}"))

        # Case folder row
        case_row = QHBoxLayout()
        case_row.addWidget(QLabel("Case folder:"))
        self._case_edit = QLineEdit(case_dir or "")
        self._case_edit.setReadOnly(True)
        case_row.addWidget(self._case_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_case)
        case_row.addWidget(browse_btn)
        layout.addLayout(case_row)

        layout.addWidget(QLabel("<hr><b>Operations</b>"))

        self._chk_header = QCheckBox("Scan file headers")
        self._chk_header.setChecked(False)
        layout.addWidget(self._chk_header)

        self._chk_nested = QCheckBox("Uncompress ZIPs in app data")
        self._chk_nested.setChecked(False)
        self._chk_nested.setToolTip(
            "Finds Archive-type files in app containers, repacks them as "
            "uncompressed ZIPs,\nand records them in the case database. "
            "Requires header scan.")
        self._chk_nested.toggled.connect(self._on_nested_toggled)
        layout.addWidget(self._chk_nested)

        # Stats block
        self._stats_label = QLabel()
        self._stats_label.setWordWrap(True)
        layout.addWidget(self._stats_label)

        # Progress / status
        self._status_label = QLabel()
        layout.addWidget(self._status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._run_btn = QPushButton("Run")
        self._run_btn.clicked.connect(self._run_operations)
        btn_row.addWidget(self._run_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._refresh_stats()

    # ── Stats display ─────────────────────────────────────────────────────────

    def _load_last_run(self) -> dict | None:
        if not self._case_dir:
            return None
        try:
            db = _open_results_db(self._case_dir)
            result = load_last_run(db, self._RUN_TYPE)
            db.close()
            return result
        except Exception:
            return None

    def _load_nested_archives_summary(self) -> list[dict]:
        if not self._case_dir:
            return []
        try:
            db = _open_cache_db(self._case_dir)
            records = load_nested_archives(db)
            db.close()
            return records
        except Exception:
            return []

    def _refresh_stats(self):
        n = self._candidate_count
        last = self._load_last_run()

        lines = [f"<b>Other-typed files in scan folders:</b> {n:,}"]

        if last is None:
            lines.append("No previous header scan recorded.")
        else:
            run_at   = last['run_at'].replace('T', ' ')
            scanned  = last['processed'] or 0
            changed  = last['output_rows'] or 0
            complete = last['complete']
            status   = "Complete" if complete else "⚠ Incomplete"
            lines.append(
                f"<b>Last header scan:</b> {run_at} — "
                f"{scanned:,} scanned, {changed:,} types identified — {status}"
            )
            if not complete:
                lines.append(
                    "<i>Previous scan was incomplete. "
                    "Re-running will clear previous results and start fresh.</i>"
                )

        nested = self._load_nested_archives_summary()
        if nested:
            last_at = max(r['processed_at'] for r in nested).replace('T', ' ')
            lines.append(
                f"<b>Nested archives extracted:</b> {len(nested):,} — "
                f"last run {last_at}"
            )
        else:
            lines.append("No nested archives extracted yet.")

        self._stats_label.setText("<br>".join(lines))

    # ── Case folder browse ────────────────────────────────────────────────────

    def _browse_case(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Case Folder",
                                                  self._case_edit.text())
        if not folder:
            return
        has_sidecar = _cd_sidecar_hash(self._zip_path, folder) is not None
        if not has_sidecar:
            btn = QMessageBox.question(
                self, "New Case Folder",
                f"The selected folder does not contain any existing case data "
                f"for this archive:\n\n{folder}\n\n"
                "Are you sure you want to use this as the case folder?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return
        elif not _do_path_change_check(self, self._zip_path, folder):
            return   # mismatch — error shown, keep current case folder
        self._case_dir = folder
        self._case_edit.setText(folder)
        self._refresh_stats()

    # ── Operations ────────────────────────────────────────────────────────────

    def _on_nested_toggled(self, checked: bool):
        if checked:
            last = self._load_last_run()
            scan_done = last is not None and last.get('complete')
            if not scan_done:
                # No complete scan on record — must run one first.
                self._chk_header.setChecked(True)
                self._chk_header.setEnabled(False)
            # If scan already done, leave the checkbox state as-is and enabled
            # so the user can optionally re-run it alongside extraction.
        else:
            self._chk_header.setEnabled(True)

    def _confirm_rerun(self) -> bool:
        """Return True if user confirms (re-)running already-completed operations."""
        parts: list[str] = []
        last = self._load_last_run()
        if self._chk_header.isChecked() and last and last.get('complete'):
            parts.append("header scan")
        nested = self._load_nested_archives_summary()
        if self._chk_nested.isChecked() and nested:
            parts.append(
                f"nested archive extraction "
                f"({len(nested):,} archive{'s' if len(nested) != 1 else ''} "
                f"already extracted)"
            )
        if not parts:
            return True
        what = " and ".join(parts)
        result = QMessageBox.question(
            self, "Confirm Re-run",
            f"A previous {what} has already been completed for this archive.\n\n"
            "Running again will overwrite the previous results.\n\n"
            "Are you sure you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def _run_operations(self):
        if not self._chk_header.isChecked() and not self._chk_nested.isChecked():
            return
        if not self._confirm_rerun():
            return
        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._scan_cancelled = False
        self._status_label.setText("")

        if self._chk_header.isChecked():
            self._start_header_scan()
        else:
            self._start_nested_extraction()

    def _start_header_scan(self):
        self._run_id: int | None = None

        if self._case_dir:
            try:
                cache_db = _open_cache_db(self._case_dir)
                clear_header_types(cache_db)
                cache_db.close()
            except Exception:
                pass
        self.header_types_cleared.emit()

        if self._case_dir:
            try:
                results_db = _open_results_db(self._case_dir)
                self._run_id = start_run_log(results_db, self._RUN_TYPE,
                                             total=self._candidate_count)
                results_db.close()
            except Exception:
                pass

        self._status_label.setText("Starting header scan…")
        self._scan_worker = HeaderScanWorker(
            self._zip_path, self._ui_metadata,
            self._adapter, self._streaming_index,
            delta=self._delta,
        )
        self._scan_worker.progress.connect(self._on_header_progress)
        self._scan_worker.done.connect(self._on_header_done)
        self._scan_worker.start()

    def _on_header_progress(self, remaining: int):
        self._status_label.setText(
            f"Scanning headers: {remaining:,} files remaining…"
            if remaining > 0 else "Finishing header scan…"
        )

    def _on_header_done(self, results: dict):
        n_found   = len(results)
        n_total   = self._candidate_count
        cancelled = self._scan_cancelled

        if self._case_dir:
            try:
                cache_db = _open_cache_db(self._case_dir)
                if results:
                    save_header_types(cache_db, results)
                cache_db.close()
                if self._run_id is not None and not cancelled:
                    results_db = _open_results_db(self._case_dir)
                    complete_run_log(results_db, self._run_id,
                                     processed=n_total, output_rows=n_found)
                    results_db.close()
            except Exception:
                pass

        self.header_scan_done.emit(results)

        if self._chk_nested.isChecked() and not cancelled:
            self._start_nested_extraction()
        else:
            msg = (f"Cancelled — {n_found:,} type{'s' if n_found != 1 else ''} "
                   "identified before cancellation."
                   if cancelled else
                   f"Header scan done — {n_found:,} type{'s' if n_found != 1 else ''} "
                   f"identified from {n_total:,} candidate{'s' if n_total != 1 else ''}.")
            self._status_label.setText(msg)
            self._finish_operations()

    def _start_nested_extraction(self):
        overrides = {}
        if self._case_dir:
            try:
                db = _open_cache_db(self._case_dir)
                overrides = load_header_types(db)
                db.close()
            except Exception:
                pass

        candidates = _collect_nested_archive_candidates(
            self._ui_metadata, self._adapter, overrides)

        if not candidates:
            self._status_label.setText("No Archive-type files found in app data.")
            self._finish_operations()
            return

        self._status_label.setText(
            f"Extracting {len(candidates):,} archive(s)")
        self._nested_worker = NestedArchiveWorker(
            self._zip_path, self._case_dir, candidates,
            self._ui_metadata, self._adapter,
            self._streaming_index, self._delta,
        )
        self._nested_errors: list[str] = []
        self._nested_worker.progress.connect(self._on_nested_progress)
        self._nested_worker.item_error.connect(self._on_nested_error)
        self._nested_worker.types_updated.connect(self.header_scan_done.emit)
        self._nested_worker.finished.connect(self._on_nested_done)
        self._nested_worker.start()

    def _on_nested_progress(self, done: int, total: int):
        self._status_label.setText(
            f"Extracting nested archives: {done:,} / {total:,}")

    def _on_nested_error(self, ui_path: str, msg: str):
        self._nested_errors.append(f"{os.path.basename(ui_path)}: {msg}")

    def _on_nested_done(self, ok: int, err: int):
        msg = f"Nested archives: {ok:,} extracted"
        if err:
            first = self._nested_errors[0] if self._nested_errors else "unknown error"
            msg += f", {err:,} failed ({first})"
        msg += "."
        self._status_label.setText(msg)
        # Rebuild compound gzip type overrides from the DB and push to the file
        # browser.  Done here (main thread) so it covers skipped files too and
        # avoids cross-thread signal chain issues.
        self._apply_gzip_type_overrides()
        self.nested_extraction_done.emit()
        self._finish_operations()

    def _apply_gzip_type_overrides(self):
        """Read all single-entry gzip-extracted archives from the DB and emit
        their detected content type (e.g. 'JSON — gzip') as header_scan_done
        so the file browser Type column updates immediately."""
        if not self._case_dir:
            return
        try:
            out_dir = os.path.join(self._case_dir, 'nested_archives')
            db      = _open_cache_db(self._case_dir)
            records = load_nested_archives(db)
            gzip_types: dict[str, str] = {}
            for rec in records:
                if rec['entry_count'] != 1:
                    continue
                stored_path = os.path.join(out_dir, rec['stored_filename'])
                if not os.path.isfile(stored_path):
                    continue
                with open(stored_path, 'rb') as f:
                    magic = f.read(2)
                if magic == b'PK':
                    continue  # stored ZIP, not a gzip extract
                entries = load_nested_archive_entries(db, rec['ui_path'])
                if entries:
                    ft = entries[0].get('file_type') or 'Other'
                    if ft != 'Other':
                        gzip_types[rec['ui_path']] = f"{ft} — gzip"
            db.close()
            if gzip_types:
                self.header_scan_done.emit(gzip_types)
        except Exception:
            pass

    def _on_cancel(self):
        self._scan_cancelled = True
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.requestInterruption()
        if self._nested_worker and self._nested_worker.isRunning():
            self._nested_worker.requestInterruption()

    def _finish_operations(self):
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._refresh_stats()

    def _is_scanning(self) -> bool:
        return ((bool(self._scan_worker and self._scan_worker.isRunning())) or
                (bool(self._nested_worker and self._nested_worker.isRunning())))

    def closeEvent(self, event):
        if self._is_scanning():
            event.ignore()
        else:
            super().closeEvent(event)

    def accept(self):
        if self._is_scanning():
            return
        super().accept()

    def reject(self):
        if self._is_scanning():
            return
        super().reject()


def _do_path_change_check(
    parent,
    zip_path: str,
    case_dir: str,
    old_zip_path: str | None = None,
) -> bool:
    """Verify zip_path belongs to case_dir after a user-initiated path change.

    Compares the .zcd sidecar hash (trusted fingerprint already in case_dir)
    against a fresh CD extraction from zip_path.  Called only when the user
    relocates an archive or picks a different case folder — never on ordinary opens.

    old_zip_path: the previous archive path, used to locate the sidecar when
                  the archive file has been moved (sidecar is named after the
                  original basename).

    Returns True if the check passes or is inconclusive (no sidecar present,
    or archive is unreadable).  Shows an error dialog and returns False on mismatch.
    """
    sidecar_lookup = old_zip_path if old_zip_path else zip_path
    trusted = _cd_sidecar_hash(sidecar_lookup, case_dir)
    if not trusted:
        return True   # no sidecar in this case folder — nothing to compare

    new_hash = _cd_extract_hash(zip_path)
    if new_hash is None:
        return True   # archive unreadable or streaming — allow

    passed = trusted == new_hash
    try:
        db = _open_results_db(case_dir)
        run_id = start_run_log(db, 'cd_integrity_check', total=1, notes=new_hash)
        complete_run_log(db, run_id, processed=1, output_rows=1 if passed else 0)
        db.close()
    except Exception:
        pass

    if not passed:
        QMessageBox.critical(
            parent, "Archive Mismatch",
            "The selected archive does not match this case folder's stored "
            "fingerprint.  They may belong to different extractions.\n\n"
            f"Case folder fingerprint: {trusted[:24]}…\n"
            f"New archive fingerprint: {new_hash[:24]}…\n\n"
            "Please create a new case folder for this archive.",
        )
    return passed


class FastZipBrowser(QMainWindow, HexViewerMixin, MediaViewerMixin, KeywordSearchMixin, ArtifactViewerMixin):  # type: ignore[reportIncompatibleMethodOverride]
    def __init__(self):
        super().__init__()
        self.setWindowTitle("iOS FFS Browser")
        self.setWindowIcon(QIcon(resource_path(os.path.join("resources", "icon.png"))))
        self.resize(1350, 850)

        self.zip_path = ""
        self.full_metadata = {}
        self.folder_map = {}
        self.guid_to_bundle = {}
        self.zip_names: frozenset = frozenset()
        self._zip_ui_paths: frozenset | None = None
        self._folder_sizes: dict = {}
        self._metadata_only_folders: set = set()
        self._file_count_cache: dict = {}
        self._precompute_zip_path: str = ""
        self._zip_handle: zipfile.ZipFile | None = None
        self._zip_open_future = None
        self._streaming_index: StreamingZipIndex | None = None
        self._local_extra_delta: int | None = None
        self._header_type_overrides: dict = {}   # {ui_path: detected_type}
        self._nested_archive_map:   dict        = {}   # archive_ui_path → {stored_path, entries}
        self._nested_virtual_paths: frozenset   = frozenset()  # virtual file paths (leaves)
        self._nested_virtual_folders: frozenset = frozenset()  # virtual folder paths (non-root)
        self._hex_worker: QThread | None = None
        self._adapter = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, "filesystem2", "filesystem1")
        self._android_user_data_path = ''   # set at load time for Android archives
        # ffs_archives: ordered list of {"path": ..., "case_dir": ..., "label": ...}, most-recent first
        self._ffs_archives: list = _load_json_file(FFS_ARCHIVES_FILE, [])
        self.recent_paths: list = [e['path'] for e in self._ffs_archives if 'path' in e]
        self._migrate_device_labels()
        self._case_dir: str | None = None   # case folder for the currently loaded zip

        # ── Menu bar ──────────────────────────────────────────────────────
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        open_act = QAction("Open FFS...", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._open_new_ffs)
        file_menu.addAction(open_act)
        file_menu.addSeparator()
        process_act = QAction("Process Archive…", self)
        process_act.triggered.connect(self._open_process_dialog)
        file_menu.addAction(process_act)
        self._artifact_act = QAction("Run Artifact Parsers…", self)
        self._artifact_act.triggered.connect(self._open_artifact_runner)
        self._artifact_act.setEnabled(False)
        file_menu.addAction(self._artifact_act)
        file_menu.addSeparator()
        self._recent_menu = file_menu.addMenu("Recently Opened FFS")
        self._recent_menu.aboutToShow.connect(self._populate_recent_menu)
        self._view_path = ""
        self._view_is_recursive = False
        self._checked_folders: set = set()
        self._rebuild_pending = False
        self._selected_file_path: str | None = None
        self._selected_media_path: str | None = None
        self._thumb_widgets: dict = {}
        self._pending_media_selection: str | None = None
        self._media_context = None   # tracks what is currently loaded in the media grid
        self._tree_splitter_sizes: list | None = None  # saved when search tab is active
        self._media_total_files: int | None = None
        self._media_sort_desc: str = ""
        self._load_gen = 0
        self._hide_empty_folders = True
        self._missing_plist_paths: set = set()
        self._tree_populating = False
        # (header_label, metadata_key) pairs for the time columns — set per archive
        self._time_cols: list[tuple[str, str]] = [('Changed', 'ctime'), ('Modified', 'mtime')]


        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(4)

        # ── Archive bar ──────────────────────────────────────────────────
        archive_bar = QHBoxLayout()
        self.archive_dropdown = QComboBox()
        self.archive_dropdown.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.archive_dropdown.setMinimumContentsLength(60)
        self.archive_dropdown.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.archive_dropdown.setMaximumWidth(9999)  # fills available space but never forces window wider
        self.update_dropdown_ui()
        self.archive_dropdown.activated.connect(self._on_dropdown_activated)
        self.archive_dropdown.view().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.archive_dropdown.view().customContextMenuRequested.connect(self._on_recent_context_menu)
        self.action_btn = QPushButton("Open FFS")
        self.action_btn.clicked.connect(self._open_new_ffs)
        self.open_export_btn = QPushButton("Open Export Folder")
        self.open_export_btn.clicked.connect(self.ensure_and_open_export_dir)
        self.folder_view_btn = QPushButton("Folder View ▾")
        self.folder_view_btn.clicked.connect(self._show_view_mode_menu)
        self.action_btn.setFixedWidth(90)
        self.open_export_btn.setFixedWidth(150)
        self.folder_view_btn.setFixedWidth(110)
        archive_bar.addWidget(self.archive_dropdown, 1)
        archive_bar.addWidget(self.action_btn)
        archive_bar.addWidget(self.open_export_btn)
        archive_bar.addWidget(self.folder_view_btn)
        layout.addLayout(archive_bar)

        # ── Folder view options ───────────────────────────────────────────
        self._view_mode_menu = QMenu(self)
        self._hide_empty_act = self._view_mode_menu.addAction("Hide Empty && Metadata-Only Folders")
        self._hide_empty_act.setCheckable(True)
        self._hide_empty_act.setChecked(True)
        self._view_mode_menu.triggered.connect(self._on_view_mode_action)

        _section_style = (
            "font-weight: bold; padding: 3px 6px;"
            "border-bottom: 1px solid palette(mid);"
        )
        _status_style = "color: grey; padding: 1px 6px;"

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self._reset_tree_model()
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.tree_model)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_view.setAutoScroll(False)  # Prevent auto-scroll to selection
        self.tree_view.customContextMenuRequested.connect(self.show_tree_context_menu)
        self.tree_view.clicked.connect(self._on_tree_clicked)
        self.tree_view.expanded.connect(self._on_tree_item_expanded)
        self.tree_view.setToolTip("Right-click a folder to export")
        # Column fills the panel; a horizontal scrollbar appears for deeply
        # indented items because the column is kept at least _TREE_COL_MIN wide.
        # The event filter on tree_view keeps the column in sync as the panel resizes.
        self.tree_view.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree_view.header().setStretchLastSection(False)
        self.tree_view.setMinimumWidth(0)
        self.tree_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tree_view.installEventFilter(self)
        self.tree_view.viewport().installEventFilter(self)
        self.tree_view.header().sectionHandleDoubleClicked.connect(
            lambda _: QTimer.singleShot(0, self._update_tree_column))

        tree_header = QLabel("Folder Tree")
        tree_header.setStyleSheet(_section_style)
        self.jump_btn = QPushButton("Jump to ▾")
        self.jump_btn.clicked.connect(self._show_jump_menu)
        self.collapse_btn = QPushButton("Collapse")
        self.collapse_btn.clicked.connect(self._collapse_tree)
        tree_top = QHBoxLayout()
        tree_top.addWidget(tree_header)
        tree_top.addStretch()
        tree_top.addWidget(self.jump_btn)
        tree_top.addWidget(self.collapse_btn)

        left_panel = QWidget()
        left_panel.setMinimumWidth(0)
        left_panel.setMaximumWidth(600)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(2)
        left_layout.addLayout(tree_top)
        left_layout.addWidget(self.tree_view)
        self.splitter.addWidget(left_panel)
        self.splitter.setCollapsible(0, True)

        self.file_headers = ['Name', 'Changed', 'Modified', 'Type', 'Size (Bytes)', 'Files', 'Path']  # rebuilt per archive
        self.proxy_model = MultiColumnFilterProxy()
        self._set_file_model(FileTableModel(self.file_headers))

        self.file_view = QTableView()
        self.file_view.setModel(self.proxy_model)
        self.file_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.file_view.setSortingEnabled(True)
        self.file_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_view.customContextMenuRequested.connect(self.show_table_context_menu)
        self.file_view.doubleClicked.connect(self.handle_table_double_click)
        self.file_view.clicked.connect(self.on_file_selected)

        filter_bar = QHBoxLayout()
        filter_bar.setContentsMargins(0, 2, 0, 2)
        self.show_files_cb = QCheckBox("Files")
        self.show_files_cb.setChecked(True)
        self.show_folders_cb = QCheckBox("Folders")
        self.show_folders_cb.setChecked(True)
        self.show_files_cb.stateChanged.connect(self._on_type_filter_changed)
        self.show_folders_cb.stateChanged.connect(self._on_type_filter_changed)
        filter_bar.addWidget(QLabel("Filter:"))
        filter_bar.addSpacing(4)
        filter_bar.addWidget(self.show_files_cb)
        filter_bar.addSpacing(8)
        filter_bar.addWidget(self.show_folders_cb)
        filter_bar.addSpacing(6)
        self.filter_col_combo = QComboBox()
        self.filter_col_combo.setMaximumWidth(140)
        self._update_filter_columns(self.file_headers)
        filter_bar.addWidget(self.filter_col_combo)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Type to filter rows...")
        self.filter_input.returnPressed.connect(self._apply_filter)
        filter_bar.addWidget(self.filter_input)
        self.filter_go_btn = QPushButton("Go")
        self.filter_go_btn.setMaximumWidth(40)
        self.filter_go_btn.clicked.connect(self._apply_filter)
        self.filter_clear_btn = QPushButton("Clear")
        self.filter_clear_btn.setMaximumWidth(50)
        self.filter_clear_btn.clicked.connect(self._clear_filter)
        filter_bar.addWidget(self.filter_go_btn)
        filter_bar.addWidget(self.filter_clear_btn)

        self.show_selected_btn = QPushButton("Show Selected Files")
        self.show_selected_btn.setVisible(False)
        self.show_selected_btn.clicked.connect(self._rebuild_file_view_from_checked)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setVisible(False)
        self.deselect_all_btn.clicked.connect(self._deselect_all_files)

        self.table_status_label = QLabel()
        self.table_status_label.setStyleSheet(_status_style)

        # ── File Browser tab ────────────────────────────────────────────────
        file_tab = QWidget()
        file_tab_layout = QVBoxLayout(file_tab)
        file_tab_layout.setContentsMargins(0, 4, 0, 0)
        file_tab_layout.setSpacing(2)

        sel_bar = QHBoxLayout()
        sel_bar.addWidget(self.table_status_label)
        sel_bar.addStretch()
        sel_bar.addWidget(self.show_selected_btn)
        sel_bar.addWidget(self.deselect_all_btn)
        file_tab_layout.addLayout(sel_bar)
        file_tab_layout.addLayout(filter_bar)
        file_tab_layout.addWidget(self.file_view)

        # ── Media Browser tab ───────────────────────────────────────────────
        media_tab = self._setup_media_tab(_status_style)

        # ── Keyword search tab ───────────────────────────────────────────────
        search_tab = self._setup_search_tab()

        # ── Artifact Viewer tab ──────────────────────────────────────────────
        artifact_tab = self._setup_artifact_tab()

        # ── Tab widget ──────────────────────────────────────────────────────
        self.center_tabs = QTabWidget()
        self.center_tabs.addTab(file_tab,    "File Browser")       # 0
        self.center_tabs.addTab(media_tab,   "Media Browser")      # 1
        self.center_tabs.addTab(search_tab,  "Keyword Search")     # 2
        self.center_tabs.addTab(artifact_tab,"Artifact Viewer")    # 3
        self.center_tabs.currentChanged.connect(self._on_center_tab_changed)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.center_tabs)

        self.splitter.addWidget(right_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        hex_panel = self._setup_hex_panel(_section_style, _status_style)

        self.outer_splitter = QSplitter(Qt.Orientation.Vertical)
        self.outer_splitter.addWidget(self.splitter)
        self.outer_splitter.addWidget(hex_panel)
        self.outer_splitter.setStretchFactor(0, 2)
        self.outer_splitter.setStretchFactor(1, 1)

        layout.addWidget(self.outer_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.hide()
        self.status_bar.addPermanentWidget(self.progress_bar)
        self.status_bar.showMessage("Ready")

    def _set_file_model(self, model):
        """Set the file model, connect its filter signals, and update the proxy."""
        if hasattr(self, 'file_model'):
            model._show_files   = self.file_model._show_files
            model._show_folders = self.file_model._show_folders
        # setSourceModel first: Qt's proxy releases its C++ pointer to the old
        # model before Python drops the last reference to it.  Reversing this
        # order lets a paint event fire between the Python decref and the C++
        # detach, calling headerData on a freed Python wrapper → SIGSEGV.
        self.proxy_model.setSourceModel(model)
        self.file_model = model
        model.filter_progress.connect(self._on_filter_progress)
        model.filter_done.connect(self._on_filter_done)

    def _on_filter_progress(self, visible, total):
        self.table_status_label.setText(f"{visible:,} of {total:,} items (filtering…)")

    def _on_filter_done(self, visible, total):
        if visible == total:
            self.table_status_label.setText(f"{total:,} items")
        else:
            self.table_status_label.setText(f"{visible:,} of {total:,} items (filtered)")
        col_name = self.filter_col_combo.currentText()
        text = self.filter_input.text()
        if text:
            self._log(f"Filter applied: \"{text}\" in {col_name} — {visible:,} of {total:,} rows visible")

    def _on_type_filter_changed(self):
        col = self.filter_col_combo.currentIndex() - 1
        text = self.filter_input.text()
        self.file_model.set_type_filter(
            self.show_files_cb.isChecked(),
            self.show_folders_cb.isChecked(),
            text, col,
        )

    def _apply_filter(self):
        col = self.filter_col_combo.currentIndex() - 1  # index 0 = "All Columns" → -1
        text = self.filter_input.text()
        total = self.file_model.rowCount()
        self.table_status_label.setText(f"0 of {total:,} items (filtering…)")
        self.proxy_model.set_filter(text, col)

    def _clear_filter(self):
        self.filter_input.clear()
        self.proxy_model.set_filter("", -1)
        self._log(f"Filter cleared in: {self._view_path}")

    def _get_zip_handle(self) -> zipfile.ZipFile | None:
        """Return the shared ZipFile handle, opening it lazily on first use.
        Returns None for streaming zips — use _streaming_index instead."""
        if self._streaming_index is not None:
            return None
        if self._zip_handle is None:
            if self._zip_open_future is not None:
                try:
                    self._zip_handle = self._zip_open_future.result()
                except Exception:
                    self._zip_handle = zipfile.ZipFile(self.zip_path, 'r')
                self._zip_open_future = None
            else:
                self._zip_handle = zipfile.ZipFile(self.zip_path, 'r')
        return self._zip_handle

    def _in_zip(self, ui_path) -> bool:
        if self._zip_ui_paths is not None:
            return ui_path in self._zip_ui_paths
        return self._adapter.resolve(ui_path) in self.zip_names

    def _should_hide_folder(self, path: str) -> bool:
        """Return True when this folder should be suppressed from both tree and browser.

        Single source of truth — used by _populate_tree_children and _classify_entry
        so the two views can never show different sets of folders.

        """
        if not self._hide_empty_folders:
            return False
        if path in self._nested_archive_map:
            return False  # extracted archives are always browseable
        if path in self._missing_plist_paths:
            return False
        if path not in self._folder_sizes:
            return False
        return self._folder_total_size(path) == 0 or path in self._metadata_only_folders

    def _is_empty_folder_entry(self, ui_path) -> bool:
        """True when the ZIP contains a bare directory entry for this path (trailing slash)."""
        return (self._adapter.resolve(ui_path) + "/") in self.zip_names

    def _folder_total_size(self, folder_path: str) -> int:
        return self._folder_sizes.get(folder_path, 0)

    def _bundle_for_path(self, path):
        """Return the bundle ID for the nearest GUID ancestor of path, or None."""
        parts = [p for p in path.split('/') if p]
        for part in parts:
            if part in self.guid_to_bundle:
                return self.guid_to_bundle[part]
        return None

    def _refresh_table_status(self):
        total = self.file_model.rowCount()
        visible = self.proxy_model.rowCount()
        is_filtered = bool(self.filter_input.text())
        parts = []
        if self._view_path:
            parts.append(self._view_path)
        bundle = self._bundle_for_path(self._view_path) if self._view_path else None
        if bundle:
            parts.append(bundle)
        if is_filtered:
            parts.append(f"{visible} of {total} items (filtered)")
        else:
            parts.append(f"{total} items")
        self.table_status_label.setText("  |  ".join(parts))

    # ── Media browser ────────────────────────────────────────────────────────

    def _on_center_tab_changed(self, index):
        if index in (2, 3):
            # Save current tree width and collapse it for full-width tabs
            sizes = self.splitter.sizes()
            if sizes[0] > 0:
                self._tree_splitter_sizes = sizes
            self.splitter.setSizes([0, sum(sizes)])
            if index == 2:
                self._update_search_status_bar()
            else:
                self._refresh_artifact_tab()
            return
        # Switching away — restore tree if it was hidden
        if self.splitter.sizes()[0] == 0 and self._tree_splitter_sizes:
            self.splitter.setSizes(self._tree_splitter_sizes)
        if index == 1:
            # Determine pending selection from the file browser
            if self._selected_file_path and \
                    os.path.splitext(self._selected_file_path)[1].lower() in MEDIA_EXTENSIONS:
                self._pending_media_selection = self._selected_file_path
            else:
                self._pending_media_selection = None

            # Context is the exact ordered tuple of media paths currently visible.
            # Any change — folder, selection, filter, sort — produces a different tuple
            # and triggers a reload. Same paths in same order means no reload needed.
            new_context = tuple(
                r[1] for r in self.file_model._rows
                if r[1] not in self.folder_map
                and os.path.splitext(r[1])[1].lower() in MEDIA_EXTENSIONS
            )

            if new_context == self._media_context:
                # Nothing changed — thumbnails already loaded, just re-apply selection
                if self._pending_media_selection and \
                        self._pending_media_selection in self._thumb_widgets:
                    self._on_thumb_clicked(self._pending_media_selection)
                    self._media_scroll.ensureWidgetVisible(
                        self._thumb_widgets[self._pending_media_selection])
                self._pending_media_selection = None
                return

            self._load_media_from_file_model()
        elif index == 0:
            # Switching back to File Browser — select the media file that was selected
            if self._selected_media_path:
                self._select_file_in_table(self._selected_media_path)

    def _update_filter_columns(self, headers):
        self.filter_col_combo.blockSignals(True)
        self.filter_col_combo.clear()
        self.filter_col_combo.addItem("All Columns")
        for h in headers:
            self.filter_col_combo.addItem(h)
        self.filter_col_combo.blockSignals(False)

    def _zip_stem(self):
        """Return (parent_dir, stem) for the currently loaded archive."""
        p = pathlib.Path(self.zip_path)
        return p.parent, p.stem

    def _get_export_dir(self):
        if self._case_dir:
            return os.path.join(self._case_dir, 'Export')
        parent, stem = self._zip_stem()
        return str(parent / f"{stem}_Export")

    def _get_or_ask_case_dir(self, zip_path: str) -> tuple[str | None, bool]:
        """Return (case_dir, scan_headers) for zip_path, showing dialog if needed."""
        existing = self._archive_entry(zip_path)
        if existing and existing.get('case_dir'):
            stored = existing['case_dir']
            if os.path.isdir(stored):
                return stored, False   # valid — no auto-scan for known archives
            # Case folder has moved or been deleted
            btn = QMessageBox.question(
                self, "Case Folder Not Found",
                f"The case folder could not be found:\n\n{stored}\n\n"
                "Would you like to locate it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if btn == QMessageBox.StandardButton.Yes:
                new_case = QFileDialog.getExistingDirectory(
                    self, "Locate Case Folder", os.path.dirname(stored))
                if new_case and os.path.isdir(new_case):
                    has_sidecar = _cd_sidecar_hash(zip_path, new_case) is not None
                    if not has_sidecar:
                        # A moved case folder always carries its sidecar with it.
                        # No sidecar here means this is not the right folder.
                        QMessageBox.warning(
                            self, "No Case Data Found",
                            "The selected folder does not contain case data for "
                            f"this archive:\n\n{new_case}\n\n"
                            "Please select the folder that was previously used as "
                            "the case folder, or create a new one below.",
                        )
                    elif _do_path_change_check(self, zip_path, new_case):
                        self._upsert_archive(zip_path, new_case)
                        return new_case, False
                    # else: mismatch error shown by _do_path_change_check
            # User declined or didn't pick — fall through to CaseSettingsDialog

        last_base = None
        for e in self._ffs_archives:
            cd = e.get('case_dir', '')
            if cd and os.path.isdir(cd):
                last_base = os.path.dirname(cd)
                break
        dlg = CaseSettingsDialog(zip_path, last_base, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None, False
        case_dir = dlg.case_dir
        self._upsert_archive(zip_path, case_dir)
        return case_dir, dlg.scan_headers

    def _get_log_path(self):
        parent, stem = self._zip_stem()
        return str(parent / f"{stem}_audit_log.txt")

    def _log(self, message):
        if not self.zip_path:
            return
        ts = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        try:
            with open(self._get_log_path(), 'a', encoding='utf-8') as f:
                f.write(f"[{ts}] {message}\n")
        except OSError:
            pass

    def _reset_tree_model(self):
        self.tree_model = QStandardItemModel()
        self.tree_model.setHorizontalHeaderLabels(['Folder Structure'])
        self.tree_model.itemChanged.connect(self.on_tree_item_changed)

    def handle_table_double_click(self, index):
        source = self.proxy_model.mapToSource(index)
        ui_path = self.file_model.index(source.row(), 0).data(Qt.ItemDataRole.UserRole)
        if ui_path in self.folder_map:
            self.navigate_tree_to_path(ui_path)
        elif ui_path in self._nested_virtual_paths:
            self._load_nested_entry_preview(ui_path)
        elif ui_path and self._in_zip(ui_path):
            self._load_file_preview(ui_path)

    def _render_as_text(self, data: bytes, name: str) -> str | None:
        """Return a (possibly pretty-printed) string for *data*, or None if binary."""
        if data[:6] == b'bplist':
            return None

        ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
        stripped = data.lstrip()
        is_json_content = stripped[:1] in (b'{', b'[')
        is_xml_content  = stripped[:1] == b'<'

        if ext not in _TEXT_RENDERABLE_EXTENSIONS and not is_json_content and not is_xml_content:
            return None

        try:
            text = data.decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            if ext not in _TEXT_RENDERABLE_EXTENSIONS:
                return None
            text = data.decode('utf-8', errors='replace')

        if ext == 'json' or (ext not in _TEXT_RENDERABLE_EXTENSIONS and is_json_content):
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except Exception:
                pass
        elif ext in ('xml', 'plist', 'html', 'htm', 'xhtml') or (
                ext not in _TEXT_RENDERABLE_EXTENSIONS and is_xml_content):
            try:
                dom = xml.dom.minidom.parseString(data)
                pretty = dom.toprettyxml(indent='  ')
                lines = pretty.splitlines()
                if lines and lines[0].startswith('<?xml') and not stripped.startswith(b'<?xml'):
                    pretty = '\n'.join(lines[1:]).lstrip('\n')
                text = pretty
            except Exception:
                pass

        return text

    def _load_file_preview(self, ui_path: str) -> None:
        """Load a file for preview — text/JSON/XML in text tab + hex tab; others hex only."""
        raw = self._read_zip_bytes(ui_path)
        if raw is None:
            self._clear_text_preview()
            self._load_hex_preview(ui_path)
            return

        data = raw
        if raw[:2] == b'\x1f\x8b':
            try:
                data = gzip.decompress(raw)
            except Exception:
                pass

        name = os.path.basename(ui_path)
        text = self._render_as_text(data, name)

        self._load_hex_preview(ui_path)
        if text is not None:
            self._load_text_preview(text, ui_path)
        else:
            self._clear_text_preview()

    def _load_nested_entry_preview(self, ui_path: str) -> None:
        """Read an entry from an extracted nested archive and display it."""
        meta      = self.full_metadata.get(ui_path, {})
        arch_path = meta.get('_archive_path')
        arch      = self._nested_archive_map.get(arch_path) if arch_path else None
        if not arch:
            return
        entry_path = ui_path[len(arch_path) + 1:]
        try:
            with zipfile.ZipFile(arch['stored_path'], 'r') as zf:
                data = zf.read(entry_path)
        except zipfile.BadZipFile:
            try:
                with open(arch['stored_path'], 'rb') as f:
                    data = f.read()
            except Exception:
                return
        except Exception:
            return

        name = meta.get('_display_name') or ui_path.rsplit('/', 1)[-1]
        text = self._render_as_text(data, name)
        self._load_hex_preview_from_bytes(data, ui_path)
        if text is not None:
            self._load_text_preview(text, ui_path)
        else:
            self._clear_text_preview()

    def _expand_to_private_var(self):
        """Expand the tree down to private/var on GrayKey load so its children
        are visible. Does not select anything or trigger the file browser."""
        invisible_root = self.tree_model.invisibleRootItem()
        if invisible_root.rowCount() == 0:
            return
        root = invisible_root.child(0)  # "/ [Full Filesystem]"
        self._ensure_children_loaded(root)
        self.tree_view.expand(self.tree_model.indexFromItem(root))
        for seg in ("private", "var"):
            found = None
            for row in range(root.rowCount()):
                child = root.child(row)
                child_path = child.data(Qt.ItemDataRole.UserRole)
                if child_path is not None and child_path.split('/')[-1] == seg:
                    found = child
                    break
            if found is None:
                break
            self._ensure_children_loaded(found)
            self.tree_view.expand(self.tree_model.indexFromItem(found))
            root = found
        # Scroll so the expanded node is visible without selecting it
        idx = self.tree_model.indexFromItem(root)
        QTimer.singleShot(0, lambda: self.tree_view.scrollTo(
            idx, self.tree_view.ScrollHint.PositionAtTop))

    def navigate_tree_to_path(self, target_path):
        # The visible root "/ [Full Filesystem]" sits under the invisible root
        invisible_root = self.tree_model.invisibleRootItem()
        if invisible_root.rowCount() == 0:
            return
        current = invisible_root.child(0)  # "/ [Full Filesystem]"
        self._ensure_children_loaded(current)
        self.tree_view.expand(self.tree_model.indexFromItem(current))
        parts = [p for p in target_path.split('/') if p]

        for part in parts:
            found = False
            for row in range(current.rowCount()):
                child = current.child(row)
                child_path = child.data(Qt.ItemDataRole.UserRole)
                if child_path is not None and child_path.split('/')[-1] == part:
                    self._ensure_children_loaded(child)
                    self.tree_view.expand(self.tree_model.indexFromItem(child))
                    current = child
                    found = True
                    break
            if not found: break

        new_idx = self.tree_model.indexFromItem(current)
        self.tree_view.setCurrentIndex(new_idx)
        # For programmatic navigation, only scroll when the target is not already visible.
        rect = self.tree_view.visualRect(new_idx)
        if not self.tree_view.viewport().rect().contains(rect):
            self.tree_view.scrollTo(new_idx, QAbstractItemView.ScrollHint.EnsureVisible)
        self._view_is_recursive = False
        self.on_folder_selected(new_idx)

    def on_file_selected(self, index):
        source = self.proxy_model.mapToSource(index)
        ui_path = self.file_model.index(source.row(), 0).data(Qt.ItemDataRole.UserRole)
        if not ui_path:
            return
        self._selected_file_path = ui_path
        self.status_bar.showMessage(ui_path)
        is_folder = ui_path in self.folder_map
        self._log(f"{'Folder' if is_folder else 'File'} selected: {ui_path}")

    def _select_file_in_table(self, ui_path):
        """Find ui_path in the current file model and select that row."""
        for row in range(self.file_model.rowCount()):
            if self.file_model.index(row, 0).data(Qt.ItemDataRole.UserRole) == ui_path:
                proxy_idx = self.proxy_model.mapFromSource(self.file_model.index(row, 0))
                if proxy_idx.isValid():
                    self.file_view.setCurrentIndex(proxy_idx)
                    self.file_view.scrollTo(proxy_idx)
                break
            
    def eventFilter(self, obj, event):
        if obj is self.tree_view and event.type() == event.Type.Resize:
            self._update_tree_column()
        if obj is self.tree_view.viewport() and event.type() == event.Type.MouseButtonPress:
            self._tree_click_hpos = self.tree_view.horizontalScrollBar().value()
        return super().eventFilter(obj, event)

    def _update_tree_column(self):
        """Keep the tree column filling the panel, with a minimum for deep nesting."""
        vw = self.tree_view.viewport().width()
        self.tree_view.header().resizeSection(0, max(vw, _TREE_COL_MIN))

    def _on_tree_clicked(self, index):
        # Preserve the horizontal scroll position when a tree item is clicked.
        hpos = getattr(self, '_tree_click_hpos', self.tree_view.horizontalScrollBar().value())
        self.on_folder_selected(index)
        QTimer.singleShot(0, lambda hpos=hpos: self.tree_view.horizontalScrollBar().setValue(hpos))

    def on_folder_selected(self, index):
        item = self.tree_model.itemFromIndex(index)
        if not item: return
        folder_path = item.data(Qt.ItemDataRole.UserRole)

        self.status_bar.showMessage(f"{folder_path}    |    Tip: Right-click to export")
        self._log(f"Folder viewed: {folder_path}")

        children = self.folder_map.get(folder_path, [])
        has_bundles = any(p.split('/')[-1] in self.guid_to_bundle for p in children)

        if has_bundles:
            headers = self.file_headers + ['UUID']
        else:
            headers = self.file_headers

        new_model = FileTableModel(headers)
        self._update_filter_columns(headers)
        self.filter_input.clear()
        self.proxy_model.set_filter("", -1)
        self._view_path = folder_path
        self._view_is_recursive = False

        batch = []
        for path in sorted(children):
            name = path.split('/')[-1]
            is_folder = path in self.folder_map
            file_type, grey_row, skip = self._classify_entry(path, name, is_folder)
            if skip:
                continue
            meta = self.full_metadata.get(path, {})
            if path in self._nested_virtual_paths:
                name = meta.get('_display_name') or name
            fc = self._count_files_recursive(path) if is_folder else -1
            cols = self._build_entry_cols(path, name, meta, is_folder, file_type, fc)
            if has_bundles:
                cols.append(name if name in self.guid_to_bundle else "")
            is_opened_archive = path in self._nested_archive_map
            batch.append((cols, path, fc, is_folder and not grey_row, grey_row, is_opened_archive))

        new_model.append_rows_batch(batch)
        self._set_file_model(new_model)

        # Resize columns but preserve splitter position
        splitter_sizes = self.splitter.sizes()
        self.file_view.resizeColumnsToContents()
        self.splitter.setSizes(splitter_sizes)
        self._refresh_table_status()

        # Refresh media tab if it's currently visible
        if self.center_tabs.currentIndex() == 1:
            self._load_media_from_file_model()

    def _display_name(self, segment: str) -> str:
        """Return the bundle ID for a GUID segment, otherwise the segment itself."""
        return self.guid_to_bundle.get(segment, segment)

    def _display_path(self, path: str) -> str:
        """Replace GUID segments in a full path with bundle IDs for display."""
        return '/'.join(self._display_name(seg) for seg in path.split('/'))

    def _strip_archive_prefix(self, path: str) -> str:
        """Strip the archive-format prefix so display paths start from the
        user-partition root (e.g. 'mobile/Containers/...' not
        'filesystem2/mobile/Containers/...')."""
        return self._adapter.strip_display_prefix(path)

    def _classify_entry(self, path: str, name: str, is_folder: bool):
        """Return (file_type, grey_row, skip) for one file/folder entry.
        skip=True means the caller should exclude this entry from the view."""
        if is_folder:
            if path in self._nested_archive_map:
                return 'Archive', False, False  # extracted archive browseable as folder
            if self._should_hide_folder(path):
                return None, None, True
            if path in self._metadata_only_folders:
                return "Folder - Metadata Only", True, False
            if self._folder_total_size(path) == 0:
                return "Empty Folder", True, False
            return 'Folder', False, False
        if path in self._nested_virtual_paths:
            ft = _get_file_type(name)
            if ft == 'Other':
                ft = self._header_type_overrides.get(path, 'Other')
            return ft, False, False
        if self._in_zip(path):
            # Directory entries in FORMAT_ZIP_EXTRAS land in zip_ui_paths with
            # their trailing slash stripped — detect them before calling
            # _get_file_type so they never appear as "Other".
            if self._is_empty_folder_entry(path):
                if self._hide_empty_folders:
                    return None, None, True
                return "Empty Folder", True, False
            if self._hide_empty_folders:
                _sz = (self.full_metadata.get(path) or {}).get('size', -1)
                if _sz == 0:
                    return None, None, True
            ft = _get_file_type(name)
            if ft == 'Other' and path in self._header_type_overrides:
                ft = self._header_type_overrides[path]
            return ft, False, False
        if self._is_empty_folder_entry(path):
            if self._hide_empty_folders:
                return None, None, True
            return "Empty Folder", True, False
        if self._hide_empty_folders:
            _sz = (self.full_metadata.get(path) or {}).get('size', -1)
            if _sz == 0:
                return None, None, True
        return "Not in Zip", True, False

    def _build_entry_cols(self, path: str, name: str, meta: dict,
                          is_folder: bool, file_type: str | None, fc: int) -> list:
        cols: list = [self._display_name(name)]
        for _, key in self._time_cols:
            cols.append(self.format_ts(meta.get(key)))
        if is_folder and path in self._nested_archive_map:
            size_val = meta.get('size', 0)   # show original archive file size
        elif is_folder:
            size_val = self._folder_total_size(path)
        else:
            size_val = meta.get('size', 0)
        cols += [
            file_type,
            f"{size_val:,}",
            f"{fc:,}" if is_folder else "",
            self._display_path(path),
        ]
        return cols

    def _count_files_recursive(self, folder_path: str, visited: set | None = None) -> int:
        """Return the total number of files under folder_path (recursive, no double-counting)."""
        if folder_path in self._file_count_cache:
            return self._file_count_cache[folder_path]
        if visited is None:
            visited = set()
        if folder_path in visited:
            return 0
        visited.add(folder_path)
        count = 0
        for child in self.folder_map.get(folder_path, []):
            if child in self.folder_map:
                count += self._count_files_recursive(child, visited)
            else:
                count += 1
        self._file_count_cache[folder_path] = count
        return count

    def _start_precompute_folder_counts(self):
        """Begin batch precomputation of all folder file counts on the main thread."""
        self._precompute_zip_path = self.zip_path
        self._precompute_folders = list(self.folder_map.keys())
        self._precompute_batch_index = 0
        self._precompute_counts_batch()

    def _precompute_counts_batch(self):
        if self.zip_path != self._precompute_zip_path:
            return  # archive changed; abort stale callbacks
        folders = self._precompute_folders
        start = self._precompute_batch_index
        end = min(start + 2000, len(folders))
        for fp in folders[start:end]:
            self._count_files_recursive(fp)
        self._precompute_batch_index = end
        if end < len(folders):
            QTimer.singleShot(0, self._precompute_counts_batch)
        else:
            self._save_folder_counts_to_db()

    def _save_folder_counts_to_db(self):
        if not self._case_dir:
            return
        counts = dict(self._file_count_cache)
        case_dir = self._case_dir
        def _save():
            try:
                db = _open_cache_db(case_dir)
                save_folder_counts(db, counts)
                db.close()
            except Exception:
                pass
        ex = ThreadPoolExecutor(max_workers=1)
        ex.submit(_save)
        ex.shutdown(wait=False)

    def format_ts(self, ts):
        if not ts: return "---"
        try:
            # APFS stores timestamps as nanoseconds since epoch
            if ts > 1e10:
                ts = ts / 1e9
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        except (ValueError, OSError, OverflowError):
            return "---"

    def ensure_and_open_export_dir(self):
        if not self.zip_path: return
        dest_dir = self._get_export_dir()
        try:
            os.makedirs(dest_dir, exist_ok=True)
            self.open_path(dest_dir)
        except OSError as e:
            self.status_bar.showMessage(f"Cannot open export folder: {e}")

    def show_tree_context_menu(self, point):
        index = self.tree_view.indexAt(point)
        if not index.isValid(): return
        self.tree_view.setCurrentIndex(index)
        item = self.tree_model.itemFromIndex(index)
        folder_path = item.data(Qt.ItemDataRole.UserRole) if item else None
        menu = QMenu(self)
        if folder_path is not None:
            checked_paths = set()
            self._collect_checked_paths(item, checked_paths)
            is_ticked = bool(checked_paths)
            if is_ticked:
                rec_act = QAction("☐ Recursively Deselect", self)
                rec_act.triggered.connect(lambda: self._recursive_untick_folder(index))
            else:
                rec_act = QAction("☑ Recursively Add All Files", self)
                rec_act.triggered.connect(lambda: self._recursive_tick_folder(index))
            menu.addAction(rec_act)
            menu.addSeparator()
        export_act = QAction("📁 Export Folder (Recursive)", self)
        export_act.triggered.connect(lambda: self.handle_export_request(is_tree=True))
        menu.addAction(export_act)
        menu.exec(self.tree_view.viewport().mapToGlobal(point))

    def _recursive_tick_folder(self, index):
        """Tick the folder and all its descendants."""
        item = self.tree_model.itemFromIndex(index)
        if item is None:
            return
        self._tree_populating = True
        self._ensure_all_descendants_loaded(item)
        self._tree_populating = False
        self.tree_model.blockSignals(True)
        if item.isCheckable():
            item.setCheckState(Qt.CheckState.Checked)
        self._cascade_check(item, Qt.CheckState.Checked)
        self.tree_model.blockSignals(False)
        self.tree_view.viewport().update()
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QTimer.singleShot(0, self._deferred_rebuild)

    def _recursive_untick_folder(self, index):
        """Untick the folder and all its descendants."""
        item = self.tree_model.itemFromIndex(index)
        if item is None:
            return
        self.tree_model.blockSignals(True)
        if item.isCheckable():
            item.setCheckState(Qt.CheckState.Unchecked)
        self._cascade_check(item, Qt.CheckState.Unchecked)
        self.tree_model.blockSignals(False)
        self.tree_view.viewport().update()
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QTimer.singleShot(0, self._deferred_rebuild)

    # ── Checkbox-driven file-view helpers ──────────────────────────────── #

    def _cascade_check(self, parent_item, state):
        """Propagate check state to all descendants of parent_item."""
        for row in range(parent_item.rowCount()):
            child = parent_item.child(row)
            if child is None:
                continue
            if child.isCheckable():
                child.setCheckState(state)
            self._cascade_check(child, state)

    def _collect_checked_paths(self, parent_item, result):
        for row in range(parent_item.rowCount()):
            item = parent_item.child(row)
            if item is None:
                continue
            if item.isCheckable() and item.checkState() == Qt.CheckState.Checked:
                path = item.data(Qt.ItemDataRole.UserRole)
                if path is not None:
                    result.add(path)
            self._collect_checked_paths(item, result)

    def _rebuild_file_view_from_checked(self, preserve_filter: bool = False, select_path: str | None = None):
        checked = set()
        self._collect_checked_paths(self.tree_model.invisibleRootItem(), checked)
        self._view_path = ""
        self._view_is_recursive = bool(checked)
        self._checked_folders = checked
        if checked:
            self.tree_view.clearSelection()
            self.tree_view.setCurrentIndex(QModelIndex())

        # Capture filter state before swapping models (new model has no filter).
        if preserve_filter:
            _filter_text = self.filter_input.text()
            _filter_col  = self.filter_col_combo.currentIndex() - 1
        else:
            _filter_text = ""
            _filter_col  = -1

        # Bump generation — any in-flight batch will see the change and abort
        self._load_gen += 1
        my_gen = self._load_gen

        # Swap in an empty model immediately so the checkbox tick feels instant
        self._set_file_model(FileTableModel(self.file_headers))

        if not checked:
            # Fall back to showing the currently highlighted folder, if any
            idx = self.tree_view.currentIndex()
            if idx.isValid():
                self.on_folder_selected(idx)
            else:
                self.table_status_label.setText("0 items")
                self.status_bar.showMessage("No folders selected")
            return

        folders = sorted(checked)
        total_folders = len(folders)
        state = {'idx': 0, 'count': 0}
        self.table_status_label.setText("0 items  (loading…)")
        self.status_bar.showMessage(f"Loading…  0 files  (0 of {total_folders} folders)")

        def _process_batch():
            if my_gen != self._load_gen:
                return  # superseded by a newer rebuild — stop silently

            # Process as many folders as fit within ~16 ms (one frame budget)
            deadline = time.monotonic() + FRAME_BUDGET_SECS
            batch_rows = []

            while state['idx'] < total_folders:
                folder = folders[state['idx']]
                state['idx'] += 1
                for child in self.folder_map.get(folder, []):
                    name = child.split('/')[-1]
                    is_folder = child in self.folder_map
                    file_type, grey_row, skip = self._classify_entry(child, name, is_folder)
                    if skip:
                        continue
                    meta = self.full_metadata.get(child, {})
                    fc = self._count_files_recursive(child) if is_folder else -1
                    cols = self._build_entry_cols(child, name, meta, is_folder, file_type, fc)
                    is_opened_archive = child in self._nested_archive_map
                    batch_rows.append((cols, child, fc, is_folder and not grey_row, grey_row, is_opened_archive))
                    state['count'] += 1
                if time.monotonic() >= deadline:
                    break  # yield back to the event loop

            self.file_model.append_rows_batch(batch_rows)
            self.status_bar.showMessage(
                f"Loading…  {state['count']:,} files  "
                f"({state['idx']} of {total_folders} folders)")
            self.table_status_label.setText(f"{state['count']:,} items  (loading…)")

            if state['idx'] < total_folders:
                QTimer.singleShot(0, _process_batch)
            else:
                self.file_view.resizeColumnsToContents()
                if _filter_text:
                    if select_path:
                        _model = self.file_model
                        def _on_filter_done_select(*_):
                            _model.filter_done.disconnect(_on_filter_done_select)
                            self._select_file_by_path(select_path)
                        _model.filter_done.connect(_on_filter_done_select)
                    self.proxy_model.set_filter(_filter_text, _filter_col)
                elif select_path:
                    self._select_file_by_path(select_path)
                self._refresh_table_status()
                self.status_bar.showMessage(
                    f"{state['count']:,} files from {total_folders:,} selected folders")
                if self.center_tabs.currentIndex() == 1:
                    self._load_media_from_file_model()

        QTimer.singleShot(0, _process_batch)

    def show_table_context_menu(self, point):
        index = self.file_view.indexAt(point)
        if not index.isValid(): return
        if not self.file_view.selectionModel().isSelected(index):
            self.file_view.selectRow(index.row())
        menu = QMenu(self)
        count = len(self.file_view.selectionModel().selectedRows())
        export_act = QAction(f"💾 Export Selected ({count} items)", self)
        export_act.triggered.connect(lambda: self.handle_export_request(is_tree=False))
        menu.addAction(export_act)

        source = self.proxy_model.mapToSource(index)
        ui_path = self.file_model.index(source.row(), 0).data(Qt.ItemDataRole.UserRole)

        if ui_path and ui_path not in self.folder_map and self._in_zip(ui_path):
            hex_act = QAction("🔍 Preview in Hex Viewer", self)
            hex_act.triggered.connect(lambda: self._load_hex_preview(ui_path))
            menu.addAction(hex_act)

        if ui_path and '/' in ui_path:
            parent_path = ui_path.rsplit('/', 1)[0]
            parent_act = QAction("📂 Open Parent Folder in Tree", self)
            parent_act.triggered.connect(lambda: self.navigate_tree_to_path(parent_path))
            menu.addAction(parent_act)

        # Collect file paths from all selected rows for header scan
        _scan_paths = []
        for _idx in self.file_view.selectionModel().selectedRows():
            _src = self.proxy_model.mapToSource(_idx)
            _p = self.file_model.index(_src.row(), 0).data(Qt.ItemDataRole.UserRole)
            if _p and _p not in self.folder_map and self._in_zip(_p):
                _scan_paths.append(_p)
        if _scan_paths and self.zip_path:
            _n = len(_scan_paths)
            menu.addSeparator()
            scan_hdr_act = QAction(
                f"🔬 Scan File Header{'s' if _n > 1 else ''} ({_n})", self)
            scan_hdr_act.triggered.connect(
                lambda checked=False, paths=_scan_paths, primary=ui_path:
                    self._scan_selected_headers(paths, primary_path=primary))
            menu.addAction(scan_hdr_act)

        # Extract nested archive option — shown for unextracted Archive/Compressed files
        if ui_path and ui_path not in self.folder_map and self._in_zip(ui_path) \
                and ui_path not in self._nested_archive_map and self._case_dir:
            _name = ui_path.rsplit('/', 1)[-1]
            _ft   = _get_file_type(_name)
            if _ft == 'Other':
                _ft = self._header_type_overrides.get(ui_path, 'Other')
            if _ft in _EXTRACTABLE_TYPES:
                menu.addSeparator()
                extract_act = QAction("📦 Extract as Nested Archive", self)
                extract_act.triggered.connect(
                    lambda checked=False, p=ui_path: self._extract_from_context_menu(p))
                menu.addAction(extract_act)

        menu.exec(self.file_view.viewport().mapToGlobal(point))

    def handle_export_request(self, is_tree=True):
        if not self.zip_path: return
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        tasks = []

        if is_tree:
            idx = self.tree_view.currentIndex()
            ui_path = self.tree_model.itemFromIndex(idx).data(Qt.ItemDataRole.UserRole)
            base_parent = ui_path.rsplit('/', 1)[0] if '/' in ui_path else ""
            tasks.append((ui_path, base_parent))
        else:
            selected_rows = self.file_view.selectionModel().selectedRows()
            for idx in selected_rows:
                source = self.proxy_model.mapToSource(idx)
                ui_path = self.file_model.index(source.row(), 0).data(Qt.ItemDataRole.UserRole)
                base_parent = ui_path.rsplit('/', 1)[0] if ui_path in self.folder_map and '/' in ui_path else ui_path
                tasks.append((ui_path, base_parent))

        dest_dir = self._get_export_dir()
        dlg = ExportProgressDialog(dest_dir, parent=self)

        self.ex_worker = ExtractorWorker(self.zip_path, tasks, dest_dir, self.folder_map, self._adapter.resolve)
        self.ex_worker.file_count.connect(dlg.on_file_count)
        self.ex_worker.progress.connect(dlg.on_progress)
        self.ex_worker.status.connect(dlg.on_status)
        self.ex_worker.finished.connect(dlg.on_finished)
        self.ex_worker.finished.connect(self.on_export_finished)
        dlg.cancel_requested.connect(self.ex_worker.cancel)

        self.ex_worker.start()
        dlg.exec()

    def on_export_finished(self, success, message, dest_path):
        QApplication.restoreOverrideCursor()
        if success:
            self._log(f"Export succeeded: {message} → {dest_path}")
        else:
            self._log(f"Export failed: {message}")

    def open_path(self, path):
        if sys.platform == 'win32': os.startfile(path)
        elif sys.platform == 'darwin': subprocess.Popen(['open', path])
        else: subprocess.Popen(['xdg-open', path])

    def _collapse_tree(self):
        self.tree_view.collapseAll()
        # Keep the root node expanded so the top-level folders are visible
        root = self.tree_model.item(0)
        if root:
            self.tree_view.expand(self.tree_model.indexFromItem(root))

    def _show_view_mode_menu(self):
        self._view_mode_menu.exec(
            self.folder_view_btn.mapToGlobal(self.folder_view_btn.rect().bottomLeft()))

    def _on_view_mode_action(self, action):
        if action is self._hide_empty_act:
            self._hide_empty_folders = action.isChecked()
            self.reload_tree_entirely()

    def _detect_time_columns(self, ui_metadata: dict) -> list[tuple[str, str]]:
        """Return [(header, meta_key)] for the time columns to show for this archive.

        iOS archives always carry btime (APFS birth time), ctime, and mtime.
        Android archives vary: many only have mtime; show ctime/btime columns
        only when at least one non-zero value is found in the metadata sample."""
        if not self._is_android_archive():
            return [('Created', 'btime'), ('Changed', 'ctime'), ('Modified', 'mtime')]
        # Sample up to 500 metadata dicts to detect which timestamps are present.
        has_ctime = has_btime = False
        checked = 0
        for v in ui_metadata.values():
            if not v:
                continue
            if v.get('ctime'):
                has_ctime = True
            if v.get('btime'):
                has_btime = True
            checked += 1
            if checked >= 500 or (has_ctime and has_btime):
                break
        cols: list[tuple[str, str]] = []
        if has_btime:
            cols.append(('Created', 'btime'))
        if has_ctime:
            cols.append(('Changed', 'ctime'))
        cols.append(('Modified', 'mtime'))
        return cols

    def _detect_android_user_data(self, folder_map: dict) -> str:
        """Return the parent path of the DCIM folder, or '' if not found.
        Skips paths under private/ which are iOS-specific."""
        for path in folder_map:
            if path.startswith('private/'):
                continue
            if path == 'DCIM' or path.endswith('/DCIM'):
                return path.rsplit('/DCIM', 1)[0] if '/' in path else ''
        return ''

    def _is_android_archive(self) -> bool:
        fmt = self._adapter.format
        if fmt == FfsAdapter.FORMAT_ZIP_EXTRAS:
            return True
        if fmt == FfsAdapter.FORMAT_GRAYKEY:
            return 'data/data' in self.folder_map
        return False

    def _android_shortcuts(self) -> list:
        user_data = self._android_user_data_path or 'data/media/0'
        return [
            ("App Data",        "data/data"),
            ("User Data",       user_data),
            None,
            ("Media", [
                ("Photos (DCIM)",    user_data + "/DCIM"),
                ("Downloads",        user_data + "/Download"),
                ("Screenshots",      user_data + "/DCIM/Screenshots"),
            ]),
            ("Communications", [
                ("SMS / MMS",        "data/data/com.android.providers.telephony/databases"),
                ("Call History",     "data/data/com.android.providers.contacts/databases"),
                ("Contacts",         "data/data/com.android.providers.contacts/databases"),
            ]),
            ("Browsing & Web", [
                ("Chrome",           "data/data/com.android.chrome/app_chrome/Default"),
                ("Samsung Browser",  "data/data/com.sec.android.app.sbrowser/databases"),
            ]),
            ("System & Device", [
                ("Accounts",         "data/system_ce/0/accounts_ce.db"),
                ("Device Logs",      "data/tombstones"),
                ("Settings DB",      "data/data/com.android.providers.settings/databases"),
                ("Location History", "data/data/com.google.android.gms/files/locationhistory"),
            ]),
        ]

    def _show_jump_menu(self):
        menu = QMenu(self)
        shortcuts = self._android_shortcuts() if self._is_android_archive() else FORENSIC_SHORTCUTS
        self._build_jump_menu(menu, shortcuts)
        menu.exec(self.jump_btn.mapToGlobal(self.jump_btn.rect().bottomLeft()))

    def _adapt_shortcut_path(self, path: str) -> str:
        """Prefix shortcut paths for iOS GrayKey, where every ui_path starts
        with private/var/.  Android GrayKey paths need no prefix."""
        if self._adapter and not self._is_android_archive():
            return self._adapter.prefix_shortcut(path)
        return path

    def _build_jump_menu(self, menu, items):
        for item in items:
            if item is None:
                menu.addSeparator()
            elif isinstance(item[1], list):
                submenu = menu.addMenu(item[0])
                self._build_jump_menu(submenu, item[1])
            else:
                name, path = item
                act = QAction(name, self)
                act.triggered.connect(
                    lambda _, p=path: self.navigate_tree_to_path(self._adapt_shortcut_path(p)))
                menu.addAction(act)

    def _on_dropdown_activated(self, index):
        if index == 0:
            return
        path = self.archive_dropdown.itemData(index)
        if not path:
            return
        if not os.path.isfile(path):
            btn = QMessageBox.question(
                self, "Archive Not Found",
                f"The archive file could not be found:\n\n{path}\n\n"
                "Would you like to locate it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if btn != QMessageBox.StandardButton.Yes:
                return
            new_path, _ = QFileDialog.getOpenFileName(
                self, "Locate Archive", os.path.dirname(path), "ZIP (*.zip)")
            if not new_path or not os.path.isfile(new_path):
                return
            # Integrity check before committing the path change
            entry = self._archive_entry(path)
            case_dir_for_check = entry.get('case_dir') if entry else None
            if case_dir_for_check and not _do_path_change_check(
                    self, new_path, case_dir_for_check, old_zip_path=path):
                return   # mismatch — error shown, keep original path
            # Update path in-place so case_dir and label are preserved
            if entry:
                entry['path'] = new_path
                self.recent_paths = [e['path'] for e in self._ffs_archives]
                self._save_ffs_archives()
                self.update_dropdown_ui()
            path = new_path
        self.start_loading(path)

    def _open_process_dialog(self):
        if not self.zip_path:
            QMessageBox.information(self, "No Archive", "Open an FFS archive first.")
            return
        dlg = ProcessDialog(
            zip_path=self.zip_path,
            case_dir=self._case_dir,
            ffs_adapter=self._adapter,
            ui_metadata=self.full_metadata,
            streaming_index=self._streaming_index,
            delta=self._local_extra_delta,
            parent=self,
        )
        dlg.header_types_cleared.connect(self._on_header_types_cleared)
        dlg.header_scan_done.connect(self._on_header_scan_done)
        dlg.nested_extraction_done.connect(self._on_nested_extraction_done)
        dlg.exec()

    def _open_new_ffs(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open ZIP", "", "ZIP (*.zip)")
        if path:
            self.start_loading(path)

    def start_loading(self, zip_path):
        case_dir, scan_headers = self._get_or_ask_case_dir(zip_path)
        if case_dir is None:
            return   # user cancelled the dialog
        self._case_dir = case_dir
        self._search_entries = None   # clear cached index for the new archive
        self.zip_path = zip_path
        # Clear search UI from the previous archive
        self._stop_keyword_search()
        self.search_field.clear()
        self.search_results_model.clear()
        self.search_results_model.setHorizontalHeaderLabels(["Name", "Hits", "Context", "Offset"])
        self._search_folder_items.clear()
        self._search_file_items.clear()
        self._pending_db_hits.clear()
        self.search_status.setText("No search running")
        self.center_tabs.setCurrentIndex(0)
        self._load_recent_searches_from_db()
        # Stop the old index worker before replacing the tree model — a running
        # worker thread can trigger Python GC which traverses freed Qt wrappers.
        if self._search_index_worker and self._search_index_worker.isRunning():
            self._search_index_worker.stop()
            self._search_index_worker.wait()
        self._log(f"SESSION START — Archive loaded: {zip_path}")
        self._reset_tree_model()
        self.tree_view.setModel(self.tree_model)
        self._start_search_index_build()
        self.show_selected_btn.setVisible(False)
        self.deselect_all_btn.setVisible(False)
        # Clear hex viewer from the previous archive
        if self._hex_worker is not None and self._hex_worker.isRunning():
            self._hex_worker.terminate()
            self._hex_worker.wait()
        self._hex_entry        = None
        self._hex_file_size    = 0
        self._hex_bytes_loaded = 0
        self._hex_view_start   = 0
        self._hex_ui_path      = ""
        self._pending_hex_jump = None
        self._hex_loading_more = False
        self.hex_view.clear()
        self.hex_label.setText("No file selected")
        self.hex_progress_bar.hide()

        # Stop any running thumbnail worker from the previous archive
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.stop()
            self._thumb_worker.wait()
        while self._media_grid.count():
            item = self._media_grid.takeAt(0)
            w = item.widget() if item else None
            if w:
                w.deleteLater()
        self._media_status.setText("Select a folder to view media")
        # Clear the file browser immediately so the previous archive's
        # content is not shown while the new one is loading.
        self._set_file_model(FileTableModel(self.file_headers))
        self._update_filter_columns(self.file_headers)
        self.table_status_label.setText("")
        self._view_path = ""
        self._checked_folders = set()
        self._selected_file_path = None
        self._selected_media_path = None
        self._pending_media_selection = None
        self._media_context = None
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self._load_start = time.monotonic()
        self.worker = ZipMetadataWorker(zip_path, scan_headers=scan_headers,
                                        case_dir=self._case_dir)
        self.worker.status_update.connect(self.status_bar.showMessage)
        self.worker.metadata_ready.connect(self.on_metadata_ready)
        self.worker.header_scan_progress.connect(self._on_header_scan_progress)
        self.worker.header_scan_done.connect(self._on_header_scan_done)
        self.worker.start()

    def on_metadata_ready(self, data, folder_map, guid_map, zip_names, ffs_adapter, missing_plist_paths, folder_sizes, zip_ui_paths=None):
        self.full_metadata = data
        self.folder_map = folder_map
        self.guid_to_bundle = guid_map
        self.zip_names = zip_names
        self._adapter = ffs_adapter
        self._android_user_data_path = self._detect_android_user_data(folder_map)
        self._missing_plist_paths = set(missing_plist_paths)
        self._folder_sizes = folder_sizes
        self._zip_ui_paths = zip_ui_paths
        if ffs_adapter.format != FfsAdapter.FORMAT_ZIP_EXTRAS:
            self._metadata_only_folders = _find_metadata_only_folders(
                folder_map, zip_names, zip_ui_paths, ffs_adapter)
        else:
            self._metadata_only_folders = set()
        self._header_type_overrides = {}
        self._file_count_cache = {}
        if self._case_dir:
            try:
                db = _open_cache_db(self._case_dir)
                self._header_type_overrides = load_header_types(db)
                self._file_count_cache, _ = load_folder_data(db)
                db.close()
            except Exception:
                pass
        if self._zip_handle:
            self._zip_handle.close()
            self._zip_handle = None
        self._zip_open_future = None
        self._streaming_index    = getattr(self.worker, '_streaming_index', None)
        self._local_extra_delta  = getattr(self.worker, '_local_extra_delta', None)
        if self._streaming_index is None:
            _path = self.zip_path
            _ex = ThreadPoolExecutor(max_workers=1)
            self._zip_open_future = _ex.submit(lambda: zipfile.ZipFile(_path, 'r'))
            _ex.shutdown(wait=False)
        self._time_cols = self._detect_time_columns(data)
        self.file_headers = (['Name'] + [h for h, _ in self._time_cols]
                             + ['Type', 'Size (Bytes)', 'Files', 'Path'])
        self._update_filter_columns(self.file_headers)
        self.progress_bar.setRange(0, 100)
        self.save_recent_list(self.zip_path)
        fmt_label = "GrayKey" if ffs_adapter.format == FfsAdapter.FORMAT_GRAYKEY else "Cellebrite"
        self._inject_nested_archives()
        self.reload_tree_entirely()
        self.tree_model.setHorizontalHeaderLabels([f"Folder Structure — {fmt_label}"])
        # Fetch device label and populate device_info after archive is loaded.
        # Always schedule so device_info is repopulated after a schema rebuild
        # even when the label is already cached in ffs_archives.json.
        QTimer.singleShot(0, lambda: self._fetch_and_store_label(self.zip_path))
        self._refresh_artifact_tab()
        self._artifact_act.setEnabled(True)
        # Start background precomputation of folder counts if not already cached.
        if len(self._file_count_cache) < len(self.folder_map):
            QTimer.singleShot(300, self._start_precompute_folder_counts)

        if missing_plist_paths:
            # Only warn about UUID folders that actually have content in the zip.
            # Metadata-only or empty folders have no extractable files so the
            # missing plist does not affect the user.
            actionable = [p for p in missing_plist_paths
                          if self._folder_total_size(p) > 0]
            if actionable:
                self._warn_and_select_missing(actionable)

    def _on_header_scan_progress(self, remaining: int):
        if remaining > 0:
            self.status_bar.showMessage(f"Scanning headers: {remaining:,} files remaining…")

    def _on_header_types_cleared(self):
        """Called before a rescan — clear in-memory overrides and refresh."""
        self._header_type_overrides.clear()
        self._refresh_folder_view()

    def _on_header_scan_done(self, results: dict):
        if not results:
            return
        self._header_type_overrides.update(results)
        if self._case_dir:
            try:
                db = _open_cache_db(self._case_dir)
                save_header_types(db, results)
                db.close()
            except Exception:
                pass
        if self._view_is_recursive:
            self._rebuild_file_view_from_checked(preserve_filter=True)
        else:
            self._refresh_folder_view(preserve_filter=True)

    def _inject_nested_archives(self):
        """Inject extracted nested-archive entries into folder_map and full_metadata
        so each archive appears as a navigable folder in the tree and file list.

        Flat layout: every entry in the archive becomes a direct child of the
        archive node.  The full relative path within the archive (e.g. subdir/file.txt)
        is stored in _display_name so the file browser can show it as the entry name.
        """
        if not self._case_dir:
            return
        db = None
        try:
            db      = _open_cache_db(self._case_dir)
            records = load_nested_archives(db)
        except Exception:
            if db:
                try:
                    db.close()
                except Exception:
                    pass
            return

        # Reset all virtual state before rebuilding.
        self._nested_archive_map    = {}
        self._nested_virtual_paths  = frozenset()
        self._nested_virtual_folders = frozenset()
        virtual_paths = set()
        out_dir = os.path.join(self._case_dir, 'nested_archives')

        for rec in records:
            ui_path     = rec['ui_path']
            stored_path = os.path.join(out_dir, rec['stored_filename'])
            if not os.path.isfile(stored_path):
                continue
            try:
                entries = load_nested_archive_entries(db, ui_path)
            except Exception:
                entries = []

            self._nested_archive_map[ui_path] = {
                'stored_path': stored_path,
                'entries':     entries,
            }

            children = []
            for e in entries:
                if e['entry_path'].endswith('/'):
                    continue   # skip directory entries
                ep = e['entry_path']
                if not ep:
                    continue
                vpath = f"{ui_path}/{ep}"
                virtual_paths.add(vpath)
                children.append(vpath)
                mtime = None
                if e.get('mdate'):
                    try:
                        dt = datetime.strptime(e['mdate'], '%Y-%m-%d %H:%M:%S')
                        mtime = dt.replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        pass
                self.full_metadata[vpath] = {
                    'size':          e['size'] or 0,
                    '_display_name': ep,
                    '_archive_path': ui_path,
                    'mtime':         mtime,
                }
                ft = e.get('file_type') or 'Other'
                if ft != 'Other':
                    self._header_type_overrides[vpath] = ft

            # Register archive as a navigable folder with its entries as children.
            self.folder_map[ui_path] = children

        db.close()
        self._nested_virtual_paths = frozenset(virtual_paths)

    def _on_nested_extraction_done(self):
        self._inject_nested_archives()
        self.reload_tree_entirely()

    def _extract_from_context_menu(self, ui_path: str) -> None:
        """Extract a single archive right-click entry without opening ProcessDialog."""
        if not self.zip_path or not self._case_dir:
            return
        self.status_bar.showMessage(f"Extracting {os.path.basename(ui_path)}…")
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        worker = NestedArchiveWorker(
            self.zip_path,
            self._case_dir,
            [ui_path],
            self.full_metadata,
            self._adapter,
            self._streaming_index,
            self._local_extra_delta,
        )
        worker.finished.connect(
            lambda ok, err, p=ui_path: self._on_context_extract_done(p, ok, err))
        self._context_extract_worker = worker
        worker.start()

    def _on_context_extract_done(self, ui_path: str, ok: int, err: int) -> None:
        QApplication.restoreOverrideCursor()
        if ok > 0:
            saved_checked = set(self._checked_folders)
            parent_path = ui_path.rsplit('/', 1)[0] if '/' in ui_path else ''

            self._inject_nested_archives()

            # Collect siblings already in the tree before inserting the new zip.
            parent_item = self._find_tree_item(parent_path)
            sibling_paths: list[str] = []
            zip_item = None
            if parent_item is not None and not self._item_has_placeholder(parent_item):
                for row in range(parent_item.rowCount()):
                    child = parent_item.child(row)
                    cp = child.data(Qt.ItemDataRole.UserRole)
                    if cp and cp != _TREE_PLACEHOLDER:
                        sibling_paths.append(cp)
                zip_item = self._insert_tree_folder_item(
                    parent_item, ui_path, Qt.CheckState.Unchecked)

            # Auto-tick the zip only when every sibling was already ticked —
            # so it joins the background aggregate list without extra user action.
            if sibling_paths and all(s in saved_checked for s in sibling_paths):
                self._checked_folders.add(ui_path)
                if zip_item is not None:
                    self._tree_populating = True
                    try:
                        zip_item.setCheckState(Qt.CheckState.Checked)
                    finally:
                        self._tree_populating = False

            # Always navigate into the archive so its contents are shown immediately.
            self.navigate_tree_to_path(ui_path)

            self.status_bar.showMessage(
                f"Extracted: {os.path.basename(ui_path)}", 5000)
        else:
            self.status_bar.showMessage(
                f"Extraction failed ({err} error(s)): {os.path.basename(ui_path)}", 5000)

    def _scan_selected_headers(self, ui_paths: list, primary_path: str | None = None):
        if not self.zip_path or not ui_paths:
            return
        n = len(ui_paths)
        self.status_bar.showMessage(f"Scanning {n} file header(s)…")
        self._single_scan_paths        = ui_paths
        self._single_scan_primary_path = primary_path
        self._single_scan_run_id: int | None = None
        if self._case_dir:
            try:
                db = _open_results_db(self._case_dir)
                self._single_scan_run_id = start_run_log(
                    db, 'header_scan_single',
                    total=n,
                    notes='\n'.join(ui_paths),
                )
                db.close()
            except Exception:
                pass
        self._single_scan_worker = SingleFileScanWorker(
            self.zip_path, ui_paths, self._adapter,
            self._streaming_index, self._local_extra_delta,
        )
        self._single_scan_worker.done.connect(self._on_single_scan_done)
        self._single_scan_worker.start()

    def _on_single_scan_done(self, results: dict):
        n_total   = len(self._single_scan_paths)
        n_updated = len(results)
        if results:
            self._header_type_overrides.update(results)
            if self._case_dir:
                try:
                    db = _open_cache_db(self._case_dir)
                    save_header_types(db, results)
                    db.close()
                except Exception:
                    pass
        if self._case_dir and self._single_scan_run_id is not None:
            try:
                db = _open_results_db(self._case_dir)
                complete_run_log(db, self._single_scan_run_id,
                                 processed=n_total, output_rows=n_updated)
                db.close()
            except Exception:
                pass
        primary = getattr(self, '_single_scan_primary_path', None)
        if self._view_is_recursive:
            self._rebuild_file_view_from_checked(preserve_filter=True, select_path=primary)
        else:
            self._refresh_folder_view(preserve_filter=True, select_path=primary)
        self.status_bar.showMessage(
            f"Header scan: {n_updated} of {n_total} file(s) identified"
        )

    def _refresh_folder_view(self, preserve_filter: bool = False, select_path: str | None = None):
        """Rebuild the file model for the currently displayed folder.

        Pass preserve_filter=True when refreshing in-place (e.g. after a
        header scan) so the user's active filter and scroll position are kept.
        Pass select_path to scroll to and re-select a specific row after refresh.
        """
        if not self._view_path and self._view_path != "":
            return
        folder_path = self._view_path
        children = self.folder_map.get(folder_path, [])
        has_bundles = any(p.split('/')[-1] in self.guid_to_bundle for p in children)
        headers = self.file_headers + (['UUID'] if has_bundles else [])
        new_model = FileTableModel(headers)
        self._update_filter_columns(headers)

        if preserve_filter:
            _filter_text = self.filter_input.text()
            _filter_col  = self.filter_col_combo.currentIndex() - 1
            _scroll_val  = self.file_view.verticalScrollBar().value()
        else:
            self.filter_input.clear()
            self.proxy_model.set_filter("", -1)

        batch = []
        for path in sorted(children):
            name = path.split('/')[-1]
            is_folder = path in self.folder_map
            file_type, grey_row, skip = self._classify_entry(path, name, is_folder)
            if skip:
                continue
            meta = self.full_metadata.get(path, {})
            fc = self._count_files_recursive(path) if is_folder else -1
            cols = self._build_entry_cols(path, name, meta, is_folder, file_type, fc)
            if has_bundles:
                cols.append(name if name in self.guid_to_bundle else "")
            is_opened_archive = path in self._nested_archive_map
            batch.append((cols, path, fc, is_folder and not grey_row, grey_row, is_opened_archive))
        new_model.append_rows_batch(batch)
        self._set_file_model(new_model)

        if preserve_filter:
            if _filter_text:
                if select_path:
                    def _on_filter_done_select(*_):
                        new_model.filter_done.disconnect(_on_filter_done_select)
                        self._select_file_by_path(select_path)
                    new_model.filter_done.connect(_on_filter_done_select)
                self.proxy_model.set_filter(_filter_text, _filter_col)
            else:
                self.file_view.verticalScrollBar().setValue(_scroll_val)
                if select_path:
                    self._select_file_by_path(select_path)
        elif select_path:
            self._select_file_by_path(select_path)

        self.file_view.resizeColumnsToContents()
        self._refresh_table_status()

    def _warn_and_select_missing(self, paths):
        from PySide6.QtWidgets import QMessageBox
        count = len(paths)
        msg = QMessageBox(self)
        msg.setWindowTitle("Archive Integrity Warning")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText(
            f"{count} UUID folder{'s' if count != 1 else ''} "
            f"{'are' if count != 1 else 'is'} missing the expected\n"
            f".com.apple.mobile_container_manager.metadata.plist file.\n\n"
            f"This may indicate a corrupt or incomplete download. The bundle ID\n"
            f"cannot be resolved for these folders — they are displayed using\n"
            f"their raw UUID. The affected folders have been selected so you\n"
            f"can press \"Show Selected Items\" to review them."
        )
        msg.setDetailedText("\n".join(sorted(paths)))
        msg.exec()
        self._tick_items_by_path(set(paths))

    def _tick_items_by_path(self, path_set):
        """Check the tree item for every path in path_set.
        Descends only the branches needed, loading lazily as it goes."""
        for path in path_set:
            self._tick_single_path(path)
        self.tree_view.viewport().update()
        self._update_selected_btn()
        self._rebuild_file_view_from_checked()

    def _tick_single_path(self, target_path):
        """Navigate to target_path in the tree, loading lazily, and tick the item."""
        invisible_root = self.tree_model.invisibleRootItem()
        if invisible_root.rowCount() == 0:
            return
        current = invisible_root.child(0)  # "/ [Full Filesystem]"
        self._ensure_children_loaded(current)
        parts = [p for p in target_path.split('/') if p]
        for part in parts:
            found = False
            for row in range(current.rowCount()):
                child = current.child(row)
                child_path = child.data(Qt.ItemDataRole.UserRole)
                if child_path is not None and child_path.split('/')[-1] == part:
                    self._ensure_children_loaded(child)
                    current = child
                    found = True
                    break
            if not found:
                return
        if current.isCheckable():
            current.setCheckState(Qt.CheckState.Checked)

    def reload_tree_entirely(self):
        if not self.folder_map: return
        if self._search_index_worker and self._search_index_worker.isRunning():
            self._search_index_worker.stop()
            self._search_index_worker.wait()
        self._reset_tree_model()
        self.tree_view.setModel(self.tree_model)
        self._tree_populating = True
        root_item = QStandardItem("/ [Full Filesystem]")
        root_item.setData("", Qt.ItemDataRole.UserRole)
        root_item.setEditable(False)
        root_item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
        root_item.setCheckable(True)
        root_item.setCheckState(Qt.CheckState.Unchecked)
        self.tree_model.invisibleRootItem().appendRow(root_item)
        self._populate_tree_children_batched(root_item, "", on_done=lambda: self._on_tree_loaded(root_item))

    def _on_tree_loaded(self, root_item):
        self.tree_view.expand(self.tree_model.indexFromItem(root_item))
        self.tree_view.horizontalScrollBar().setValue(0)
        self._tree_populating = False
        if hasattr(self, '_adapter'):
            fmt_label = "GrayKey" if self._adapter.format == FfsAdapter.FORMAT_GRAYKEY else "Cellebrite"
            self.tree_model.setHorizontalHeaderLabels([f"Folder Structure — {fmt_label}"])
        self.progress_bar.hide()
        elapsed = time.monotonic() - getattr(self, '_load_start', time.monotonic())
        self.status_bar.showMessage(
            f"Loaded: {os.path.basename(self.zip_path)}  —  {elapsed:.1f}s")
        if hasattr(self, '_adapter') and self._adapter.format == FfsAdapter.FORMAT_GRAYKEY:
            QTimer.singleShot(0, self._expand_to_private_var)
        QTimer.singleShot(0, self._fit_splitter_to_tree)

    def _populate_tree_children_batched(self, parent_item, parent_path, on_done=None):
        """Add immediate folder children of parent_path in frame-sized batches
        so the UI stays responsive for large archives or slow network paths."""
        children = sorted(
            p for p in self.folder_map.get(parent_path, [])
            if p in self.folder_map or self._is_empty_folder_entry(p)
        )
        state = {'idx': 0}

        def _batch():
            deadline = time.monotonic() + FRAME_BUDGET_SECS
            while state['idx'] < len(children):
                p = children[state['idx']]
                state['idx'] += 1
                in_fm = p in self.folder_map
                if in_fm and self._should_hide_folder(p):
                    continue
                if not in_fm and self._hide_empty_folders:
                    continue
                name = p.split('/')[-1]
                item = QStandardItem(self._display_name(name))
                item.setData(p, Qt.ItemDataRole.UserRole)
                item.setEditable(False)
                item.setCheckable(True)
                item.setCheckState(Qt.CheckState.Unchecked)
                parent_item.appendRow(item)
                if self.folder_map.get(p):
                    placeholder = QStandardItem()
                    placeholder.setData(_TREE_PLACEHOLDER, Qt.ItemDataRole.UserRole)
                    placeholder.setEditable(False)
                    item.appendRow(placeholder)
                if time.monotonic() >= deadline:
                    QTimer.singleShot(0, _batch)
                    return
            if on_done:
                on_done()

        QTimer.singleShot(0, _batch)

    def _fit_splitter_to_tree(self):
        """Resize the splitter so the tree panel is wide enough to show its
        top-level content.  The user can drag it smaller or larger."""
        # Measure natural content width (only top-level rows are loaded due to
        # lazy expansion).  resizeColumnToContents sets the column to content
        # width, but _update_tree_column will immediately restore it to the
        # correct 1000 px minimum — so we read the value first, then restore.
        self.tree_view.resizeColumnToContents(0)
        content_w = self.tree_view.columnWidth(0)
        self._update_tree_column()   # restore to max(viewport, _TREE_COL_MIN)
        tree_w = max(content_w, 280) + self.tree_view.verticalScrollBar().sizeHint().width() + 12
        total = self.splitter.width()
        if total > 0:
            self.splitter.setSizes([tree_w, max(total - tree_w, 200)])

    def _populate_tree_children(self, parent_item, parent_path):
        """Add immediate folder children of parent_path to parent_item.
        Each child that itself has children gets a placeholder so the tree
        shows an expand arrow; actual grandchildren are loaded on demand."""
        children = sorted(self.folder_map.get(parent_path, []))
        for p in children:
            in_fm = p in self.folder_map
            is_empty_dir = not in_fm and self._is_empty_folder_entry(p)
            if not in_fm and not is_empty_dir:
                continue  # genuine file — not a directory
            if in_fm and self._should_hide_folder(p):
                continue
            if is_empty_dir and self._hide_empty_folders:
                continue
            name = p.split('/')[-1]
            item = QStandardItem(self._display_name(name))
            item.setData(p, Qt.ItemDataRole.UserRole)
            item.setEditable(False)
            item.setCheckable(True)
            item.setCheckState(Qt.CheckState.Unchecked)
            parent_item.appendRow(item)
            # Add a placeholder so Qt shows the expand arrow without
            # loading all grandchildren upfront.
            if self.folder_map.get(p):
                placeholder = QStandardItem()
                placeholder.setData(_TREE_PLACEHOLDER, Qt.ItemDataRole.UserRole)
                placeholder.setEditable(False)
                item.appendRow(placeholder)

    def _on_tree_item_expanded(self, index):
        """Lazy-load children when a tree node is expanded for the first time.

        The scroll position is saved before children are added and restored
        after Qt's internal scrollTo settles, so expanding a node keeps the
        viewport stable.
        """
        hpos = self.tree_view.horizontalScrollBar().value()
        vpos = self.tree_view.verticalScrollBar().value()
        item = self.tree_model.itemFromIndex(index)
        if item is None or item.rowCount() != 1:
            return
        placeholder = item.child(0)
        if placeholder is None or placeholder.data(Qt.ItemDataRole.UserRole) != _TREE_PLACEHOLDER:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        self._tree_populating = True
        item.removeRow(0)
        self._populate_tree_children(item, path)
        self._tree_populating = False
        QTimer.singleShot(0, lambda hpos=hpos, vpos=vpos: (
            self.tree_view.horizontalScrollBar().setValue(hpos),
            self.tree_view.verticalScrollBar().setValue(vpos)
        ))

    def _ensure_children_loaded(self, item):
        """If item still holds only a placeholder, replace it with real children."""
        if item.rowCount() == 1:
            placeholder = item.child(0)
            if placeholder and placeholder.data(Qt.ItemDataRole.UserRole) == _TREE_PLACEHOLDER:
                path = item.data(Qt.ItemDataRole.UserRole)
                if path is not None:
                    self._tree_populating = True
                    item.removeRow(0)
                    self._populate_tree_children(item, path)
                    self._tree_populating = False

    def _ensure_all_descendants_loaded(self, item):
        """Recursively force-load every level under item (used for recursive tick)."""
        self._ensure_children_loaded(item)
        for row in range(item.rowCount()):
            child = item.child(row)
            if child is not None:
                self._ensure_all_descendants_loaded(child)

    def _find_tree_item(self, path: str):
        """Return the tree QStandardItem for *path* if already loaded, else None."""
        invisible_root = self.tree_model.invisibleRootItem()
        if invisible_root.rowCount() == 0:
            return None
        current = invisible_root.child(0)  # "/ [Full Filesystem]"
        if not path:
            return current
        for part in (p for p in path.split('/') if p):
            found = None
            for row in range(current.rowCount()):
                child = current.child(row)
                child_path = child.data(Qt.ItemDataRole.UserRole)
                if child_path is not None and child_path.split('/')[-1] == part:
                    found = child
                    break
            if found is None:
                return None
            current = found
        return current

    def _item_has_placeholder(self, item) -> bool:
        """Return True if item's only child is an unloaded placeholder."""
        return (item.rowCount() == 1
                and item.child(0) is not None
                and item.child(0).data(Qt.ItemDataRole.UserRole) == _TREE_PLACEHOLDER)

    def _insert_tree_folder_item(self, parent_item, ui_path: str, check_state):
        """Insert a folder item for *ui_path* under *parent_item* if not already present."""
        for row in range(parent_item.rowCount()):
            child = parent_item.child(row)
            if child.data(Qt.ItemDataRole.UserRole) == ui_path:
                return child
        name = ui_path.split('/')[-1]
        item = QStandardItem(self._display_name(name))
        item.setData(ui_path, Qt.ItemDataRole.UserRole)
        item.setEditable(False)
        item.setCheckable(True)
        item.setCheckState(check_state)
        if self.folder_map.get(ui_path):
            placeholder = QStandardItem()
            placeholder.setData(_TREE_PLACEHOLDER, Qt.ItemDataRole.UserRole)
            item.appendRow(placeholder)
        self._tree_populating = True
        try:
            parent_item.appendRow(item)
        finally:
            self._tree_populating = False
        return item

    def _select_file_by_path(self, path: str) -> None:
        """Select and scroll to the file-browser row for *path*, if visible."""
        for row in range(self.file_model.rowCount()):
            if self.file_model._rows[row][1] == path:
                src_idx   = self.file_model.index(row, 0)
                proxy_idx = self.proxy_model.mapFromSource(src_idx)
                if proxy_idx.isValid():
                    self.file_view.selectRow(proxy_idx.row())
                    self.file_view.scrollTo(
                        proxy_idx, QAbstractItemView.ScrollHint.EnsureVisible)
                return

    def on_tree_item_changed(self, item):
        if self._tree_populating:
            return
        # Single folder tick — expand to show unticked children
        if item.checkState() == Qt.CheckState.Checked:
            self.tree_view.expand(self.tree_model.indexFromItem(item))
        self.tree_view.viewport().update()
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QTimer.singleShot(0, self._deferred_rebuild)

    def _update_selected_btn(self):
        """Recount ticked folders and update the status label and deselect button."""
        checked = set()
        self._collect_checked_paths(self.tree_model.invisibleRootItem(), checked)
        if not checked:
            self.show_selected_btn.setVisible(False)
            self.deselect_all_btn.setVisible(False)
            return
        item_count = sum(len(self.folder_map.get(folder, [])) for folder in checked)
        self.show_selected_btn.setText(f"Show {item_count:,} Selected Items")
        self.show_selected_btn.setVisible(True)
        self.deselect_all_btn.setVisible(True)

    def _deselect_all_files(self):
        """Untick all folders in the tree and clear the file browser."""
        self.tree_model.blockSignals(True)
        root = self.tree_model.invisibleRootItem()
        self._cascade_check(root, Qt.CheckState.Unchecked)
        self.tree_model.blockSignals(False)
        self.tree_view.viewport().update()
        self._view_is_recursive = False
        self._update_selected_btn()
        # Show the highlighted folder if one is selected, otherwise clear
        idx = self.tree_view.currentIndex()
        if idx.isValid():
            self.on_folder_selected(idx)
        else:
            self._set_file_model(FileTableModel(self.file_headers))
            self._update_filter_columns(self.file_headers)
            self._refresh_table_status()

    def _deferred_rebuild(self):
        self._rebuild_pending = False
        self._update_selected_btn()
        self._rebuild_file_view_from_checked()

    def _on_recent_context_menu(self, point):
        view = self.archive_dropdown.view()
        index = view.indexAt(point)
        if not index.isValid():
            return
        row = index.row()
        path = self.archive_dropdown.itemData(row)
        if not path:
            return
        menu = QMenu(self)
        act = QAction("Remove from recent list", self)
        act.triggered.connect(lambda: self._remove_recent(path))
        menu.addAction(act)
        menu.exec(view.viewport().mapToGlobal(point))

    # ── ffs_archives helpers ──────────────────────────────────────────────────

    def _migrate_device_labels(self):
        """One-time migration: copy labels from the old device_labels.json into
        ffs_archives entries, then delete the file."""
        old_file = 'device_labels.json'
        if not os.path.exists(old_file):
            return
        try:
            with open(old_file, 'r', encoding='utf-8') as f:
                old_labels: dict = json.load(f)
        except Exception:
            return
        changed = False
        for entry in self._ffs_archives:
            p = entry.get('path', '')
            if p and not entry.get('label') and p in old_labels:
                entry['label'] = old_labels[p]
                changed = True
        if changed:
            self._save_ffs_archives()
        try:
            os.remove(old_file)
        except OSError:
            pass

    def _archive_entry(self, path: str) -> dict | None:
        """Return the archive entry for *path*, or None if not present."""
        for e in self._ffs_archives:
            if e.get('path') == path:
                return e
        return None

    def _save_ffs_archives(self):
        """Persist the in-memory _ffs_archives list to disk."""
        try:
            with open(FFS_ARCHIVES_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._ffs_archives, f, indent=2)
        except OSError:
            pass

    def _upsert_archive(self, path: str, case_dir: str | None = None):
        """Move *path* to front of _ffs_archives (max 5), optionally setting case_dir."""
        # Preserve any existing label when reinserting
        existing = self._archive_entry(path)
        existing_label = existing.get('label', '') if existing else ''
        self._ffs_archives = [e for e in self._ffs_archives if e.get('path') != path]
        entry: dict = {'path': path}
        if case_dir is not None:
            entry['case_dir'] = case_dir
        if existing_label:
            entry['label'] = existing_label
        self._ffs_archives.insert(0, entry)
        self._ffs_archives = self._ffs_archives[:5]
        self.recent_paths = [e['path'] for e in self._ffs_archives]
        self._save_ffs_archives()

    def _remove_recent(self, path):
        self._ffs_archives = [e for e in self._ffs_archives if e.get('path') != path]
        self.recent_paths = [e['path'] for e in self._ffs_archives]
        self._save_ffs_archives()
        self.update_dropdown_ui()

    def save_recent_list(self, path):
        if not self._archive_entry(path):
            self._upsert_archive(path, self._case_dir)
        self.update_dropdown_ui()

    def _fetch_and_store_label(self, path):
        entry      = self._archive_entry(path)
        has_label  = bool(entry and entry.get('label'))
        has_db_info = False
        if self._case_dir:
            try:
                db = _open_results_db(self._case_dir)
                has_db_info = bool(load_device_info(db))
                db.close()
            except Exception:
                pass
        if has_label and has_db_info:
            return

        fields, label = _read_device_info(path)

        if self._case_dir:
            try:
                db = _open_results_db(self._case_dir)
                if fields and not has_db_info:
                    save_device_info(db, fields)
                # Always record which archive this case folder belongs to
                upsert_device_info_field(db, 'zip_path', path, 'archive')
                db.close()
            except Exception:
                pass
        if entry is not None and not has_label:
            entry['label'] = label
            self._save_ffs_archives()
            self.update_dropdown_ui()

    @staticmethod
    def _archive_display(path: str, label: str) -> str:
        """Build the display string: '[X.XX GB]  [label  —  ] path'."""
        try:
            size_gb = os.path.getsize(path) / 1_073_741_824
            size_str = f"{size_gb:.2f} GB"
        except OSError:
            size_str = "? GB"
        prefix = f"{label}  —  " if label else ""
        return f"[{size_str}]  {prefix}{path}"

    def update_dropdown_ui(self):
        self.archive_dropdown.blockSignals(True)
        self.archive_dropdown.clear()
        # Header item — always index 0, not selectable
        self.archive_dropdown.addItem("Recently Opened FFS")
        from PySide6.QtGui import QStandardItemModel as _QStdModel
        model = self.archive_dropdown.model()
        assert isinstance(model, _QStdModel)
        item = model.item(0)
        from PySide6.QtCore import Qt as _Qt
        item.setFlags(item.flags() & ~(_Qt.ItemFlag.ItemIsSelectable | _Qt.ItemFlag.ItemIsEnabled))
        for p in self.recent_paths:
            entry = self._archive_entry(p)
            label = entry.get('label', '') if entry else ''
            display = self._archive_display(p, label)
            self.archive_dropdown.addItem(display, userData=p)
        self.archive_dropdown.setCurrentIndex(0)
        self.archive_dropdown.blockSignals(False)
        # Re-select the currently loaded archive if any
        if self.zip_path:
            for i in range(1, self.archive_dropdown.count()):
                if self.archive_dropdown.itemData(i) == self.zip_path:
                    self.archive_dropdown.setCurrentIndex(i)
                    break

    def _populate_recent_menu(self):
        self._recent_menu.clear()
        if not self.recent_paths:
            empty_act = QAction("No recently opened archives", self)
            empty_act.setEnabled(False)
            self._recent_menu.addAction(empty_act)
            return
        for p in self.recent_paths:
            entry = self._archive_entry(p)
            label = entry.get('label', '') if entry else ''
            display = self._archive_display(p, label)
            act = QAction(display, self)
            act.triggered.connect(lambda _checked, path=p: self.start_loading(path))
            self._recent_menu.addAction(act)
        self._recent_menu.addSeparator()
        clear_act = QAction("Clear Recent List", self)
        clear_act.triggered.connect(self._clear_recent_list)
        self._recent_menu.addAction(clear_act)

    def _clear_recent_list(self):
        self._ffs_archives.clear()
        self.recent_paths.clear()
        self._save_ffs_archives()
        self.update_dropdown_ui()

    def _stop_all_workers(self):
        """Stop every background QThread gracefully before the window closes.

        Qt aborts with SIGABRT if QThread is destroyed while still running, so
        all workers must be stopped before Python tears down Qt objects.
        Safe to call more than once (closeEvent + atexit both call it).
        """
        if getattr(self, '_workers_stopped', False):
            return
        self._workers_stopped = True

        def _stop(worker, has_stop=False):
            try:
                if worker is None or not worker.isRunning():
                    return
                if has_stop:
                    worker.stop()
                else:
                    worker.quit()
                worker.wait(2000)
            except Exception:
                pass

        _stop(getattr(self, 'worker', None))
        _stop(getattr(self, '_scan_worker', None))
        _stop(getattr(self, '_single_scan_worker', None))
        _stop(getattr(self, '_hex_worker', None))
        _stop(getattr(self, '_thumb_worker', None), has_stop=True)
        _stop(getattr(self, '_search_index_worker', None), has_stop=True)

    def closeEvent(self, event):
        self._stop_all_workers()
        super().closeEvent(event)


def _qt_message_handler(msg_type, context, message):
    if "qt.text.font.db" in (context.category or "") and "OpenType support missing" in message:
        return
    if msg_type == QtMsgType.QtWarningMsg:
        print(f"Qt warning: {message}", file=sys.stderr)
    elif msg_type in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        print(f"Qt error: {message}", file=sys.stderr)

if __name__ == "__main__":
    qInstallMessageHandler(_qt_message_handler)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path(os.path.join("resources", "icon.png"))))
    window = FastZipBrowser()
    app.aboutToQuit.connect(window._stop_all_workers)
    atexit.register(window._stop_all_workers)  # runs before Qt's atexit on exception exit
    window.show()
    sys.exit(app.exec())