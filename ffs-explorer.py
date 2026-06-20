import sys
import os
import re
import io
import time
import atexit
import zipfile
import gzip
import zlib
import json
import hashlib
import struct
import plistlib
import xml.dom.minidom
import pathlib
import subprocess
import configparser
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from functools import lru_cache, partial
from itertools import islice
from operator import itemgetter

_APP_DIR             = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app')    # adapters, db_utils, etc.
_CONFIG_DIR          = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config') # JSON config files
FFS_ARCHIVES_FILE    = os.path.join(_CONFIG_DIR, 'ffs_archives.json')                     # recently opened archives
HARDWARE_MODELS_FILE = os.path.join('config', 'hardware_models.json')                      # device make/model lookup tables (relative — used with resource_path() for PyInstaller)
sys.path.insert(0, _APP_DIR)

from adapters import FfsAdapter
from db_utils import (_open_cache_db, _open_results_db, OldSchemaError,
                      check_cache_schema, check_results_schema,
                      save_blob, load_blob,
                      save_header_types, load_header_types, clear_header_types,
                      save_guid_bundle_map, load_guid_bundle_map,
                      save_folder_counts, save_folder_sizes, load_folder_data,
                      save_device_info, load_device_info,
                      upsert_device_info_field,
                      start_run_log, complete_run_log, load_last_run,
                      load_run_history,
                      save_nested_archive, save_nested_archive_entries,
                      save_nested_archive_failure,
                      load_nested_archives, load_nested_archive_entries,
                      load_bookmark_groups, save_bookmark_group,
                      save_bookmark_entries, load_bookmark_entries,
                      delete_bookmark_group)
import header_scan
from hex_viewer import HexViewerMixin
from media_viewer import MediaViewerMixin, MEDIA_EXTENSIONS
from keyword_search import KeywordSearchMixin
from artifact_viewer import ArtifactViewerMixin
from sqlite_viewer import SqliteViewerMixin, _SQLITE_MAGIC
from segb_viewer import SegbViewerMixin, is_segb
from artifact_db import load_artifact_results
from streaming_zip import StreamingZipIndex
from zip_reader import read_nested_entry
from zip_cd_cache import (CachedZipView, is_valid as _cd_cache_valid,
                          save as _cd_cache_save, load as _cd_cache_load,
                          probe_delta as _probe_delta,
                          compute_data_offsets as _compute_data_offsets,
                          sidecar_hash as _cd_sidecar_hash,
                          extract_cd_hash as _cd_extract_hash)
from ffs_metadata import (_build_folder_tree, _compute_folder_sizes,
                          _find_metadata_only_folders, _find_missing_plists,
                          _UUID_RE, parse_archive_metadata, persist_result,
                          _parse_child as _ffs_parse_child,
                          pack_snapshot, unpack_snapshot,
                          SNAPSHOT_KEY as _SNAPSHOT_KEY,
                          SNAPSHOT_VERSION as _SNAPSHOT_VERSION)
from datetime import datetime, timedelta, timezone
from PySide6.QtWidgets import (QApplication, QMainWindow, QTreeView, QTableView, QVBoxLayout,
                              QHBoxLayout, QGridLayout, QWidget, QHeaderView, QPushButton,
                              QFileDialog, QProgressBar, QMenu, QDialog,
                              QComboBox, QSplitter, QStatusBar, QFrame,
                              QLineEdit, QLabel,
                              QTabWidget, QSizePolicy,
                              QMessageBox, QCheckBox, QAbstractItemView,
                              QTreeWidget, QTreeWidgetItem, QTextEdit,
                              QListWidget, QListWidgetItem, QDateEdit)
from PySide6.QtGui import (QStandardItemModel, QStandardItem, QAction, QFont,
                           QColor, QIcon)
from PySide6.QtCore import (Qt, QThread, Signal, QSortFilterProxyModel, QTimer, QDate,
                             QModelIndex, QPersistentModelIndex, QAbstractTableModel,
                             qInstallMessageHandler, QtMsgType)
from PySide6.QtCore import QSettings as _QSettings

FRAME_BUDGET_SECS = 0.016   # max seconds per UI batch — keeps the interface responsive at 60 fps while the tree builds

# Shared pool for small fire-and-forget background jobs (cache DB writes,
# zip pre-opening) — replaces creating a throwaway ThreadPoolExecutor per call.
_BG_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='ffs-bg')

# Load-snapshot cache: the complete result of a first archive load, stored in
# casecache.db so re-opens skip the CD parse, metadata read, and derivations.
# _SNAPSHOT_KEY / _SNAPSHOT_VERSION are imported from ffs_metadata (single
# source of truth, shared with the subprocess parser).

_TREE_PLACEHOLDER  = "__placeholder__"
_BM_ROOT           = "__bookmarks__"
_BM_GROUP_PREFIX   = "__bm_group_"

_SETTINGS_ORG = "KlasScripts"
_SETTINGS_APP = "FFS Explorer"

_DEFAULT_CASE_NAME_SLOTS = ['case_folder', 'make_model', 'os_version', 'ffs_path', 'ffs_size']

# Ordered list of (display label, pref key) for case-name slot dropdowns.
CASE_SLOT_OPTIONS = [
    ("Make & Model",     "make_model"),
    ("OS & Version",     "os_version"),
    ("Case Folder Name", "case_folder"),
    ("FFS File Size",    "ffs_size"),
    ("Full Path",        "ffs_path"),
    ("(not required)",   "none"),
]


def _load_prefs() -> dict:
    s = _QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    slots = [s.value(f'case_name_slot_{i}', _DEFAULT_CASE_NAME_SLOTS[i], type=str)
             for i in range(5)]
    return {
        'case_data_root':  s.value('case_data_root', '', type=str),
        'case_name_slots': slots,
        'case_sort_order': s.value('case_sort_order', 'recent', type=str),
    }


def _save_prefs(prefs: dict):
    s = _QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    s.setValue('case_data_root', prefs.get('case_data_root', ''))
    for i, slot in enumerate(prefs.get('case_name_slots', _DEFAULT_CASE_NAME_SLOTS)):
        s.setValue(f'case_name_slot_{i}', slot)
    s.setValue('case_sort_order', prefs.get('case_sort_order', 'recent'))


# Formatted archive sizes, keyed by path.  Stat-ing every archive on each
# dropdown/menu rebuild blocks the GUI when archives live on network shares,
# and forensic archives never change size — so cache the first result.
_FFS_SIZE_CACHE: dict = {}


def _slot_value(entry: dict, slot_key: str) -> str:
    """Return the display string for one slot key from an archive entry dict."""
    label = entry.get('label', '')
    match slot_key:
        case '' | None | 'none':
            return ''
        case 'case_folder':
            cd = entry.get('case_dir', '')
            return os.path.basename(cd) if cd else ''
        case 'ffs_size':
            path = entry.get('path', '')
            if not path:
                return ''
            cached = _FFS_SIZE_CACHE.get(path)
            if cached is None:
                try:
                    cached = f"{os.path.getsize(path) / 1_073_741_824:.2f} GB"
                except OSError:
                    return '? GB'
                _FFS_SIZE_CACHE[path] = cached
            return cached
        case 'ffs_path':
            return entry.get('path', '')
        case 'make_model':
            return label.split(' · ')[0].strip() if ' · ' in label else label.strip()
        case 'os_version':
            parts = label.split(' · ', 1)
            return parts[1].strip() if len(parts) > 1 else ''
        case _:
            return ''


def _format_archive_entry(entry: dict, prefs: dict) -> str:
    """Format a case menu entry using the slot preferences."""
    slots  = prefs.get('case_name_slots', _DEFAULT_CASE_NAME_SLOTS)
    parts  = [v for slot in slots if (v := _slot_value(entry, slot))]
    return '  —  '.join(parts) if parts else (entry.get('path') or entry.get('case_dir') or '?')


# Minimum tree column width (px).  The column always fills the panel, but never
# shrinks below this so deeply-indented items can be reached via horizontal scroll.
_TREE_COL_MIN = 1000

# _SYSTEM_METADATA_NAMES and _UUID_RE now live in ffs_metadata.py (Qt-free,
# shared with the subprocess parser).  _UUID_RE is imported back above.

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


# Single extension → type lookup table.  One O(1) dict hit replaces a cascade
# of set-membership tests — _get_file_type runs once per entry in several
# full-archive loops.  The extension sets are mutually disjoint.
_EXT_TO_TYPE: dict = {
    **{e: 'Picture'       for e in PICTURE_EXTENSIONS},
    **{e: 'Video'         for e in VIDEO_EXTENSIONS},
    **{e: 'Database'      for e in DATABASE_EXTENSIONS},
    '.plist': 'Property List',
    **{e: 'Log'           for e in LOG_EXTENSIONS},
    **{e: 'Document'      for e in DOCUMENT_EXTENSIONS},
    **{e: 'Audio'         for e in AUDIO_EXTENSIONS},
    **{e: 'Archive'       for e in ARCHIVE_EXTENSIONS},
    **{e: 'Application'   for e in APPLICATION_EXTENSIONS},
    **{e: 'JSON'          for e in JSON_EXTENSIONS},
    **{e: 'XML'           for e in XML_EXTENSIONS},
    **{e: 'Web / Data'    for e in WEB_DATA_EXTENSIONS},
    **{e: 'Certificate'   for e in CERTIFICATE_EXTENSIONS},
}


def _get_file_type(name):
    # Dotted sidecar extensions (.db-wal, .sqlite-journal, …) are all in the
    # table; only bare dotless companions ('foo-wal') need the endswith fallback.
    ft = _EXT_TO_TYPE.get(os.path.splitext(name)[1].lower())
    if ft is not None:
        return ft
    return 'Database' if name.lower().endswith(('-wal', '-shm')) else 'Other'



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


def _read_text_from_zip(z, names: frozenset, *candidates) -> str:
    """Return the text content of the first matching candidate path, or ''."""
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


def _read_plist_from_zip(z: zipfile.ZipFile, names: frozenset, *candidates) -> dict:
    """Try each candidate path in the zip and return the first successfully
    parsed plist as a dict, or {} if none found."""
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


def _read_ios_info(z: zipfile.ZipFile, names: frozenset, ffs_adapter) -> tuple[list[tuple[str,str,str]], str]:
    """Extract iOS device info from MobileGestalt, SystemVersion, and preferences plists.

    Returns (fields, label) where fields are (field_name, data, source) triples.
    Returns ([], '') if no usable data is found.
    """
    mg_plist = _read_plist_from_zip(z, names, *ffs_adapter.user_candidates(_MG_PLIST_SUFFIX))

    ios_version = _plist_find(mg_plist, 'ProductVersion') or ''
    ios_version_source = 'MobileGestalt.plist' if ios_version else ''
    if not ios_version:
        sv = _read_plist_from_zip(z, names,
            *ffs_adapter.system_candidates('System/Library/CoreServices/SystemVersion.plist'),
            *ffs_adapter.user_candidates('run/SystemVersion.plist'),
        )
        ios_version = sv.get('ProductVersion', '')
        if ios_version:
            ios_version_source = 'SystemVersion.plist'

    pref = _read_plist_from_zip(z, names,
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


def _read_android_info(z: zipfile.ZipFile, names: frozenset, ffs_adapter) -> tuple[list[tuple[str,str,str]], str]:
    """Extract Android device info by merging system/vendor/product build.prop files.

    Returns (fields, label) where fields are (field_name, data, source) triples.
    Returns ([], '') if no usable data is found.
    """
    bp: dict[str, tuple[str, str]] = {}   # key → (value, source_file)
    for prop_file in ('system/build.prop', 'vendor/build.prop', 'product/build.prop'):
        content = _read_text_from_zip(z, names, *ffs_adapter.system_candidates(prop_file))
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
            names = frozenset(z.namelist())
            ffs_adapter = FfsAdapter.detect(z, names)
            result = _read_ios_info(z, names, ffs_adapter)
            if not result[0]:
                result = _read_android_info(z, names, ffs_adapter)
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


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


@lru_cache(maxsize=4096)
def _date_prefix(days: int) -> str:
    """'YYYY-MM-DD' for a day number since the epoch (cached — few unique days)."""
    return (_EPOCH_UTC + timedelta(days=days)).strftime('%Y-%m-%d')


@lru_cache(maxsize=1 << 16)
def _format_ts_cached(ts) -> str:
    """Format an epoch timestamp (s or APFS ns) as a UTC display string.

    strftime per row dominated recursive-view row building, so the date part
    is cached per day and the time of day is computed arithmetically —
    ~5x faster on archives with diverse timestamps, identical output.
    """
    try:
        if ts > 1e10:
            ts = ts / 1e9   # APFS stores timestamps as nanoseconds since epoch
        days, rem = divmod(int(ts // 1), 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        return f"{_date_prefix(days)} {h:02d}:{m:02d}:{s:02d} UTC"
    except (ValueError, OSError, OverflowError):
        return "---"


def _count_header_candidates(ui_metadata: dict, ffs_adapter) -> int:
    """Count 'Other'-typed files in scan_folders — no I/O, used for dialog display."""
    scan_folders = tuple(ffs_adapter.scan_folders())
    return sum(
        1 for ui_path in ui_metadata
        if ui_path.startswith(scan_folders)
        and _get_file_type(ui_path.rsplit('/', 1)[-1]) == 'Other'
    )


_EXTRACTABLE_TYPES = {'Archive', 'Compressed'}

# Modern Office/iWork documents are ZIP containers, so they can be extracted
# like nested archives — that is what makes their inner XML keyword-searchable.
# Legacy .doc/.xls/.ppt are OLE2, not zip, and stay out.
_ZIP_DOCUMENT_EXTENSIONS = {'.docx', '.xlsx', '.pptx',
                            '.pages', '.numbers', '.key'}


def _is_extractable(name: str, ft: str) -> bool:
    """True if the file can go through nested-archive extraction."""
    return (ft in _EXTRACTABLE_TYPES or
            os.path.splitext(name)[1].lower() in _ZIP_DOCUMENT_EXTENSIONS)


# File-browser columns appended for images matched in the Photos Metadata
# artifact results (Hidden and Trashed are merged into 'Status' to keep the
# browser compact).
_PHOTO_HEADERS = ['Flags', 'Taken', 'Added', 'Original Name', 'Album', 'Labels',
                  'Origin', 'Camera', 'Place', 'GPS', 'Faces',
                  'Face Ages', 'Status']

# Triage flags derived from the photo Labels (Apple's scene/object classes).
# A clean overlay over the messy raw label list — sortable/filterable.  These
# are review-prioritisation aids, never detection: Apple does not classify
# drugs or abuse material.  Overridable via config/photo_flags.json.
_DEFAULT_PHOTO_FLAGS: dict = {
    'Weapon':   {'firearm', 'gun', 'pistol', 'revolver', 'rifle', 'shotgun',
                 'ammunition', 'bullet', 'cartridge', 'knife', 'blade',
                 'sword', 'holster', 'weapon'},
    'Document': {'document', 'receipt', 'ticket', 'passport', 'license plate',
                 'identity document', 'credit card', 'id card'},
    'Vehicle':  {'car', 'truck', 'motorcycle', 'motor home', 'sportscar',
                 'vehicle', 'license plate'},
    'People':   {'people', 'crowd', 'person'},
    'Drug':     {'pill', 'tablet', 'syringe', 'needle', 'cannabis',
                 'marijuana', 'bong', 'pipe', 'powder', 'drug'},
}


def _photo_flags_path() -> str:
    """User-editable photo_flags.json location.

    In a frozen build it sits next to the .exe so investigators can find and
    edit it (and edits survive app restarts, unlike anything inside the
    PyInstaller bundle).  In dev it stays in config/ beside the source."""
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'photo_flags.json')
    return os.path.join(_CONFIG_DIR, 'photo_flags.json')


def _seed_photo_flags(path: str) -> None:
    """Create photo_flags.json on first run so the user has something to edit.

    Copies the template bundled in the exe; if that's missing, writes the
    built-in defaults.  Never overwrites an existing (possibly edited) file."""
    if os.path.exists(path):
        return
    try:
        template = resource_path(os.path.join('config', 'photo_flags.json'))
        if os.path.exists(template) and os.path.abspath(template) != os.path.abspath(path):
            shutil.copyfile(template, path)
        else:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({k: sorted(v) for k, v in _DEFAULT_PHOTO_FLAGS.items()},
                          f, indent=2)
    except OSError:
        pass


def _load_photo_flag_rules() -> dict:
    """Return {flag_name: frozenset(lowercased terms)} from the user-editable
    photo_flags.json (next to the exe), falling back to the built-in defaults."""
    rules = {k: set(v) for k, v in _DEFAULT_PHOTO_FLAGS.items()}
    path = _photo_flags_path()
    _seed_photo_flags(path)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        loaded = {k: {str(t).lower() for t in v}
                  for k, v in data.items()
                  if not k.startswith('_') and isinstance(v, list)}
        if loaded:
            rules = loaded
    except (OSError, ValueError):
        pass
    return {k: frozenset(v) for k, v in rules.items()}


def _classify_archive(ui_path: str, ffs_adapter) -> str:
    """Return the top-level category for an archive ui_path.

    Android returns: 'AndroidApp', 'AndroidMedia'
    iOS returns:     'SMS', 'Mail', 'Files', 'App'
    """
    if ffs_adapter.format == ffs_adapter.FORMAT_ZIP_EXTRAS:
        return 'AndroidMedia' if ui_path.startswith('data/media/') else 'AndroidApp'
    # GrayKey can hold an Android device — check Android paths before iOS paths
    if ffs_adapter.format == ffs_adapter.FORMAT_GRAYKEY:
        if ui_path.startswith('data/data/'):
            return 'AndroidApp'
        if ui_path.startswith('data/media/'):
            return 'AndroidMedia'
    pv = 'private/var/' if ffs_adapter.format == ffs_adapter.FORMAT_GRAYKEY else ''
    lib = pv + 'mobile/Library/'
    if ui_path.startswith(lib + 'SMS/Attachments/'):
        return 'SMS'
    if ui_path.startswith(lib + 'Mail/'):
        return 'Mail'
    if ui_path.startswith(lib + 'Mobile Documents/'):
        return 'Files'
    return 'App'


def _android_package(ui_path: str) -> str:
    """Return the package name from a data/data/<package>/... path."""
    parts = ui_path.split('/')
    if len(parts) >= 3 and parts[0] == 'data' and parts[1] == 'data':
        return parts[2]
    return ''


def _android_media_parts(ui_path: str) -> tuple[str, str]:
    """Return (volume_label, subfolder) for a data/media/<volume>/[subfolder]/... path."""
    parts = ui_path.split('/')
    if len(parts) < 3:
        return 'Internal Storage', '(root)'
    volume = parts[2]
    volume_label = 'Internal Storage' if volume == '0' else f'SD Card ({volume})'
    subfolder = parts[3] if len(parts) > 3 else '(root)'
    return volume_label, subfolder


def _discover_all_archives(
    ui_metadata: dict,
    ffs_adapter,
    header_type_overrides: dict,
) -> list[dict]:
    """Return all archives across container + Library paths.

    Each entry: {'ui_path', 'category', 'file_type', 'mtime'}
    mtime is nanoseconds since epoch (0 if unknown).
    """
    scan_roots = tuple(ffs_adapter.archive_discovery_folders())
    results = []
    for ui_path, meta in ui_metadata.items():
        if not ui_path.startswith(scan_roots):
            continue
        name = ui_path.rsplit('/', 1)[-1]
        ft = _get_file_type(name)
        if ft == 'Other':
            ft = header_type_overrides.get(ui_path, 'Other')
        if not _is_extractable(name, ft):
            continue
        category = _classify_archive(ui_path, ffs_adapter)
        mtime = meta.get('mtime', 0) if isinstance(meta, dict) else 0
        results.append({'ui_path': ui_path, 'category': category,
                        'file_type': ft, 'mtime': mtime})
    return results


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
    scan_roots = tuple(ffs_adapter.scan_folders())
    results = []
    for ui_path in ui_metadata:
        if not ui_path.startswith(scan_roots):
            continue
        name = ui_path.rsplit('/', 1)[-1]
        ft   = _get_file_type(name)
        if ft == 'Other':
            ft = header_type_overrides.get(ui_path, 'Other')
        if not _is_extractable(name, ft):
            continue
        results.append(ui_path)
    return results


def _resolve_file_candidates(
    zip_path: str,
    ui_paths: list,
    ffs_adapter,
    z=None,
    streaming_index=None,
    delta: int | None = None,
) -> list[tuple[str, int, int]]:
    """Return [(ui_path, data_offset, file_size)] for the given ui_paths.

    Opens its own ZipFile when neither *z* nor *streaming_index* is supplied.
    """
    if not ui_paths:
        return []
    candidates: list[tuple[str, int, int]] = []
    resolve = ffs_adapter.resolve

    if streaming_index is not None:
        entries = streaming_index._entries
        for ui_path in ui_paths:
            row = entries.get(resolve(ui_path))
            if row:
                candidates.append((ui_path, row[0], row[2]))
        return candidates

    close_z = z is None
    if close_z:
        try:
            z = zipfile.ZipFile(zip_path, 'r')
        except Exception:
            return []
    try:
        target_infos = []
        target_meta: dict[str, tuple[str, int]] = {}  # filename → (ui_path, file_size)
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
        if close_z:
            z.close()
    return candidates


def _collect_header_candidates(
    zip_path: str,
    ui_metadata: dict,
    ffs_adapter,
    z=None,
    streaming_index=None,
    delta: int | None = None,
) -> list[tuple[str, int, int]]:
    """Return [(ui_path, data_offset, file_size)] for 'Other'-typed files in scan_folders."""
    scan_folders = tuple(ffs_adapter.scan_folders())
    targets = [
        ui_path for ui_path in ui_metadata
        if ui_path.startswith(scan_folders)
        and _get_file_type(ui_path.rsplit('/', 1)[-1]) == 'Other'
    ]
    return _resolve_file_candidates(zip_path, targets, ffs_adapter,
                                    z=z, streaming_index=streaming_index, delta=delta)


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
    metadata_ready       = Signal(dict, dict, dict, object, object, object, dict, object, object)  # ui_metadata, folder_map, guid_to_bundle, zip_names, adapter, missing_plist_paths, folder_sizes, zip_ui_paths, metadata_only_folders
    header_scan_progress = Signal(int)   # remaining (decreasing)
    header_scan_done     = Signal(dict)  # {ui_path: detected_type}

    def __init__(self, zip_path: str, scan_headers: bool = False, case_dir: str | None = None):
        super().__init__()
        self.zip_path = zip_path
        self.scan_headers = scan_headers
        self.case_dir = case_dir

    def _try_load_from_snapshot(self) -> bool:
        """Fast re-open: restore the previous load's full result from casecache.db.

        Skips the .zcd parse, format detection, metadata read (msgpack /
        extra fields), folder-tree build, and metadata-only scan.  Returns
        True if the snapshot was emitted, False to fall back to a full load.
        """
        try:
            with closing(_open_cache_db(self.case_dir)) as db:
                raw = load_blob(db, _SNAPSHOT_KEY, _SNAPSHOT_VERSION)
                if raw is None:
                    return False
                _, folder_sizes = load_folder_data(db)
                guid_to_bundle  = load_guid_bundle_map(db)
            if not folder_sizes:
                return False   # sizes are required downstream — do a full load
            self.status_update.emit("Loading archive metadata from local cache…")
            # Chunked deserialize, yielding the GIL between chunks so the GUI
            # and progress bar keep animating instead of hanging on one big
            # unpack (~1-2 s for a large Cellebrite archive).
            snap = unpack_snapshot(raw, yield_cb=lambda: time.sleep(0))
            fmt, user_pfx, sys_pfx, old_layout = snap['adapter']
            ffs_adapter = FfsAdapter(fmt, user_pfx, sys_pfx, bool(old_layout))
            self._local_extra_delta = snap['delta']
            zip_names = frozenset(snap['zip_names'])
            zui = snap['zip_ui_paths']
            zip_ui_paths = frozenset(zui) if zui is not None else None
            ui_metadata = snap['ui_metadata']
            folder_map  = snap['folder_map']
            metadata_only_folders = set(snap['metadata_only'])
        except Exception:
            return False

        missing_plist_paths = _find_missing_plists(folder_map, ffs_adapter, guid_to_bundle)
        self.metadata_ready.emit(
            ui_metadata, folder_map, guid_to_bundle, zip_names,
            ffs_adapter, missing_plist_paths, folder_sizes, zip_ui_paths,
            metadata_only_folders)
        return True

    def _run_parse_offloaded(self) -> bool:
        """Parse the (non-streaming) archive and emit metadata_ready.

        Tries a child process first — true parallelism, GUI stays responsive.
        The child persists its result to casecache.db, so on success we simply
        load it back through the tested snapshot fast-path.  If the subprocess
        is unavailable for any reason, we compute in this thread instead
        (correct, just less responsive).  Returns True if metadata was emitted,
        False if parsing failed entirely (caller falls back to the legacy path).
        """
        if self._parse_in_subprocess():
            if self._try_load_from_snapshot():
                return True
            # Child reported done but the snapshot is unusable — fall through
            # to an in-thread parse rather than leaving the user with nothing.

        self.status_update.emit("Parsing archive metadata…")
        try:
            result = parse_archive_metadata(
                self.zip_path, self.case_dir,
                status_cb=self.status_update.emit)
        except Exception as exc:
            self.status_update.emit(f"Metadata parse error: {exc}")
            return False
        try:
            self._emit_and_persist_result(result)
        except Exception as exc:
            self.status_update.emit(f"Metadata parse error: {exc}")
            return False
        return True

    def _parse_in_subprocess(self) -> bool:
        """Run the parse in a child process which persists to casecache.db.
        Returns True if it finished successfully, False on any failure (so the
        caller can fall back to an in-process parse)."""
        try:
            import multiprocessing as mp
            ctx = mp.get_context('spawn')
            recv_conn, send_conn = ctx.Pipe(duplex=False)
            proc = ctx.Process(
                target=_ffs_parse_child,
                args=(send_conn, self.zip_path, self.case_dir),
                daemon=True,
            )
            proc.start()
            send_conn.close()   # parent only receives
        except Exception as exc:
            self.status_update.emit(f"Could not start parser process: {exc}")
            return False

        done = False
        try:
            while True:
                if not recv_conn.poll(0.2):
                    if not proc.is_alive():
                        break       # died without reporting
                    continue
                try:
                    msg = recv_conn.recv()
                except EOFError:
                    break
                tag = msg[0]
                if tag == 'status':
                    self.status_update.emit(msg[1])
                elif tag == 'done':
                    done = True
                    break
                elif tag == 'error':
                    self.status_update.emit(
                        "Parser process failed — retrying in-process.")
                    print(f"[ffs parse subprocess]\n{msg[1]}")
                    break
        finally:
            recv_conn.close()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
        return done

    def _emit_and_persist_result(self, result: dict):
        """Emit metadata_ready from an in-thread parse result, then persist the
        snapshot / guid map / folder sizes to the case DB."""
        fmt, user_pfx, sys_pfx, old_layout = result['adapter_args']
        ffs_adapter = FfsAdapter(fmt, user_pfx, sys_pfx, bool(old_layout))
        self._local_extra_delta = result['delta']
        self._streaming_index   = None
        zip_names    = frozenset(result['zip_names'])
        zip_ui_paths = result['zip_ui_paths']
        ui_metadata  = result['ui_metadata']
        folder_map   = result['folder_map']
        guid_to_bundle = result['guid_to_bundle']
        folder_sizes   = result['folder_sizes']
        missing_plist_paths = _find_missing_plists(
            folder_map, ffs_adapter, guid_to_bundle)

        # Emit first — the UI tree appears immediately; DB writes follow.
        self.metadata_ready.emit(
            ui_metadata, folder_map, guid_to_bundle, zip_names,
            ffs_adapter, missing_plist_paths, folder_sizes, zip_ui_paths,
            set(result['metadata_only']))
        try:
            persist_result(self.case_dir, result)
        except Exception:
            pass

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

            # Fast path: a previous load of this archive left a snapshot in the
            # cache DB — re-emit it and skip all archive I/O and derivations.
            # (Header scans need an open archive handle, so they take the full path.)
            if self.case_dir and not self.scan_headers and self._try_load_from_snapshot():
                return

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

                # Fast path: offload the heavy non-streaming parse to a child
                # process.  It runs on its own interpreter/GIL so the CPU-bound
                # work doesn't freeze the GUI (a QThread can't achieve this for
                # pure-Python work — one shared GIL).  Header scans and streaming
                # archives stay on the in-thread path below.
                if self._streaming_index is None and not self.scan_headers:
                    if self._run_parse_offloaded():
                        return

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
                    # One pass: the size dict's keys double as the name set.
                    zip_sizes = {info.filename: info.file_size for info in _infolist}
                    zip_names = frozenset(zip_sizes)
                    ffs_adapter = FfsAdapter.detect(z_ctx, zip_names)
                    _stored = [i for i in _infolist
                               if i.compress_type == zipfile.ZIP_STORED and i.file_size > 0]
                    self._local_extra_delta = _probe_delta(self.zip_path, _stored)
                time.sleep(0)   # yield GIL after C-level frozenset build

                cached_guid_bundle: dict | None = None
                if self.case_dir:
                    try:
                        with closing(_open_cache_db(self.case_dir)) as _db:
                            _cached = load_guid_bundle_map(_db)
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

            missing_plist_paths = _find_missing_plists(
                folder_map, ffs_adapter, guid_to_bundle)

            _cached_sizes: dict | None = None
            if self.case_dir:
                try:
                    with closing(_open_cache_db(self.case_dir)) as _db:
                        _, _fs = load_folder_data(_db)
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

            # Metadata-only detection looks for Apple container metadata files,
            # so run it only for iOS archives (Cellebrite iOS, GrayKey iOS).
            # GrayKey Android has no private/var tree — the walk would find nothing.
            _is_ios = (
                ffs_adapter.format == FfsAdapter.FORMAT_CELLEBRITE
                or (ffs_adapter.format == FfsAdapter.FORMAT_GRAYKEY
                    and 'private/var' in folder_map)
            )
            if _is_ios:
                self.status_update.emit("Identifying metadata-only folders...")
                metadata_only_folders = _find_metadata_only_folders(
                    folder_map, zip_names, zip_ui_paths, ffs_adapter)
            else:
                metadata_only_folders = set()

            # Pack the re-open snapshot BEFORE emitting — once the main thread
            # processes metadata_ready it mutates these dicts (nested-archive
            # injection), which would race with serialisation.
            snapshot_blob = None
            if self.case_dir and self._streaming_index is None:
                self.status_update.emit("Saving load cache…")
                try:
                    snapshot_blob = pack_snapshot(
                        [ffs_adapter.format, ffs_adapter.user_prefix,
                         ffs_adapter.sys_prefix, ffs_adapter.old_layout],
                        self._local_extra_delta, zip_names, zip_ui_paths,
                        ui_metadata, folder_map, metadata_only_folders)
                except Exception:
                    snapshot_blob = None

            # Emit first — UI tree appears immediately. DB and cache writes follow.
            self.metadata_ready.emit(
                ui_metadata, folder_map, guid_to_bundle, zip_names,
                ffs_adapter, missing_plist_paths, folder_sizes, zip_ui_paths,
                metadata_only_folders)

            if self.case_dir:
                try:
                    with closing(_open_cache_db(self.case_dir)) as _db:
                        if _need_save_guid:
                            save_guid_bundle_map(_db, guid_to_bundle)
                        if _cached_sizes is None:
                            save_folder_sizes(_db, folder_sizes)
                        if snapshot_blob is not None:
                            save_blob(_db, _SNAPSHOT_KEY, _SNAPSHOT_VERSION,
                                      snapshot_blob)
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
    'Taken':         'Photos.sqlite — when the photo was taken (ZDATECREATED; from EXIF, not a filesystem date)',
    'Added':         'Photos.sqlite — when the photo was added to this device\'s library (ZADDEDDATE); a large gap from Taken means it was received/imported, not captured here',
    'Original Name': 'Photos.sqlite — filename before any rename by the Photos library',
    'Album':         'Photos.sqlite — album(s) containing this asset',
    'Flags':         'Triage categories derived from Labels (Weapon, Document, Vehicle…) plus Minor (face) from the face-age estimate. A review aid for prioritising images — NOT detection; Apple does not classify drugs or abuse material. Edit photo_flags.json (next to the app) to customise.',
    'Labels':        'psi.sqlite — Apple\'s scene/object classification (e.g. Dog, Beach, Receipt)',
    'Origin':        'Photos.sqlite ZIMPORTEDBY — how the photo entered this library, naming the source: Back/Front Camera = captured on this device; an app name (WhatsApp, Instagram…), AirDrop, Messages, Safari or Screenshot = arrived another way',
    'Camera':        'Photos.sqlite — EXIF camera make and model; a model differing from the device suggests an imported/received photo',
    'Place':         'Photos.sqlite — reverse-geocoded location recorded by Apple',
    'GPS':           'Photos.sqlite — latitude, longitude (blank if no GPS data)',
    'Faces':         'Photos.sqlite — number of faces Apple detected in the image',
    'Face Ages':     'Photos.sqlite — Apple\'s age estimate per detected face (e.g. Adult ×2, Child)',
    'Status':        'Photos.sqlite — Favorite, Hidden and/or Recently Deleted (in trash, not yet purged) state',
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

    def set_filter(self, text, col=-1, text_cols=None,
                   date_col=-1, date_from='', date_to='',
                   type_col=-1, types=None):
        """Start an incremental filter. Clears visible rows immediately, then
        adds matching rows in chunks so the UI count updates live.

        text/text_cols — substring match across the given columns (all if None
                         and col < 0, the single column if col >= 0).
        date_col/from/to — keep rows whose 'YYYY-MM-DD …' cell date is within
                           [date_from, date_to] (inclusive, 'YYYY-MM-DD').
        type_col/types — keep rows whose type cell is in the *types* set
                         (None = no type filtering)."""
        self._filter_gen += 1
        my_gen = self._filter_gen
        needle = text.casefold()
        ncols  = self.columnCount()
        if text_cols is not None:
            check_cols = [c for c in text_cols if c < ncols]
        else:
            check_cols = list(range(ncols)) if col < 0 else [col]
        total  = len(self._all_rows)

        # Clear visible rows immediately
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

        show_files   = self._show_files
        show_folders = self._show_folders

        def _row_ok(r):
            is_folder = r[2] >= 0  # fc == -1 for files, >= 0 for folders
            if not (show_folders if is_folder else show_files):
                return False
            cols = r[0]
            if date_col >= 0:
                v = cols[date_col] if date_col < len(cols) else ''
                if not v or not (date_from <= v[:10] <= date_to):
                    return False
            if types is not None:
                tv = cols[type_col] if 0 <= type_col < len(cols) else ''
                if (tv or '') not in types:
                    return False
            if needle:
                if not any(needle in cols[c].casefold()
                           for c in check_cols if c < len(cols) and cols[c]):
                    return False
            return True

        if not needle and date_col < 0 and types is None:
            visible = [r for r in self._all_rows if _row_ok(r)]
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
            matched = [r for r in source[idx[0]:end] if _row_ok(r)]
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

    def distinct_values(self, header: str) -> list[str]:
        """Sorted distinct cell values for the given header across all rows."""
        try:
            col = self._headers.index(header)
        except ValueError:
            return []
        seen = {r[0][col] for r in self._all_rows
                if col < len(r[0]) and r[0][col]}
        return sorted(seen)

    def set_type_filter(self, show_files: bool, show_folders: bool, **filter_args):
        self._show_files   = show_files
        self._show_folders = show_folders
        self.set_filter(**filter_args)

    # ── sorting ──────────────────────────────────────────────────────────────

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """Sort both _all_rows and _rows so sort survives re-filtering."""
        self.layoutAboutToBeChanged.emit()
        self._sort_col = column
        self._sort_order = order
        reverse = (order == Qt.SortOrder.DescendingOrder)
        if column == self._files_col:
            key = itemgetter(2)   # C-level accessor — faster than a lambda on large row lists
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

    def set_filter(self, *args, **kwargs):
        src = self.sourceModel()
        if src is not None:
            assert isinstance(src, FileTableModel)
            src.set_filter(*args, **kwargs)


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

    def __init__(self, zip_path: str, base_folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Case Folder Settings")
        self.setModal(True)
        self.setMinimumWidth(620)

        self._accepted_dir: str | None = None
        self._base_folder = base_folder

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # FFS path display — read-only, selectable so the user can copy exhibit references
        layout.addWidget(QLabel("<b>Archive path</b>"))
        hint = QLabel("Select and copy any part of the path to use as the case name below.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        dir_edit = QLineEdit(zip_path)
        dir_edit.setReadOnly(True)
        dir_edit.setFrame(False)
        dir_edit.setStyleSheet("background: transparent;")
        layout.addWidget(dir_edit)

        layout.addSpacing(6)

        # Case folder name
        layout.addWidget(QLabel("<b>Case folder name</b>"))
        self._name_edit = QLineEdit()
        self._name_edit.setText(pathlib.Path(zip_path).stem)
        self._name_edit.textChanged.connect(self._on_name_changed)
        layout.addWidget(self._name_edit)

        # Header scan option
        self._scan_cb = QCheckBox("Scan unknown file headers for precise type detection")
        self._scan_cb.setChecked(False)
        layout.addWidget(self._scan_cb)

        layout.addStretch()

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

        self._on_name_changed()

    def _on_name_changed(self):
        self._save_btn.setEnabled(bool(self._name_edit.text().strip()))

    def _on_save(self):
        base = self._base_folder
        name = self._name_edit.text().strip()
        if not name:
            return
        if not base:
            # No global root set — ask user to pick a folder directly
            base = QFileDialog.getExistingDirectory(self, "Select Base Folder")
            if not base:
                return
            self._base_folder = base
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


class PreferencesDialog(QDialog):
    """Global (cross-case) application preferences."""

    def __init__(self, parent=None, example_archive: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.setMinimumWidth(560)
        prefs = _load_prefs()

        # Example archive used for the live preview; fall back to placeholder.
        self._example_archive = example_archive or {
            'path':     '/case_data/MyCase/extraction.zip',
            'case_dir': '/case_data/MyCase',
            'label':    'Apple iPhone 14 · iOS 17.4',
        }

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Case Data Root ────────────────────────────────────────────────────
        layout.addWidget(QLabel("<b>Case Data Root</b>"))
        layout.addWidget(QLabel(
            "New archives will store their cache and exports inside a subfolder here. "
            "Leave blank to be prompted each time."
        ))
        ssd_note = QLabel(
            "<i>Tip: Use an SSD for this location. The cache involves many small random "
            "reads and writes — an SSD will significantly improve load and scan times.</i>"
        )
        ssd_note.setWordWrap(True)
        layout.addWidget(ssd_note)

        root_row = QHBoxLayout()
        self._root_edit = QLineEdit(prefs.get('case_data_root', ''))
        self._root_edit.setPlaceholderText("Not set — will ask each time")
        root_row.addWidget(self._root_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)
        root_row.addWidget(browse_btn)
        layout.addLayout(root_row)

        # ── Case Menu Display ─────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)
        layout.addWidget(QLabel("<b>Case Menu Display</b>"))
        layout.addWidget(QLabel(
            "Choose up to five pieces of information to show for each case. "
            "Slots are joined left to right; empty slots are skipped."
        ))

        slots_grid = QGridLayout()
        slots_grid.setColumnStretch(1, 1)
        current_slots = prefs.get('case_name_slots', _DEFAULT_CASE_NAME_SLOTS)
        self._slot_combos: list[QComboBox] = []
        for i in range(5):
            slots_grid.addWidget(QLabel(f"Slot {i + 1}:"), i, 0)
            combo = QComboBox()
            for display, key in CASE_SLOT_OPTIONS:
                combo.addItem(display, userData=key)
            target = current_slots[i] if i < len(current_slots) else 'none'
            idx = next((j for j, (_, k) in enumerate(CASE_SLOT_OPTIONS) if k == target),
                       len(CASE_SLOT_OPTIONS) - 1)
            combo.setCurrentIndex(idx)
            combo.currentIndexChanged.connect(self._update_preview)
            slots_grid.addWidget(combo, i, 1)
            self._slot_combos.append(combo)
        layout.addLayout(slots_grid)

        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort order:"))
        self._sort_combo = QComboBox()
        self._sort_combo.addItem("Most Recent First", userData="recent")
        self._sort_combo.addItem("Alphabetical",      userData="alphabetical")
        self._sort_combo.setCurrentIndex(
            0 if prefs.get('case_sort_order', 'recent') == 'recent' else 1)
        sort_row.addWidget(self._sort_combo)
        sort_row.addStretch()
        layout.addLayout(sort_row)

        layout.addWidget(QLabel("Preview:"))
        self._preview_label = QLabel()
        self._preview_label.setWordWrap(True)
        self._preview_label.setStyleSheet(
            "background:#f5f5f5; border:1px solid #ccc; border-radius:4px; padding:4px 8px;")
        layout.addWidget(self._preview_label)
        self._update_preview()

        # ── Buttons ───────────────────────────────────────────────────────────
        layout.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Case Data Root",
                                                  self._root_edit.text())
        if folder:
            self._root_edit.setText(folder)

    def _update_preview(self):
        slots = [combo.currentData() for combo in self._slot_combos]
        text  = _format_archive_entry(self._example_archive, {'case_name_slots': slots})
        self._preview_label.setText(text or "<i>(empty — select at least one slot)</i>")

    def _on_save(self):
        slots = [combo.currentData() for combo in self._slot_combos]
        _save_prefs({
            'case_data_root':  self._root_edit.text().strip(),
            'case_name_slots': slots,
            'case_sort_order': self._sort_combo.currentData(),
        })
        self.accept()


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
            streaming_index=self._streaming_index, delta=self._delta,
        )
        if not candidates:
            self.done.emit({})
            return
        results = header_scan.scan_entries(self._zip_path, candidates)
        self.done.emit(results)


class DeviceInfoWorker(QThread):
    """Reads device make/model/OS info from the archive off the GUI thread.

    _read_device_info opens the zip (a full central-directory read, slow on
    network shares) — running it on the main thread froze the UI after load.
    """
    done = Signal(list, str)   # fields, label

    def __init__(self, zip_path: str, parent=None):
        super().__init__(parent)
        self._zip_path = zip_path

    def run(self):
        fields, label = _read_device_info(self._zip_path)
        self.done.emit(fields, label)


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
            completed: set[str] = set()
            with closing(_open_cache_db(self._case_dir)) as db:
                for row in load_nested_archives(db):
                    ui_path        = row['ui_path']
                    stored_filename = row['stored_filename']
                    expected_count  = row['entry_count']
                    disk_path       = os.path.join(out_dir, stored_filename)
                    if not os.path.isfile(disk_path):
                        continue
                    actual_count = len(load_nested_archive_entries(db, ui_path))
                    if actual_count == expected_count:
                        completed.add(ui_path)
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
            with closing(_open_cache_db(self._case_dir)) as db:
                save_nested_archive(db, ui_path, stored_filename,
                                    file_size, entry_count)
                save_nested_archive_entries(db, ui_path, content_rows)
            return True, compound_type

        except Exception as exc:
            msg = str(exc)
            self.item_error.emit(ui_path, msg)
            try:
                with closing(_open_cache_db(self._case_dir)) as db:
                    save_nested_archive_failure(db, ui_path, msg)
            except Exception:
                pass
            try:
                log_path = os.path.join(self._case_dir, 'nested_archive_errors.log')
                ts = datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[{ts}] FAILED: {ui_path}\n  {msg}\n")
            except OSError:
                pass
            return False, None


_SHA256_HEX_RE = re.compile(rb'[0-9a-fA-F]{64}')


def _integrity_sidecar_path(zip_path: str, ffs_adapter) -> tuple:
    """Return (sidecar_path | None, kind_label, candidate_names) for the
    capture record file.

    Cellebrite extractions ship a .ufd named like the FFS zip.  GrayKey
    ships a .pdf report named after the bare device UDID — the zip adds a
    suffix (e.g. 00008110-0008196A2299401E_files_full.zip comes with
    00008110-0008196A2299401E.pdf) — so the stem truncated at the first
    underscore is also tried."""
    base = os.path.splitext(zip_path)[0]
    if ffs_adapter.format == FfsAdapter.FORMAT_GRAYKEY:
        # 1. Hash sidecar shipped with the extraction: <name>.zip.sha256
        candidates = [zip_path + ext for ext in ('.sha256', '.SHA256')]
        for path in candidates:
            if os.path.isfile(path):
                return path, 'SHA256 hash file', candidates
        # 2. PDF report named after the device UDID
        folder, stem = os.path.split(base)
        stem = stem.split('_', 1)[0]
        pdf_candidates = [os.path.join(folder, stem + ext)
                          for ext in ('.pdf', '.PDF')]
        candidates += pdf_candidates
        for path in pdf_candidates:
            if os.path.isfile(path):
                return path, 'GrayKey PDF report', candidates
        # The report's serial does not always match the zip's, but the model
        # prefix before the dash (e.g. '00008110-') is shared — look for a
        # PDF starting with it, then fall back to a lone PDF in the folder.
        try:
            pdfs = [os.path.join(folder, n) for n in os.listdir(folder)
                    if n.lower().endswith('.pdf')]
        except OSError:
            pdfs = []
        if '-' in stem:
            prefix = stem.split('-', 1)[0] + '-'
            prefixed = [p for p in pdfs
                        if os.path.basename(p).startswith(prefix)]
            if len(prefixed) == 1:
                return prefixed[0], 'GrayKey PDF report', candidates
        if len(pdfs) == 1:
            return pdfs[0], 'GrayKey PDF report', candidates
        return None, 'GrayKey capture record (.sha256 or PDF report)', candidates
    kind = 'Cellebrite UFD file'
    candidates = [base + ext for ext in ('.ufd', '.UFD')]
    for path in candidates:
        if os.path.isfile(path):
            return path, kind, candidates
    return None, kind, candidates


_PDF_ESCAPES = {0x6e: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                0x28: 0x28, 0x29: 0x29, 0x5c: 0x5c}


def _pdf_unescape(b: bytes) -> bytes:
    """Resolve backslash escapes (\\(, \\), \\\\, \\n, octal…) in a PDF string."""
    out, i = bytearray(), 0
    while i < len(b):
        c = b[i]
        if c == 0x5c and i + 1 < len(b):
            n = b[i + 1]
            if n in _PDF_ESCAPES:
                out.append(_PDF_ESCAPES[n])
                i += 2
                continue
            if 0x30 <= n <= 0x37:           # octal escape, 1-3 digits
                j, digits = i + 1, b''
                while j < len(b) and len(digits) < 3 and 0x30 <= b[j] <= 0x37:
                    digits += bytes([b[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
        out.append(c)
        i += 1
    return bytes(out)


def _extract_sidecar_hashes(path: str) -> set:
    """Return every 64-hex-digit string in the sidecar file, lowercased.

    For PDFs, FlateDecode streams are decompressed and the text-show
    strings unescaped.  GrayKey/VeraKey reports encode text as UTF-16BE
    (each character preceded by a NUL byte), so a null-stripped variant
    of the text is scanned as well.  Over-collecting is harmless: a stray
    candidate can never equal the zip's real SHA256 by accident."""
    with open(path, 'rb') as f:
        raw = f.read()
    hashes = {m.group(0).lower().decode() for m in _SHA256_HEX_RE.finditer(raw)}
    if path.lower().endswith('.pdf'):
        string_re = re.compile(rb'\(((?:[^()\\]|\\.)*)\)')
        for m in re.finditer(rb'stream\r?\n(.*?)endstream', raw, re.DOTALL):
            try:
                data = zlib.decompress(m.group(1))
            except zlib.error:
                continue
            segs = [_pdf_unescape(s.group(1))
                    for s in string_re.finditer(data)]
            for text in (data,
                         b'\n'.join(segs),                       # one string per cell
                         b''.join(segs),                         # hash split across strings
                         b'\n'.join(segs).replace(b'\x00', b''),  # UTF-16BE text
                         b''.join(segs).replace(b'\x00', b'')):
                hashes.update(h.group(0).lower().decode()
                              for h in _SHA256_HEX_RE.finditer(text))
    return hashes


class IntegrityCheckWorker(QThread):
    """Computes the SHA256 of the FFS zip in a background thread."""
    # 'qint64' — a plain int signal is 32-bit in C++ and overflows past 2 GB
    progress = Signal('qint64', 'qint64')   # (bytes_done, bytes_total)
    done     = Signal(str)                  # computed hex digest, '' on cancel/error

    def __init__(self, zip_path: str, parent=None):
        super().__init__(parent)
        self._zip_path = zip_path

    def run(self):
        sha = hashlib.sha256()
        try:
            total = os.path.getsize(self._zip_path)
            done_bytes = 0
            last_emit = 0.0
            with open(self._zip_path, 'rb') as f:
                while True:
                    if self.isInterruptionRequested():
                        self.done.emit('')
                        return
                    chunk = f.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    sha.update(chunk)
                    done_bytes += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= 0.2:
                        last_emit = now
                        self.progress.emit(done_bytes, total)
            self.done.emit(sha.hexdigest())
        except OSError:
            self.done.emit('')


class ProcessDialog(QDialog):
    """Lets the user view header-scan history and re-run the file header scan."""
    header_scan_done       = Signal(dict)
    header_types_cleared   = Signal()   # emitted before a rescan so the main window can reset
    nested_extraction_done = Signal()   # emitted after NestedArchiveWorker finishes

    _RUN_TYPE           = 'header_scan'
    _RUN_TYPE_INTEGRITY = 'integrity_check'

    def __init__(self, zip_path, case_dir, ffs_adapter, ui_metadata,
                 streaming_index=None, delta=None, preselect_nested=False,
                 auto_archive_selection=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Process Case")
        self.setModal(True)
        self.setMinimumWidth(620)
        self._zip_path        = zip_path
        self._case_dir        = case_dir
        self._adapter         = ffs_adapter
        self._ui_metadata     = ui_metadata
        self._streaming_index = streaming_index
        self._delta           = delta
        self._candidate_count  = _count_header_candidates(ui_metadata, ffs_adapter)
        self._scan_worker:      HeaderScanWorker     | None = None
        self._nested_worker:    NestedArchiveWorker  | None = None
        self._integrity_worker: IntegrityCheckWorker | None = None

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

        self._chk_integrity = QCheckBox("Check data integrity")
        self._chk_integrity.setToolTip(
            "Computes the SHA256 of the FFS zip and compares it against the "
            "value recorded at capture\n(the .ufd file for Cellebrite, the "
            ".pdf report for GrayKey, next to the zip).\n"
            "A mismatch means the data has changed since acquisition.")
        # Ticked by default for a fresh case; once a check is on record it
        # starts unticked so a huge zip isn't re-hashed on every Run.
        self._chk_integrity.setChecked(
            self._load_last_integrity_run() is None)
        layout.addWidget(self._chk_integrity)

        integrity_row = QHBoxLayout()
        integrity_row.setContentsMargins(22, 0, 0, 0)   # align under checkbox text
        self._integrity_label = QLabel()
        self._integrity_label.setWordWrap(True)
        integrity_row.addWidget(self._integrity_label, 1)
        self._integrity_hist_btn = QPushButton("Full History…")
        self._integrity_hist_btn.clicked.connect(self._show_integrity_history)
        integrity_row.addWidget(self._integrity_hist_btn)
        layout.addLayout(integrity_row)

        layout.addWidget(QLabel("<hr><b>Operations</b>"))

        header_row = QHBoxLayout()
        self._chk_header = QCheckBox("Scan file headers")
        self._chk_header.setChecked(False)
        header_row.addWidget(self._chk_header, 1)
        scan_folders_btn = QPushButton("Scanned Folders…")
        scan_folders_btn.setToolTip(
            "Show which folders the header scan covers — files outside "
            "these are not scanned.")
        scan_folders_btn.clicked.connect(self._show_scan_folders)
        header_row.addWidget(scan_folders_btn)
        layout.addLayout(header_row)

        self._chk_nested = QCheckBox("Find and select archives for extraction")
        self._chk_nested.setChecked(False)
        self._chk_nested.setToolTip(
            "Scans app containers, iCloud/Files, Mail and SMS locations for archive "
            "files,\nthen shows a selection dialog so you can choose which to extract.\n"
            "Requires a completed header scan to catch extensionless archives.")
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
        self._sync_header_checkbox()
        if preselect_nested:
            self._chk_nested.setChecked(True)
        if auto_archive_selection and self._scan_complete():
            # Came from keyword search with the scan already done — jump
            # straight to the archive-selection step.
            self._chk_integrity.setChecked(False)
            self._chk_nested.setChecked(True)
            QTimer.singleShot(0, self._run_operations)

    # ── Stats display ─────────────────────────────────────────────────────────

    def _load_last_run(self) -> dict | None:
        if not self._case_dir:
            return None
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                return load_last_run(db, self._RUN_TYPE)
        except Exception:
            return None

    def _load_last_integrity_run(self) -> dict | None:
        if not self._case_dir:
            return None
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                return load_last_run(db, self._RUN_TYPE_INTEGRITY)
        except Exception:
            return None

    def _load_integrity_history(self) -> list[dict]:
        if not self._case_dir:
            return []
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                return load_run_history(db, self._RUN_TYPE_INTEGRITY)
        except Exception:
            return []

    def _refresh_integrity_status(self):
        last = self._load_last_integrity_run()
        if last is None:
            self._integrity_label.setText(
                "<i>No data integrity check recorded.</i>")
            self._integrity_hist_btn.setVisible(False)
            return
        run_at  = last['run_at'].replace('T', ' ')
        passed  = (last['output_rows'] or 0) > 0
        verdict = ("<span style='color:#2e7d32'>Passed</span>" if passed else
                   "<span style='color:#c62828'>⚠ FAILED — data may be "
                   "compromised</span>")
        self._integrity_label.setText(
            f"<b>Last check:</b> {run_at} — {verdict}")
        self._integrity_hist_btn.setVisible(True)

    def _show_scan_folders(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Header Scan Coverage")
        dlg.setMinimumSize(520, 300)
        lay = QVBoxLayout(dlg)
        intro = QLabel(
            "The header scan only examines files <b>without a recognised "
            "extension</b> inside the folders below. Files anywhere else "
            "keep their extension-based type and are <b>not</b> scanned.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        folder_list = QListWidget()
        for prefix in self._adapter.scan_folders():
            QListWidgetItem(prefix, folder_list)
        lay.addWidget(folder_list)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    def _show_integrity_history(self):
        history = self._load_integrity_history()
        dlg = QDialog(self)
        dlg.setWindowTitle("Data Integrity Check History")
        dlg.setMinimumSize(620, 280)
        lay = QVBoxLayout(dlg)
        tree = QTreeWidget()
        tree.setHeaderLabels(["Checked At (UTC)", "Result", "Computed SHA256"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for run in history:
            passed = (run['output_rows'] or 0) > 0
            notes  = run['notes'] or ''
            sha    = ''
            m = re.search(r'sha256=([0-9a-f]{64})', notes)
            if m:
                sha = m.group(1)
            item = QTreeWidgetItem([
                run['run_at'].replace('T', ' '),
                "Passed" if passed else "FAILED",
                sha,
            ])
            if not passed:
                item.setForeground(1, QColor('#c62828'))
            tree.addTopLevelItem(item)
        for col in range(3):
            tree.resizeColumnToContents(col)
        lay.addWidget(tree)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    def _load_nested_archives_summary(self) -> list[dict]:
        if not self._case_dir:
            return []
        try:
            with closing(_open_cache_db(self._case_dir)) as db:
                return load_nested_archives(db)
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
            lines.append(
                f"<b>Header scan:</b> {run_at} — "
                f"{scanned:,} scanned, {changed:,} types identified"
            )
            if not last['complete']:
                lines.append(
                    "<i>⚠ Previous scan was incomplete. "
                    "Re-running will clear previous results and start fresh.</i>"
                )

        self._refresh_integrity_status()

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
        self._sync_header_checkbox()

    # ── Operations ────────────────────────────────────────────────────────────

    def _scan_complete(self) -> bool:
        last = self._load_last_run()
        return last is not None and bool(last.get('complete'))

    def _sync_header_checkbox(self):
        """Lock the header-scan checkbox to reflect what Run will actually do."""
        if self._scan_complete():
            # Already done — show ticked and locked; Run will not redo it.
            self._chk_header.setChecked(True)
            self._chk_header.setEnabled(False)
            self._chk_header.setToolTip(
                "Header scan already completed for this case — it will not be re-run.")
        elif self._chk_nested.isChecked():
            # Extraction needs a completed scan first — force it on.
            self._chk_header.setChecked(True)
            self._chk_header.setEnabled(False)
            self._chk_header.setToolTip(
                "A header scan is required before archive extraction.")
        else:
            self._chk_header.setEnabled(True)
            self._chk_header.setToolTip("")

    def _on_nested_toggled(self, checked: bool):
        self._sync_header_checkbox()

    def _run_operations(self):
        run_scan      = self._chk_header.isChecked() and not self._scan_complete()
        run_integrity = self._chk_integrity.isChecked()
        if not run_integrity and not run_scan and not self._chk_nested.isChecked():
            self._status_label.setText(
                "Nothing to do — header scan already completed."
                if self._chk_header.isChecked() else
                "Nothing to do — no operations selected.")
            return
        self._run_btn.setEnabled(False)
        self._cancel_btn.setVisible(True)
        self._scan_cancelled = False
        self._status_label.setText("")
        self._post_integrity_scan = run_scan

        if run_integrity:
            self._start_integrity_check()
        else:
            self._continue_after_integrity()

    def _continue_after_integrity(self):
        if self._post_integrity_scan:
            self._start_header_scan()
        elif self._chk_nested.isChecked():
            self._show_archive_selection()
        else:
            self._finish_operations()

    # ── Data integrity check ──────────────────────────────────────────────────

    def _start_integrity_check(self):
        sidecar, kind, candidates = _integrity_sidecar_path(
            self._zip_path, self._adapter)
        if sidecar is None:
            tried = "\n".join(
                f"  • {os.path.basename(c)}" for c in candidates
                if not c.endswith(('.PDF', '.UFD', '.SHA256')))
            if 'PDF' in kind:
                stem = os.path.splitext(
                    os.path.basename(self._zip_path))[0].split('_', 1)[0]
                if '-' in stem:
                    prefix = stem.split('-', 1)[0] + '-'
                    tried += f"\n  • a .pdf starting with '{prefix}'"
                tried += "\n  • any single .pdf file in the same folder"
            QMessageBox.warning(
                self, "Integrity Check Skipped",
                f"No {kind} was found next to the archive — there is "
                "nothing to check the data against.\n\n"
                f"Looked for:\n{tried}\n\n"
                "The data integrity check was skipped.")
            self._continue_after_integrity()
            return
        if 'PDF' in kind:
            udid_re = re.compile(r'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$')
            zip_id = os.path.splitext(
                os.path.basename(self._zip_path))[0].split('_', 1)[0]
            pdf_id = os.path.splitext(
                os.path.basename(sidecar))[0].split('_', 1)[0]
            if (udid_re.match(zip_id) and udid_re.match(pdf_id)
                    and zip_id.lower() != pdf_id.lower()):
                QMessageBox.warning(
                    self, "Integrity Check Skipped",
                    "The GrayKey report found next to the archive belongs to "
                    "a DIFFERENT device:\n\n"
                    f"  Archive UDID: {zip_id}\n"
                    f"  Report UDID:  {pdf_id}\n\n"
                    "The archive cannot be verified against this report.\n"
                    "The data integrity check was skipped.")
                self._continue_after_integrity()
                return
        try:
            self._expected_hashes = _extract_sidecar_hashes(sidecar)
        except OSError as exc:
            QMessageBox.warning(self, "Integrity Check Skipped",
                                f"Could not read {os.path.basename(sidecar)}:\n"
                                f"{exc}\n\nThe data integrity check was skipped.")
            self._continue_after_integrity()
            return
        if not self._expected_hashes:
            QMessageBox.warning(
                self, "Integrity Check Skipped",
                f"No SHA256 value could be found in "
                f"{os.path.basename(sidecar)}.\n\n"
                "The data integrity check was skipped.")
            self._continue_after_integrity()
            return
        self._status_label.setText("Hashing archive (SHA256)…")
        self._integrity_worker = IntegrityCheckWorker(self._zip_path)
        self._integrity_worker.progress.connect(self._on_integrity_progress)
        self._integrity_worker.done.connect(self._on_integrity_done)
        self._integrity_worker.start()

    def _on_integrity_progress(self, done_bytes: int, total: int):
        gb = 1024 ** 3
        pct = (done_bytes / total * 100) if total else 0
        self._status_label.setText(
            f"Hashing archive (SHA256): {done_bytes / gb:.1f} / "
            f"{total / gb:.1f} GB ({pct:.0f}%)")

    def _on_integrity_done(self, digest: str):
        if not digest:
            self._status_label.setText(
                "Integrity check cancelled." if self._scan_cancelled
                else "Integrity check failed — could not read the archive.")
            self._finish_operations()
            return
        matched = digest in self._expected_hashes
        if self._case_dir:
            try:
                with closing(_open_results_db(self._case_dir)) as db:
                    run_id = start_run_log(
                        db, self._RUN_TYPE_INTEGRITY, total=1,
                        notes=f"sha256={digest} "
                              f"{'match' if matched else 'MISMATCH'}")
                    complete_run_log(db, run_id, processed=1,
                                     output_rows=1 if matched else 0)
            except Exception:
                pass
        if matched:
            self._status_label.setText(
                "Integrity check passed — SHA256 matches the capture record.")
        else:
            QMessageBox.warning(
                self, "Data Integrity Warning",
                "The SHA256 hash of the FFS archive does NOT match the value "
                "recorded at capture.\n\n"
                f"Computed: {digest}\n\n"
                "The data has changed since it was captured and may be "
                "compromised.")
            self._status_label.setText(
                "⚠ Integrity check FAILED — archive hash does not match the "
                "capture record.")
        self._chk_integrity.setChecked(False)
        self._refresh_stats()
        self._continue_after_integrity()

    def _start_header_scan(self):
        self._run_id: int | None = None

        if self._case_dir:
            try:
                with closing(_open_cache_db(self._case_dir)) as cache_db:
                    clear_header_types(cache_db)
            except Exception:
                pass
        self.header_types_cleared.emit()

        if self._case_dir:
            try:
                with closing(_open_results_db(self._case_dir)) as results_db:
                    self._run_id = start_run_log(results_db, self._RUN_TYPE,
                                                 total=self._candidate_count)
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
                with closing(_open_cache_db(self._case_dir)) as cache_db:
                    if results:
                        save_header_types(cache_db, results)
                if self._run_id is not None and not cancelled:
                    with closing(_open_results_db(self._case_dir)) as results_db:
                        complete_run_log(results_db, self._run_id,
                                         processed=n_total, output_rows=n_found)
            except Exception:
                pass

        self.header_scan_done.emit(results)

        if self._chk_nested.isChecked() and not cancelled:
            self._show_archive_selection()
        else:
            msg = (f"Cancelled — {n_found:,} type{'s' if n_found != 1 else ''} "
                   "identified before cancellation."
                   if cancelled else
                   f"Header scan done — {n_found:,} type{'s' if n_found != 1 else ''} "
                   f"identified from {n_total:,} candidate{'s' if n_total != 1 else ''}.")
            self._status_label.setText(msg)
            self._finish_operations()

    def _show_archive_selection(self):
        """Load header overrides, discover archives, show selection dialog, then extract."""
        overrides = {}
        guid_to_bundle = {}
        if self._case_dir:
            try:
                with closing(_open_cache_db(self._case_dir)) as db:
                    overrides = load_header_types(db)
                    guid_to_bundle = load_guid_bundle_map(db)
            except Exception:
                pass

        archives = _discover_all_archives(self._ui_metadata, self._adapter, overrides)

        # Build lookup: ui_path -> DB record (includes error_msg)
        db_records: dict[str, dict] = {}
        if self._case_dir:
            try:
                with closing(_open_cache_db(self._case_dir)) as db:
                    db_records = {r['ui_path']: r for r in load_nested_archives(db)}
            except Exception:
                pass

        already_extracted = {p for p, r in db_records.items() if not r.get('error_msg')}
        already_failed    = {p for p, r in db_records.items() if r.get('error_msg')}

        # New: not yet attempted. Done: succeeded. Failed: previously errored (retryable).
        new_archives  = [a for a in archives if a['ui_path'] not in db_records]
        done_archives = [a for a in archives if a['ui_path'] in already_extracted]

        # Include ALL DB failure records — even ones outside current scan paths.
        arch_lookup = {a['ui_path']: a for a in archives}
        failed_archives = []
        for path in already_failed:
            err = db_records[path].get('error_msg', '')
            discovered = arch_lookup.get(path)
            if discovered:
                failed_archives.append(dict(discovered, error_msg=err))
            else:
                failed_archives.append({
                    'ui_path':   path,
                    'file_type': _get_file_type(path.rsplit('/', 1)[-1]),
                    'error_msg': err,
                    'mtime':     0,
                })

        if not new_archives and not done_archives and not failed_archives:
            self._status_label.setText("No archive files found across all scanned locations.")
            self._finish_operations()
            return

        is_android = (self._adapter.format == self._adapter.FORMAT_ZIP_EXTRAS or
                      any(a['category'] in ('AndroidApp', 'AndroidMedia') for a in archives))
        dlg = ArchiveSelectionDialog(new_archives, guid_to_bundle=guid_to_bundle,
                                     is_android=is_android,
                                     done_archives=done_archives,
                                     failed_archives=failed_archives, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected_paths:
            self._status_label.setText("Archive selection cancelled.")
            self._finish_operations()
            return

        self._start_nested_extraction(candidates=dlg.selected_paths)

    def _start_nested_extraction(self, candidates: list[str] | None = None):
        if candidates is None:
            overrides = {}
            if self._case_dir:
                try:
                    with closing(_open_cache_db(self._case_dir)) as db:
                        overrides = load_header_types(db)
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
            gzip_types: dict[str, str] = {}
            with closing(_open_cache_db(self._case_dir)) as db:
                for rec in load_nested_archives(db):
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
        if self._integrity_worker and self._integrity_worker.isRunning():
            self._integrity_worker.requestInterruption()

    def _finish_operations(self):
        self._run_btn.setEnabled(True)
        self._cancel_btn.setVisible(False)
        self._refresh_stats()
        self._sync_header_checkbox()

    def _is_scanning(self) -> bool:
        return ((bool(self._scan_worker and self._scan_worker.isRunning())) or
                (bool(self._nested_worker and self._nested_worker.isRunning())) or
                (bool(self._integrity_worker and
                      self._integrity_worker.isRunning())))

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


def _display_path(ui_path: str, guid_to_bundle: dict) -> str:
    """Return ui_path with any container UUID replaced by its resolved bundle ID."""
    parts = ui_path.split('/')
    for i, part in enumerate(parts):
        if _UUID_RE.match(part):
            parts[i] = guid_to_bundle.get(part, part)
            break
    return '/'.join(parts)


def _bundle_id_for_path(ui_path: str, guid_to_bundle: dict) -> str:
    """Return the bundle ID (or raw UUID if unresolved) for the container in this path."""
    for part in ui_path.split('/'):
        if _UUID_RE.match(part):
            return guid_to_bundle.get(part, part)
    return ''


class ArchiveSelectionDialog(QDialog):
    """Tree dialog for selecting which discovered archives to extract."""

    def __init__(self, archives: list[dict], guid_to_bundle: dict | None = None,
                 is_android: bool = False, done_archives: list[dict] | None = None,
                 failed_archives: list[dict] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Archives for Extraction")
        self.setModal(True)
        self.setMinimumSize(820, 520)
        self.selected_paths: list[str] = []
        self._archives = archives
        self._done_archives = done_archives or []
        self._failed_archives = failed_archives or []
        self._guid_to_bundle = guid_to_bundle or {}
        self._is_android = is_android
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        n = len(archives)
        n_done = len(self._done_archives)
        n_fail = len(self._failed_archives)
        parts = []
        if n_done:
            parts.append(f"{n_done:,} previously extracted")
        if n_fail:
            parts.append(f"{n_fail:,} previously failed")
        if n == 0:
            header = (", ".join(parts).capitalize() + ". "
                      if parts else "") + "No new archives to extract."
        else:
            header = (f"<b>{n:,} archive{'s' if n != 1 else ''} available for extraction.</b>"
                      + (f" ({', '.join(parts)}, shown below.)" if parts else ""))
        layout.addWidget(QLabel(header))

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Path", "Type"])
        self._tree.setColumnWidth(0, 620)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSortingEnabled(False)
        layout.addWidget(self._tree)

        self._count_label = QLabel()
        layout.addWidget(self._count_label)

        btn_row = QHBoxLayout()
        sel_all = QPushButton("Select All")
        sel_all.clicked.connect(self._select_all)
        btn_row.addWidget(sel_all)
        desel_all = QPushButton("Deselect All")
        desel_all.clicked.connect(self._deselect_all)
        btn_row.addWidget(desel_all)
        btn_row.addStretch()
        self._extract_btn = QPushButton("Extract Selected")
        self._extract_btn.setDefault(True)
        self._extract_btn.clicked.connect(self._accept)
        btn_row.addWidget(self._extract_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._build_tree()
        self._tree.itemChanged.connect(self._on_item_changed)
        self._refresh_count()

    # ── Shared tree helpers ───────────────────────────────────────────────────

    # Every archive dict carries an 'mtime' key (discovery and the DB-failure
    # fallback both set it), so the C-level itemgetter is safe here.
    _mtime_key = staticmethod(itemgetter('mtime'))

    @classmethod
    def _max_mtime(cls, arcs: list) -> int:
        return max(map(cls._mtime_key, arcs), default=0)

    @staticmethod
    def _mtime_label(ns: int) -> str:
        if not ns:
            return ''
        try:
            return ('  — ' + datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
                    .strftime('%Y-%m-%d'))
        except Exception:
            return ''

    def _make_file_item(self, parent, arc: dict) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent)
        item.setText(0, _display_path(arc['ui_path'], self._guid_to_bundle))
        item.setText(1, arc['file_type'])
        item.setCheckState(0, Qt.CheckState.Unchecked)
        item.setData(0, Qt.ItemDataRole.UserRole, arc['ui_path'])
        return item

    def _make_app_node(self, parent, label: str, arcs: list[dict]) -> QTreeWidgetItem:
        mt = self._max_mtime(arcs)
        node = QTreeWidgetItem(parent)
        node.setText(0, f"{label}  ({len(arcs):,}){self._mtime_label(mt)}")
        node.setCheckState(0, Qt.CheckState.Unchecked)
        node.setExpanded(False)
        for arc in sorted(arcs, key=self._mtime_key, reverse=True):
            self._make_file_item(node, arc)
        return node

    def _make_app_group(self, label: str, buckets: dict) -> None:
        if not buckets:
            return
        total = sum(len(v) for v in buckets.values())
        group = QTreeWidgetItem(self._tree)
        group.setText(0, f"{label}  ({total:,})")
        group.setCheckState(0, Qt.CheckState.Unchecked)
        group.setExpanded(True)
        for key in sorted(buckets, key=lambda k: self._max_mtime(buckets[k]), reverse=True):
            self._make_app_node(group, key, buckets[key])

    # ── Platform-specific tree builders ──────────────────────────────────────

    def _build_tree(self):
        if self._is_android:
            self._build_tree_android()
        else:
            self._build_tree_ios()
        if self._failed_archives:
            self._build_tree_failed()
        if self._done_archives:
            self._build_tree_done()

    def _build_tree_failed(self):
        """Append a collapsed, pre-checked 'Previously Failed' section (retryable)."""
        n = len(self._failed_archives)
        group = QTreeWidgetItem(self._tree)
        group.setText(0, f"Previously Failed — retry?  ({n:,})")
        group.setText(1, "")
        group.setCheckState(0, Qt.CheckState.Checked)
        group.setExpanded(False)
        red = group.foreground(0)
        red.setColor(Qt.GlobalColor.red)
        group.setForeground(0, red)
        group.setForeground(1, red)
        font = group.font(0)
        font.setBold(True)
        group.setFont(0, font)

        for arc in sorted(self._failed_archives, key=self._mtime_key, reverse=True):
            item = QTreeWidgetItem(group)
            item.setText(0, _display_path(arc['ui_path'], self._guid_to_bundle))
            item.setText(1, arc.get('file_type', ''))
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, arc['ui_path'])
            err = arc.get('error_msg', '')
            if err:
                item.setToolTip(0, err)
            item.setForeground(0, red)
            item.setForeground(1, red)

    def _build_tree_done(self):
        """Append a collapsed, non-checkable 'Previously Extracted' section."""
        n = len(self._done_archives)
        group = QTreeWidgetItem(self._tree)
        group.setText(0, f"Previously Extracted  ({n:,})")
        group.setText(1, "")
        # No check box — set flags without ItemIsUserCheckable
        group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        grey = group.foreground(0)
        grey.setColor(Qt.GlobalColor.gray)
        group.setForeground(0, grey)
        group.setForeground(1, grey)
        font = group.font(0)
        font.setItalic(True)
        group.setFont(0, font)
        group.setExpanded(False)

        for arc in sorted(self._done_archives, key=self._mtime_key, reverse=True):
            item = QTreeWidgetItem(group)
            item.setText(0, _display_path(arc['ui_path'], self._guid_to_bundle))
            item.setText(1, arc.get('file_type', ''))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
                                       & ~Qt.ItemFlag.ItemIsEnabled)
            item.setForeground(0, grey)
            item.setForeground(1, grey)

    def _build_tree_ios(self):
        g2b = self._guid_to_bundle
        sms_items:   list[dict] = []
        mail_items:  list[dict] = []
        files_items: list[dict] = []
        app_buckets: dict[str, list[dict]] = {}

        for arc in self._archives:
            cat = arc['category']
            if cat == 'SMS':
                sms_items.append(arc)
            elif cat == 'Mail':
                mail_items.append(arc)
            elif cat == 'Files':
                files_items.append(arc)
            else:
                bid = _bundle_id_for_path(arc['ui_path'], g2b) or 'Unknown'
                app_buckets.setdefault(bid, []).append(arc)

        def make_top_level(label: str, items: list[dict]) -> QTreeWidgetItem:
            node = QTreeWidgetItem(self._tree)
            node.setText(0, f"{label}  ({len(items):,})")
            node.setCheckState(0, Qt.CheckState.Unchecked)
            node.setExpanded(True)
            for arc in sorted(items, key=self._mtime_key, reverse=True):
                self._make_file_item(node, arc)
            return node

        if sms_items:
            make_top_level("SMS Attachments", sms_items)
        if mail_items:
            make_top_level("Mail", mail_items)
        if files_items:
            make_top_level("Files / iCloud", files_items)

        apple      = {b: a for b, a in app_buckets.items() if b.startswith('com.apple.')}
        third_party = {b: a for b, a in app_buckets.items() if not b.startswith('com.apple.')}
        self._make_app_group("Default Apps", apple)
        self._make_app_group("Third-Party Apps", third_party)

    def _build_tree_android(self):
        app_buckets:   dict[str, list[dict]] = {}
        media_buckets: dict[str, dict[str, list[dict]]] = {}  # volume → subfolder → archives

        for arc in self._archives:
            if arc['category'] == 'AndroidMedia':
                vol, sub = _android_media_parts(arc['ui_path'])
                media_buckets.setdefault(vol, {}).setdefault(sub, []).append(arc)
            else:
                pkg = _android_package(arc['ui_path']) or 'Unknown'
                app_buckets.setdefault(pkg, []).append(arc)

        # App Data — all packages sorted by most recently active
        self._make_app_group("App Data", app_buckets)

        # User Storage — one top-level node per volume (Internal first, then SD cards)
        for vol_label in sorted(media_buckets,
                                key=lambda v: (0 if v == 'Internal Storage' else 1, v)):
            sub_map = media_buckets[vol_label]
            vol_total = sum(len(v) for v in sub_map.values())
            vol_node = QTreeWidgetItem(self._tree)
            vol_node.setText(0, f"User Storage — {vol_label}  ({vol_total:,})")
            vol_node.setCheckState(0, Qt.CheckState.Unchecked)
            vol_node.setExpanded(True)
            for sub in sorted(sub_map):
                arcs = sub_map[sub]
                sub_node = QTreeWidgetItem(vol_node)
                sub_node.setText(0, f"{sub}  ({len(arcs):,})")
                sub_node.setCheckState(0, Qt.CheckState.Unchecked)
                sub_node.setExpanded(True)
                for arc in sorted(arcs, key=self._mtime_key, reverse=True):
                    self._make_file_item(sub_node, arc)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if column != 0 or self._updating:
            return
        self._updating = True
        state = item.checkState(0)
        # Propagate downward to children
        self._set_children(item, state)
        # Update parent group/location tri-state
        parent = item.parent()
        while parent is not None:
            self._sync_parent(parent)
            parent = parent.parent()
        self._updating = False
        self._refresh_count()

    def _set_children(self, item: QTreeWidgetItem, state: Qt.CheckState):
        for i in range(item.childCount()):
            child = item.child(i)
            child.setCheckState(0, state)
            self._set_children(child, state)

    def _sync_parent(self, item: QTreeWidgetItem):
        n = item.childCount()
        checked = sum(
            1 for i in range(n)
            if item.child(i).checkState(0) == Qt.CheckState.Checked
        )
        if checked == 0:
            item.setCheckState(0, Qt.CheckState.Unchecked)
        elif checked == n:
            item.setCheckState(0, Qt.CheckState.Checked)
        else:
            item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _collect_selected(self) -> list[str]:
        selected = []
        root = self._tree.invisibleRootItem()
        self._walk(root, selected)
        return selected

    def _walk(self, item: QTreeWidgetItem, out: list[str]):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path and item.checkState(0) == Qt.CheckState.Checked:
            out.append(path)
        for i in range(item.childCount()):
            self._walk(item.child(i), out)

    def _refresh_count(self):
        selected = self._collect_selected()
        n = len(selected)
        total = len(self._archives) + len(self._failed_archives)
        self._count_label.setText(f"{n:,} of {total:,} archives selected")
        self._extract_btn.setText(f"Extract Selected ({n:,})")
        self._extract_btn.setEnabled(n > 0)

    def _select_all(self):
        self._updating = True
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            group.setCheckState(0, Qt.CheckState.Checked)
            self._set_children(group, Qt.CheckState.Checked)
        self._updating = False
        self._refresh_count()

    def _deselect_all(self):
        self._updating = True
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            group = root.child(i)
            group.setCheckState(0, Qt.CheckState.Unchecked)
            self._set_children(group, Qt.CheckState.Unchecked)
        self._updating = False
        self._refresh_count()

    def _accept(self):
        self.selected_paths = self._collect_selected()
        self.accept()


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
        with closing(_open_results_db(case_dir)) as db:
            run_id = start_run_log(db, 'cd_integrity_check', total=1, notes=new_hash)
            complete_run_log(db, run_id, processed=1, output_rows=1 if passed else 0)
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


class FastZipBrowser(QMainWindow, HexViewerMixin, MediaViewerMixin, KeywordSearchMixin, ArtifactViewerMixin, SqliteViewerMixin, SegbViewerMixin):  # type: ignore[reportIncompatibleMethodOverride]
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FFS Explorer")
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
        self._device_info_worker: QThread | None = None
        self._retired_workers: list = []   # stopped workers awaiting thread exit
        self._adapter = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, "filesystem2", "filesystem1")
        self._android_user_data_path = ''   # set at load time for Android archives
        # ffs_archives: ordered list of {"path": ..., "case_dir": ..., "label": ..., "last_opened": ...}
        self._ffs_archives: list = self._load_ffs_archives()
        self.recent_paths: list = [e['path'] for e in self._ffs_archives if 'path' in e]
        self._migrate_device_labels()
        self._case_dir: str | None = None   # case folder for the currently loaded zip
        # {'DCIM/100APPLE/IMG_0001.HEIC': [values in _PHOTO_HEADERS order]}
        self._photo_index: dict[str, list] = {}
        self._photos_prompt_shown = False   # one Media-folder offer per archive
        self._photo_flag_rules = _load_photo_flag_rules()

        # ── Menu bar ──────────────────────────────────────────────────────
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")
        open_act = QAction("Open FFS...", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._open_new_ffs)
        file_menu.addAction(open_act)
        file_menu.addSeparator()
        process_act = QAction("Process Case…", self)
        process_act.triggered.connect(lambda: self._open_process_dialog())
        file_menu.addAction(process_act)
        self._artifact_act = QAction("Run Artifact Parsers…", self)
        self._artifact_act.triggered.connect(self._open_artifact_runner)
        self._artifact_act.setEnabled(False)
        file_menu.addAction(self._artifact_act)
        file_menu.addSeparator()
        self._recent_menu = file_menu.addMenu("Cases")
        self._recent_menu.aboutToShow.connect(self._populate_recent_menu)
        file_menu.addSeparator()
        prefs_act = QAction("Preferences…", self)
        prefs_act.triggered.connect(self._open_preferences)
        file_menu.addAction(prefs_act)
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
        self.folder_view_btn = QPushButton("Show Empty Folders")
        self.folder_view_btn.setCheckable(True)
        self.folder_view_btn.setChecked(False)
        self.folder_view_btn.toggled.connect(self._on_folder_view_toggled)
        self.action_btn.setFixedWidth(90)
        self.open_export_btn.setFixedWidth(150)
        self.folder_view_btn.setFixedWidth(150)
        archive_bar.addWidget(self.archive_dropdown, 1)
        archive_bar.addWidget(self.action_btn)
        archive_bar.addWidget(self.open_export_btn)
        archive_bar.addWidget(self.folder_view_btn)
        layout.addLayout(archive_bar)


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

        # Vertical splitter: tree_view top, bookmark panel bottom.
        self.left_splitter = QSplitter(Qt.Orientation.Vertical)
        self.left_splitter.addWidget(self.tree_view)

        self._bookmark_panel = QWidget()
        _bm_layout = QVBoxLayout(self._bookmark_panel)
        _bm_layout.setContentsMargins(0, 0, 0, 0)
        _bm_layout.setSpacing(0)
        _bm_hdr = QLabel("Bookmarks")
        _bm_hdr.setStyleSheet(
            "font-size: 11px; font-weight: bold; padding: 2px 4px;"
            " background: palette(mid); border-top: 1px solid palette(dark);")
        _bm_layout.addWidget(_bm_hdr)
        self._bookmark_list = QListWidget()
        self._bookmark_list.setFrameShape(QListWidget.Shape.NoFrame)
        self._bookmark_list.clicked.connect(self._on_bookmark_item_clicked)
        self._bookmark_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._bookmark_list.customContextMenuRequested.connect(
            self._show_bookmark_panel_menu)
        _bm_layout.addWidget(self._bookmark_list)
        self.left_splitter.addWidget(self._bookmark_panel)
        self.left_splitter.setStretchFactor(0, 1)   # tree takes all extra space
        self.left_splitter.setStretchFactor(1, 0)   # bookmark panel stays compact
        self.left_splitter.setCollapsible(1, True)

        left_layout.addWidget(self.left_splitter)
        self.splitter.addWidget(left_panel)
        self.splitter.setCollapsible(0, True)

        self.file_headers = ['Name', 'Changed', 'Modified', 'Type', 'Size (Bytes)', 'Files', 'Path']  # rebuilt per archive
        self.proxy_model = MultiColumnFilterProxy()
        self._set_file_model(FileTableModel(self.file_headers))

        self.file_view = QTableView()
        self.file_view.setModel(self.proxy_model)
        self.file_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.file_view.setSortingEnabled(True)
        # User-defined column order, persisted across views and sessions.
        _s = _QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        saved_order = _s.value('file_column_order', []) or []
        self._column_order: list[str] = [str(h) for h in saved_order]
        self._applying_column_order = False
        # Per-folder saved layouts {folder_ui_path: {'order': [...], 'hidden': [...]}}
        self._folder_column_configs: dict = {}
        _raw_cfg = _s.value('folder_column_configs', '', type=str)
        if _raw_cfg:
            try:
                self._folder_column_configs = json.loads(_raw_cfg)
            except Exception:
                self._folder_column_configs = {}
        _hdr = self.file_view.horizontalHeader()
        _hdr.setSectionsMovable(True)
        _hdr.sectionMoved.connect(self._on_section_moved)
        self.file_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_view.customContextMenuRequested.connect(self.show_table_context_menu)
        self.file_view.doubleClicked.connect(self.handle_table_double_click)
        self.file_view.clicked.connect(self.on_file_selected)

        filter_bar = QHBoxLayout()
        filter_bar.setContentsMargins(0, 2, 0, 2)
        filter_bar.addWidget(QLabel("Filter:"))
        filter_bar.addSpacing(6)

        # Text — substring match in the chosen string column (or all of them)
        self.filter_col_combo = QComboBox()
        self.filter_col_combo.setMaximumWidth(120)
        filter_bar.addWidget(self.filter_col_combo)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Type to filter rows...")
        self.filter_input.setMinimumWidth(150)
        self.filter_input.returnPressed.connect(self._apply_filter)
        filter_bar.addWidget(self.filter_input, 1)
        filter_bar.addSpacing(10)

        # Date range — applied to the timestamp column chosen in the combo
        self.filter_date_enable = QCheckBox("Date")
        self.filter_date_enable.toggled.connect(self._on_date_filter_toggled)
        filter_bar.addWidget(self.filter_date_enable)
        self.filter_date_combo = QComboBox()
        self.filter_date_combo.currentIndexChanged.connect(
            lambda _=None: self._apply_filter_if_date_enabled())
        filter_bar.addWidget(self.filter_date_combo)
        self.filter_date_from = QDateEdit(QDate(2000, 1, 1))
        self.filter_date_to   = QDateEdit(QDate.currentDate())
        for de in (self.filter_date_from, self.filter_date_to):
            de.setDisplayFormat("yyyy-MM-dd")
            de.setCalendarPopup(True)
            de.dateChanged.connect(
                lambda _=None: self._apply_filter_if_date_enabled())
        filter_bar.addWidget(self.filter_date_from)
        filter_bar.addWidget(QLabel("–"))
        filter_bar.addWidget(self.filter_date_to)
        self._set_date_filter_enabled(False)
        filter_bar.addSpacing(10)

        # Type — multi-select menu of the types present in the current view
        self._type_filter_selected: set | None = None   # None = all types
        self.filter_type_btn = QPushButton("Type: All")
        self._type_menu = QMenu(self.filter_type_btn)
        self._type_menu.aboutToShow.connect(self._populate_type_menu)
        self.filter_type_btn.setMenu(self._type_menu)
        filter_bar.addWidget(self.filter_type_btn)
        filter_bar.addSpacing(6)

        self._update_filter_columns(self.file_headers)
        self.filter_go_btn = QPushButton("Go")
        self.filter_go_btn.setMaximumWidth(40)
        self.filter_go_btn.clicked.connect(self._apply_filter)
        self.filter_clear_btn = QPushButton("Clear")
        self.filter_clear_btn.setMaximumWidth(70)
        self.filter_clear_btn.clicked.connect(self._clear_filter)
        filter_bar.addWidget(self.filter_go_btn)
        filter_bar.addWidget(self.filter_clear_btn)

        self.show_selected_btn = QPushButton("Show Selected Files")
        self.show_selected_btn.setVisible(False)
        self.show_selected_btn.clicked.connect(self._rebuild_file_view_from_checked)

        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.setVisible(False)
        self.deselect_all_btn.clicked.connect(self._deselect_all_files)

        # Hidden-column names for the selected-files (aggregate) view —
        # remembered across rebuilds; plain folder views always reset.
        self._agg_hidden_columns: set[str] = set()
        self.columns_btn = QPushButton("Columns…")
        self.columns_btn.setToolTip(
            "Show or hide columns in the current view.\n"
            "Resets when you change folder; the Show Selected Files view "
            "remembers its setting.")
        self.columns_btn.clicked.connect(self._show_columns_dialog)

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
        sel_bar.addWidget(self.columns_btn)
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
        # Add the generic SQLite browser as a third preview tab (Hex / Text / Database)
        self._sql_tab_index = self.preview_tabs.addTab(
            self._setup_sqlite_tab(), "Database")
        self._segb_tab_index = self.preview_tabs.addTab(
            self._setup_segb_tab(), "SEGB")

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
        self._apply_column_visibility()
        self._apply_column_order()

    def _apply_column_visibility(self):
        """Apply per-view column hiding.  A single folder with a saved layout
        uses its hidden set; the selected-files (aggregate) view re-applies the
        session hidden set; a plain folder view shows every column."""
        if not hasattr(self, 'file_view'):
            return   # first _set_file_model call during __init__
        headers = self.file_model._headers
        cfg = self._current_folder_config()
        if cfg is not None:
            hidden = set(cfg.get('hidden', []))
        elif getattr(self, '_view_is_recursive', False):
            hidden = getattr(self, '_agg_hidden_columns', set())
        else:
            hidden = set()
        for i, h in enumerate(headers):
            self.file_view.setColumnHidden(i, h in hidden)
        self._update_columns_indicator()

    # ── Column ordering & per-folder layouts (persisted) ──────────────────────

    def _current_folder_config(self):
        """Return the saved layout dict for the current single-folder view, or
        None (aggregate views and unsaved folders have no per-folder layout)."""
        if (not getattr(self, '_view_is_recursive', False)
                and getattr(self, '_view_path', '')):
            return getattr(self, '_folder_column_configs', {}).get(self._view_path)
        return None

    def _save_column_order(self):
        s = _QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        s.setValue('file_column_order', self._column_order)

    def _save_folder_configs(self):
        s = _QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        s.setValue('folder_column_configs', json.dumps(self._folder_column_configs))

    def _record_column_order(self, visual_order: list):
        """Merge the current view's visual order into the persisted global
        order, keeping known-but-absent columns in their old relative spot."""
        seen = set(visual_order)
        self._column_order = list(visual_order) + [
            h for h in self._column_order if h not in seen]
        self._save_column_order()

    def _commit_order(self, visual_order: list):
        """Persist a new visual order — into the folder's saved layout if it
        has one, otherwise into the global default order."""
        cfg = self._current_folder_config()
        if cfg is not None:
            cfg['order'] = list(visual_order)
            self._save_folder_configs()
        else:
            self._record_column_order(visual_order)

    def _apply_column_order(self):
        """Rearrange header sections to match the saved order (the folder's
        layout if present, otherwise the global default)."""
        if not hasattr(self, 'file_view'):
            return
        cfg = self._current_folder_config()
        order_src = (cfg.get('order') if cfg and cfg.get('order')
                     else self._column_order)
        header  = self.file_view.horizontalHeader()
        headers = self.file_model._headers
        # An empty order_src falls through to identity order, which resets any
        # custom arrangement left over from a previously-viewed folder layout.
        order = [h for h in (order_src or []) if h in headers]
        order += [h for h in headers if h not in order]
        self._applying_column_order = True
        try:
            for visual_pos, name in enumerate(order):
                logical = headers.index(name)
                cur = header.visualIndex(logical)
                if cur != visual_pos:
                    header.moveSection(cur, visual_pos)
        finally:
            self._applying_column_order = False

    def _on_section_moved(self, *_):
        """User dragged a column header — record the new order."""
        if self._applying_column_order:
            return
        header  = self.file_view.horizontalHeader()
        headers = self.file_model._headers
        self._commit_order(
            [headers[header.logicalIndex(v)] for v in range(len(headers))])

    def _update_columns_indicator(self):
        """Reflect hidden-column count and any saved-folder layout on the
        Columns button so the state is visible at a glance."""
        if not hasattr(self, 'columns_btn') or not hasattr(self, 'file_view'):
            return
        headers = self.file_model._headers
        hidden_names = [headers[i] for i in range(len(headers))
                        if self.file_view.isColumnHidden(i)]
        saved = self._current_folder_config() is not None
        if hidden_names:
            self.columns_btn.setText(f"Columns ⊘{len(hidden_names)}")
            tip = "Hidden: " + ", ".join(hidden_names)
        else:
            self.columns_btn.setText("Columns…")
            tip = "Show, hide or reorder columns in the current view."
        if saved:
            tip += "\nLayout saved for this folder."
        self.columns_btn.setToolTip(tip)

    def _show_columns_dialog(self):
        headers = self.file_model._headers
        header_view = self.file_view.horizontalHeader()
        # A real single folder is selected (not the aggregate or no-selection state)
        single_folder = not self._view_is_recursive and bool(self._view_path)
        dlg = QDialog(self)
        dlg.setWindowTitle("Columns")
        dlg.setMinimumSize(300, 400)
        lay = QVBoxLayout(dlg)
        note = QLabel(
            "Tick to show, untick to hide. Drag rows — or use the arrows — "
            "to reorder. " +
            ("Use “Save for this folder” to keep this layout for this folder."
             if single_folder else
             "Order is saved permanently; hiding lasts for this view."))
        note.setWordWrap(True)
        lay.addWidget(note)
        lst = QListWidget()
        lst.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        # List in the on-screen (visual) order
        for v in range(len(headers)):
            logical = header_view.logicalIndex(v)
            item = QListWidgetItem(headers[logical])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Unchecked
                if self.file_view.isColumnHidden(logical)
                else Qt.CheckState.Checked)
            lst.addItem(item)

        def _on_item_changed(item):
            name = item.text()
            if name not in headers:
                return
            logical = headers.index(name)
            hide = item.checkState() != Qt.CheckState.Checked
            self.file_view.setColumnHidden(logical, hide)
            cfg = self._current_folder_config()
            if cfg is not None:
                hidden = set(cfg.get('hidden', []))
                hidden.add(name) if hide else hidden.discard(name)
                cfg['hidden'] = sorted(hidden)
                self._save_folder_configs()
            elif self._view_is_recursive:
                self._agg_hidden_columns.add(name) if hide \
                    else self._agg_hidden_columns.discard(name)
            self._update_columns_indicator()

        def _on_reordered(*_):
            self._commit_order([lst.item(i).text() for i in range(lst.count())])
            self._apply_column_order()

        def _move_current(delta: int):
            row = lst.currentRow()
            if row < 0 or not (0 <= row + delta < lst.count()):
                return
            item = lst.takeItem(row)
            lst.insertItem(row + delta, item)
            lst.setCurrentRow(row + delta)
            _on_reordered()

        lst.itemChanged.connect(_on_item_changed)
        lst.model().rowsMoved.connect(_on_reordered)
        lay.addWidget(lst)

        # Per-folder save / remove (single-folder views only)
        if single_folder:
            has_cfg = self._current_folder_config() is not None

            def _save_for_folder():
                order  = [lst.item(i).text() for i in range(lst.count())]
                hidden = [lst.item(i).text() for i in range(lst.count())
                          if lst.item(i).checkState() != Qt.CheckState.Checked]
                self._folder_column_configs[self._view_path] = {
                    'order': order, 'hidden': hidden}
                self._save_folder_configs()
                self.status_bar.showMessage(
                    "Column layout saved for this folder", 4000)
                self._update_columns_indicator()
                dlg.accept()

            def _remove_for_folder():
                self._folder_column_configs.pop(self._view_path, None)
                self._save_folder_configs()
                dlg.accept()
                self._apply_column_order()
                self._apply_column_visibility()

            save_row = QHBoxLayout()
            if has_cfg:
                remove_btn = QPushButton("Remove saved layout")
                remove_btn.clicked.connect(_remove_for_folder)
                save_row.addWidget(remove_btn)
            save_btn = QPushButton("Save for this folder")
            save_btn.clicked.connect(_save_for_folder)
            save_row.addWidget(save_btn)
            save_row.addStretch()
            lay.addLayout(save_row)

        btn_row = QHBoxLayout()
        up_btn = QPushButton("↑")
        up_btn.setFixedWidth(36)
        up_btn.clicked.connect(partial(_move_current, -1))
        btn_row.addWidget(up_btn)
        down_btn = QPushButton("↓")
        down_btn.setFixedWidth(36)
        down_btn.clicked.connect(partial(_move_current, 1))
        btn_row.addWidget(down_btn)
        show_all = QPushButton("Show All")

        def _show_all():
            for i in range(lst.count()):
                lst.item(i).setCheckState(Qt.CheckState.Checked)

        show_all.clicked.connect(_show_all)
        btn_row.addWidget(show_all)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        dlg.exec()

    def _on_filter_progress(self, visible, total):
        self.table_status_label.setText(f"{visible:,} of {total:,} items (filtering…)")

    def _on_filter_done(self, visible, total):
        if visible == total:
            self.table_status_label.setText(f"{total:,} items")
        else:
            self.table_status_label.setText(f"{visible:,} of {total:,} items (filtered)")
        args = self._current_filter_args()
        if self._filter_is_active(args):
            parts = []
            if args.get('text'):
                parts.append(f"text \"{args['text']}\"")
            if 'date_col' in args:
                parts.append(f"{self.filter_date_combo.currentText()} "
                             f"{args['date_from']}–{args['date_to']}")
            if 'types' in args:
                parts.append(f"types {sorted(args['types'])}")
            self._log(f"Filter applied: {', '.join(parts)} — "
                      f"{visible:,} of {total:,} rows visible")

    def _apply_filter(self):
        total = self.file_model.rowCount()
        self.table_status_label.setText(f"0 of {total:,} items (filtering…)")
        self.proxy_model.set_filter(**self._current_filter_args())

    def _clear_filter(self):
        self.filter_input.clear()
        self.filter_date_enable.setChecked(False)
        self._type_filter_selected = None
        self.filter_type_btn.setText("Type: All")
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

    _TIME_HEADERS = ('Created', 'Changed', 'Modified')

    def _update_filter_columns(self, headers):
        """Reconfigure the text/date column combos and type filter for new headers."""
        string_headers = [h for h in headers
                          if h not in self._TIME_HEADERS and h != 'Type']
        prev_text_col = self.filter_col_combo.currentText()
        self.filter_col_combo.blockSignals(True)
        self.filter_col_combo.clear()
        self.filter_col_combo.addItem("All")
        for h in string_headers:
            self.filter_col_combo.addItem(h)
        if prev_text_col in string_headers or prev_text_col == "All":
            self.filter_col_combo.setCurrentText(prev_text_col)
        elif 'Name' in string_headers:
            self.filter_col_combo.setCurrentText('Name')
        self.filter_col_combo.blockSignals(False)

        prev = self.filter_date_combo.currentText()
        self.filter_date_combo.blockSignals(True)
        self.filter_date_combo.clear()
        date_headers = [h for h in headers if h in self._TIME_HEADERS]
        for h in date_headers:
            self.filter_date_combo.addItem(h)
        if prev in date_headers:
            self.filter_date_combo.setCurrentText(prev)
        elif 'Modified' in date_headers:
            self.filter_date_combo.setCurrentText('Modified')
        self.filter_date_combo.blockSignals(False)
        has_dates = bool(date_headers)
        self.filter_date_enable.setVisible(has_dates)
        self.filter_date_combo.setVisible(has_dates)
        self.filter_date_from.setVisible(has_dates)
        self.filter_date_to.setVisible(has_dates)
        if not has_dates:
            self.filter_date_enable.setChecked(False)
        self.filter_type_btn.setVisible('Type' in headers)
        # Headers changed → previously selected types may not exist any more
        self._type_filter_selected = None
        self.filter_type_btn.setText("Type: All")

    def _set_date_filter_enabled(self, on: bool):
        self.filter_date_combo.setEnabled(on)
        self.filter_date_from.setEnabled(on)
        self.filter_date_to.setEnabled(on)

    def _on_date_filter_toggled(self, on: bool):
        self._set_date_filter_enabled(on)
        if on:
            self._autofill_date_range()
        self._apply_filter()

    def _apply_filter_if_date_enabled(self):
        if self.filter_date_enable.isChecked():
            self._apply_filter()

    def _autofill_date_range(self):
        """Initialise from/to with the oldest and newest dates of the rows
        currently shown in the file window."""
        if not self.file_model:
            return
        headers = self.file_model._headers
        header = self.filter_date_combo.currentText()
        if header not in headers:
            return
        col  = headers.index(header)
        rows = self.file_model._rows or self.file_model._all_rows
        dates = [r[0][col][:10] for r in rows
                 if col < len(r[0]) and r[0][col]]
        if not dates:
            return
        self._set_date_range(min(dates), max(dates))

    def _set_date_range(self, lo: str, hi: str):
        """Set both date editors ('YYYY-MM-DD' strings) without re-filtering."""
        for de, val in ((self.filter_date_from, lo), (self.filter_date_to, hi)):
            d = QDate.fromString(val, 'yyyy-MM-dd')
            if d.isValid():
                de.blockSignals(True)
                de.setDate(d)
                de.blockSignals(False)

    def _filter_by_date(self, header: str, date_str: str, week: bool):
        """Activate the date filter for *header*, same day or a week window
        centred on *date_str* (right-click 'Filter by Date' actions)."""
        d = QDate.fromString(date_str[:10], 'yyyy-MM-dd')
        if not d.isValid():
            return
        self.filter_date_combo.blockSignals(True)
        self.filter_date_combo.setCurrentText(header)
        self.filter_date_combo.blockSignals(False)
        if week:
            lo, hi = d.addDays(-3), d.addDays(3)
        else:
            lo, hi = d, d
        self._set_date_range(lo.toString('yyyy-MM-dd'), hi.toString('yyyy-MM-dd'))
        self.filter_date_enable.blockSignals(True)
        self.filter_date_enable.setChecked(True)
        self.filter_date_enable.blockSignals(False)
        self._set_date_filter_enabled(True)
        self._apply_filter()

    _FOLDER_TYPE_NAMES = {'Folder', 'Empty Folder', 'Folder - Metadata Only'}

    def _populate_type_menu(self):
        self._type_menu.clear()
        all_act = self._type_menu.addAction("All Types")
        all_act.triggered.connect(partial(self._set_type_filter, None))
        self._type_menu.addSeparator()
        values = self.file_model.distinct_values('Type') if self.file_model else []
        folder_vals = [v for v in values if v in self._FOLDER_TYPE_NAMES]
        file_vals   = [v for v in values if v not in self._FOLDER_TYPE_NAMES]
        selected = self._type_filter_selected

        def _checked(v):
            return selected is None or v in selected

        files_act = self._type_menu.addAction("Files")
        files_act.setCheckable(True)
        files_act.setChecked(all(map(_checked, file_vals)) if file_vals else True)
        files_act.toggled.connect(partial(self._toggle_type_group, file_vals))
        folders_act = self._type_menu.addAction("Folders")
        folders_act.setCheckable(True)
        folders_act.setChecked(all(map(_checked, folder_vals)) if folder_vals else True)
        folders_act.toggled.connect(partial(self._toggle_type_group, folder_vals))
        self._type_menu.addSeparator()
        for v in file_vals:
            act = self._type_menu.addAction(v)
            act.setCheckable(True)
            act.setChecked(_checked(v))
            act.toggled.connect(partial(self._on_type_menu_toggled, v))

    def _all_type_values(self) -> set:
        return set(self.file_model.distinct_values('Type')
                   if self.file_model else [])

    def _toggle_type_group(self, values: list, checked: bool):
        all_values = self._all_type_values()
        cur = (set(all_values) if self._type_filter_selected is None
               else set(self._type_filter_selected))
        cur = cur | set(values) if checked else cur - set(values)
        self._set_type_filter(None if cur >= all_values else cur)

    def _on_type_menu_toggled(self, value: str, checked: bool):
        self._toggle_type_group([value], checked)

    def _set_type_filter(self, selected: set | None):
        self._type_filter_selected = selected
        self.filter_type_btn.setText(
            "Type: All" if selected is None else "Type: Filtered")
        self._apply_filter()

    def _current_filter_args(self) -> dict:
        """Collect the filter bar's state into kwargs for FileTableModel.set_filter."""
        headers = self.file_model._headers if self.file_model else self.file_headers
        args: dict = {'text': self.filter_input.text()}
        sel = self.filter_col_combo.currentText()
        if sel != "All" and sel in headers:
            args['text_cols'] = [headers.index(sel)]
        else:
            args['text_cols'] = [i for i, h in enumerate(headers)
                                 if h not in self._TIME_HEADERS and h != 'Type']
        if (self.filter_date_enable.isChecked()
                and self.filter_date_combo.currentText() in headers):
            args['date_col']  = headers.index(self.filter_date_combo.currentText())
            args['date_from'] = self.filter_date_from.date().toString('yyyy-MM-dd')
            args['date_to']   = self.filter_date_to.date().toString('yyyy-MM-dd')
        if self._type_filter_selected is not None and 'Type' in headers:
            args['type_col'] = headers.index('Type')
            args['types']    = set(self._type_filter_selected)
        return args

    @staticmethod
    def _filter_is_active(args: dict) -> bool:
        return bool(args.get('text')) or 'date_col' in args or 'types' in args

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

        prefs = _load_prefs()
        base_folder = prefs.get('case_data_root', '').strip()
        if not base_folder or not os.path.isdir(base_folder):
            for e in self._ffs_archives:
                cd = e.get('case_dir', '')
                if cd and os.path.isdir(cd):
                    base_folder = os.path.dirname(cd)
                    break
        dlg = CaseSettingsDialog(zip_path, base_folder, parent=self)
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
        self._maybe_load_structured_preview(ui_path, data)

    def _maybe_load_structured_preview(self, ui_path: str, data: bytes,
                                       segb_sidecar_reader=None) -> None:
        """Route a selected file to the Database or SEGB preview based on its
        header (not its extension); clear both otherwise so nothing stale is
        left on screen."""
        if data[:16] == _SQLITE_MAGIC:
            self._clear_segb_preview()
            self._load_sqlite_preview(
                ui_path, raw=data,
                sidecar_reader=lambda suffix: self._read_zip_bytes(ui_path + suffix))
            self.preview_tabs.setCurrentIndex(self._sql_tab_index)
        elif is_segb(data):
            self._clear_sqlite_preview()
            self._load_segb_preview(ui_path, data)
            self.preview_tabs.setCurrentIndex(self._segb_tab_index)
        else:
            self._clear_sqlite_preview()
            self._clear_segb_preview()

    def _load_nested_entry_preview(self, ui_path: str) -> None:
        """Read an entry from an extracted nested archive and display it."""
        meta      = self.full_metadata.get(ui_path, {})
        arch_path = meta.get('_archive_path')
        arch      = self._nested_archive_map.get(arch_path) if arch_path else None
        if not arch:
            return
        entry_path = ui_path[len(arch_path) + 1:]
        data = read_nested_entry(arch['stored_path'], entry_path)
        if data is None:
            return

        name = meta.get('_display_name') or ui_path.rsplit('/', 1)[-1]
        text = self._render_as_text(data, name)
        self._load_hex_preview_from_bytes(data, ui_path)
        if text is not None:
            self._load_text_preview(text, ui_path)
        else:
            self._clear_text_preview()
        if data[:16] == _SQLITE_MAGIC:
            stored = arch['stored_path']
            self._clear_segb_preview()
            self._load_sqlite_preview(
                ui_path, raw=data,
                sidecar_reader=lambda suffix: read_nested_entry(
                    stored, entry_path + suffix))
            self.preview_tabs.setCurrentIndex(self._sql_tab_index)
        elif is_segb(data):
            self._clear_sqlite_preview()
            self._load_segb_preview(ui_path, data)
            self.preview_tabs.setCurrentIndex(self._segb_tab_index)
        else:
            self._clear_sqlite_preview()
            self._clear_segb_preview()

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

        self._view_path = folder_path
        self._view_is_recursive = False
        self._refresh_folder_view()

        # Offer to process Photos.sqlite the first time a Media folder is opened
        QTimer.singleShot(
            0, partial(self._maybe_offer_photos_processing, folder_path))

        # Refresh media tab if it's currently visible
        if self.center_tabs.currentIndex() == 1:
            self._load_media_from_file_model()

    def _build_entry_row(self, path: str, has_bundles: bool = False,
                         display_parent: str | None = None,
                         photo_cols: bool = False):
        """Return one file-model row tuple for *path*, or None if hidden.

        Single source of truth for building file-browser rows — used by the
        folder view, the refresh path, and the checked-folders aggregate view.

        display_parent — the parent folder's display path, precomputed once
        per folder by callers iterating folder children.  Avoids re-resolving
        GUID segments of the same folder prefix for every row.
        """
        seg = path.rsplit('/', 1)[-1]
        is_folder = path in self.folder_map
        meta = self.full_metadata.get(path, {})
        file_type, grey_row, skip = self._classify_entry(
            path, seg, is_folder, size_hint=meta.get('size', 0))
        if skip:
            return None
        name = seg
        if path in self._nested_virtual_paths:
            name = meta.get('_display_name') or seg
        fc = self._count_files_recursive(path) if is_folder else -1
        cols = self._build_entry_cols(path, name, meta, is_folder, file_type, fc,
                                      display_parent=display_parent, seg=seg)
        if has_bundles:
            cols.append(name if name in self.guid_to_bundle else "")
        if photo_cols:
            info = self._photo_index.get(self._photo_key(path) or '')
            cols.extend(info if info else [''] * len(_PHOTO_HEADERS))
        return (cols, path, fc, is_folder and not grey_row, grey_row,
                path in self._nested_archive_map)

    def _display_name(self, segment: str) -> str:
        """Return the bundle ID for a GUID segment, otherwise the segment itself."""
        return self.guid_to_bundle.get(segment, segment)

    def _display_path(self, path: str) -> str:
        """Replace GUID segments in a full path with bundle IDs for display."""
        # GUID segments always contain '-'; most paths have none to replace.
        if not self.guid_to_bundle or '-' not in path:
            return path
        return '/'.join(self._display_name(seg) for seg in path.split('/'))

    def _strip_archive_prefix(self, path: str) -> str:
        """Strip the archive-format prefix so display paths start from the
        user-partition root (e.g. 'mobile/Containers/...' not
        'filesystem2/mobile/Containers/...')."""
        return self._adapter.strip_display_prefix(path)

    def _classify_entry(self, path: str, name: str, is_folder: bool,
                        size_hint: int | None = None):
        """Return (file_type, grey_row, skip) for one file/folder entry.
        skip=True means the caller should exclude this entry from the view.

        size_hint — the entry's metadata size when the caller already has it.
        Bare directory entries always have size 0, so a non-zero size skips
        the (resolve-heavy) directory-entry probe entirely."""
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
            if not size_hint and self._is_empty_folder_entry(path):
                if self._hide_empty_folders:
                    return None, None, True
                return "Empty Folder", True, False
            ft = _get_file_type(name)
            if ft == 'Other' and path in self._header_type_overrides:
                ft = self._header_type_overrides[path]
            return ft, False, False
        if self._is_empty_folder_entry(path):
            if self._hide_empty_folders:
                return None, None, True
            return "Empty Folder", True, False
        return "Not in Zip", True, False

    def _build_entry_cols(self, path: str, name: str, meta: dict,
                          is_folder: bool, file_type: str | None, fc: int,
                          display_parent: str | None = None,
                          seg: str | None = None) -> list:
        cols: list = [self._display_name(name)]
        for _, key in self._time_cols:
            cols.append(self.format_ts(meta.get(key)))
        if is_folder and path in self._nested_archive_map:
            size_val = meta.get('size', 0)   # show original archive file size
        elif is_folder:
            size_val = self._folder_total_size(path)
        else:
            size_val = meta.get('size', 0)
        if display_parent is None:
            disp_path = self._display_path(path)
        else:
            tail = self._display_name(seg if seg is not None else name)
            disp_path = f"{display_parent}/{tail}" if display_parent else tail
        cols += [
            file_type,
            f"{size_val:,}",
            f"{fc:,}" if is_folder else "",
            disp_path,
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
                with closing(_open_cache_db(case_dir)) as db:
                    save_folder_counts(db, counts)
            except Exception:
                pass
        _BG_POOL.submit(_save)

    def format_ts(self, ts):
        if not ts: return "---"
        return _format_ts_cached(ts)

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
            if self._branch_has_checked(folder_path):
                rec_act = QAction("☐ Recursively Deselect", self)
                rec_act.triggered.connect(partial(self._recursive_untick_folder, index))
            else:
                rec_act = QAction("☑ Recursively Add All Files", self)
                rec_act.triggered.connect(partial(self._recursive_tick_folder, index))
            menu.addAction(rec_act)
            menu.addSeparator()
            # Bookmark all files under this folder — collected lazily: walking
            # a big branch is O(archive), so defer it until a bookmark action
            # is actually clicked instead of paying it on every right-click.
            self._bookmark_submenu(
                menu, partial(self._collect_bookmark_paths, folder_path))
            menu.addSeparator()
        export_act = QAction("📁 Export Folder (Recursive)", self)
        export_act.triggered.connect(partial(self.handle_export_request, is_tree=True))
        menu.addAction(export_act)
        menu.exec(self.tree_view.viewport().mapToGlobal(point))

    def _descendant_folders(self, root: str) -> list[str]:
        """Return *root* and every descendant folder path, from folder_map.

        Pure dict traversal — never touches the tree widgets, so it stays
        fast (<0.5 s) even for the archive root on 500k-entry archives.
        """
        fm = self.folder_map
        if root not in fm:
            return []
        seen = {root}
        out = [root]
        stack = [root]
        while stack:
            for child in fm.get(stack.pop(), ()):
                if child in fm and child not in seen:
                    seen.add(child)
                    out.append(child)
                    stack.append(child)
        return out

    def _set_branch_checked(self, index, checked: bool):
        """Tick/untick a folder and all its descendants.

        _checked_folders (the source of truth) is updated from folder_map
        directly; only already-loaded tree items get their visual state set.
        Lazily-loaded children pick their state up from the set when populated
        — the tree is never force-materialised (which used to create ~100k
        QStandardItems and freeze the UI on large archives).
        """
        item = self.tree_model.itemFromIndex(index)
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if path is None:
            return
        descendants = self._descendant_folders(path)
        if checked:
            self._checked_folders.update(descendants)
            state = Qt.CheckState.Checked
        else:
            self._checked_folders.difference_update(descendants)
            state = Qt.CheckState.Unchecked
        self.tree_model.blockSignals(True)
        if item.isCheckable():
            item.setCheckState(state)
        self._cascade_check(item, state)   # loaded descendants only
        self.tree_model.blockSignals(False)
        self.tree_view.viewport().update()
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QTimer.singleShot(0, self._deferred_rebuild)

    def _recursive_tick_folder(self, index):
        self._set_branch_checked(index, True)

    def _recursive_untick_folder(self, index):
        self._set_branch_checked(index, False)

    # ── Checkbox-driven file-view helpers ──────────────────────────────── #

    def _branch_has_checked(self, path: str) -> bool:
        """True when *path* or any folder beneath it is ticked."""
        if not self._checked_folders:
            return False
        if not path:
            return True   # root — any ticked folder is beneath it
        prefix = path + '/'
        return (path in self._checked_folders
                or any(p.startswith(prefix) for p in self._checked_folders))

    def _collect_bookmark_paths(self, folder_path: str) -> list:
        """Return [(ui_path, display_name)] for every file under folder_path."""
        return [(p, p.rsplit('/', 1)[-1])
                for p in self._collect_files_recursive(folder_path)]

    def _cascade_check(self, parent_item, state):
        """Propagate check state to all descendants of parent_item."""
        for row in range(parent_item.rowCount()):
            child = parent_item.child(row)
            if child is None:
                continue
            if child.isCheckable():
                child.setCheckState(state)
            self._cascade_check(child, state)

    def _rebuild_file_view_from_checked(self, preserve_filter: bool = False, select_path: str | None = None):
        checked = set(self._checked_folders)   # copy — set may change while batches run
        self._view_path = ""
        self._view_is_recursive = bool(checked)
        if checked:
            self.tree_view.clearSelection()
            self.tree_view.setCurrentIndex(QModelIndex())

        # Capture filter state before swapping models (new model has no filter).
        if preserve_filter:
            _filter_args = self._current_filter_args()
        else:
            _filter_args = None

        # Bump generation — any in-flight batch will see the change and abort
        self._load_gen += 1
        my_gen = self._load_gen

        # Swap in an empty model immediately so the checkbox tick feels instant
        # Photo columns are included when any checked folder sits under the
        # Media partition (DCIM, PhotoData, …) and photo metadata is loaded.
        has_photos = bool(self._photo_index) and any(
            self._photo_key(f) for f in checked)
        headers = self.file_headers + (_PHOTO_HEADERS if has_photos else [])
        self._set_file_model(FileTableModel(headers))
        # _update_filter_columns resets the type selection — restore it when
        # the filter should survive the rebuild.
        _type_sel   = self._type_filter_selected
        _type_label = self.filter_type_btn.text()
        self._update_filter_columns(headers)
        if preserve_filter:
            self._type_filter_selected = _type_sel
            self.filter_type_btn.setText(_type_label)

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
                disp_parent = self._display_path(folder)
                for child in self.folder_map.get(folder, []):
                    row = self._build_entry_row(child, display_parent=disp_parent,
                                                photo_cols=has_photos)
                    if row is None:
                        continue
                    batch_rows.append(row)
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
                if _filter_args is not None and self._filter_is_active(_filter_args):
                    if select_path:
                        _model = self.file_model
                        def _on_filter_done_select(*_):
                            _model.filter_done.disconnect(_on_filter_done_select)
                            self._select_file_by_path(select_path)
                        _model.filter_done.connect(_on_filter_done_select)
                    self.proxy_model.set_filter(**_filter_args)
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
        export_act.triggered.connect(partial(self.handle_export_request, is_tree=False))
        menu.addAction(export_act)

        source = self.proxy_model.mapToSource(index)
        ui_path = self.file_model.index(source.row(), 0).data(Qt.ItemDataRole.UserRole)

        if ui_path and ui_path not in self.folder_map and self._in_zip(ui_path):
            hex_act = QAction("🔍 Preview in Hex Viewer", self)
            hex_act.triggered.connect(partial(self._load_hex_preview, ui_path))
            menu.addAction(hex_act)

        if ui_path and '/' in ui_path:
            parent_path = ui_path.rsplit('/', 1)[0]
            parent_act = QAction("📂 Open Parent Folder in Tree", self)
            parent_act.triggered.connect(partial(self.navigate_tree_to_path, parent_path))
            menu.addAction(parent_act)

        # Filter by this row's dates — same day, or a week centred on it
        row_cols = self.file_model._rows[source.row()][0]
        headers  = self.file_model._headers
        date_opts = [(h, row_cols[headers.index(h)])
                     for h in self._TIME_HEADERS
                     if h in headers and headers.index(h) < len(row_cols)
                     and row_cols[headers.index(h)]]
        if date_opts:
            date_menu = menu.addMenu("📅 Filter by This Date")
            for header, val in date_opts:
                act = date_menu.addAction(f"{header} — same day ({val[:10]})")
                act.triggered.connect(
                    partial(self._filter_by_date, header, val, False))
            date_menu.addSeparator()
            for header, val in date_opts:
                act = date_menu.addAction(f"{header} — same week (±3 days)")
                act.triggered.connect(
                    partial(self._filter_by_date, header, val, True))

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
                partial(self._scan_selected_headers, _scan_paths, primary_path=ui_path))
            menu.addAction(scan_hdr_act)

        # Extract nested archive option — shown for unextracted Archive/Compressed files
        if ui_path and ui_path not in self.folder_map and self._in_zip(ui_path) \
                and ui_path not in self._nested_archive_map and self._case_dir:
            _name = ui_path.rsplit('/', 1)[-1]
            _ft   = _get_file_type(_name)
            if _ft == 'Other':
                _ft = self._header_type_overrides.get(ui_path, 'Other')
            if _is_extractable(_name, _ft):
                menu.addSeparator()
                extract_act = QAction("📦 Extract as Nested Archive", self)
                extract_act.triggered.connect(
                    partial(self._extract_from_context_menu, ui_path))
                menu.addAction(extract_act)

        # Bookmarks submenu
        bm_paths = self._get_paths_for_bookmark()
        if bm_paths:
            menu.addSeparator()
            self._bookmark_submenu(menu, bm_paths)

        menu.exec(self.file_view.viewport().mapToGlobal(point))

    def handle_export_request(self, is_tree=True):
        if not self.zip_path: return
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

    def _on_folder_view_toggled(self, checked: bool):
        self._hide_empty_folders = not checked
        self.folder_view_btn.setText(
            "Hide Empty Folders" if checked else "Show Empty Folders"
        )
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
        for v in islice((v for v in ui_metadata.values() if v), 500):
            has_ctime = has_ctime or bool(v.get('ctime'))
            has_btime = has_btime or bool(v.get('btime'))
            if has_ctime and has_btime:
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

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_prefs_checked', False):
            self._prefs_checked = True
            prefs = _load_prefs()
            if not prefs.get('case_data_root', '').strip():
                QTimer.singleShot(0, self._open_preferences)

    def _open_preferences(self):
        example = self._ffs_archives[0] if self._ffs_archives else None
        dlg = PreferencesDialog(parent=self, example_archive=example)
        if dlg.exec():
            self._ffs_archives = self._load_ffs_archives()
            self.update_dropdown_ui()
            self._populate_recent_menu()

    def _unextracted_archive_count(self) -> int:
        """Number of discoverable archives not yet successfully extracted.

        Used by keyword search to skip the coverage reminder when there is
        nothing left to extract."""
        overrides = getattr(self, '_header_type_overrides', {}) or {}
        archives = _discover_all_archives(self.full_metadata, self._adapter,
                                          overrides)
        if not archives:
            return 0
        records: dict = {}
        if self._case_dir:
            try:
                with closing(_open_cache_db(self._case_dir)) as db:
                    records = {r['ui_path']: r
                               for r in load_nested_archives(db)}
            except Exception:
                records = {}
        extracted = {p for p, r in records.items() if not r.get('error_msg')}
        return sum(1 for a in archives if a['ui_path'] not in extracted)

    def _open_process_dialog(self, preselect_nested=False, resume_search=False,
                             auto_archive_selection=False):
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
            preselect_nested=preselect_nested,
            auto_archive_selection=auto_archive_selection,
            parent=self,
        )
        dlg.header_types_cleared.connect(self._on_header_types_cleared)
        dlg.header_scan_done.connect(self._on_header_scan_done)
        dlg.nested_extraction_done.connect(self._on_nested_extraction_done)
        dlg.exec()
        # Came here from a keyword search — run that search automatically
        # once the dialog closes, whether or not anything was extracted.
        if resume_search and self.search_field.text().strip():
            self._skip_search_reminder_once = True
            self._start_keyword_search()

    # ── Photos.sqlite metadata index ──────────────────────────────────────────

    @staticmethod
    def _photo_key(ui_path: str) -> str | None:
        """Map a browser ui_path to the Photos.sqlite asset_path key.

        Asset paths are relative to the Media folder (ZDIRECTORY/ZFILENAME,
        e.g. 'DCIM/100APPLE/IMG_0001.HEIC'), so strip everything up to
        '/Media/'.  Works for both Cellebrite ('mobile/Media/…') and GrayKey
        ('private/var/mobile/Media/…') layouts."""
        i = ui_path.find('/Media/')
        return ui_path[i + 7:] if i >= 0 else None

    def _load_photo_index(self):
        """(Re)build the asset_path → photo column values index from the
        Photos Metadata artifact results, if present in the case DB."""
        self._photo_index = {}
        if not self._case_dir:
            return
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                columns, rows = load_artifact_results(db, 'photos_metadata')
        except Exception:
            return
        if not rows or 'asset_path' not in columns:
            return
        idx = {c: i for i, c in enumerate(columns)}

        def get(r, col):
            i = idx.get(col)
            return (r[i] or '') if i is not None else ''

        for r in rows:
            key = get(r, 'asset_path')
            if not key:
                continue
            status = ', '.join(x for x in (get(r, 'Favorite'),
                                           get(r, 'Hidden'),
                                           get(r, 'Trashed')) if x)
            flags = self._compute_photo_flags(get(r, 'Labels'),
                                              get(r, 'Face Ages'))
            self._photo_index[key] = [
                flags, get(r, 'Taken'), get(r, 'Added'), get(r, 'Original Name'),
                get(r, 'Album'), get(r, 'Labels'), get(r, 'Origin'),
                get(r, 'Camera'), get(r, 'Place'), get(r, 'GPS'),
                get(r, 'Faces'), get(r, 'Face Ages'), status,
            ]

    def _compute_photo_flags(self, labels_str: str, face_ages_str: str) -> str:
        """Map a photo's raw labels to triage flag categories (Weapon,
        Document, …).  A face estimated Baby/Child adds 'Minor (face)' as a
        review prompt.  Returns a compact comma-joined string, '' if none."""
        label_set = {l.strip().lower()
                     for l in (labels_str or '').split(',') if l.strip()}
        hits = [flag for flag, terms in self._photo_flag_rules.items()
                if label_set & terms]
        fa = (face_ages_str or '').lower()
        if 'baby' in fa or 'child' in fa:
            hits.append('Minor (face)')
        return ', '.join(hits)

    def _on_photo_index_changed(self):
        """Rebuild the photo index after artifact parsers ran, then refresh
        the current view so the columns appear immediately."""
        self._load_photo_index()
        if self._view_is_recursive:
            self._rebuild_file_view_from_checked(preserve_filter=True)
        else:
            self._refresh_folder_view(preserve_filter=True)

    def _open_new_ffs(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open ZIP", "", "ZIP (*.zip)")
        if path:
            self.start_loading(path)

    def start_loading(self, zip_path):
        case_dir, scan_headers = self._get_or_ask_case_dir(zip_path)
        if case_dir is None:
            return   # user cancelled the dialog
        self._case_dir = case_dir
        self._photo_index = {}        # rebuilt in on_metadata_ready
        self._photos_prompt_shown = False
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

        # Retire any running thumbnail worker from the previous archive
        # (no blocking wait — it may be inside a long ffmpeg call)
        self._retire_worker(self._thumb_worker)
        self._thumb_worker = None
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
        # Busy/indeterminate (range 0,0) so the bar keeps animating for the whole
        # load — a static bar looked hung.  Hidden only once BOTH the tree has
        # finished populating and the worker thread is done (covers any header
        # scan that runs after metadata_ready); see _maybe_hide_load_progress.
        self._tree_done   = False
        self._worker_done = False
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self._load_start = time.monotonic()
        self.worker = ZipMetadataWorker(zip_path, scan_headers=scan_headers,
                                        case_dir=self._case_dir)
        self.worker.status_update.connect(self.status_bar.showMessage)
        self.worker.metadata_ready.connect(self.on_metadata_ready)
        self.worker.header_scan_progress.connect(self._on_header_scan_progress)
        self.worker.header_scan_done.connect(self._on_header_scan_done)
        self.worker.finished.connect(self._on_load_worker_finished)
        self.worker.start()

    def on_metadata_ready(self, data, folder_map, guid_map, zip_names, ffs_adapter, missing_plist_paths, folder_sizes, zip_ui_paths=None, metadata_only_folders=None):
        self.full_metadata = data
        self.folder_map = folder_map
        self.guid_to_bundle = guid_map
        self.zip_names = zip_names
        self._adapter = ffs_adapter
        self._android_user_data_path = self._detect_android_user_data(folder_map)
        self._missing_plist_paths = set(missing_plist_paths)
        self._folder_sizes = folder_sizes
        self._zip_ui_paths = zip_ui_paths
        self._metadata_only_folders = metadata_only_folders or set()
        self._header_type_overrides = {}
        self._file_count_cache = {}
        if self._case_dir:
            try:
                with closing(_open_cache_db(self._case_dir)) as db:
                    self._header_type_overrides = load_header_types(db)
                    self._file_count_cache, _ = load_folder_data(db)
            except Exception:
                pass
        if self._zip_handle:
            self._zip_handle.close()
            self._zip_handle = None
        self._zip_open_future = None
        self._streaming_index    = getattr(self.worker, '_streaming_index', None)
        self._local_extra_delta  = getattr(self.worker, '_local_extra_delta', None)
        if self._streaming_index is None:
            self._zip_open_future = _BG_POOL.submit(zipfile.ZipFile, self.zip_path, 'r')
        self._time_cols = self._detect_time_columns(data)
        self.file_headers = (['Name'] + [h for h, _ in self._time_cols]
                             + ['Type', 'Size (Bytes)', 'Files', 'Path'])
        self._update_filter_columns(self.file_headers)
        # (Progress bar stays indeterminate/animated — hidden later once the
        # tree and worker are both finished, not here.)
        self.save_recent_list(self.zip_path)
        fmt_label = "GrayKey" if ffs_adapter.format == FfsAdapter.FORMAT_GRAYKEY else "Cellebrite"
        self._inject_nested_archives()
        self.reload_tree_entirely()
        self.tree_model.setHorizontalHeaderLabels([f"Folder Structure — {fmt_label}"])
        # Fetch device label and populate device_info after archive is loaded.
        # Always schedule so device_info is repopulated after a schema rebuild
        # even when the label is already cached in ffs_archives.json.
        QTimer.singleShot(0, partial(self._fetch_and_store_label, self.zip_path))
        self._refresh_artifact_tab()
        self._load_photo_index()
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
                with closing(_open_cache_db(self._case_dir)) as db:
                    save_header_types(db, results)
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
        try:
            db = _open_cache_db(self._case_dir)
        except Exception:
            return

        # Reset all virtual state before rebuilding.
        self._nested_archive_map    = {}
        self._nested_virtual_paths  = frozenset()
        self._nested_virtual_folders = frozenset()
        virtual_paths = set()
        out_dir = os.path.join(self._case_dir, 'nested_archives')

        with closing(db):
            try:
                records = load_nested_archives(db)
            except Exception:
                return

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
                    ep = e['entry_path']
                    if not ep or ep.endswith('/'):
                        continue   # skip directory entries
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

        self._nested_virtual_paths = frozenset(virtual_paths)

    def _on_nested_extraction_done(self):
        saved_checked = set(self._checked_folders)
        before = set(self._nested_archive_map.keys())

        self._inject_nested_archives()

        new_archives = set(self._nested_archive_map.keys()) - before

        # Insert newly-extracted archive folders into the tree where their
        # parent is already visible, and auto-tick them when their parent
        # was checked so children appear in the aggregate file view.
        for ui_path in sorted(new_archives):
            parent_path = ui_path.rsplit('/', 1)[0] if '/' in ui_path else ''
            parent_item = self._find_tree_item(parent_path)
            if parent_item is None:
                continue
            # Collect existing sibling paths before inserting the new item.
            sibling_paths = []
            if not self._item_has_placeholder(parent_item):
                for row in range(parent_item.rowCount()):
                    child = parent_item.child(row)
                    cp = child.data(Qt.ItemDataRole.UserRole)
                    if cp and cp != _TREE_PLACEHOLDER:
                        sibling_paths.append(cp)
            # Auto-tick when the parent was ticked AND every existing sibling
            # was already ticked (vacuously true when there are no siblings).
            auto_tick = (parent_path in saved_checked and
                         all(s in saved_checked for s in sibling_paths))
            if auto_tick:
                self._checked_folders.add(ui_path)
            check_state = (Qt.CheckState.Checked if auto_tick
                           else Qt.CheckState.Unchecked)
            self._insert_tree_folder_item(parent_item, ui_path, check_state)

        if new_archives:
            self._rebuild_file_view_from_checked(preserve_filter=True)

    def _extract_from_context_menu(self, ui_path: str) -> None:
        """Extract a single archive right-click entry without opening ProcessDialog."""
        if not self.zip_path or not self._case_dir:
            return
        self.status_bar.showMessage(f"Extracting {os.path.basename(ui_path)}…")
        worker = NestedArchiveWorker(
            self.zip_path,
            self._case_dir,
            [ui_path],
            self.full_metadata,
            self._adapter,
            self._streaming_index,
            self._local_extra_delta,
        )
        # partial binds ui_path; the signal appends (ok, err).
        worker.finished.connect(partial(self._on_context_extract_done, ui_path))
        self._context_extract_worker = worker
        worker.start()

    def _on_context_extract_done(self, ui_path: str, ok: int, err: int) -> None:
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
                with closing(_open_results_db(self._case_dir)) as db:
                    self._single_scan_run_id = start_run_log(
                        db, 'header_scan_single',
                        total=n,
                        notes='\n'.join(ui_paths),
                    )
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
                    with closing(_open_cache_db(self._case_dir)) as db:
                        save_header_types(db, results)
                except Exception:
                    pass
        if self._case_dir and self._single_scan_run_id is not None:
            try:
                with closing(_open_results_db(self._case_dir)) as db:
                    complete_run_log(db, self._single_scan_run_id,
                                     processed=n_total, output_rows=n_updated)
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
        has_photos = bool(self._photo_index) and any(
            (k := self._photo_key(p)) and k in self._photo_index
            for p in children)
        headers = (self.file_headers + (['UUID'] if has_bundles else [])
                   + (_PHOTO_HEADERS if has_photos else []))
        new_model = FileTableModel(headers)
        # _update_filter_columns resets the type selection — save the filter
        # bar state around it when the filter must survive the refresh.
        if preserve_filter:
            _type_sel    = self._type_filter_selected
            _type_label  = self.filter_type_btn.text()
            _scroll_val  = self.file_view.verticalScrollBar().value()
        self._update_filter_columns(headers)

        if preserve_filter:
            self._type_filter_selected = _type_sel
            self.filter_type_btn.setText(_type_label)
        else:
            self.filter_input.clear()
            self.proxy_model.set_filter("", -1)

        batch = []
        disp_parent = self._display_path(folder_path)
        for path in sorted(children):
            row = self._build_entry_row(path, has_bundles, display_parent=disp_parent,
                                        photo_cols=has_photos)
            if row is not None:
                batch.append(row)
        new_model.append_rows_batch(batch)
        self._set_file_model(new_model)

        if preserve_filter:
            # Compute against the new model so column indexes line up.
            _filter_args = self._current_filter_args()
            if self._filter_is_active(_filter_args):
                if select_path:
                    def _on_filter_done_select(*_):
                        new_model.filter_done.disconnect(_on_filter_done_select)
                        self._select_file_by_path(select_path)
                    new_model.filter_done.connect(_on_filter_done_select)
                self.proxy_model.set_filter(**_filter_args)
            else:
                self.file_view.verticalScrollBar().setValue(_scroll_val)
                if select_path:
                    self._select_file_by_path(select_path)
        elif select_path:
            self._select_file_by_path(select_path)

        # Resize columns but preserve the splitter position
        splitter_sizes = self.splitter.sizes()
        self.file_view.resizeColumnsToContents()
        self.splitter.setSizes(splitter_sizes)
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
        # Record in the checked set first — the tree walk below only updates
        # the visual state of items it can reach.
        self._checked_folders.update(p for p in path_set if p in self.folder_map)
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
        if not self.folder_map:
            # Nothing to populate — don't leave the load bar spinning forever.
            self._tree_done = True
            self._maybe_hide_load_progress()
            return
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
        root_item.setCheckState(Qt.CheckState.Checked if "" in self._checked_folders
                                else Qt.CheckState.Unchecked)
        self.tree_model.invisibleRootItem().appendRow(root_item)
        self._populate_tree_children_batched(root_item, "", on_done=lambda: self._on_tree_loaded(root_item))

    def _on_load_worker_finished(self):
        """The metadata worker thread has fully exited (including any header
        scan that ran after metadata_ready).  Hide the load bar only if the
        tree has also finished — whichever completes last."""
        self._worker_done = True
        self._maybe_hide_load_progress()

    def _maybe_hide_load_progress(self):
        """Hide the indeterminate load bar once both the tree population and the
        worker thread are done, so it keeps animating while either is still
        working."""
        if getattr(self, '_tree_done', False) and getattr(self, '_worker_done', False):
            self.progress_bar.hide()

    def _on_tree_loaded(self, root_item):
        self.tree_view.expand(self.tree_model.indexFromItem(root_item))
        self.tree_view.horizontalScrollBar().setValue(0)
        self._tree_populating = False
        if hasattr(self, '_adapter'):
            fmt_label = "GrayKey" if self._adapter.format == FfsAdapter.FORMAT_GRAYKEY else "Cellebrite"
            self.tree_model.setHorizontalHeaderLabels([f"Folder Structure — {fmt_label}"])
        self._tree_done = True
        self._maybe_hide_load_progress()
        elapsed = time.monotonic() - getattr(self, '_load_start', time.monotonic())
        self.status_bar.showMessage(
            f"Loaded: {os.path.basename(self.zip_path)}  —  {elapsed:.1f}s")
        if hasattr(self, '_adapter') and self._adapter.format == FfsAdapter.FORMAT_GRAYKEY:
            QTimer.singleShot(0, self._expand_to_private_var)
        QTimer.singleShot(0, self._fit_splitter_to_tree)
        QTimer.singleShot(0, self._refresh_bookmark_panel)

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
                item.setCheckState(Qt.CheckState.Checked if p in self._checked_folders
                                   else Qt.CheckState.Unchecked)
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

    # ── Bookmarks ─────────────────────────────────────────────────────────────

    def _refresh_bookmark_panel(self):
        """Repopulate the bookmark list panel below the folder tree."""
        if not getattr(self, '_case_dir', None):
            return
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                groups = load_bookmark_groups(db)
                if not groups:
                    save_bookmark_group(db, 'Evidence', 'Files directly relevant to the investigation')
                    save_bookmark_group(db, 'Interesting', 'Files worth reviewing further')
                    groups = load_bookmark_groups(db)
        except Exception:
            return
        self._bookmark_list.clear()
        for g in groups:
            item = QListWidgetItem(f"{g['name']}  ({g['count']:,})")
            item.setData(Qt.ItemDataRole.UserRole, g['id'])
            if g.get('description'):
                item.setToolTip(g['description'])
            self._bookmark_list.addItem(item)
        # Fix the list height to exactly fit its rows (up to a cap), then scroll.
        MAX_VISIBLE = 8
        row_h = self._bookmark_list.sizeHintForRow(0) if groups else 22
        if row_h <= 0:
            row_h = 22
        list_h = row_h * min(len(groups), MAX_VISIBLE) + 2
        self._bookmark_list.setFixedHeight(list_h)
        QTimer.singleShot(0, self._fit_bookmark_panel_size)
        # Keep search scope combo in sync with current bookmark groups.
        if hasattr(self, 'search_scope_combo'):
            self._refresh_search_scope_combo()

    def _fit_bookmark_panel_size(self):
        """Shrink the left splitter's bookmark panel to exactly fit its content."""
        total_h = self.left_splitter.height()
        if total_h <= 0:
            return
        panel_h = self._bookmark_panel.sizeHint().height()
        self.left_splitter.setSizes([max(50, total_h - panel_h), panel_h])

    def _on_bookmark_item_clicked(self, index):
        group_id = self._bookmark_list.item(index.row()).data(Qt.ItemDataRole.UserRole)
        if group_id is not None:
            self._show_bookmark_group(int(group_id))

    def _show_bookmark_panel_menu(self, point):
        item = self._bookmark_list.itemAt(point)
        menu = QMenu(self)
        new_act = QAction("New Group…", self)
        new_act.triggered.connect(partial(self._new_bookmark_group_dialog, []))
        menu.addAction(new_act)
        if item is not None:
            group_id = item.data(Qt.ItemDataRole.UserRole)
            menu.addSeparator()
            del_act = QAction(f"Delete '{item.text().split('  (')[0]}'", self)
            del_act.triggered.connect(partial(self._delete_bookmark_group, group_id))
            menu.addAction(del_act)
        menu.exec(self._bookmark_list.viewport().mapToGlobal(point))

    def _delete_bookmark_group(self, group_id: int):
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                delete_bookmark_group(db, group_id)
        except Exception as e:
            QMessageBox.warning(self, "Bookmark Error", str(e))
            return
        self._refresh_bookmark_panel()

    def _show_bookmark_group(self, group_id: int):
        """Populate the file browser with entries from a bookmark group."""
        if not self._case_dir:
            return
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                entries = load_bookmark_entries(db, group_id)
                groups  = load_bookmark_groups(db)
        except Exception:
            return
        group = next((g for g in groups if g['id'] == group_id), None)
        group_name = group['name'] if group else 'Bookmarks'

        headers = self.file_headers + ['Bookmarked']
        new_model = FileTableModel(headers)
        self._update_filter_columns(headers)
        self.filter_input.clear()
        self.proxy_model.set_filter("", -1)

        batch = []
        for entry in entries:
            ui_path = entry['ui_path']
            meta = self.full_metadata.get(ui_path, {})
            name = (meta.get('_display_name')
                    or entry.get('display_name')
                    or ui_path.rsplit('/', 1)[-1])
            is_folder = ui_path in self.folder_map
            file_type, grey_row, skip = self._classify_entry(ui_path, name, is_folder)
            if skip:
                continue
            fc = self._count_files_recursive(ui_path) if is_folder else -1
            cols = self._build_entry_cols(ui_path, name, meta, is_folder, file_type, fc)
            # Append bookmarked timestamp as plain readable string.
            bm_ts = entry.get('bookmarked_at', '')
            if bm_ts:
                bm_ts = bm_ts.replace('T', ' ')
            cols.append(bm_ts)
            batch.append((cols, ui_path, fc, is_folder and not grey_row,
                          grey_row, ui_path in self._nested_archive_map))

        new_model.append_rows_batch(batch)
        self._view_path = f"{_BM_GROUP_PREFIX}{group_id}"
        self._view_is_recursive = False
        self._set_file_model(new_model)
        n = len(entries)
        self.status_bar.showMessage(
            f"Bookmarks: {group_name}  —  {n:,} entr{'y' if n == 1 else 'ies'}")

    def _is_folder_path(self, ui_path: str) -> bool:
        """True if ui_path is a folder (real or empty directory entry)."""
        return ui_path in self.folder_map or self._is_empty_folder_entry(ui_path)

    def _collect_files_recursive(self, folder_path: str) -> list[str]:
        """Return all file paths under folder_path, depth-first, excluding folders."""
        files: list[str] = []
        seen: set[str] = {folder_path}   # also prevents folder_path itself appearing in output
        stack = [folder_path]
        while stack:
            current = stack.pop()
            for child in self.folder_map.get(current, []):
                if child in seen:
                    continue
                seen.add(child)
                if self._is_folder_path(child):
                    stack.append(child)
                else:
                    files.append(child)
        return files

    def _get_paths_for_bookmark(self) -> list[tuple[str, str]]:
        """Return [(ui_path, display_name)] for the current file-browser selection.

        Folder rows are expanded to their immediate file children.
        """
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for idx in self.file_view.selectionModel().selectedRows():
            src = self.proxy_model.mapToSource(idx)
            ui_path = self.file_model.index(src.row(), 0).data(Qt.ItemDataRole.UserRole)
            if not ui_path or ui_path in seen:
                continue
            if ui_path in self.folder_map:
                for child in self._collect_files_recursive(ui_path):
                    if child not in seen:
                        seen.add(child)
                        result.append((child, child.rsplit('/', 1)[-1]))
            else:
                seen.add(ui_path)
                result.append((ui_path, ui_path.rsplit('/', 1)[-1]))
        return result

    def _bookmark_submenu(self, menu: 'QMenu', paths) -> None:
        """Append a populated Bookmarks submenu to *menu*.

        *paths* is either a list of (ui_path, display_name) pairs or a
        zero-arg callable returning one — the callable form defers an
        expensive recursive collection until an action is actually clicked.
        """
        if not self._case_dir or (not callable(paths) and not paths):
            return
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                groups = load_bookmark_groups(db)
        except Exception:
            groups = []
        bm_menu = menu.addMenu("Bookmarks")
        for g in groups:
            act = QAction(f"{g['name']}  ({g['count']:,})", self)
            act.triggered.connect(partial(self._add_to_bookmark_group, paths, g['id']))
            bm_menu.addAction(act)
        if groups:
            bm_menu.addSeparator()
        new_act = QAction("New Group…", self)
        new_act.triggered.connect(partial(self._new_bookmark_group_dialog, paths))
        bm_menu.addAction(new_act)

    def _add_to_bookmark_group(self, paths, group_id: int):
        """Save *paths* (list or lazy callable) into an existing bookmark group."""
        if not self._case_dir:
            return
        if callable(paths):
            paths = paths()
        try:
            with closing(_open_results_db(self._case_dir)) as db:
                save_bookmark_entries(db, group_id, paths)
                groups = load_bookmark_groups(db)
        except Exception as e:
            QMessageBox.warning(self, "Bookmark Error", str(e))
            return
        group = next((g for g in groups if g['id'] == group_id), None)
        name = group['name'] if group else ''
        n = len(paths)
        self.status_bar.showMessage(
            f"Added {n:,} file{'s' if n != 1 else ''} to '{name}'", 4000)
        self._refresh_bookmark_panel()

    def _new_bookmark_group_dialog(self, paths):
        """Show a dialog to name and describe a new bookmark group, then save."""
        if callable(paths):
            paths = paths()
        dlg = QDialog(self)
        dlg.setWindowTitle("New Bookmark Group")
        dlg.setModal(True)
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Creating bookmark group for {len(paths):,} "
                                f"file{'s' if len(paths) != 1 else ''}."))
        layout.addWidget(QLabel("Group name (required):"))
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("e.g. Suspect documents")
        layout.addWidget(name_edit)
        layout.addWidget(QLabel("Description (optional):"))
        desc_edit = QTextEdit()
        desc_edit.setFixedHeight(60)
        desc_edit.setPlaceholderText("Purpose or notes about this group…")
        layout.addWidget(desc_edit)
        err_label = QLabel()
        err_label.setStyleSheet("color: red;")
        layout.addWidget(err_label)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("Create && Add")
        ok_btn.setDefault(True)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        def _try_create():
            name = name_edit.text().strip()
            if not name:
                err_label.setText("Name cannot be blank.")
                return
            desc = desc_edit.toPlainText().strip()
            try:
                with closing(_open_results_db(self._case_dir)) as db:
                    group_id = save_bookmark_group(db, name, desc)
                    save_bookmark_entries(db, group_id, paths)
            except Exception as e:
                err_label.setText(str(e))
                return
            dlg.accept()
            n = len(paths)
            self.status_bar.showMessage(
                f"Created '{name}' with {n:,} file{'s' if n != 1 else ''}", 4000)
            self._refresh_bookmark_panel()

        ok_btn.clicked.connect(_try_create)
        dlg.exec()

    # ── End bookmarks ──────────────────────────────────────────────────────────

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
            item.setCheckState(Qt.CheckState.Checked if p in self._checked_folders
                               else Qt.CheckState.Unchecked)
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
        path = item.data(Qt.ItemDataRole.UserRole)
        if path is not None and path != _TREE_PLACEHOLDER:
            if item.checkState() == Qt.CheckState.Checked:
                self._checked_folders.add(path)
                # Single folder tick — expand to show unticked children
                self.tree_view.expand(self.tree_model.indexFromItem(item))
            else:
                self._checked_folders.discard(path)
        self.tree_view.viewport().update()
        if not self._rebuild_pending:
            self._rebuild_pending = True
            QTimer.singleShot(0, self._deferred_rebuild)

    def _update_selected_btn(self):
        """Recount ticked folders and update the status label and deselect button."""
        checked = self._checked_folders
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
        self._checked_folders.clear()
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
        act.triggered.connect(partial(self._remove_recent, path))
        menu.addAction(act)
        menu.exec(view.viewport().mapToGlobal(point))

    # ── ffs_archives helpers ──────────────────────────────────────────────────

    def _archives_file_path(self) -> str:
        """Return the path to ffs_archives.json — in case root if set, else config/."""
        prefs = _load_prefs()
        root = prefs.get('case_data_root', '').strip()
        if root and os.path.isdir(root):
            return os.path.join(root, 'ffs_archives.json')
        return FFS_ARCHIVES_FILE

    def _load_ffs_archives(self) -> list:
        """Load and return the merged, sorted archive list.

        If a case root is configured, scans it for case folders not already in
        the JSON (identified by the presence of caseresults.db) and adds them,
        reading the zip path from the DB.  Entries whose case_dir no longer
        exists are removed.  Result is sorted by last_opened descending.
        """
        archives_path = self._archives_file_path()
        stored: list = _load_json_file(archives_path, [])

        # Also try the old config location if we moved to case root
        if archives_path != FFS_ARCHIVES_FILE:
            old = _load_json_file(FFS_ARCHIVES_FILE, [])
            known_dirs = {e.get('case_dir') for e in stored}
            for e in old:
                if e.get('case_dir') and e['case_dir'] not in known_dirs:
                    stored.append(e)

        by_case_dir: dict = {e['case_dir']: e for e in stored if e.get('case_dir')}

        # Scan case root for any case folders not already in the list
        prefs = _load_prefs()
        root = prefs.get('case_data_root', '').strip()
        if root and os.path.isdir(root):
            # scandir caches the is_dir result per entry — one directory pass
            # instead of a stat call per name (matters on network case roots).
            with os.scandir(root) as it:
                dir_entries = [e for e in it if e.is_dir()]
            for dir_entry in dir_entries:
                case_dir = dir_entry.path
                if case_dir in by_case_dir:
                    continue
                db_path = os.path.join(case_dir, 'caseresults.db')
                if not os.path.exists(db_path):
                    continue
                # Found a case folder — try to read zip_path from DB
                zip_path = ''
                try:
                    with closing(_open_results_db(case_dir)) as db:
                        rows = load_device_info(db)
                    zip_path = next(
                        (data for field, data, _src in rows if field == 'zip_path'), '')
                except Exception:
                    pass
                entry: dict = {'case_dir': case_dir}
                if zip_path:
                    entry['path'] = zip_path
                by_case_dir[case_dir] = entry

        # Drop entries whose case_dir no longer exists
        result = [e for e in by_case_dir.values() if os.path.isdir(e.get('case_dir', ''))]

        # Sort according to preference
        sort_order = prefs.get('case_sort_order', 'recent')
        if sort_order == 'alphabetical':
            def _alpha_key(e):
                label = e.get('label', '')
                return (label or os.path.basename(e.get('case_dir', '')) or e.get('path', '')).lower()
            result.sort(key=_alpha_key)
        else:
            result.sort(key=lambda e: e.get('last_opened', ''), reverse=True)
        return result

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
            with open(self._archives_file_path(), 'w', encoding='utf-8') as f:
                json.dump(self._ffs_archives, f, indent=2)
        except OSError:
            pass

    def _upsert_archive(self, path: str, case_dir: str | None = None):
        """Update or insert an archive entry, stamping last_opened and moving to front."""
        existing = self._archive_entry(path)
        existing_label = existing.get('label', '') if existing else ''
        existing_case  = existing.get('case_dir', '') if existing else ''
        self._ffs_archives = [e for e in self._ffs_archives if e.get('path') != path]
        entry: dict = {
            'path':        path,
            'last_opened': datetime.now(tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
        }
        if case_dir is not None:
            entry['case_dir'] = case_dir
        elif existing_case:
            entry['case_dir'] = existing_case
        if existing_label:
            entry['label'] = existing_label
        self._ffs_archives.insert(0, entry)
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
                with closing(_open_results_db(self._case_dir)) as db:
                    has_db_info = bool(load_device_info(db))
            except Exception:
                pass
        if has_label and has_db_info:
            return

        # Reading device info opens the zip — run it off the GUI thread.
        # partial binds the context up front; the signal appends (fields, label).
        worker = DeviceInfoWorker(path)
        worker.done.connect(partial(self._on_device_info_ready,
                                    path, self._case_dir, has_db_info, has_label))
        self._device_info_worker = worker
        worker.start()

    def _on_device_info_ready(self, path, case_dir, has_db_info, has_label,
                              fields, label):
        if case_dir:
            try:
                with closing(_open_results_db(case_dir)) as db:
                    if fields and not has_db_info:
                        save_device_info(db, fields)
                    # Always record which archive this case folder belongs to
                    upsert_device_info_field(db, 'zip_path', path, 'archive')
            except Exception:
                pass
        entry = self._archive_entry(path)
        if entry is not None and not has_label:
            entry['label'] = label
            self._save_ffs_archives()
            self.update_dropdown_ui()

    def _entry_display(self, entry: dict, prefs: dict) -> str:
        """Build the display string for one archive entry using slot preferences."""
        p  = entry.get('path', '')
        cd = entry.get('case_dir', '')
        if not p:
            label = entry.get('label', '')
            name  = os.path.basename(cd) if cd else '?'
            prefix = f"{label}  —  " if label else ""
            return f"[case folder only]  {prefix}{name}"
        return _format_archive_entry(entry, prefs)

    def update_dropdown_ui(self):
        prefs = _load_prefs()
        self.archive_dropdown.blockSignals(True)
        self.archive_dropdown.clear()
        # Header item — always index 0, not selectable
        self.archive_dropdown.addItem("Cases")
        from PySide6.QtGui import QStandardItemModel as _QStdModel
        model = self.archive_dropdown.model()
        assert isinstance(model, _QStdModel)
        item = model.item(0)
        from PySide6.QtCore import Qt as _Qt
        item.setFlags(item.flags() & ~(_Qt.ItemFlag.ItemIsSelectable | _Qt.ItemFlag.ItemIsEnabled))
        for entry in self._ffs_archives:
            p  = entry.get('path', '')
            cd = entry.get('case_dir', '')
            display = self._entry_display(entry, prefs)
            self.archive_dropdown.addItem(display, userData=p or cd)
        self.archive_dropdown.setCurrentIndex(0)
        self.archive_dropdown.blockSignals(False)
        # Re-select the currently loaded archive if any
        if self.zip_path:
            for i in range(1, self.archive_dropdown.count()):
                if self.archive_dropdown.itemData(i) == self.zip_path:
                    self.archive_dropdown.setCurrentIndex(i)
                    break

    def _populate_recent_menu(self):
        prefs = _load_prefs()
        self._recent_menu.clear()
        if not self._ffs_archives:
            empty_act = QAction("No cases found", self)
            empty_act.setEnabled(False)
            self._recent_menu.addAction(empty_act)
            return
        for entry in self._ffs_archives:
            p = entry.get('path', '')
            display = self._entry_display(entry, prefs)
            act = QAction(display, self)
            act.setEnabled(bool(p))
            if p:
                act.triggered.connect(partial(self.start_loading, p))
            self._recent_menu.addAction(act)
        self._recent_menu.addSeparator()
        clear_act = QAction("Clear List", self)
        clear_act.triggered.connect(self._clear_recent_list)
        self._recent_menu.addAction(clear_act)

    def _clear_recent_list(self):
        self._ffs_archives.clear()
        self.recent_paths.clear()
        self._save_ffs_archives()
        self.update_dropdown_ui()

    def _retire_worker(self, worker):
        """Stop a QThread without blocking the GUI.

        Disconnects all its signals (so stale results are ignored), requests a
        stop, and keeps a reference until the thread actually finishes — Qt
        aborts if a running QThread is garbage-collected.  Use this instead of
        stop()+wait() when the worker may take seconds to notice the stop
        (e.g. a thumbnail worker inside an ffmpeg call).
        """
        if worker is None or not worker.isRunning():
            return
        try:
            worker.disconnect()
        except (RuntimeError, TypeError):
            pass
        if hasattr(worker, 'stop'):
            worker.stop()
        else:
            worker.requestInterruption()
        self._retired_workers.append(worker)
        worker.finished.connect(partial(self._reap_retired_worker, worker))

    def _reap_retired_worker(self, worker):
        try:
            self._retired_workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

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
        _stop(getattr(self, '_device_info_worker', None))
        _stop(getattr(self, '_thumb_worker', None), has_stop=True)
        _stop(getattr(self, '_search_index_worker', None), has_stop=True)
        for w in list(getattr(self, '_retired_workers', [])):
            _stop(w, has_stop=hasattr(w, 'stop'))

    def closeEvent(self, event):
        self._stop_all_workers()
        self._clear_sqlite_preview()
        self._clear_segb_preview()
        super().closeEvent(event)


def _qt_message_handler(msg_type, context, message):
    if "qt.text.font.db" in (context.category or "") and "OpenType support missing" in message:
        return
    if msg_type == QtMsgType.QtWarningMsg:
        print(f"Qt warning: {message}", file=sys.stderr)
    elif msg_type in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        print(f"Qt error: {message}", file=sys.stderr)

if __name__ == "__main__":
    # Must run before any process is spawned (and is essential under PyInstaller,
    # where a spawned child re-launches the exe — freeze_support reroutes it to
    # the worker target instead of opening another GUI window).
    import multiprocessing
    multiprocessing.freeze_support()
    qInstallMessageHandler(_qt_message_handler)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(resource_path(os.path.join("resources", "icon.png"))))
    window = FastZipBrowser()
    app.aboutToQuit.connect(window._stop_all_workers)
    atexit.register(window._stop_all_workers)  # runs before Qt's atexit on exception exit
    window.show()
    sys.exit(app.exec())