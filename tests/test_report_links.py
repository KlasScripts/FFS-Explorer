"""Report -> File Browser links for files a report refers to indirectly:
Android device paths, MediaStore/Viber URIs, and link columns holding
several paths (one per line)."""
import base64
import importlib.util
import os

from artifact_db import build_attachment_links
from artifact_runner import device_path_to_ui_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parser(platform, name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, 'artifacts', platform, f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_device_paths_map_to_archive_paths():
    assert device_path_to_ui_path('/storage/emulated/0/Download/Imgur/3qaBztA.png') == \
        'data/media/0/Download/Imgur/3qaBztA.png'
    assert device_path_to_ui_path('file:///sdcard/DCIM/a%20b.jpg') == 'data/media/0/DCIM/a b.jpg'
    assert device_path_to_ui_path('/data/user/0/com.x/files/y') == 'data/data/com.x/files/y'
    assert device_path_to_ui_path('/system/fonts/x.ttf') == ''
    assert device_path_to_ui_path('') == ''


def test_link_column_with_several_paths():
    rows = [{'a': 'x/1.jpg', 'faces': 'db/f-1.jpg\ndb/f-2.jpg'}, {'a': '', 'faces': ''},
            {'a': 'x/3.jpg', 'faces': ['db/f-3.jpg']}]
    links = build_attachment_links('photos', rows, ['a', 'faces'], 'Photos')
    assert [(l['ui_path'], l['row_id']) for l in links] == [
        ('x/1.jpg', 1), ('db/f-1.jpg', 1), ('db/f-2.jpg', 1), ('x/3.jpg', 3), ('db/f-3.jpg', 3)]


def test_viber_extra_uri_decoding():
    viber = _parser('android', 'viber')
    inner = 'content://media/external/images/media/1000000648'
    token = base64.urlsafe_b64encode(inner.encode()).decode().rstrip('=')
    wrapped = f'content://com.viber.voip.provider.internal_files/message/image/{token}?duplicate=true'
    assert viber._media_uri(wrapped) == inner                       # real shape, A14 message 19
    assert viber._media_uri('content://media/external_primary/images/media/1000000684') == \
        'content://media/external_primary/images/media/1000000684'
    assert viber._media_uri('content://com.viber.voip.provider.internal_files/newmedia?dlid=0-02') == ''
    assert viber._media_uri(None) == ''


def test_ios_whatsapp_chat_members():
    """Everyone in the chat, as at extraction (no per-message recipients)."""
    wa = _parser('ios', 'whatsapp')
    names = {'111@s.whatsapp.net': 'Liz', '222@s.whatsapp.net': 'Ed'}
    members = {7: [('999@s.whatsapp.net', 1), ('111@s.whatsapp.net', 1), ('222@s.whatsapp.net', 0)]}
    base = {'chat_id': 7, 'contact_jid': None, 'to_jid': None, 'group_member_jid': None}
    one = {**base, 'session_type': 0, 'contact_jid': '111@s.whatsapp.net'}
    for from_me in (0, 1):     # the same for every message of the chat
        assert wa._chat_members({**one, 'is_from_me': from_me}, '999@s.whatsapp.net', names, members) == \
            'Me (999@s.whatsapp.net), Liz (111@s.whatsapp.net)'
    group = {**base, 'session_type': 1}
    assert wa._chat_members(group, '999@s.whatsapp.net', names, members) == \
        'Me (999@s.whatsapp.net), Liz (111@s.whatsapp.net), Ed (222@s.whatsapp.net) (inactive)'
    assert wa._chat_members({**group, 'chat_id': 8}, None, names, members) == \
        'Me, [group members not recorded]'


def test_android_whatsapp_chat_type():
    wa = _parser('android', 'whatsapp')
    assert wa._chat_type('120363196890904472@newsletter') == 'Channel'
    assert wa._chat_type('628976424828-1512149678@g.us') == 'Group'
    assert wa._chat_type('19199282177@s.whatsapp.net') == '1:1'
    assert wa._chat_type(None) == ''


def test_ios_whatsapp_disappearing_metadata():
    wa = _parser('ios', 'whatsapp')
    # the two real CTF23 ZMETADATA blobs on message type 28
    assert wa._metadata_field(bytes.fromhex('a00280ceda03'), 36) == 90 * 86400
    assert wa._metadata_field(bytes.fromhex('a00280f524e80200'), 36) == 7 * 86400
    assert wa._metadata_field(b'not protobuf \xff', 36) is None
    assert wa._duration_label(7 * 86400) == '7 days' and wa._duration_label(0) == 'Off'


def test_deletion_activity_columns():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from artifact_viewer import _deletion_activity
    fmt = lambda t: str(t)
    day = 86400
    base = {'dfe_me': 0, 'dfe_them': 0, 'expiry_seen': False, 'has_sort': True, 'has_setting': True}
    # IOS17 Liz Dehner shape: one gap, no timer -> not explained
    liz = {**base, 'seq': [(22, 100), (23, 200), (25, 300)], 'issued': 26, 'settings': []}
    r = _deletion_activity(liz, 10 * day, fmt, 'Liz')
    assert r[0] == '26 / 3' and r[1] == 1 and r[2] == 0 and r[9] == 0 and r[10] == 1
    # CTF23 Felix Davey shape: 7-day timer set by the other party, 6 missing at end
    felix = {**base, 'seq': [(1, 0), (2, 0)], 'issued': 9, 'settings': [(0, False, 7 * day)]}
    r = _deletion_activity(felix, 26 * day, fmt, 'Felix')
    assert r[2] == 6 and r[5] == 'On (7 days)' and r[6] == 'Other party' and r[9] == 6 and r[10] == 0
