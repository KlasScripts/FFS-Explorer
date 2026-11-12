"""FfsAdapter.ui_path_from_physical is the inverse of resolve() on every layout
in ASSUMPTIONS.md A-3, and differs from strip_display_prefix() where A-3 says
the display form differs (old-layout Cellebrite, GrayKey iOS). Keyword
Search's "Open Parent Folder" navigates with it."""
from adapters.ffs import FfsAdapter

CASES = [
    # (adapter, physical, ui_path)
    (FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, 'filesystem2', 'filesystem1'),
     'filesystem2/mobile/Library/SMS/sms.db', 'mobile/Library/SMS/sms.db'),
    (FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, 'filesystem1', 'filesystem1', old_layout=True),
     'filesystem1/private/var/mobile/Library/SMS/sms.db', 'mobile/Library/SMS/sms.db'),
    (FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, 'private/var', ''),
     '/private/var/mobile/Library/SMS/sms.db', 'private/var/mobile/Library/SMS/sms.db'),
    (FfsAdapter(FfsAdapter.FORMAT_GRAYKEY, '', ''),
     '/data/data/com.whatsapp/databases/msgstore.db', 'data/data/com.whatsapp/databases/msgstore.db'),
    (FfsAdapter(FfsAdapter.FORMAT_CELLEBRITE, 'Dump', ''),
     'Dump/data/data/com.whatsapp/databases/msgstore.db', 'data/data/com.whatsapp/databases/msgstore.db'),
    (FfsAdapter(FfsAdapter.FORMAT_ZIP_EXTRAS, 'Dump', ''),
     'Dump/data/data/com.whatsapp/databases/msgstore.db', 'data/data/com.whatsapp/databases/msgstore.db'),
]


def test_inverse_of_resolve_on_every_layout():
    for a, physical, ui in CASES:
        assert a.ui_path_from_physical(physical) == ui, (a.format, physical)
        assert a.resolve(ui) == physical, (a.format, ui)


def test_not_the_display_form_where_a3_says_they_differ():
    old = CASES[1][0]
    assert old.strip_display_prefix(CASES[1][1]) == 'private/var/mobile/Library/SMS/sms.db'
    gk = CASES[2][0]
    assert gk.strip_display_prefix(CASES[2][1]) == 'mobile/Library/SMS/sms.db'


def test_entries_outside_the_tree_have_no_ui_path():
    old = CASES[1][0]
    assert old.ui_path_from_physical('filesystem1/System/Library/CoreServices/SystemVersion.plist') is None
    new = CASES[0][0]
    assert new.ui_path_from_physical('filesystem1/System/Library/x') is None
    assert new.ui_path_from_physical('filesystem2/') is None
