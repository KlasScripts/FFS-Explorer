"""Container naming (GUID -> bundle id) never loses a folder silently.
Archives are written byte by byte (the writer from test_zip_offsets) in the
GrayKey iOS layout. Covers: a plist read at a wrong offset is retried at its
local header; an unreadable plist is reported, not dropped; and a stale
cached map is repaired on open from the app registry's App Group GUIDs and
from re-reading the missing plists."""
import os
import plistlib
from contextlib import closing

import db_utils
import ffs_metadata as M
import zip_cd_cache as zc
from adapters.ffs import FfsAdapter, _build_guid_bundle_map, _mcm_identifier_at
from test_zip_offsets import _write_zip

META = '.com.apple.mobile_container_manager.metadata.plist'
PFX = '/private/var/mobile/Containers/'
APP = '11111111-1111-1111-1111-111111111111'
GROUP = '22222222-2222-2222-2222-222222222222'
GROUP_NO_PLIST_READ = '33333333-3333-3333-3333-333333333333'


def _plist(identifier):
    return plistlib.dumps({'MCMMetadataIdentifier': identifier}, fmt=plistlib.FMT_BINARY)


def _archive(tmp_path, corrupt_group=False):
    entries = [
        {'name': f'{PFX}Data/Application/{APP}/{META}'.encode(), 'data': _plist('com.example.app')},
        {'name': f'{PFX}Data/Application/{APP}/Documents/a.txt'.encode(), 'data': b'a'},
        {'name': f'{PFX}Shared/AppGroup/{GROUP}/{META}'.encode(),
         'data': b'not a plist at all' if corrupt_group else _plist('group.com.example')},
        {'name': f'{PFX}Shared/AppGroup/{GROUP_NO_PLIST_READ}/{META}'.encode(),
         'data': b'garbage, but LaunchServices knows this group'},
    ]
    zip_path = str(tmp_path / 'ffs.zip')
    offsets = _write_zip(zip_path, entries)
    case_dir = str(tmp_path / 'case')
    os.makedirs(case_dir)
    assert zc.save(zip_path, case_dir)
    infos = zc.load(zip_path, case_dir)
    return zip_path, case_dir, infos, offsets


def test_wrong_offset_is_retried_at_local_header(tmp_path):
    zip_path, _, infos, offsets = _archive(tmp_path)
    name = f'{PFX}Data/Application/{APP}/{META}'
    info = next(i for i in infos if i.filename == name)
    with open(zip_path, 'rb') as rf:
        # 12 bytes off: the size of the pre-v0.45.0 error on GrayKey zips
        assert _mcm_identifier_at(rf, offsets[name] - 12, info.file_size,
                                  info.header_offset) == ('com.example.app', '')


def test_unreadable_plist_is_reported_not_dropped(tmp_path):
    zip_path, _, infos, _ = _archive(tmp_path, corrupt_group=True)
    failures = []
    got = _build_guid_bundle_map(zip_path, frozenset(i.filename for i in infos),
                                 z=zc.CachedZipView(zip_path, infos),
                                 container_prefix=PFX, failures=failures)
    assert got == {APP: 'com.example.app'}
    failed = {name.split('/')[-2]: reason for name, reason in failures}
    assert set(failed) == {GROUP, GROUP_NO_PLIST_READ}
    assert 'unreadable plist' in failed[GROUP]


def test_stale_cached_map_is_repaired_on_open(tmp_path):
    zip_path, case_dir, infos, _ = _archive(tmp_path)
    names = frozenset(i.filename for i in infos)
    adapter = FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, '', '', False)
    folder_map = M._build_folder_tree({n.lstrip('/'): {} for n in names})
    with closing(db_utils._open_cache_db(case_dir)) as db:
        # an old build's map: every container lost; the app registry knows one group
        db_utils.save_guid_bundle_map(db, {})
        db_utils.save_app_registry(db, [{
            'bundle_id': 'com.example.other', 'display_name': 'Other', 'team_id': None,
            'bundle_container_path': None, 'data_container_path': None,
            'app_group_paths': {'group.com.example.known': GROUP_NO_PLIST_READ},
            'has_parser': False}])
    fixed = M.repair_guid_map(zip_path, case_dir, adapter, folder_map, names, {})
    assert fixed == {APP: 'com.example.app', GROUP: 'group.com.example',
                     GROUP_NO_PLIST_READ: 'group.com.example.known'}
    assert M._find_missing_plists(folder_map, adapter, fixed) == []
    with closing(db_utils._open_cache_db(case_dir)) as db:
        assert db_utils.load_guid_bundle_map(db) == fixed


def test_complete_map_is_left_alone(tmp_path):
    zip_path, case_dir, infos, _ = _archive(tmp_path)
    names = frozenset(i.filename for i in infos)
    adapter = FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, '', '', False)
    folder_map = M._build_folder_tree({n.lstrip('/'): {} for n in names})
    full = {APP: 'a', GROUP: 'g', GROUP_NO_PLIST_READ: 'h'}
    assert M.repair_guid_map(zip_path, case_dir, adapter, folder_map, names, full) is full
