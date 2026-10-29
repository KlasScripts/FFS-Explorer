"""Report -> File Browser links for files a report refers to indirectly:
Android device paths, MediaStore/Viber URIs, and link columns holding
several paths (one per line)."""
import base64
import importlib.util
import os
import sqlite3

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
    assert wa._chat_type('12345@lid') == '1:1'
    assert wa._chat_type(None) == ''
    # jid_map direction on Android: lid_row_id is @lid, jid_row_id is the phone.
    # A group address and the lid_me sentinel are neither.
    assert wa._phone_and_lid('12345@lid', 7, '447700900123@s.whatsapp.net', 9) == (
        '447700900123@s.whatsapp.net', 9, '12345@lid', 7)
    assert wa._phone_and_lid('447700900123@s.whatsapp.net', 9, None, None) == (
        '447700900123@s.whatsapp.net', 9, None, None)
    assert wa._phone_and_lid('12345@lid', 7, None, None) == (None, None, '12345@lid', 7)
    assert wa._phone_and_lid('120363@g.us', 3, '447700900123@s.whatsapp.net', 9) == (
        None, None, None, None)
    assert wa._phone_and_lid('lid_me', 11, None, None) == (None, None, None, None)
    assert wa._owner_label('447700900123', 'k.example') == 'Me (+447700900123 @k.example)'
    assert wa._owner_label('447700900123', '@k.example') == 'Me (+447700900123 @k.example)'
    assert wa._owner_label('447700900123', None) == 'Me (+447700900123)'
    assert wa._owner_label(None, 'k.example') == 'Me (@k.example)'
    assert wa._owner_label(None, None) == 'Me'
    assert wa._with_poll_options('GTD-OUT-POLL', ['Alpha', 'Beta']) == 'GTD-OUT-POLL — Alpha | Beta'
    assert wa._with_poll_options('', ['Alpha']) == 'Alpha'
    assert wa._with_poll_options('hello', []) == 'hello'
    assert wa._with_poll_options(None, None) == ''
    assert wa._with_vote_count('GTD-OUT-POLL — Alpha | Beta', 0) == 'GTD-OUT-POLL — Alpha | Beta'
    assert wa._with_vote_count('GTD-OUT-POLL — Alpha | Beta', 1) == 'GTD-OUT-POLL — Alpha | Beta · 1 vote'
    assert wa._with_vote_count('GTD-OUT-POLL — Alpha | Beta', 2) == 'GTD-OUT-POLL — Alpha | Beta · 2 votes'
    assert wa._with_vote_count('', 1) == '1 vote'
    assert wa._location_fields(None, 59.9, 10.7, None, None, None, None) == (
        'Current location', 'Current location')
    assert wa._location_fields(None, 59.9, 10.7, 'Sentrum Scene', 'An address', 'http://example.test', None) == (
        'Selected place: Sentrum Scene', 'Selected place')
    assert wa._location_fields('meet here', 59.9, 10.7, None, None, None, None) == (
        'meet here — Current location', 'Current location')
    assert wa._location_fields(None, None, None, None, None, None, None) == ('', '')
    assert wa._location_fields(None, 59.9, 10.7, None, None, None, 900) == (
        'Live location', 'Live location')
    assert wa._call_phrase(0, 5, 125) == 'Voice call, Answered, 2 min 5 sec'
    assert wa._call_phrase(1, 2, 0) == 'Video call, Not answered'
    assert wa._call_phrase(1, 4, 0) == 'Video call, result 4'
    assert wa._call_phrase(0, 5, 3600) == 'Voice call, Answered, 1 h'
    assert wa._system_phrase('privacy') == 'Encryption notice'
    assert wa._system_phrase('blocked', blocked=1) == 'Contact blocked'
    assert wa._system_phrase('blocked', blocked=0) == 'Contact unblocked'
    assert wa._system_phrase('group') == ''
    assert wa._system_phrase('group', action=4, joined=1) == 'You were added'
    assert wa._system_phrase('group', action=4, joined=0) == ''
    assert wa._system_phrase('photo') == 'Group icon changed'
    assert wa._system_phrase('business', privacy_type=1) == 'Encryption notice'
    assert wa._system_phrase('business', privacy_type=2) == 'Business secure service'
    assert wa._system_phrase('group', action=5) == 'Left the group'
    assert wa._system_phrase('group', action=11, text='') == 'You created this group'
    assert wa._system_phrase('group', action=11, text='Family') == ''
    assert wa._system_phrase('group', action=14) == 'Removed from the group'
    assert wa._system_phrase('group', action=15) == "You're now an admin"
    assert wa._system_phrase('group', action=56, duration=86400) == 'Message timer updated, 24 hours'
    assert wa._system_phrase('group', action=56, duration=604800) == 'Message timer updated, 7 days'
    assert wa._system_phrase('group', action=56, duration=7776000) == 'Message timer updated, 90 days'
    assert wa._system_phrase('group', action=56, duration=0) == 'Disappearing messages off'
    assert wa._timer_phrase(86400) == 'Message timer updated, 24 hours'
    assert wa._timer_phrase(0) == 'Disappearing messages off'
    assert wa._system_phrase('', action=118) == 'Message pinned'
    assert wa._unknown_label(7, 4) == 'Unknown system action 4'
    assert wa._unknown_label(99) == 'Unknown message type 99'
    assert wa._unknown_label(None) == 'Unknown message'
    assert wa._system_phrase('') == ''
    assert wa._kind_phrase(1) == 'Photo'
    assert wa._kind_phrase(13) == 'GIF' and wa._kind_phrase(20) == 'Sticker'
    assert wa._kind_phrase(2) == ''


def test_android_whatsapp_poll_vote_rows(tmp_path):
    wa = _parser('android', 'whatsapp')
    msg = sqlite3.connect(tmp_path / 'msgstore.db')
    msg.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT, type INTEGER);
        CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                              text_data TEXT, timestamp INTEGER);
        CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, from_me INTEGER,
                                     sender_jid_row_id INTEGER, parent_message_row_id INTEGER,
                                     timestamp INTEGER, message_add_on_type INTEGER);
        CREATE TABLE message_poll_option (_id INTEGER PRIMARY KEY, message_row_id INTEGER, option_name TEXT);
        CREATE TABLE message_add_on_poll_vote_selected_option (
            _id INTEGER PRIMARY KEY, message_add_on_row_id INTEGER, message_poll_option_id INTEGER);
        INSERT INTO jid VALUES
            (2, '999', 'lid', '999@lid', 18),
            (3, '447700900123', 's.whatsapp.net', '447700900123@s.whatsapp.net', 0),
            (4, '120', 'g.us', '120@g.us', 1),
            (5, '555', 'lid', '555@lid', 18),
            (6, '447700900999', 's.whatsapp.net', '447700900999@s.whatsapp.net', 0),
            (8, '', 'lid_me', 'lid_me', 11);
        INSERT INTO jid_map VALUES (2, 3), (5, 6);
        INSERT INTO chat VALUES (1, 2, NULL), (2, 4, 'Lab group');
        INSERT INTO message VALUES
            (10, 1, 1, 'GTD-VOTE-POLL', 1000),
            (11, 1, 0, 'Lunch', 1500),
            (12, 2, 0, 'Pick', 1600);
        INSERT INTO message_add_on VALUES
            (1, 1, -1, 10, 1000, 67),
            (2, 0, -1, 11, 2000, 67),
            (3, 0, 5, 12, 3000, 67),
            (5, 0, -1, 10, 5000, 67),
            (6, 0, 8, 12, 6000, 67);
        INSERT INTO message_poll_option VALUES
            (100, 10, 'Alpha'), (102, 11, 'Bravo'), (103, 12, 'Alpha'), (104, 12, 'Beta');
        INSERT INTO message_add_on_poll_vote_selected_option VALUES
            (1, 1, 100), (2, 2, 102), (3, 3, 103), (4, 3, 104), (6, 6, 103);
    """)
    wa_db = sqlite3.connect(tmp_path / 'wa.db')
    wa_db.executescript("""
        CREATE TABLE wa_contacts (_id INTEGER PRIMARY KEY, jid TEXT, display_name TEXT,
                                  wa_name TEXT, given_name TEXT);
        INSERT INTO wa_contacts VALUES
            (1, '447700900123@s.whatsapp.net', 'Sam', NULL, NULL),
            (2, '447700900999@s.whatsapp.net', 'Ada', NULL, NULL);
    """)
    wa_db.commit()
    wa_db.close()
    msg.execute("ATTACH DATABASE ? AS contacts_db", (str(tmp_path / 'wa.db'),))
    me = 'Me (+447700900123 @k.example)'
    rows = wa.poll_vote_records(msg, me)
    msg.close()
    assert [(r['sent_time'], r['chat'], r['voter'], r['option'], r['direction']) for r in rows] == [
        (1000, 'Sam', me, 'Alpha', 'Sent'),
        (2000, 'Sam', 'Sam', 'Bravo', 'Received'),
        (3000, 'Lab group', 'Ada', 'Alpha', 'Received'),
        (3000, 'Lab group', 'Ada', 'Beta', 'Received'),
        (5000, 'Sam', 'Sam', '', 'Received'),
        (6000, 'Lab group', me, 'Alpha', 'Sent'),
    ]
    ada = rows[2]
    assert ada['chat_type'] == 'Group' and ada['phone_jid'] == '' and ada['voter_lid'].startswith('555@lid')
    assert '447700900999' in ada['voter_phone_jid']
    sam = rows[1]
    assert sam['chat_type'] == '1:1' and '447700900123' in sam['voter_phone_jid']
    assert '447700900123' in sam['phone_jid'] and sam['lid'].startswith('999@lid')
    assert rows[4]['option'] == '' and rows[4]['selected_id'] is None
    counts = {}
    for r in rows:
        counts[r['message_id']] = counts.get(r['message_id'], 0) + 1
    assert counts == {10: 2, 11: 1, 12: 3}
    assert wa.poll_vote_records(sqlite3.connect(':memory:'), 'Me') == []

    votes = _parser('android', 'whatsapp_poll_votes')
    prefs = tmp_path / 'prefs.xml'
    prefs.write_text(
        '<map><string name="registration_jid">447700900123</string>'
        '<string name="self_user_name">k.example</string></map>',
        encoding='utf-8')
    listed = votes.run({
        'msgstore': str(tmp_path / 'msgstore.db'),
        'wa': str(tmp_path / 'wa.db'),
        'prefs_light': str(prefs),
    })
    assert [r['voter'] for r in listed if r['direction'] == 'Sent'] == [me, me]


def test_android_whatsapp_reactions_name_each_person(tmp_path):
    wa = _parser('android', 'whatsapp')
    msg = sqlite3.connect(tmp_path / 'msgstore.db')
    msg.executescript("""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT, type INTEGER);
        CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                              text_data TEXT, timestamp INTEGER);
        CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, from_me INTEGER,
                                     sender_jid_row_id INTEGER, parent_message_row_id INTEGER,
                                     timestamp INTEGER, message_add_on_type INTEGER);
        CREATE TABLE message_add_on_reaction (message_add_on_row_id INTEGER PRIMARY KEY,
                                              reaction TEXT, sender_timestamp INTEGER);
        INSERT INTO jid VALUES
            (2, '999', 'lid', '999@lid', 18),
            (3, '447700900123', 's.whatsapp.net', '447700900123@s.whatsapp.net', 0),
            (4, '120', 'g.us', '120@g.us', 1),
            (5, '555', 'lid', '555@lid', 18),
            (6, '447700900999', 's.whatsapp.net', '447700900999@s.whatsapp.net', 0);
        INSERT INTO jid_map VALUES (2, 3), (5, 6);
        INSERT INTO chat VALUES (1, 2, NULL), (2, 4, 'Lab group');
        INSERT INTO message VALUES
            (10, 1, 1, 'GTD-SEND-1', 1000),
            (12, 2, 0, 'Pick', 1600);
        INSERT INTO message_add_on VALUES
            (1, 0, -1, 10, 2000, 56),
            (2, 0, 5, 12, 3000, 56),
            (3, 1, -1, 12, 4000, 56),
            (4, 0, -1, 10, 5000, 56),
            (5, 0, -1, 10, 6000, 67);
        INSERT INTO message_add_on_reaction VALUES
            (1, '👍', 2042), (2, '❤️', 3010), (3, '😂', 4005), (4, '', 5001);
    """)
    wa_db = sqlite3.connect(tmp_path / 'wa.db')
    wa_db.executescript("""
        CREATE TABLE wa_contacts (_id INTEGER PRIMARY KEY, jid TEXT, display_name TEXT,
                                  wa_name TEXT, given_name TEXT);
        INSERT INTO wa_contacts VALUES
            (1, '447700900123@s.whatsapp.net', 'Sam', NULL, NULL),
            (2, '447700900999@s.whatsapp.net', 'Ada', NULL, NULL);
    """)
    wa_db.commit()
    wa_db.close()
    msg.execute("ATTACH DATABASE ? AS contacts_db", (str(tmp_path / 'wa.db'),))
    # 1:1 reaction with no sender is the chat partner. A blank emoji and
    # the poll vote (type 67) are not reactions.
    at = ' 00:00:{:02d} 01 Jan 1970 UTC'.format
    assert wa.reaction_summaries(msg, 'Me') == {
        10: ('👍 Sam' + at(2), 2000),
        12: ('❤️ Ada' + at(3) + ', 😂 Me' + at(4), 4000),
    }
    assert wa.reaction_summaries(sqlite3.connect(':memory:'), 'Me') == {}
    msg.close()


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


def test_chat_summary_columns_follow_the_android_declaration():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from artifact_viewer import _chat_summary_columns, _chat_summary_extra
    wa = _parser('android', 'whatsapp')
    android = wa.chat_groups
    ios = _parser('ios', 'whatsapp').chat_groups
    viber = _parser('android', 'viber').chat_groups
    a_cols = _chat_summary_columns(android)
    i_cols = _chat_summary_columns(ios)
    assert 'Missing (Gaps)' not in a_cols and 'Disappearing' not in a_cols
    assert 'Deleted For Everyone (Me/Them)' not in a_cols and 'Calls' in a_cols
    assert 'Missing (Gaps)' in i_cols and 'Disappearing' in i_cols
    assert 'Deleted For Everyone (Me/Them)' in i_cols
    assert 'Calls' in _chat_summary_columns(viber) and 'Missing (Gaps)' not in _chat_summary_columns(viber)
    act = ('issued', 1, 0, 'window', '1/0', 'On (24 hours)', 'Me', 't', 'hist', 2, 3)
    assert _chat_summary_extra(act, android) == ()
    assert _chat_summary_extra(act, ios) == act
    assert a_cols[0] == 'Icon' and 'Icon' not in i_cols
    messages = _parser('android', 'google_messages').chat_groups
    m_cols = _chat_summary_columns(messages)
    assert m_cols[0] == 'conversation_id'
    assert 'Last Incoming Notification' not in m_cols
    assert 'Last Incoming Notification' not in a_cols
    assert 'Last Incoming Notification' not in i_cols
    assert len(a_cols) == 19 and len(i_cols) == len(act) + 17
    paths = {"_zip_names": ["data/data/com.whatsapp/files/Avatars/120@g.us.j"]}
    assert wa._avatar_ui_path(paths, "120@g.us").endswith("120@g.us.j")
    assert wa._avatar_ui_path(paths, "447700900123@s.whatsapp.net") == ""
    assert wa._avatar_ui_path(paths, "missing@g.us") == ""


def test_notes_pages_open_with_the_court_warning():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from artifact_viewer import _notes_markdown_html
    html = _notes_markdown_html("# WhatsApp Messages (Android): read this first\n\nA message row.")
    banner = html.lower().find("should not be used in court")
    title = html.lower().find("read this first")
    assert banner != -1 and title != -1 and banner < title
    assert "work in progress" in html.lower()
    assert "last incoming notification" not in html.lower()
    assert "Warning" not in html
    warned = _notes_markdown_html("# Notes\n\nText.", "Experimental, lead only.")
    assert warned.find("lead only") < warned.find("<h1")
    assert warned.find("should not be used in court") < warned.find("lead only")


def test_phone_and_lid_are_shared_by_group_and_one_to_one():
    wa = _parser('android', 'whatsapp')
    partner = ('447700900456@s.whatsapp.net', 11, '999@lid', 7)
    sender = ('447700900123@s.whatsapp.net', 10, '555@lid', 18)
    both = ('447700900456@s.whatsapp.net (11)', '999@lid (7)')
    assert wa._party_addresses(False, False, partner, sender) == both
    assert wa._party_addresses(False, True, partner, sender) == both
    assert wa._party_addresses(True, False, partner, sender) == (
        '447700900123@s.whatsapp.net (10)', '555@lid (18)')
    assert wa._party_addresses(True, True, partner, sender) == ('', '')
    assert wa._party_addresses(
        True, False, partner, ('447700900123@s.whatsapp.net', 10, None, None)
    ) == ('447700900123@s.whatsapp.net (10)', '')


def test_template_wording_is_read_from_its_own_table():
    wa = _parser('android', 'whatsapp')
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE message_template (
            message_row_id INTEGER, content_text_data TEXT, footer_text_data TEXT);
        CREATE TABLE message_template_button (
            _id INTEGER, message_row_id INTEGER, text_data TEXT);
        INSERT INTO message_template VALUES (5, 'Your code is 000000', 'Do not share this code');
        INSERT INTO message_template_button VALUES (1, 5, 'Copy code');
    """)
    assert wa._load_template_text(conn) == {
        5: 'Your code is 000000\nDo not share this code\nCopy code'}
    assert wa._load_template_text(sqlite3.connect(':memory:')) == {}


def test_a_missing_subtype_table_still_names_the_encryption_notice():
    wa = _parser('android', 'whatsapp')
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE message (_id INTEGER PRIMARY KEY, text_data TEXT);
        CREATE TABLE message_system (message_row_id INTEGER, action_type INTEGER);
        CREATE TABLE message_system_initial_privacy_provider (
            message_row_id INTEGER, privacy_provider INTEGER);
        INSERT INTO message VALUES (1, NULL);
        INSERT INTO message_system VALUES (1, 67);
        INSERT INTO message_system_initial_privacy_provider VALUES (1, 0);
    """)
    assert wa._load_system_phrases(conn) == {1: 'Encryption notice'}


def test_a_column_blank_in_the_open_chat_is_hidden():
    import report_columns_store as store
    conn = sqlite3.connect(':memory:')
    conn.execute(
        'CREATE TABLE r (chat_id INT, chat_subject TEXT, text TEXT)')
    conn.execute("INSERT INTO r VALUES (1, 'Family', 'hello')")
    conn.execute("INSERT INTO r VALUES (2, '', 'there')")
    cols = ['chat_subject', 'text']
    whole = store.nonempty_column_names(conn, 'r', cols)
    one = store.nonempty_column_names(
        conn, 'r', cols, where='"chat_id" = ?', args=(2,))
    assert whole == {'chat_subject', 'text'}
    assert one == {'text'}
    assert store.default_visible_columns(cols, [], one) == ['text']
    # A group stores its name on every message. Inside that one chat the
    # column only repeats the view title, so it is left out.
    group = store.nonempty_column_names(
        conn, 'r', cols, where='"chat_id" = ?', args=(1,))
    assert store.columns_for_this_view(cols, group, one_chat=True) == ['text']
    assert store.columns_for_this_view(cols, group, one_chat=False) == cols
    party = ['chat_jid', 'phone_jid', 'lid', 'text']
    filled = set(party)
    assert store.columns_for_this_view(party, filled, one_chat=True, direct=True) == [
        'phone_jid', 'lid', 'text']
    assert store.columns_for_this_view(party, filled, one_chat=True, direct=False) == party
    assert store.columns_for_this_view(party, filled, one_chat=False, direct=True) == party


def test_gboard_clipboard_leaves_out_the_seeded_instructions():
    import tempfile
    gboard = _parser('android', 'gboard_clipboard')
    with tempfile.TemporaryDirectory() as folder:
        db = os.path.join(folder, 'gboard_clipboard.db')
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE clips (_id INTEGER, text TEXT, html_text TEXT, "
            "item_type INTEGER, entity_type INTEGER, timestamp INTEGER, "
            "sensitive INTEGER, uri TEXT, group_id INTEGER)")
        seeded = [
            (6, "Use the edit icon to pin, add or delete clips."),
            (10, "Touch and hold a clip to pin it. Unpinned clips will be deleted after 1 hour."),
            (18, "Tap on a clip to paste it in the text box."),
            (34, "Welcome to Gboard clipboard, any text you copy will be saved here."),
        ]
        for item_type, text in seeded:
            conn.execute(
                "INSERT INTO clips VALUES (?, ?, NULL, ?, 0, 1740695131220, NULL, '', NULL)",
                (item_type, text, item_type))
        conn.execute(
            "INSERT INTO clips VALUES (99, 'GTD-CLIP-REAL', NULL, 1, 0, 1740695200000, NULL, '', NULL)")
        conn.commit()
        conn.close()
        rows = gboard.run({
            "clipboard_db": db,
            "_app_base_ui_path": "data/data/com.google.android.inputmethod.latin",
        })
    assert [row["text"] for row in rows] == ["GTD-CLIP-REAL"]


def _varint(value):
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        if value:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def _proto_field(number, payload):
    if isinstance(payload, int):
        return _varint((number << 3) | 0) + _varint(payload)
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def test_usagestats_reads_the_event_once_from_the_finest_interval():
    usage = _parser('android', 'usagestats')
    # Token 7's string list: package name, then the channel id at index 1.
    mapping = _proto_field(2, _proto_field(1, 7) + _proto_field(2, b"com.whatsapp") + _proto_field(2, b"individual_chat_defaults_3"))
    begin = 1_000_000
    event = (
        _proto_field(1, 7)
        + _proto_field(3, 2500)
        + _proto_field(5, 12)
        + _proto_field(9, 2)
    )
    stats = _proto_field(22, event)
    older = _proto_field(22, _proto_field(1, 7) + _proto_field(3, 100) + _proto_field(5, 1))
    files = {
        "Dump/data/system_ce/0/usagestats/mappings": mapping,
        f"Dump/data/system_ce/0/usagestats/daily/{begin}": stats,
        f"Dump/data/system_ce/0/usagestats/weekly/{begin}": stats,
        f"Dump/data/system_ce/0/usagestats/yearly/{begin - 500_000}": older,
    }
    rows = usage.run({
        "_zip_names": list(files),
        "_read_zip_bytes": files.get,
    })
    assert [(row["interval"], row["event_type"], row["package"], row["notification_channel"], row["event_time"]) for row in rows] == [
        ("yearly", "ACTIVITY_RESUMED", "com.whatsapp", "", begin - 500_000 + 100),
        ("daily", "NOTIFICATION_INTERRUPTION", "com.whatsapp", "individual_chat_defaults_3", begin + 2500),
    ]
    assert all(row["conversation_id"] == "" for row in rows)


def test_usagestats_shortcut_and_locus_rows_keep_the_conversation_id():
    usage = _parser('android', 'usagestats')
    # Token 9: package, then shortcut "10", "11", and locus "20".
    mapping = _proto_field(2, _proto_field(1, 9) + _proto_field(2, b"com.google.android.apps.messaging")
                           + _proto_field(2, b"10") + _proto_field(2, b"11") + _proto_field(2, b"20"))
    begin = 2_000_000

    def shortcut(token):
        return _proto_field(22, _proto_field(1, 9) + _proto_field(3, 400) + _proto_field(5, 8) + _proto_field(7, token))
    locus = _proto_field(22, _proto_field(1, 9) + _proto_field(3, 900) + _proto_field(5, 30) + _proto_field(13, 4))
    files = {
        "Dump/data/system_ce/0/usagestats/mappings": mapping,
        f"Dump/data/system_ce/0/usagestats/daily/{begin}": shortcut(2) + shortcut(3) + locus,
    }
    rows = usage.run({"_zip_names": list(files), "_read_zip_bytes": files.get})
    # Two chats at the same millisecond stay two rows.
    assert [(row["event_type"], row["conversation_id"]) for row in rows] == [
        ("SHORTCUT_INVOCATION", "10"),
        ("SHORTCUT_INVOCATION", "11"),
        ("LOCUS_ID_SET", "20"),
    ]
