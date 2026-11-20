"""A parser's version hash ignores line endings: a Windows checkout (git
autocrlf) must not bump every parser's version."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app import parser_versions as pv  # noqa: E402


def test_crlf_and_lf_hash_the_same(tmp_path):
    lf = tmp_path / 'lf.py'
    crlf = tmp_path / 'crlf.py'
    lf.write_bytes(b'def run(ctx, paths):\n    return []\n')
    crlf.write_bytes(b'def run(ctx, paths):\r\n    return []\r\n')
    assert pv._hash_source(str(lf)) == pv._hash_source(str(crlf))


def test_a_real_change_still_changes_the_hash(tmp_path):
    a = tmp_path / 'a.py'
    b = tmp_path / 'b.py'
    a.write_bytes(b'x = 1\n')
    b.write_bytes(b'x = 2\n')
    assert pv._hash_source(str(a)) != pv._hash_source(str(b))


def test_shipped_versions_match_the_checked_out_scripts():
    """Every recorded hash matches its script as checked out, whatever
    the line endings, so opening the app bumps nothing."""
    root = os.path.join(os.path.dirname(__file__), '..')
    parsers = pv._load()
    stale = []
    for key, entry in parsers.items():
        platform, name = key.split(':', 1)
        path = os.path.join(root, 'artifacts', platform, name + '.py')
        if os.path.exists(path) and pv._hash_source(path) != entry.get('hash'):
            stale.append(key)
    assert not stale, stale
