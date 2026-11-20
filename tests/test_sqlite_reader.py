"""sqlite_reader: the one way an evidence database is opened. Each test
checks against what the real SQLite engine wrote (tests/sqlite_fixtures.py)
and that the copy given is never changed and gains no files."""
import hashlib
import os
import sqlite3

import pytest

import sqlite_reader as sr
from sqlite_fixtures import hot_journal_db, wal_db, wal_mode_without_wal


def _state(folder):
    out = {}
    for f in sorted(os.listdir(folder)):
        with open(os.path.join(folder, f), 'rb') as fh:
            out[f] = hashlib.md5(fh.read()).hexdigest()
    return out


def _rows(conn):
    return dict(conn.execute('select id, body from messages').fetchall())


def test_wal_is_applied_and_the_copy_is_unchanged(tmp_path):
    path, truth = wal_db(str(tmp_path))
    folder = os.path.dirname(path)
    before = _state(folder)
    conn, notes = sr.connect_committed(path)
    assert _rows(conn) == truth['committed']
    conn.close()
    after = _state(folder)
    assert {k: v for k, v in after.items() if not k.endswith('-shm')} == before
    assert notes == []


def test_checkpoint_view_is_the_main_file_alone(tmp_path):
    path, truth = wal_db(str(tmp_path))
    conn = sr.connect_checkpoint(path)
    assert _rows(conn) == truth['checkpoint']
    conn.close()


def test_no_checkpoint_view_without_a_wal(tmp_path):
    path = wal_mode_without_wal(str(tmp_path))
    assert sr.connect_checkpoint(path) is None


def test_wal_mode_without_wal_creates_no_files_and_says_so(tmp_path):
    path = wal_mode_without_wal(str(tmp_path))
    folder = os.path.dirname(path)
    before = _state(folder)
    conn, notes = sr.connect_committed(path)
    assert len(_rows(conn)) == 20
    conn.close()
    assert _state(folder) == before                    # no empty -wal / -shm made
    assert len(notes) == 1 and 'WAL mode with no -wal beside it' in notes[0]


def test_plain_mode_ro_would_have_made_files(tmp_path):
    """Why the immutable open exists: documents the SQLite behaviour."""
    path = wal_mode_without_wal(str(tmp_path))
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    c.execute('select count(*) from messages').fetchone()
    c.close()
    assert os.path.exists(path + '-wal')


def test_immutable_would_ignore_a_real_wal(tmp_path):
    """Why immutable is never used with a -wal beside the file."""
    path, truth = wal_db(str(tmp_path))
    c = sqlite3.connect(f'file:{path}?mode=ro&immutable=1', uri=True)
    assert _rows(c) == truth['checkpoint'] != truth['committed']
    c.close()


def test_hot_journal_is_rolled_back_on_a_copy(tmp_path):
    path, n = hot_journal_db(str(tmp_path))
    folder = os.path.dirname(path)
    before = _state(folder)
    conn, notes = sr.connect_committed(path)
    count, changed = conn.execute(
        "select count(*), sum(url like 'changed%') from favicons").fetchone()
    assert (count, changed) == (n, 0)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("insert into favicons(url) values('no')")
    conn.close()
    assert _state(folder) == before
    assert len(notes) == 1 and 'SQLITE_READONLY_ROLLBACK' in notes[0]


def test_no_checkpoint_view_with_a_journal(tmp_path):
    path, _ = hot_journal_db(str(tmp_path))
    assert sr.connect_checkpoint(path) is None


def test_committed_image_has_the_wal_applied(tmp_path):
    path, truth = wal_db(str(tmp_path))
    conn, _ = sr.connect_committed(path)
    image = sr.committed_image(conn)
    conn.close()
    m = sqlite3.connect(':memory:')
    m.deserialize(sr.readable_image(image))
    assert _rows(m) == truth['committed']


def test_committed_image_of_a_hot_journal_is_rolled_back(tmp_path):
    path, n = hot_journal_db(str(tmp_path))
    conn, _ = sr.connect_committed(path)
    image = sr.committed_image(conn)
    conn.close()
    m = sqlite3.connect(':memory:')
    m.deserialize(sr.readable_image(image))
    assert m.execute("select count(*), sum(url like 'changed%') from favicons").fetchone() == (n, 0)


def test_a_folder_name_with_hash_and_question_mark(tmp_path):
    # Windows forbids '?' in a folder name, so there only '#' is tested
    odd = tmp_path / ('Case #12 draft' if os.name == 'nt' else 'Case #12 ?draft')
    odd.mkdir()
    path, truth = wal_db(str(odd))
    conn, _ = sr.connect_committed(path)
    assert _rows(conn) == truth['committed']
    conn.close()


def test_stage_leaves_out_a_sidecar_that_is_really_the_database(tmp_path):
    path, truth = wal_db(str(tmp_path))
    main = open(path, 'rb').read()
    st = sr.stage('msgstore.db', main, wal=main)       # a reader that fell back to the db
    assert not st.has_wal and not os.path.exists(st.path + '-wal')
    assert st.notes and 'no WAL header' in st.notes[0]
    st2 = sr.stage('msgstore.db', main, wal=open(path + '-wal', 'rb').read())
    conn, _ = sr.connect_committed(st2.path)
    assert _rows(conn) == truth['committed']
    conn.close()


def test_journal_pages_are_the_pages_before_the_change(tmp_path):
    path, _ = hot_journal_db(str(tmp_path))
    pages = sr.journal_pages(open(path + '-journal', 'rb').read())
    assert pages
    assert all(len(img) == len(pages[0][1]) for _p, img in pages)
    assert any(b'x' * 200 in img for _p, img in pages)
    assert not any(b'changed' in img for _p, img in pages)
