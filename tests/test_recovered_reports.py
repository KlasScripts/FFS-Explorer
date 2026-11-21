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


@pytest.mark.parametrize('path', RECOVERED, ids=lambda p: os.path.relpath(p, ROOT))
def test_recovered_report_is_declared_consistently(path):
    assert 'def run(' not in open(path, encoding='utf-8').read(), 'a recovered_records report has no run()'
    m = _loaded()[0][path]                     # as the app loads it (deleted_of resolved)
    keys = set(m.files) | set(getattr(m, 'optional_files', {}) or {})
    names = set()
    for spec in m.recovered_records:
        assert spec['file_key'] in keys, spec['file_key']
        assert spec.get('fields'), spec['table']
        assert set(spec.get('version_fields') or ()) <= set(spec['fields']), spec['table']
        assert set(spec.get('json_fields') or ()) <= set(spec['fields']), spec['table']
        names |= set(spec['fields'].values())
    for f in getattr(m, 'timestamp_fields', {}) or {}:
        assert f in names or f.split('.', 1)[0] in names, f'timestamp field {f} is not a report field'


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
    assert main.deleted_report is rep
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
