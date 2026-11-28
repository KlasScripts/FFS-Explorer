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
