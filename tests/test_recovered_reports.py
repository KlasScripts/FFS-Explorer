"""Main reports hold live rows only; deleted and changed records are in a
separate report_type = "recovered_records" report per app (ASSUMPTIONS.md
A-95, 2026-11-21). recoverable_tables carved on every run: 23.9 s of 74 s
for 500,000 WhatsApp messages."""
import glob
import importlib.util
import json
import os
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARSERS = sorted(glob.glob(os.path.join(ROOT, 'artifacts', '*', '*.py')))


def _load(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize('path', PARSERS, ids=lambda p: os.path.relpath(p, ROOT))
def test_no_main_report_carves(path):
    m = _load(path)
    for retired in ('recoverable_tables', 'recovery_source_labels', 'recovery_field_notes'):
        assert not hasattr(m, retired), f'{retired} is retired: use a recovered_records report'


RECOVERED = [p for p in PARSERS if 'report_type = "recovered_records"' in open(p, encoding='utf-8').read()]


def _loaded():
    import artifact_runner as ar
    out, errors = {}, []
    for plat in ('android', 'ios'):
        mods, errs = ar.load_artifacts(plat)
        errors += errs
        out.update({os.path.join(ROOT, 'artifacts', plat, n + '.py'): m for n, m in mods})
    return out, errors


def test_every_report_loads_and_deleted_of_resolves():
    _mods, errors = _loaded()
    assert not errors, errors


def _recovered_names():
    import artifact_runner as ar
    return [(plat, n) for plat in ('android', 'ios') for n, m in ar.load_artifacts(plat)[0]
            if getattr(m, 'report_type', None) == 'recovered_records']


@pytest.mark.parametrize('plat,script', _recovered_names(), ids=lambda v: str(v))
def test_recovered_report_is_declared_consistently(plat, script):
    """Every deleted-records report, a file of its own or declared in its
    main report's deleted_tables: one table (A-95, 2026-11-27), its file a
    file of the report, its fields and times consistent."""
    import artifact_runner as ar
    m = dict(ar.load_artifacts(plat)[0])[script]      # as the app loads it (deleted_of resolved)
    path = os.path.join(ROOT, 'artifacts', plat, script + '.py')
    if os.path.exists(path):
        assert 'def run(' not in open(path, encoding='utf-8').read(), 'a recovered_records report has no run()'
    assert len(m.recovered_records) == 1, 'one table per deleted-records report (A-95)'
    keys = set(m.files) | set(getattr(m, 'optional_files', {}) or {})
    names = set()
    for spec in m.recovered_records:
        assert spec['file_key'] in keys, spec['file_key']
        assert spec.get('fields'), spec['table']
        assert set(spec.get('version_fields') or ()) <= set(spec['fields']), spec['table']
        assert set(spec.get('json_fields') or ()) <= set(spec['fields']), spec['table']
        assert set(spec.get('blob_files') or ()) <= set(spec['fields']), spec['table']
        names |= set(spec['fields'].values())
        names |= set((spec.get('blob_files') or {}).values())
        names |= {lk['name'] for lk in spec.get('links') or ()}
    for f in getattr(m, 'timestamp_fields', {}) or {}:
        assert f in names or f.split('.', 1)[0] in names, f'timestamp field {f} is not a report field'
    for f in getattr(m, 'media_fields', ()) or ():
        assert f in names, f'media field {f} is not a report field'


def test_json_fields_and_field_notes(tmp_path):
    """A JSON-object column (Burner's MessageEntity) is split into
    <field>.<key>, and a checked caveat is on every row."""
    import artifact_runner as ar
    import types
    db = str(tmp_path / 'm.db')
    c = sqlite3.connect(db)
    c.execute('PRAGMA secure_delete = OFF')
    c.execute('CREATE TABLE MessageEntity (id INTEGER PRIMARY KEY, value TEXT)')
    for i in range(1, 30):
        c.execute('INSERT INTO MessageEntity VALUES (?, ?)',
                  (i, json.dumps({'text': f'message number {i} ' * 3, 'dateCreated': 1700000000000 + i})))
    c.commit()
    c.execute('DELETE FROM MessageEntity WHERE id = 7')
    c.commit()
    c.close()
    mod = types.SimpleNamespace(recovered_records=[{
        'file_key': 'main', 'table': 'MessageEntity', 'label': 'Message',
        'fields': {'id': 'message_id', 'value': 'value'}, 'version_fields': ['value'],
        'json_fields': ['value'], 'field_notes': {'value': 'checked caveat'}}])
    rows = ar.recovered_records_report(mod, {'main': db})
    # found in free space: its rowid (the id column) is not always known
    gone = [r for r in rows if str(r.get('value.text', '')).startswith('message number 7 ')]
    assert gone, rows
    assert gone[0]['value.dateCreated'] == 1700000000007
    assert gone[0]['value_caveat'] == 'checked caveat'


def test_deleted_of_takes_app_and_files_from_the_main_report():
    import artifact_runner as ar
    import types
    main = types.SimpleNamespace(app_path='data/data/x', files={'db': 'databases/x.db'},
                                 optional_files={'side': 'databases/side.db'}, name='X')
    rep = types.SimpleNamespace(deleted_of='x', name='X deleted', recovered_records=[
        {'file_key': 'db', 'table': 't'}, {'file_key': 'side', 'table': 'u'}])
    assert ar._take_from_main_report(rep, main) is None
    assert rep.app_path == 'data/data/x'
    assert rep.files == {'db': 'databases/x.db'} and rep.optional_files == {'side': 'databases/side.db'}
    assert main.deleted_reports == [rep]
    assert ar._take_from_main_report(rep, main) is None      # loaded again: listed once
    assert main.deleted_reports == [rep]
    bad = types.SimpleNamespace(deleted_of='x', recovered_records=[{'file_key': 'nope', 'table': 't'}])
    assert 'nope' in ar._take_from_main_report(bad, main)
    assert 'not a report' in ar._take_from_main_report(rep, None)


def test_identical_copies_under_new_row_ids_are_one_row(tmp_path):
    """Burner rewrites a message by delete + insert: the same content under
    a new rowid each time. One row, the copies counted, every location kept."""
    import artifact_runner as ar
    import types
    import shutil
    live = str(tmp_path / 'live.db')
    c = sqlite3.connect(live)
    c.execute('PRAGMA journal_mode = WAL')
    c.execute('PRAGMA wal_autocheckpoint = 0')
    c.execute('CREATE TABLE MessageEntity (value TEXT, id TEXT)')
    c.commit()
    body = json.dumps({'text': 'the very same message text ' * 3, 'dateCreated': 1700000000000})
    for i in range(4):          # rewritten four times: each copy under a new rowid
        c.execute("DELETE FROM MessageEntity WHERE id = 'm1'")
        c.execute('INSERT INTO MessageEntity VALUES (?, ?)', (body, 'm1'))
        c.execute('INSERT INTO MessageEntity VALUES (?, ?)', (f'filler {i}', f'f{i}'))
        c.commit()
    c.execute("DELETE FROM MessageEntity WHERE id = 'm1'")
    c.commit()
    db = str(tmp_path / 'm.db')             # copied while open: the WAL keeps every version
    shutil.copy(live, db)
    shutil.copy(live + '-wal', db + '-wal')
    c.close()
    mod = types.SimpleNamespace(recovered_records=[{
        'file_key': 'main', 'table': 'MessageEntity', 'label': 'Message',
        'fields': {'id': 'message_id', 'value': 'value'}, 'version_fields': ['value']}])
    rows = [r for r in ar.recovered_records_report(mod, {'main': db}) if r.get('message_id') == 'm1']
    assert len(rows) == 1, [(r['row_id'], r['copies']) for r in rows]
    assert rows[0]['copies'] >= 2
    assert rows[0]['copy_locations'].count('offset') == rows[0]['copies']


def test_orphaned_live_rows_are_listed(tmp_path):
    """A media/attachment row still live whose message is gone is in
    neither report otherwise: not deleted, and the main report lists a row
    only with its message. Direct, and through a join table (iOS SMS)."""
    import artifact_runner as ar
    import types
    db = str(tmp_path / 'm.db')
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE message (_id INTEGER PRIMARY KEY, text TEXT);
        CREATE TABLE media (message_row_id INTEGER, path TEXT);
        CREATE TABLE attachment (filename TEXT);
        CREATE TABLE join_t (message_id INTEGER, attachment_id INTEGER);
        INSERT INTO message VALUES (1, 'kept');
        INSERT INTO media VALUES (1, 'a.jpg'), (2, 'gone.jpg');
        INSERT INTO attachment VALUES ('x.jpg'), ('y.jpg'), ('z.jpg');
        INSERT INTO join_t VALUES (1, 1), (9, 2);
    """)
    c.commit()
    c.close()
    mod = types.SimpleNamespace(recovered_records=[
        {'file_key': 'db', 'table': 'media', 'label': 'Media',
         'fields': {'message_row_id': 'message_id', 'path': 'media_path'},
         'orphan_of': {'column': 'message_row_id', 'in': ['message', '_id']}},
        {'file_key': 'db', 'table': 'attachment', 'label': 'Attachment',
         'fields': {'filename': 'filename'},
         'orphan_of': {'column': 'ROWID', 'via': ['join_t', 'attachment_id', 'message_id'],
                       'in': ['message', '_id']}},
    ])
    rows = [r for r in ar.recovered_records_report(mod, {'db': db}) if r['status'] == 'Orphaned']
    got = sorted((r['record'], r.get('media_path') or r.get('filename')) for r in rows)
    # media 2's message is gone; attachment 2's join row names a missing
    # message, attachment 3 has no join row at all
    assert got == [('Attachment', 'y.jpg'), ('Attachment', 'z.jpg'), ('Media', 'gone.jpg')]
    assert all(r['recovered'] is False and r['raw_rowid'] for r in rows)


def _wal_copy(tmp_path, script, name='m.db'):
    """A database built by *script* (statements run one commit each), copied
    while open so the WAL keeps the deleted rows, as on a phone."""
    import shutil
    live = str(tmp_path / ('live_' + name))
    c = sqlite3.connect(live)
    c.execute('PRAGMA journal_mode = WAL')
    c.execute('PRAGMA wal_autocheckpoint = 0')
    c.execute('PRAGMA secure_delete = OFF')
    for stmt in script:
        c.executescript(stmt)
        c.commit()
    db = str(tmp_path / name)
    shutil.copy(live, db)
    shutil.copy(live + '-wal', db + '-wal')
    c.close()
    return db


def test_links_say_where_the_other_tables_row_was_found(tmp_path):
    """One table per deleted report (A-95, 2026-11-27): each row says where
    the row it names in the other table was found, live, recovered (with
    the report that lists it) or not found; ids compared, nothing joined."""
    import artifact_runner as ar
    import types
    db = _wal_copy(tmp_path, [
        'CREATE TABLE message (_id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT);'
        'CREATE TABLE media (message_row_id INTEGER PRIMARY KEY, path TEXT);',
        ''.join(f"INSERT INTO message VALUES ({i}, 'message text number {i} here');" for i in range(1, 31))
        + ''.join(f"INSERT INTO media VALUES ({i}, 'Media/picture_{i}.jpg');" for i in range(1, 21)),
        'DELETE FROM message WHERE _id IN (5, 7, 25); DELETE FROM media WHERE message_row_id IN (5, 9);',
    ])
    msg = {'file_key': 'db', 'table': 'message', 'label': 'Message',
           'fields': {'_id': 'message_id', 'text': 'message'}, 'version_fields': ['text'],
           'links': [{'name': 'linked_media', 'what': 'media', 'column': '_id', 'table': 'media',
                      'key': 'message_row_id', 'report_name': 'Deleted Media'}]}
    med = {'file_key': 'db', 'table': 'media', 'label': 'Media',
           'fields': {'message_row_id': 'message_id', 'path': 'media_path'}, 'version_fields': ['path'],
           'orphan_of': {'column': 'message_row_id', 'in': ['message', '_id']},
           'links': [{'name': 'linked_message', 'what': 'message', 'column': 'message_row_id',
                      'table': 'message', 'key': '_id', 'report_name': 'Deleted Messages'}]}
    m_rows = ar.recovered_records_report(types.SimpleNamespace(recovered_records=[msg]), {'db': db})
    d_rows = ar.recovered_records_report(types.SimpleNamespace(recovered_records=[med]), {'db': db})
    by_msg = {r['message_id']: r['linked_media'] for r in m_rows if r['status'] == 'Deleted'}
    by_med = {(r['status'], r['message_id']): r['linked_message'] for r in d_rows}
    assert by_msg == {5: 'media 5: recovered (in Deleted Media)',     # both gone
                      7: 'media 7: live',                              # its media left behind
                      25: 'media 25: not found'}                       # it had none
    assert by_med == {('Deleted', 5): 'message 5: recovered (in Deleted Messages)',
                      ('Deleted', 9): 'message 9: live',               # media gone, message kept
                      ('Orphaned', 7): 'message 7: recovered (in Deleted Messages)'}
    assert not any('media_path' in r for r in m_rows)      # nothing joined in
    assert not any('message' in r for r in d_rows)


def test_links_one_to_many_and_through_a_join_table(tmp_path):
    """A message's parts (one to many, Google Messages) and SMS-style links
    through a join table whose rows go with the deleted ones."""
    import artifact_runner as ar
    import types
    db = _wal_copy(tmp_path, [
        'CREATE TABLE messages (_id INTEGER PRIMARY KEY AUTOINCREMENT, sub TEXT);'
        'CREATE TABLE parts (_id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INT, text TEXT);'
        'CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT);'
        'CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);',
        ''.join(f"INSERT INTO messages VALUES ({i}, 'subject {i}');" for i in range(1, 11))
        + ''.join(f"INSERT INTO parts VALUES ({10 * i + k}, {i}, 'part {k} of message {i} text');"
                  for i in range(1, 11) for k in (1, 2))
        + ''.join(f"INSERT INTO attachment VALUES ({i}, 'Library/SMS/Attachments/a{i}.heic');" for i in range(1, 11))
        + ''.join(f"INSERT INTO message_attachment_join VALUES ({i}, {i});" for i in range(1, 11)),
        'DELETE FROM messages WHERE _id = 3; DELETE FROM parts WHERE _id = 31;'
        'DELETE FROM message_attachment_join WHERE message_id = 3; DELETE FROM attachment WHERE ROWID = 3;',
    ])
    msg = {'file_key': 'db', 'table': 'messages', 'label': 'Message',
           'fields': {'_id': 'message_id', 'sub': 'sub'},
           'links': [{'name': 'linked_parts', 'what': 'parts', 'column': '_id', 'table': 'parts',
                      'key': 'message_id', 'report_name': 'Deleted Parts'},
                     {'name': 'linked_attachments', 'what': 'attachment', 'column': '_id',
                      'via': ['message_attachment_join', 'message_id', 'attachment_id'],
                      'table': 'attachment', 'key': 'ROWID', 'report_name': 'Deleted Attachments'}]}
    att = {'file_key': 'db', 'table': 'attachment', 'label': 'Attachment',
           'fields': {'ROWID': 'attachment_id', 'filename': 'filename'},
           'links': [{'name': 'linked_message', 'what': 'message', 'column': 'ROWID',
                      'via': ['message_attachment_join', 'attachment_id', 'message_id'],
                      'table': 'messages', 'key': '_id', 'report_name': 'Deleted Messages'}]}
    [m] = [r for r in ar.recovered_records_report(types.SimpleNamespace(recovered_records=[msg]), {'db': db})
           if r['status'] == 'Deleted']
    assert m['message_id'] == 3
    assert m['linked_parts'] == 'parts with message_id = 3: live row 32; recovered row 31 (in Deleted Parts)'
    assert m['linked_attachments'] == ('attachment 3: recovered (in Deleted Attachments) '
                                       '(recovered message_attachment_join row)')
    [a] = [r for r in ar.recovered_records_report(types.SimpleNamespace(recovered_records=[att]), {'db': db})
           if r['status'] == 'Deleted']
    assert a['attachment_id'] == 3
    assert a['linked_message'] == ('message 3: recovered (in Deleted Messages) '
                                   '(recovered message_attachment_join row)')


def test_main_report_names_each_of_its_deleted_reports(tmp_path):
    """Under a main report, one line names every deleted report (one per
    table): a link for one that has rows, how to run it for one never run."""
    import types
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    import artifact_viewer as av

    class Label:
        text = ''
        visible = None

        def setText(self, t):
            self.text = t

        def setVisible(self, v):
            self.visible = v
    msgs = types.SimpleNamespace(__name__='x_deleted', name='X — Deleted Messages')
    media = types.SimpleNamespace(__name__='x_deleted_media', name='X — Deleted Media')
    fake = types.SimpleNamespace(
        _case_dir=str(tmp_path), _art_deleted_link=Label(),
        _art_find_tree_item_by_role=lambda role: object() if role.endswith(':x_deleted') else None)
    av.ArtifactViewerMixin._update_art_deleted_link(
        fake, types.SimpleNamespace(deleted_reports=[msgs, media]))
    t = fake._art_deleted_link.text
    assert fake._art_deleted_link.visible is True
    assert '<a href="x_deleted">X — Deleted Messages</a>' in t
    assert '"X — Deleted Media": not run yet' in t
    av.ArtifactViewerMixin._update_art_deleted_link(fake, types.SimpleNamespace())
    assert fake._art_deleted_link.visible is False


def test_deleted_tables_entry_becomes_its_own_report():
    """A main report's deleted_tables entry (A-95, 2026-11-27): its own
    report, named by 'script', deleted_of the main report, one table; a name
    already taken or a missing key is refused, never silently dropped."""
    import types
    import artifact_runner as ar
    main = types.SimpleNamespace(name='Main', __file__=__file__)
    entry = {'script': 'x_deleted', 'name': 'X — Deleted', 'description': 'd',
             'file_key': 'db', 'table': 't', 'fields': {'a': 'a'}, 'core_fields': ['status'],
             'links': [{'name': 'linked', 'what': 'y', 'column': 'a', 'table': 'u', 'key': 'k'}]}
    m, problem = ar._deleted_table_module('android', 'x', main, entry, {'x'})
    assert problem is None
    assert m.__name__ == 'x_deleted' and m.deleted_of == 'x' and m.name == 'X — Deleted'
    assert [s['table'] for s in m.recovered_records] == ['t']
    assert 'name' not in m.recovered_records[0] and 'core_fields' not in m.recovered_records[0]
    assert m.hidden_fields == ar.DELETED_HIDDEN_FIELDS and callable(m.run)
    assert ar._deleted_table_module('android', 'x', main, entry, {'x', 'x_deleted'})[1]
    bad = {k: v for k, v in entry.items() if k != 'fields'}
    assert 'fields' in ar._deleted_table_module('android', 'x', main, bad, {'x'})[1]


def test_a_main_report_runs_with_its_deleted_reports():
    """Choosing a main report runs its deleted-records reports right after
    it, each once (artifact_viewer.with_deleted_reports)."""
    import types
    from artifact_viewer import with_deleted_reports
    d1, d2 = types.ModuleType('w_del'), types.ModuleType('w_del_media')
    w = types.SimpleNamespace(deleted_reports=[d1, d2])
    other = types.SimpleNamespace()
    got = with_deleted_reports([('w', w), ('other', other), ('w_del', d1)])
    assert [n for n, _m in got] == ['w', 'w_del', 'w_del_media', 'other']
