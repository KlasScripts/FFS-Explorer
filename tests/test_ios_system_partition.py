"""The iOS system partition is not included (ASSUMPTIONS.md A-82):
FfsAdapter.is_ios_system_entry on every layout, and the old-layout Cellebrite
listing (build_zip_entries), with zip entry names as seen on real archives
(IOS17 old layout, IOS18 CTF25 GrayKey)."""
from adapters.ffs import FfsAdapter


def test_cellebrite_old_layout():
    a = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, 'filesystem1', 'filesystem1', old_layout=True)
    system = ['filesystem1/System/Library/CoreServices/SystemVersion.plist',
              'filesystem1/Applications/ActivityMessagesApp.app/PlugIns/'
              'ActivityMessagesExtension.appex/fr_CA.lproj/',
              'filesystem1/usr/lib/dyld', 'filesystem1/tmp/x', 'filesystem1/private/etc/hosts']
    user = ['filesystem1/private/var/mobile/Library/SMS/sms.db',
            'filesystem1/private/var/tmp/x', 'filesystem1/private/var/', 'filesystem1/private/']
    neither = ['extra/KeychainDump', 'Log.txt', 'metadata1/filesystem.msgpack']
    assert all(a.is_ios_system_entry(n) for n in system)
    assert not any(a.is_ios_system_entry(n) for n in user + neither)
    listed = a.build_zip_entries(frozenset(system + user + neither))
    assert set(listed) == {'mobile/Library/SMS/sms.db', 'tmp/x'}
    assert listed['tmp/x'] == 'filesystem1/private/var/tmp/x'      # /private/var/tmp, not /tmp
    for ui, phys in listed.items():
        assert a.resolve(ui) == phys


def test_cellebrite_new_layout():
    a = FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, 'filesystem2', 'filesystem1')
    assert a.is_ios_system_entry('filesystem1/System/Library/x')
    assert not a.is_ios_system_entry('filesystem2/mobile/Library/SMS/sms.db')
    assert not a.is_ios_system_entry('extra/KeychainDump')
    assert set(a.build_zip_entries(frozenset(['filesystem1/System/x', 'filesystem2/mobile/a']))) == {'mobile/a'}


def test_graykey():
    a = FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, 'private/var', '')
    assert a.is_ios_system_entry('/Applications/MobileSMS.app/Info.plist')
    assert a.is_ios_system_entry('System/Library/x')            # metadata keys have no leading '/'
    assert not a.is_ios_system_entry('/private/var/mobile/Library/SMS/sms.db')
    assert not a.is_ios_system_entry('private/var')
    assert not a.is_ios_system_entry('private')


def test_android_layout_has_no_rule():
    a = FfsAdapter(FfsAdapter.FORMAT_ZIP_EXTRAS, 'Dump', '')
    assert not a.is_ios_system_entry('Dump/system/build.prop')
