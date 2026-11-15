"""Timeline (app/timeline.py): only declared times, nothing dropped silently,
raw values kept, one time conversion shared with the reports."""
import sqlite3
import types
from datetime import datetime, timezone

import pytest

import timeline
from artifact_db import write_artifact_results


def _utc_ms(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def _mod(name, units, decl=None, skip=None):
    m = types.SimpleNamespace(name=name, timestamp_fields=units)
    if decl is not None:
        m.timeline_fields = decl
    if skip is not None:
        m.timeline_skip = skip
    return m


@pytest.fixture
def dbs():
    res = sqlite3.connect(':memory:')
    res.execute('CREATE TABLE run_log (id INTEGER PRIMARY KEY, run_type TEXT)')
    cache = sqlite3.connect(':memory:')
    return res, cache


def test_units_match_known_dates():
    t = _utc_ms(2026, 11, 14, 6, 56, 12) / 1000
    assert timeline.to_unix_seconds(t, 's') == t
    assert timeline.to_unix_seconds(t * 1000, 'ms') == pytest.approx(t)
    assert timeline.to_unix_seconds(t - 978307200, 'cocoa_s') == pytest.approx(t)
    assert timeline.to_unix_seconds((t - 978307200) * 1e9, 'cocoa_ns') == pytest.approx(t)
    assert timeline.to_unix_seconds((t + 11644473600) * 1e6, 'webkit_us') == pytest.approx(t)
    assert timeline.to_unix_seconds('abc', 'ms') is None
    assert timeline.to_unix_seconds('5', 'minutes') is None
    assert timeline.to_unix_seconds('nan', 's') is None


def test_only_declared_times_and_every_value_accounted(dbs):
    res, cache = dbs
    t0 = _utc_ms(2026, 11, 14, 7, 0, 0)
    write_artifact_results(res, 'usage', [
        {'event_time': str(t0), 'package': 'com.a', 'event_type': 'ACTIVITY_RESUMED',
         'class_name': 'Main', 'other_time': str(t0)},
        {'event_time': str(t0 + 1500), 'package': 'com.b', 'event_type': 'ACTIVITY_PAUSED',
         'source': 'Carved — events (unverified match)', 'other_time': ''},
        {'event_time': '', 'package': 'com.c'},
        {'event_time': '0', 'package': 'com.d'},
        {'event_time': 'junk', 'package': 'com.e'},
        {'event_time': '12345', 'package': 'com.f'},          # 1970: kept, labelled
    ])
    write_artifact_results(res, 'trash', [{'expires': '1796500152', 'when': '1793900152'}])
    modules = {
        'usage': _mod('Usage', {'event_time': 'ms', 'other_time': 'ms'}, {
            'event_time': {'meaning': 'App event', 'app': 'package', 'event': 'event_type',
                           'detail': ['class_name'], 'caution': 'Clock changes move it.'}}),
        'trash': _mod('Trash', {'expires': 's', 'when': 's'},
                      {'when': {'meaning': 'Moved to trash', 'event_name': 'Trashed'}},
                      skip={'expires': 'not an event'}),
        'never_run': _mod('Never run', {'t': 'ms'}, {'t': {'meaning': 'x'}}),
        'no_meaning': _mod('No meaning', {'t': 'ms'}, {'t': {'app': 'package'}}),
    }
    out = timeline.build(res, cache, modules)
    assert out['events'] == 4
    rows, total = timeline.query(cache)
    assert total == 4
    byapp = {r[timeline.COLUMNS.index('app')]: r for r in rows}
    a = byapp['com.a']
    assert a[0] == t0
    assert a[timeline.COLUMNS.index('meaning')] == 'App event'
    assert a[timeline.COLUMNS.index('event')] == 'ACTIVITY_RESUMED'
    assert a[timeline.COLUMNS.index('detail')] == 'Main'
    assert a[timeline.COLUMNS.index('caution')] == 'Clock changes move it.'
    assert a[timeline.COLUMNS.index('raw')] == str(t0)          # raw kept
    assert a[timeline.COLUMNS.index('record')] == ''
    assert byapp['com.b'][timeline.COLUMNS.index('record')].startswith('Carved')
    assert 'outside 2000–2100' in byapp['com.f'][timeline.COLUMNS.index('record')]
    trashed = [r for r in rows if r[timeline.COLUMNS.index('script')] == 'trash'][0]
    assert trashed[timeline.COLUMNS.index('event')] == 'Trashed'
    # No other_time or expires event: undeclared / skipped.
    assert {r[timeline.COLUMNS.index('field')] for r in rows} == {'event_time', 'when'}

    src = {(s[0], s[2]): s for s in timeline.source_list(cache)}
    ev = src[('usage', 'event_time')]
    assert ev[3] == timeline.ON_TIMELINE
    assert ev[4:9] == (3, 1, 1, 1, 1)     # events, blank, zero, not a time, outside
    assert src[('usage', 'other_time')][3] == timeline.NOT_DECLARED
    assert src[('trash', 'expires')][3] == timeline.NOT_AN_EVENT
    assert src[('trash', 'expires')][9] == 'not an event'
    assert src[('never_run', 't')][3] == timeline.NOT_RUN
    assert src[('no_meaning', 't')][3] == timeline.DECLARATION_ERROR


def test_run_state_is_named(dbs):
    """Not run, run with no rows, failed, and rows left from an earlier run
    are told apart (write_artifact_results keeps the old table when a run
    finds nothing)."""
    res, cache = dbs
    t0 = _utc_ms(2026, 1, 1)
    res.execute('ALTER TABLE run_log ADD COLUMN complete INTEGER')
    res.execute('ALTER TABLE run_log ADD COLUMN output_rows INTEGER')
    write_artifact_results(res, 'stale', [{'t': str(t0)}])
    res.executemany('INSERT INTO run_log (run_type, complete, output_rows) VALUES (?,?,?)', [
        ('artifact_stale', 1, 1), ('artifact_stale', 1, 0),       # re-run found nothing
        ('artifact_empty', 1, 0), ('artifact_failed', 0, None)])
    decl = {'t': {'meaning': 'm'}}
    mods = {n: _mod(n, {'t': 'ms'}, decl) for n in ('stale', 'empty', 'failed', 'never')}
    timeline.build(res, cache, mods)
    st = {s[0]: s[3] for s in timeline.source_list(cache)}
    assert st == {'stale': timeline.STALE, 'empty': timeline.RAN_NO_ROWS,
                  'failed': timeline.RUN_FAILED, 'never': timeline.NOT_RUN}
    row = timeline.query(cache)[0][0]
    assert 'earlier run' in row[timeline.COLUMNS.index('record')]


def test_rebuild_replaces_and_fingerprint_follows_inputs(dbs):
    res, cache = dbs
    t0 = _utc_ms(2026, 1, 1)
    write_artifact_results(res, 'r', [{'t': str(t0)}])
    modules = {'r': _mod('R', {'t': 'ms'}, {'t': {'meaning': 'm'}})}
    timeline.build(res, cache, modules)
    fp = timeline.stored_fingerprint(cache)
    assert fp == timeline.fingerprint(res, modules)
    timeline.build(res, cache, modules)
    assert timeline.query(cache)[1] == 1                       # replaced, not doubled
    write_artifact_results(res, 'r', [{'t': str(t0)}, {'t': str(t0 + 1)}])
    assert timeline.fingerprint(res, modules) != fp            # rows changed
    modules2 = {'r': _mod('R', {'t': 'ms'}, {'t': {'meaning': 'changed'}})}
    assert timeline.fingerprint(res, modules2) != timeline.fingerprint(res, modules)


def test_query_window_order_and_cap(dbs):
    res, cache = dbs
    t0 = _utc_ms(2026, 1, 1)
    write_artifact_results(res, 'r', [{'t': str(t0 + i * 60_000), 'a': f'app{i % 2}'}
                                      for i in range(10)])
    timeline.build(res, cache, {'r': _mod('R', {'t': 'ms'}, {'t': {'meaning': 'm', 'app': 'a'}})})
    rows, total = timeline.query(cache, t0 + 2 * 60_000, t0 + 5 * 60_000)
    assert total == 4 and [r[0] for r in rows] == sorted(r[0] for r in rows)
    rows, total = timeline.query(cache, limit=3)
    assert len(rows) == 3 and total == 10                      # cap is reported
    rows, _ = timeline.query(cache, descending=True)
    assert rows[0][0] == t0 + 9 * 60_000
    rows, total = timeline.query(cache, app='app1')
    assert total == 5
    assert timeline.apps(cache) == ['app0', 'app1']


def test_pivot_time_prefers_the_clicked_time_column():
    mod = _mod('R', {'a': 'ms', 'b': 's'}, {'b': {'meaning': 'm'}})
    row = {'a': '1700000000000', 'b': '1700000100'}
    assert timeline.time_of_row(mod, row, 'a') == (1700000000000, 'a')
    assert timeline.time_of_row(mod, row, 'other') == (1700000100000, 'b')
    assert timeline.time_of_row(mod, {'a': '', 'b': '0'}) is None


def test_shipped_declarations_are_valid():
    """Every shipped timeline_fields entry has a meaning and a known unit,
    and every timeline_skip names a real time column. Loads the parser
    files directly: list_artifacts would record parser versions in the
    repo's config. Whether app/event/detail columns are ones the parser
    writes needs real rows (see the app check)."""
    import glob
    import importlib.util
    import os
    root = os.path.join(os.path.dirname(__file__), '..', 'artifacts')
    checked = 0
    for path in glob.glob(os.path.join(root, '*', '*.py')):
        spec = importlib.util.spec_from_file_location('_tl_check', path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            continue      # a parser that needs the app's runtime; not ours to judge
        for field, d in timeline.declared_fields(mod).items():
            assert 'error' not in d, (path, field, d.get('error'))
            checked += 1
        for field in (getattr(mod, 'timeline_skip', {}) or {}):
            assert field in (getattr(mod, 'timestamp_fields', {}) or {}), (path, field)
    assert checked >= 10


def test_a_run_with_no_rows_removes_the_earlier_table():
    """Rows from an earlier run must never look like the latest run's."""
    res = sqlite3.connect(':memory:')
    write_artifact_results(res, 'r', [{'t': '1'}])
    assert write_artifact_results(res, 'r', []) == 0
    assert not res.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_r'").fetchone()
    assert write_artifact_results(res, 'never', []) == 0      # no table: still fine
