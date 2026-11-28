"""report_type = "recovered_records": a declaration-only parser's rows,
checked against what the real SQLite engine wrote (a WAL database copied
while the app had it open)."""
import os
import shutil
import sqlite3
from types import SimpleNamespace

import artifact_runner as ar


def _chat_db(folder):
    """20 messages checkpointed; then in the WAL: message 3's text edited,
    message 4's status changed only, message 5 deleted, chat 1's preview
    set to a text no message has. Returns the copied path."""
    src = os.path.join(folder, 'work.db')
    c = sqlite3.connect(src, isolation_level=None)
    c.execute('pragma journal_mode=wal')
    c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table msg(id integer primary key, chat integer, sender integer, '
              'body text, kind integer, status integer, ts integer)')
    c.execute('create table chat(id integer primary key, preview text)')
    for i in range(1, 21):
        c.execute('insert into msg values(?,?,?,?,?,?,?)',
                  (i, 1, None if i % 2 else 7, f'message number {i} ' + 'x' * 30, 0, 1,
                   1700000000000 + i))
    for i in range(1, 6):
        c.execute('insert into chat values(?,?)', (i, f'message number {i} ' + 'x' * 30))
    c.execute('pragma wal_checkpoint(truncate)')
    c.execute("update msg set body='edited text of three' where id=3")
    c.execute('update msg set status=2 where id=4')
    c.execute('delete from msg where id=5')
    c.execute("update chat set preview='Call time 1:36' where id=1")
    c.execute("update chat set preview='message number 7 ' || substr('xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 1, 30) where id=1")
    out = os.path.join(folder, 'phone', 'chat.db')
    os.makedirs(os.path.dirname(out))
    for s in ('', '-wal'):
        shutil.copyfile(src + s, out + s)
    c.close()
    return out


MODULE = SimpleNamespace(recovered_records=[
    {'file_key': 'db', 'table': 'msg', 'label': 'Message',
     'fields': {'ts': 'timestamp', 'body': 'message', 'kind': 'content_type', 'sender': 'sender_id'},
     'version_fields': ['body', 'kind', 'sender', 'chat'],
     'labels': {'kind': {0: 'text'}},
     'derived': {'direction': {'from': 'sender', 'values': {None: 'Outgoing'}, 'else': 'Incoming'}}},
    {'file_key': 'db', 'table': 'chat', 'label': 'Chat preview',
     'fields': {'preview': 'message'}, 'version_fields': ['preview'],
     'not_found_in': {'preview': ['msg', 'body']}},
])


def test_rows_and_filters(tmp_path):
    path = _chat_db(str(tmp_path))
    ar.take_open_notes()
    rows = ar.recovered_records_report(MODULE, {'db': path})
    notes = ar.take_open_notes()
    msgs = {(r['status'], r['row_id']): r for r in rows if r['record'] == 'Message'}
    # deleted: whole row, labelled and derived
    d = msgs[('Deleted', 5)]
    assert d['message'] == 'message number 5 ' + 'x' * 30
    assert d['content_type'] == 'text (0)' and d['direction'] == 'Outgoing'
    assert d['raw_file'] == 'main' and d['raw_offset']
    # changed text: earlier version listed with the current value beside it
    ch = msgs[('Changed', 3)]
    assert ch['message'] == 'message number 3 ' + 'x' * 30
    assert ch['message (now)'] == 'edited text of three' and ch['changed_fields'] == 'message'
    # status-only change: not listed, counted in the run log
    assert ('Changed', 4) not in msgs
    assert any('differ only in columns other than' in n for n in notes)
    # every listed message row is deleted or changed in a declared column
    assert set(msgs) == {('Deleted', 5), ('Changed', 3)}
    # chat previews: only the one whose text is in no message
    previews = [r['message'] for r in rows if r['record'] == 'Chat preview']
    assert previews == ['Call time 1:36']
    assert any('already in the report or the live table' in n for n in notes)


def test_declaration_only_parser_gets_a_run(tmp_path):
    """The loader gives such a parser its run() and a record_source; one
    declared in its main report's deleted_tables is its own report, one
    table each, run with the main report (A-95, 2026-11-27)."""
    mods = dict(ar.load_artifacts('ios')[0])
    m = mods['line_deleted_messages']
    assert callable(m.run)
    assert [e['table'] for e in m.record_source] == ['ZMESSAGE']
    assert [d.__name__ for d in mods['line'].deleted_reports] == [
        'line_deleted_messages', 'line_deleted_chats', 'line_deleted_users']
    assert mods['line_deleted_chats'].files_folder == mods['line'].name
    assert 'damaged' in m.core_fields


def test_derived_lookup_of_the_local_users_ids(tmp_path):
    """derived 'in': Outgoing when the sender is one of the ids a where-clause
    selects in another table (ids that differ per phone); no field of that
    table is added to the row."""
    db = str(tmp_path / 'm.db')
    c = sqlite3.connect(db, isolation_level=None)
    c.execute('create table member(id integer primary key, me integer, name text)')
    c.executemany('insert into member values(?,?,?)', [(1, 0, 'a'), (2, 1, 'b'), (3, 0, 'c')])
    c.execute('create table msg(id integer primary key, sender integer, body text)')
    for i in range(1, 31):
        c.execute('insert into msg values(?,?,?)', (5000 + i, 2 if i % 2 else 3, f'message body {i:02d} ' + 'z' * 20))
    c.execute('delete from msg where id in (5003, 5004)')
    c.close()
    module = SimpleNamespace(recovered_records=[{
        'file_key': 'db', 'table': 'msg', 'label': 'Message',
        'fields': {'body': 'message', 'sender': 'sender_id'},
        'derived': {'direction': {'from': 'sender',
                                  'in': {'table': 'member', 'column': 'id', 'where': 'me = 1'},
                                  'then': 'Outgoing', 'else': 'Incoming'}}}])
    rows = ar.recovered_records_report(module, {'db': db})
    got = {r['message'][:15]: r['direction'] for r in rows}
    assert got == {'message body 03': 'Outgoing', 'message body 04': 'Incoming'}
    assert all('name' not in r for r in rows)


def test_identity_ties_a_rewritten_row_to_its_live_row(tmp_path):
    """'identity': a record whose row id is no longer live but whose unique
    key is a live row's (the app deleted it and wrote it again under a new
    id) is an earlier version of that row: Changed when a column that
    matters differs, counted when not; a really deleted one stays Deleted."""
    src = str(tmp_path / 'work.db')
    c = sqlite3.connect(src, isolation_level=None)
    c.execute('pragma journal_mode=wal')
    c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table item(id integer primary key, uuid text, body text, seen integer)')
    for i in range(1, 21):
        c.execute('insert into item values(?,?,?,?)',
                  (i, f'uuid-{i:04d}-abcdef', f'body text of item {i} ' + 'y' * 30, 0))
    c.execute('pragma wal_checkpoint(truncate)')
    # 3: rewritten under a new id with a new body; 4: rewritten, only seen changed
    c.execute('delete from item where id in (3, 4)')
    c.execute("insert into item values(21, 'uuid-0003-abcdef', 'new body of three', 0)")
    c.execute("insert into item values(22, 'uuid-0004-abcdef', ?, 1)",
              ('body text of item 4 ' + 'y' * 30,))
    c.execute('delete from item where id = 5')                   # really gone
    out = str(tmp_path / 'p' / 'item.db')
    os.makedirs(os.path.dirname(out))
    for s in ('', '-wal'):
        shutil.copyfile(src + s, out + s)
    c.close()
    module = SimpleNamespace(recovered_records=[{
        'file_key': 'db', 'table': 'item', 'label': 'Item', 'identity': ['uuid'],
        'fields': {'id': 'item_id', 'uuid': 'uuid', 'body': 'body', 'seen': 'seen'},
        'version_fields': ['uuid', 'body']}])
    ar.take_open_notes()
    rows = ar.recovered_records_report(module, {'db': out})
    notes = ar.take_open_notes()
    got = {(r['status'], r['uuid']): r for r in rows}
    assert set(got) == {('Changed', 'uuid-0003-abcdef'), ('Deleted', 'uuid-0005-abcdef')}
    ch = got[('Changed', 'uuid-0003-abcdef')]
    assert ch['row_id'] == 21 and ch['raw_rowid'] == 3
    assert ch['body (now)'] == 'new body of three' and ch['changed_fields'] == 'body'
    assert 'stored under row id 3, no longer live: tied to live row 21 by uuid' in ch['how_found']
    assert any('2 record(s) with no row id, or one no longer live' in n for n in notes)
    assert any('differ only in columns other than' in n for n in notes)


def test_deletion_log_via_check_and_fallback(tmp_path):
    """deletion_log lists the app's own record of a deleted row; via_check
    compares the join row's date; fallback infers the chat from the handle
    only when that handle is in one chat."""
    src = str(tmp_path / 'work.db')
    c = sqlite3.connect(src, isolation_level=None)
    c.execute('pragma journal_mode=wal')
    c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table message(ROWID integer primary key autoincrement, guid text, '
              'handle_id integer, date integer, text text)')
    c.execute('create table chat(ROWID integer primary key autoincrement, ident text)')
    c.execute('create table chat_message_join(chat_id integer, message_id integer, message_date integer)')
    c.execute('create table chat_handle_join(chat_id integer, handle_id integer)')
    c.execute('create table sync_deleted_messages(ROWID integer primary key autoincrement, '
              'guid text, recordID text)')
    c.execute("insert into chat values(1, 'one'), (2, 'two'), (3, 'three')")
    c.execute('insert into chat_handle_join values(1, 10), (2, 20), (3, 20)')   # 20: two chats
    for i in range(1, 21):
        h = 10 if i % 2 else 20
        c.execute('insert into message values(?,?,?,?,?)',
                  (i, f'GUID-{i:04d}', h, 1000 + i, f'message text number {i} ' + 'q' * 30))
        c.execute('insert into chat_message_join values(?,?,?)', (1 if h == 10 else 2, i, 1000 + i))
    c.execute('pragma wal_checkpoint(truncate)')
    # 5 and 6: their join rows wiped for good first (zeroed, then checkpointed),
    # so only the fallback can name a chat; 7: its join row recoverable
    c.execute('pragma secure_delete=on')
    c.execute('delete from chat_message_join where message_id in (5, 6)')
    c.execute('pragma wal_checkpoint(truncate)')
    c.execute('pragma secure_delete=off')
    for i in (5, 6, 7):
        c.execute('delete from message where ROWID=?', (i,))
        if i == 7:
            c.execute('delete from chat_message_join where message_id=?', (i,))
        c.execute("insert into sync_deleted_messages(guid) values(?)", (f'GUID-{i:04d}',))
    c.execute("insert into sync_deleted_messages(guid, recordID) values('GUID-GONE', 'rec-1')")
    out = str(tmp_path / 'p' / 'sms.db')
    os.makedirs(os.path.dirname(out))
    for s in ('', '-wal'):
        shutil.copyfile(src + s, out + s)
    c.close()
    module = SimpleNamespace(recovered_records=[{
        'file_key': 'db', 'table': 'message', 'label': 'Message',
        'fields': {'ROWID': 'message_id', 'guid': 'guid', 'handle_id': 'handle_id',
                   'date': 'date', 'text': 'message'},
        'version_fields': ['text'],
        'links': [{'name': 'linked_chat', 'what': 'chat', 'column': 'ROWID',
                   'via': ['chat_message_join', 'message_id', 'chat_id'],
                   'via_check': {'column': 'date', 'join_column': 'message_date'},
                   'fallback': {'via': ['chat_handle_join', 'handle_id', 'chat_id'],
                                'column': 'handle_id', 'what': 'handle'},
                   'none': [0], 'table': 'chat', 'key': 'ROWID'}],
        'deletion_log': {'table': 'sync_deleted_messages', 'key': 'guid', 'column': 'guid',
                         'fields': {'recordID': 'cloudkit_record_id'}}}])
    rows = ar.recovered_records_report(module, {'db': out})
    by = {r['guid']: r for r in rows if r['status'] != 'Deleted (recorded by the app)'}
    assert by['GUID-0005']['status'] == 'Deleted'
    assert by['GUID-0005']['linked_chat'] == (
        "no chat_message_join row for 5; inferred, not stored: chat 1: live "
        "(handle 10 is in this one chat only, by chat_handle_join)")
    assert by['GUID-0006']['linked_chat'] == (
        "no chat_message_join row for 6; not inferred: handle 20 is in 2 chats (chat_handle_join)")
    assert by['GUID-0007']['linked_chat'] == (
        "chat 1: live (recovered chat_message_join row, its message_date is this record's date)")
    logged = [r for r in rows if r['status'] == 'Deleted (recorded by the app)']
    assert [r['guid'] for r in logged] == ['GUID-0005', 'GUID-0006', 'GUID-0007', 'GUID-GONE']
    gone = [r for r in logged if r['guid'] == 'GUID-GONE'][0]
    assert gone['cloudkit_record_id'] == 'rec-1'
    assert 'no record of it was recovered' in gone['how_found']
    assert [r for r in logged if r['guid'] == 'GUID-0005'][0]['how_found'].endswith(
        'its record was also recovered (listed in this report)')
