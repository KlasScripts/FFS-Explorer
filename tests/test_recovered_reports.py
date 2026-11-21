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


@pytest.mark.parametrize('path', RECOVERED, ids=lambda p: os.path.relpath(p, ROOT))
def test_recovered_report_is_declared_consistently(path):
    m = _load(path)
    assert not hasattr(m, 'run'), 'a recovered_records report has no run()'
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
