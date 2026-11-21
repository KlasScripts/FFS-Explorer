"""What opening a report needs beyond its rows is worked out when the
report is made and saved (report_extras.build_all): which columns have
values (whole report and each chat), the media paths, the row count and
the Chat Groups summary, so opening a report never scans every row
(2026-11-21: 5.15 s, 2.65 s, 4 s and 17 s for 500,000 messages)."""
import os
import sqlite3
import types

import chat_flags
import report_columns_store as rcs
import report_extras as rx

COLS = ['chat_jid', 'chat_subject', 'direction', 'message', 'media', 'note', 'ts']
ROWS = [('a', 'Alice', 'Sent', 'hi', '', None, '1700000000'),
        ('a', 'Alice', 'Received', 'pic', 'm/p1.jpg', '', '1700000100'),
        ('a', 'Alice', 'Received', 'two', 'm/p1.jpg\nm/p2.jpg', '', '1700000200'),
        ('b', 'Bob', 'Received', 'https://x.example', '', '  ', '1700000300'),
        ('', '', 'Received', 'lost', '', 'n', ''),
        (None, None, 'Sent', 'x', None, None, None)]

MOD = types.SimpleNamespace(
    chat_groups={'key': 'chat_jid', 'name': 'chat_subject', 'direction': 'direction',
                 'media': ['media'], 'time': 'ts'},
    media_fields=['media'], timestamp_fields={'ts': 's'})


def _case(tmp_path, rows=ROWS):
    case = str(tmp_path)
    c = sqlite3.connect(os.path.join(case, 'caseresults.db'))
    c.execute('CREATE TABLE "artifact_chat" (' + ', '.join(f'"{x}" TEXT' for x in COLS) + ')')
    c.executemany(f'INSERT INTO "artifact_chat" VALUES ({",".join("?" * len(COLS))})', rows)
    c.commit()
    c.close()
    return case


def _stamp(case):
    return chat_flags.report_stamp(case, 'chat')


def test_filled_columns_match_the_live_check(tmp_path):
    case = _case(tmp_path)
    rx.build_all(case, 'chat', MOD)
    conn = sqlite3.connect(os.path.join(case, 'caseresults.db'))
    for scope, where, args in [(rcs.WHOLE_REPORT, None, ()),
                               (rcs.chat_scope_key('a'), '"chat_jid" = ?', ['a']),
                               (rcs.chat_scope_key('b'), '"chat_jid" = ?', ['b']),
                               (rcs.chat_scope_key(None), '("chat_jid" IS NULL OR "chat_jid" = \'\')', ())]:
        saved = set(rx.load(case, 'chat', _stamp(case), 'filled:' + scope))
        assert saved == rcs.nonempty_column_names(conn, 'artifact_chat', COLS, where, args), scope


def test_rows_media_and_chat_index(tmp_path):
    case = _case(tmp_path)
    rx.build_all(case, 'chat', MOD)
    st = _stamp(case)
    assert rx.load(case, 'chat', st, 'rows') == len(ROWS)
    assert sorted(rx.load(case, 'chat', st, 'media')['media']) == ['m/p1.jpg', 'm/p1.jpg\nm/p2.jpg']
    index = rx.load(case, 'chat', st, 'chat_index')
    assert index == [['', '[no conversation]', 2], ['a', 'Alice', 3], ['b', 'Bob', 1]]


def test_chat_groups_saved_with_bookmarks_counted_at_display(tmp_path):
    case = _case(tmp_path)
    rx.build_all(case, 'chat', MOD)
    chats = rx.chat_groups(case, 'chat', _stamp(case), set())
    a = next(c for c in chats if c['key'] == 'a')
    assert (a['messages'], a['incoming'], a['outgoing'], a['media_in']) == (3, 2, 1, 2)
    assert a['times'] == [1700000000, 1700000100, 1700000200] and a['bookmarked'] == 0
    assert next(c for c in chats if c['key'] == 'b')['links'] == 1
    # the row with two bookmarked paths counts once
    chats = rx.chat_groups(case, 'chat', _stamp(case), {'m/p1.jpg', 'm/p2.jpg'})
    assert next(c for c in chats if c['key'] == 'a')['bookmarked'] == 2
    # same result as building it directly
    conn = sqlite3.connect(os.path.join(case, 'caseresults.db'))
    direct, links = rx.build_chat_groups(conn, 'chat', MOD.chat_groups, MOD)
    rx.apply_bookmarks(direct, links, {'m/p1.jpg', 'm/p2.jpg'})
    assert chats == rx._tuples(direct)


def test_a_rerun_is_never_matched_with_older_extras(tmp_path):
    case = _case(tmp_path)
    rx.build_all(case, 'chat', MOD)
    c = sqlite3.connect(os.path.join(case, 'caseresults.db'))
    c.execute('INSERT INTO "artifact_chat" (chat_jid, message) VALUES (?,?)', ('c', 'new'))
    c.commit()
    c.close()
    assert rx.load(case, 'chat', _stamp(case), 'rows') is None
    assert rx.chat_groups(case, 'chat', _stamp(case), set()) is None


def test_report_without_chats_or_media(tmp_path):
    case = _case(tmp_path)
    got = rx.build_all(case, 'chat', types.SimpleNamespace())
    assert 'chat_groups' not in got and got['media'] == {}
    assert set(got['filled:' + rcs.WHOLE_REPORT]) == set(COLS)


def test_stamp_is_the_last_rowid_not_a_count(tmp_path):
    case = _case(tmp_path)
    assert _stamp(case).endswith(f'|{len(ROWS)}|{len(ROWS)}')


def test_an_older_saved_layout_is_never_read(tmp_path, monkeypatch):
    case = _case(tmp_path)
    rx.build_all(case, 'chat', MOD)
    monkeypatch.setattr(rx, 'EXTRAS_VERSION', rx.EXTRAS_VERSION + 1)
    assert rx.load(case, 'chat', _stamp(case), 'rows') is None
