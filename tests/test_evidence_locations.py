"""Evidence locations (EVIDENCE_LOCATIONS.md): the jump menu's per-version
candidates, the shared evidence folders (scan_rules), and the warning for
an OS version whose locations were not checked."""
import json
import os

import pytest

import evidence_shortcuts as es
import os_coverage
import scan_rules

ROOT = os.path.join(os.path.dirname(__file__), '..')

# Jump targets deliberately outside the evidence folders (too large, no
# user data to type-scan): app executables only.
_NOT_SCANNED = {'containers/Bundle/Application'}


def _in_scope(path, platform):
    m = lambda p, q: p == q.rstrip('/') or p.startswith(q.rstrip('/') + '/')
    return (any(m(path, q) for q in scan_rules.include_prefixes(platform))
            and not any(m(path, q) for q in scan_rules.exclude_prefixes(platform)))


@pytest.mark.parametrize('platform, items', [
    ('ios', es.IOS_SHORTCUTS), ('android', es.android_shortcuts())])
def test_every_jump_target_is_an_evidence_folder(platform, items):
    """Header Scan and the other scans cover every place the jump menu
    calls evidence (a pattern is checked by its fixed part)."""
    missing = []
    for trail, entry in es.leaves(items):
        for cand in es.candidates_of(entry):
            fixed = cand.split('*')[0].rstrip('/')
            if fixed in _NOT_SCANNED:
                continue
            if not _in_scope(fixed, platform) and not _in_scope(fixed + '/x', platform):
                missing.append((' > '.join(trail), cand))
    assert not missing, missing


def test_user_media_is_unchanged_by_the_new_folders():
    """The new evidence folders are scanned, never shown as User Media."""
    assert set(scan_rules.user_media_prefixes('android')) == {'data/data/', 'data/media/'}
    assert set(scan_rules.user_media_prefixes('ios')) == {
        'mobile/Containers/Data/Application', 'mobile/Containers/Data/PluginKitPlugin',
        'mobile/Containers/Shared/AppGroup', 'mobile/Media/DCIM', 'mobile/Media/PhotoData'}


@pytest.mark.parametrize('platform', ['ios', 'android'])
def test_rule_files_are_well_formed(platform):
    with open(os.path.join(ROOT, 'config', f'scan_rules_{platform}.json'), encoding='utf-8') as f:
        rules = json.load(f)['rules']
    paths = [r['path'] for r in rules]
    assert len(paths) == len(set(paths)), "a folder is listed twice"
    for r in rules:
        assert r.get('include') in (0, 1), r
        assert r.get('user_media') in ('not_user_media', 'user_media_only', 'also_user_media'), r
        assert not r['path'].startswith(('/', 'private/var/')), r   # canonical form
    inc = [r['path'].rstrip('/') for r in rules if r['include'] == 1]
    for r in rules:
        if r['include'] == 0:    # an exclude always sits inside an include
            p = r['path'].rstrip('/')
            assert any(p.startswith(i + '/') for i in inc), r


def test_android_covers_other_users_and_device_encrypted_data():
    inc = set(scan_rules.include_prefixes('android'))
    for p in ('data/user/', 'data/user_de/', 'data/system/', 'data/system_ce/',
              'data/system_de/', 'data/misc/', 'data/misc_ce/', 'data/misc_de/'):
        assert p in inc, p


def test_resolve_takes_the_first_location_this_extraction_has():
    folders = {'data/system/users/0', 'mobile/Library/CallHistoryDB'}
    files = {'mobile/Containers/Shared/AppGroup/B-UUID/NoteStore.sqlite',
             'mobile/Containers/Shared/AppGroup/A-UUID/NoteStore.sqlite',
             'data/system_ce/0/accounts_ce.db'}
    # newest location listed first, older one used only when it is absent
    assert es.resolve(('data/system/users/0', 'data/data/x'), folders, files) == 'data/system/users/0'
    assert es.resolve(('nope', 'mobile/Library/CallHistoryDB'), folders, files) == \
        'mobile/Library/CallHistoryDB'
    # a file target
    assert es.resolve(('data/system_ce/0/accounts_ce.db',), folders, files) == \
        'data/system_ce/0/accounts_ce.db'
    # a pattern opens the folder of its first match, in a fixed order
    assert es.resolve(('mobile/Containers/Shared/AppGroup/*/NoteStore.sqlite',), folders, files) == \
        'mobile/Containers/Shared/AppGroup/A-UUID'
    # nothing there: None, never the nearest existing folder
    assert es.resolve(('mobile/Library/Notes',), folders, files) is None


def test_resolve_applies_the_graykey_prefix():
    gk = lambda p: p if p.startswith('private/') else 'private/var/' + p
    assert es.resolve(('Keychains',), {'private/var/Keychains'}, set(), adapt=gk) == \
        'private/var/Keychains'
    assert es.resolve(('Keychains',), {'private/var/Keychains'}, set()) is None


def test_ios_jump_paths_are_written_without_private_var():
    """Cellebrite ui_paths drop private/var/; GrayKey gets it from
    prefix_shortcut. A hard-coded prefix finds nothing on Cellebrite."""
    for _trail, entry in es.leaves(es.IOS_SHORTCUTS):
        for cand in es.candidates_of(entry):
            assert not cand.startswith('private/'), cand


def test_os_coverage_warning():
    assert os_coverage.warning('iOS', '17.5.1') is None
    assert os_coverage.warning('Android', '14') is None
    assert os_coverage.warning('Android', '15') is None
    newer = os_coverage.warning('iOS', '26.1')
    assert 'iOS 26.1, newer' in newer and 'iOS 17' in newer and 'moved' in newer
    assert 'older' in os_coverage.warning('Android', '12')
    assert 'could not be read' in os_coverage.warning('Android', '')
    assert 'could not be read' in os_coverage.warning('Android', 'Baklava')
    rows = [('Make', 'Google', 'UFD'), ('Android Version', '16', 'build.prop')]
    assert os_coverage.version_of(rows, 'Android') == '16'
    assert os_coverage.version_of(rows, 'iOS') == ''
