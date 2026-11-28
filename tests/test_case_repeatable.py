"""The same archive gives the same case whatever Python's hash seed (the
order of a set changes with it): the file list in archive order, and the
lists saved from sets sorted (2026-11-28)."""
import os
import subprocess
import sys
import zipfile

APP = os.path.join(os.path.dirname(__file__), '..', 'app')

CHILD = r'''
import hashlib, sys
sys.path.insert(0, sys.argv[1])
from adapters.ffs import FfsAdapter
a = FfsAdapter.__new__(FfsAdapter)
a.user_prefix, a.old_layout = 'filesystem2', False
names = {f'filesystem2/mobile/d{i % 9}/f{i}.txt': i for i in range(2000)}
out = list(a.build_zip_entries(dict(names)))            # archive order kept
srt = list(a.build_zip_entries(frozenset(names)))       # a set: sorted
print(hashlib.sha256('\n'.join(out).encode()).hexdigest(),
      hashlib.sha256('\n'.join(srt).encode()).hexdigest(),
      out == [n.removeprefix('filesystem2/') for n in names])
'''


def test_file_list_order_does_not_follow_the_hash_seed():
    runs = []
    for seed in ('1', '2', '3'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, '-c', CHILD, os.path.abspath(APP)],
                           capture_output=True, text=True, env=env, check=True)
        runs.append(r.stdout.split())
    assert len({tuple(r) for r in runs}) == 1, runs
    assert runs[0][2] == 'True'


def test_sets_saved_sorted(tmp_path):
    import sqlite3
    import db_utils
    conn = sqlite3.connect(str(tmp_path / 'c.db'))
    conn.execute('CREATE TABLE guid_bundle (guid TEXT, bundle_id TEXT)')
    db_utils.save_guid_bundle_map(conn, {'b': 'x', 'a': 'y', 'c': 'z'})
    assert [r[0] for r in conn.execute('SELECT guid FROM guid_bundle')] == ['a', 'b', 'c']
