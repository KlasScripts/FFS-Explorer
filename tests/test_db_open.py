"""open_db_readonly: errors name their database, and a database SQLite must
write to before it can be read (a hot -journal: the app was mid-write at
acquisition) is read from a temporary copy, leaving the extracted copy
byte-for-byte unchanged. The hot journal is made the real way: a child
process changes the database inside an open transaction, the files are
copied mid-transaction, and the child dies without committing."""
import hashlib
import os
import sqlite3
import subprocess
import sys

import pytest

from artifact_runner import open_db_readonly, take_open_notes


def _md5(path):
    with open(path, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def _hot_journal_db(tmp_path):
    src = tmp_path / 'src.db'
    c = sqlite3.connect(src)
    c.execute('create table favicons(id integer primary key, url text)')
    c.executemany('insert into favicons(url) values(?)', [('x' * 200,) for _ in range(5000)])
    c.commit()
    c.close()
    code = (
        "import sqlite3, shutil, os\n"
        f"c = sqlite3.connect({str(src)!r}, isolation_level=None)\n"
        "c.execute('PRAGMA cache_size=5'); c.execute('begin')\n"
        "c.execute(\"update favicons set url='changed'||url\")\n"
        f"shutil.copy({str(src)!r}, {str(tmp_path / 'Favicons')!r})\n"
        f"shutil.copy({str(src) + '-journal'!r}, {str(tmp_path / 'Favicons-journal')!r})\n"
        "os._exit(0)\n")
    subprocess.run([sys.executable, '-c', code], check=True)
    return str(tmp_path / 'Favicons')


def test_hot_journal_is_read_from_a_temporary_copy(tmp_path):
    db = _hot_journal_db(tmp_path)
    with pytest.raises(sqlite3.OperationalError):     # what every parser hit
        sqlite3.connect(f'file:{db}?mode=ro', uri=True).execute('select 1 from favicons')
    before = (_md5(db), _md5(db + '-journal'))
    take_open_notes()
    conn = open_db_readonly(db)
    n, changed = conn.execute(
        "select count(*), sum(url like 'changed%') from favicons").fetchone()
    assert (n, changed) == (5000, 0)                   # the last committed state
    assert conn.execute('select url from favicons limit 1').fetchone()['url'] == 'x' * 200
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("insert into favicons(url) values('no')")   # still query-only
    conn.close()
    assert (_md5(db), _md5(db + '-journal')) == before            # evidence copy untouched
    notes = take_open_notes()
    assert len(notes) == 1 and 'Favicons [Favicons-journal' in notes[0]
    assert 'SQLITE_READONLY_ROLLBACK' in notes[0]


def test_normal_database_opens_in_place_without_a_note(tmp_path):
    db = str(tmp_path / 'History')
    c = sqlite3.connect(db)
    c.execute('create table urls(url text)')
    c.execute("insert into urls values('a')")
    c.commit()
    c.close()
    take_open_notes()
    assert open_db_readonly(db).execute('select url from urls').fetchone()['url'] == 'a'
    assert take_open_notes() == []


def test_errors_name_the_database(tmp_path):
    db = str(tmp_path / 'History')
    with open(db, 'wb') as f:
        f.write(b'not a database' * 100)
    with pytest.raises(sqlite3.DatabaseError, match=r'^History: file is not a database'):
        open_db_readonly(db)


def test_log_note_reaches_the_run_log():
    from artifact_runner import log_note
    take_open_notes()
    log_note('Account Web Data has no autofill table')
    assert take_open_notes() == ['  Note: Account Web Data has no autofill table']
    assert take_open_notes() == []
