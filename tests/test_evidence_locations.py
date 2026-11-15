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
    ('ios', es.IOS_SHORTCUTS), ('android', es.android_shortcuts()),
    ('android', es.android_shortcuts(user=10))])
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


# ── several Android users on one phone ─────────────────────────────────────

def _folders_with_work_profile():
    """Layout of an Android 16 emulator with a managed work profile (user
    10), as observed in Grok's lab: separate per-user trees; data/data is
    user 0 only."""
    return {'data/data', 'data/data/com.android.chrome', 'data/user', 'data/user/0',
            'data/user/10', 'data/user/10/com.android.chrome',
            'data/user/10/com.android.chrome/app_chrome/Default',
            'data/data/com.android.chrome/app_chrome/Default',
            'data/user_de', 'data/user_de/0', 'data/user_de/10',
            'data/media', 'data/media/0', 'data/media/10', 'data/media/10/DCIM',
            'data/system_ce', 'data/system_ce/0', 'data/system_ce/10',
            'data/system/users', 'data/system/users/0', 'data/system/users/10',
            'data/user_de/150'}


def test_users_are_read_from_the_extraction():
    assert es.android_users(_folders_with_work_profile()) == [0, 10, 150]
    assert es.android_users({'data/data', 'data/user_de/0'}) == [0]
    # a numeric folder somewhere else is not a user
    assert es.android_users({'data/data', 'data/app/12', 'data/user/0/x/5'}) == [0]


def test_another_users_entries_never_point_at_user_0():
    for _trail, entry in es.leaves(es.android_shortcuts(user=10)):
        for cand in es.candidates_of(entry):
            assert not cand.startswith('data/data'), cand
            assert '/0/' not in cand and not cand.endswith('/0'), cand
    labels = [e[0] for _t, e in es.leaves(es.android_shortcuts(user=10))]
    assert 'Wi-Fi Networks' not in labels        # device-wide: owner's menu only
    assert "App Data (user 10)" in labels


def test_work_profile_chrome_is_its_own_entry():
    folders = _folders_with_work_profile()
    def chrome(user):
        entry = next(e for _t, e in es.leaves(es.android_shortcuts(user=user)) if e[0] == 'Chrome')
        return es.resolve(es.candidates_of(entry), folders, set())
    assert chrome(0) == 'data/data/com.android.chrome/app_chrome/Default'
    assert chrome(10) == 'data/user/10/com.android.chrome/app_chrome/Default'
    dcim = next(e for _t, e in es.leaves(es.android_shortcuts(user=10)) if e[0] == 'Photos (DCIM)')
    assert es.resolve(es.candidates_of(dcim), folders, set()) == 'data/media/10/DCIM'


def test_user_type_label():
    work = b'<?xml version="1.0"?><user id="10" type="android.os.usertype.profile.MANAGED" flags="4144" />'
    assert es.user_type_label(work) == 'work profile'
    private = b'<user id="11" type="android.os.usertype.profile.PRIVATE" />'
    assert es.user_type_label(private) == 'Private Space'
    odd = b'<user id="150" type="com.example.usertype.profile.VAULT" />'
    assert es.user_type_label(odd) == 'profile.VAULT'             # not guessed
    old_work = b'<user id="10" flags="48" />'                    # 0x20 managed profile
    assert es.user_type_label(old_work) == 'work profile'
    named = b'<user id="12" type="android.os.usertype.full.SECONDARY"><name>Someone</name></user>'
    assert es.user_type_label(named) == 'secondary user'          # never the name
    assert es.user_type_label(b'') is None
    assert es.user_type_label(b'not xml') is None
    assert es.users_summary([0, 10], {0: 'owner', 10: 'work profile'}) == \
        'This extraction has 2 users: 0 (owner), 10 (work profile)'


_ALICE = os.path.join(ROOT, 'test_data', 'alice_work_profile_user10')


@pytest.mark.skipif(not os.path.isdir(_ALICE), reason="Alice lab fixture not on this machine")
def test_user_type_from_real_android_16_abx():
    """data/system/users/<id>.xml as Android 16 writes it (ABX), from
    Grok's lab emulator with a managed work profile (user 10)."""
    def label(name):
        with open(os.path.join(_ALICE, name), 'rb') as f:
            return es.user_type_label(f.read())
    assert label('10.xml') == 'work profile'
    assert label('0.xml') == 'owner'
    assert label('10.decoded.xml') == 'work profile'   # same file as plain XML
