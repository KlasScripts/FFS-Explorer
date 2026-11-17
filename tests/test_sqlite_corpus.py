"""The deleted-record carver (app/sqlite_recovery.py) scored on the SQLite
Forensic Corpus v2.0 (test_data/sqlite_forensic_corpus, CC0; Nemetz, Schmitt,
Freiling, DFRWS EU 2018).

Ground truth is independent of the tool: each database's own .sql script is
replayed on an in-memory database with triggers that record every row as it
is deleted (DELETE), overwritten (UPDATE, the old values) or dropped (DROP
TABLE). A recovered record is "exact" when every value equals a recorded
one (a rowid-alias column found without a rowid may be blank), "cut off"
when its values are a prefix of one, and "wrong" otherwise.

What is checked:
- no wrong records, apart from the two listed in KNOWN_WRONG (real deleted
  rows whose last bytes were overwritten in the file; DC3 sqlite_dissect
  1.0.0 reports the same wrong values for them, 2026-11-17);
- exact recall per category at least the level measured on 2026-11-17
  (BASELINE): a change that loses recall fails here;
- every database is carved without an error, apart from the two that
  SQLite itself refuses to open (KNOWN_UNREADABLE).

Comparison measured the same day with the same ground truth (exact records):
  category        truth  ours  FQLite 5.21  sqlite_dissect 1.0.0  old carver
  0A del. tables    100     0        99           0 (no output)        0
  0B overwr. tbl     20     8         8           4                    8
  0C del. records   101   100       100          68                   24
  0D overwr. rec     45    19        19          17                    2
  0E del. overflow   12     3         4           3                    3
  09 pointer map   1148    36        36          35                    3
"""
import collections
import os
import re
import sqlite3
import zipfile

import pytest

import sqlite_reader as sr
import sqlite_recovery as rv

ZIP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'test_data', 'sqlite_forensic_corpus', 'sqlite_forensic_corpus_v2.0.zip')

BASELINE = {'0A': 0, '0B': 8, '0C': 100, '0D': 19, '0E': 3, '09': 36, '07': 1}
KNOWN_WRONG = {
    ('09-01.db', (21540, 'Felix', 'Roth', 71565)),
    ('09-01.db', (21703, 'Simone', 'Simon', 1478213)),
}
KNOWN_UNREADABLE = {'19-03_antifor.db', '19-04_antifor.db'}


def _statements(sql):
    buf, out = '', []
    for line in sql.splitlines(True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ''
    return out


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def ground_truth(sql_text):
    """([(table, 'deleted'|'overwritten'|'dropped', values)], {table: {live rows}})."""
    m = sqlite3.connect(':memory:')
    log = []
    m.create_function('gt', -1, lambda *a: log.append((a[0], a[1], tuple(a[2:]))))
    watched = set()
    for st in _statements(sql_text):
        u = st.lstrip().upper()
        if u.startswith('DROP TABLE'):
            name = re.sub(r'(?is)^\s*DROP\s+TABLE\s+(IF\s+EXISTS\s+)?', '', st).strip().rstrip(';')
            name = name.strip('\'"`[] ')
            try:
                for r in m.execute(f'SELECT * FROM {_q(name)}'):
                    log.append((name, 'dropped', tuple(r)))
            except sqlite3.Error:
                pass
        try:
            m.execute(st)
        except sqlite3.Error:
            try:
                m.executescript(st)
            except sqlite3.Error:
                continue
        if u.startswith('CREATE TABLE'):
            for (name,) in m.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if name in watched:
                    continue
                watched.add(name)
                try:
                    cols = [r[1] for r in m.execute(f'PRAGMA table_info({_q(name)})')]
                    old = ', '.join('OLD.' + _q(c) for c in cols)
                    lit = name.replace("'", "''")
                    for kind, ev in (('deleted', 'DELETE'), ('overwritten', 'UPDATE')):
                        m.execute(f'CREATE TEMP TRIGGER {_q("gt_" + kind + "_" + name)} BEFORE {ev} '
                                  f"ON {_q(name)} BEGIN SELECT gt('{lit}', '{kind}', {old}); END")
                except sqlite3.Error:
                    pass
    live = collections.defaultdict(set)
    for (name,) in m.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            live[name] = {tuple(r) for r in m.execute(f'SELECT * FROM {_q(name)}')}
        except sqlite3.Error:
            pass
    return log, live


def carve(db):
    """[(table, record)] for every table, through the same calls reports use."""
    conn, _ = sr.connect_committed(db)
    ck = sr.connect_checkpoint(db)

    def rd(p):
        return open(p, 'rb').read() if os.path.exists(p) else None
    fr = rv.for_connection(rd(db), rd(db + '-wal'), rd(db + '-journal'), conn)
    out = []
    try:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            t = rv.table_info(conn, name, ck)
            if t is None:
                continue
            alias = t.columns.index(t.id_col) if t.id_col in t.columns else None
            recs, _ = fr.records(t)
            out += [(name, r, alias) for r in recs]
    finally:
        conn.close()
        if ck:
            ck.close()
    return out


def _same(vals, truth, alias):
    return len(vals) == len(truth) and all(
        a == b or (i == alias and a is None) for i, (a, b) in enumerate(zip(vals, truth)))


@pytest.fixture(scope='module')
def corpus(tmp_path_factory):
    if not os.path.exists(ZIP):
        pytest.skip('SQLite Forensic Corpus not in test_data')
    d = tmp_path_factory.mktemp('corpus')
    with zipfile.ZipFile(ZIP) as z:          # the corpus zip, not an FFS archive
        z.extractall(d)
    return os.path.join(d, 'sqlite_forensic_corpus_v2.0')


@pytest.fixture(scope='module')
def scores(corpus):
    return score_corpus(corpus)


def score_corpus(corpus):
    """(exact per folder, wrong records, errors per database)."""
    exact = collections.Counter()
    wrong, errors = [], {}
    for folder in sorted(os.listdir(corpus)):
        path = os.path.join(corpus, folder)
        if not os.path.isdir(path):
            continue
        for f in sorted(os.listdir(path)):
            if not f.endswith('.db'):
                continue
            db = os.path.join(path, f)
            base = db[:-3].replace('_antifor', '')
            log, live = ground_truth(open(base + '.sql', encoding='utf-8', errors='replace').read())
            truth = [v for _t, _k, v in log]
            try:
                found = carve(db)
            except Exception as exc:
                errors[f] = exc
                continue
            hit = set()
            for _table, r, alias in found:
                vals = tuple(r['values'])
                full = [g for g in truth if _same(vals, g, alias)]
                if full:
                    hit.update(full)
                    continue
                if vals and any(len(vals) < len(g) and _same(vals, g[:len(vals)], alias) for g in truth):
                    continue                                    # cut off: a true prefix
                wrong.append((f, vals, r['method']))
            exact[folder] += len(hit)
    return exact, wrong, errors


def test_no_wrong_records(scores):
    _exact, wrong, _errors = scores
    unexpected = [w for w in wrong if (w[0], w[1]) not in KNOWN_WRONG]
    assert unexpected == [], f'records that were never written: {unexpected[:5]}'


@pytest.mark.parametrize('category', sorted(BASELINE))
def test_recall_does_not_drop(scores, category):
    exact, _wrong, _errors = scores
    assert exact[category] >= BASELINE[category], (
        f'{category}: {exact[category]} exact, baseline {BASELINE[category]}')


def test_every_readable_database_is_carved(scores):
    _exact, _wrong, errors = scores
    assert set(errors) == KNOWN_UNREADABLE, {k: repr(v) for k, v in errors.items()}
    assert all(isinstance(e, sqlite3.DatabaseError) for e in errors.values())
