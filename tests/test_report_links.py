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


def test_ios_whatsapp_recipients():
    wa = _parser('ios', 'whatsapp')
    names = {'111@s.whatsapp.net': 'Liz', '222@s.whatsapp.net': 'Ed'}
    members = {7: [('999@s.whatsapp.net', 1), ('111@s.whatsapp.net', 1), ('222@s.whatsapp.net', 0)]}
    base = {'chat_id': 7, 'contact_jid': None, 'to_jid': None, 'group_member_jid': None}
    one = {**base, 'session_type': 0, 'contact_jid': '111@s.whatsapp.net'}
    assert wa._recipients({**one, 'is_from_me': 1}, '999@s.whatsapp.net', names, members) == \
        'Liz (111@s.whatsapp.net)'
    assert wa._recipients({**one, 'is_from_me': 0}, '999@s.whatsapp.net', names, members) == \
        'Me (999@s.whatsapp.net)'
    group = {**base, 'session_type': 1}
    assert wa._recipients({**group, 'is_from_me': 1}, '999@s.whatsapp.net', names, members) == \
        'Liz (111@s.whatsapp.net), Ed (222@s.whatsapp.net) (inactive)'
    assert wa._recipients({**group, 'is_from_me': 0, 'group_member_jid': '111@s.whatsapp.net'},
                          '999@s.whatsapp.net', names, members) == \
        'Me (999@s.whatsapp.net), Ed (222@s.whatsapp.net) (inactive)'
    assert wa._recipients({**group, 'chat_id': 8, 'is_from_me': 1}, None, names, members) == \
        '[group members not recorded]'


def test_android_whatsapp_chat_type():
    wa = _parser('android', 'whatsapp')
    assert wa._chat_type('120363196890904472@newsletter') == 'Channel'
    assert wa._chat_type('628976424828-1512149678@g.us') == 'Group'
    assert wa._chat_type('19199282177@s.whatsapp.net') == '1:1'
    assert wa._chat_type(None) == ''
