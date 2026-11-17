"""Awkward SQLite databases made by the real SQLite engine, shared by the
reader and recovery tests (sqlite_reader.py, sqlite_recovery.py).

Each builder writes into a folder of its own and returns the path of the
database as it would be extracted from a phone: the main file plus the
sidecars the phone had at that moment. Ground truth (which rows are live,
deleted or changed) is returned with it, so a test checks against what was
written, never against the tool's own output.
"""
import os
import shutil
import sqlite3
import subprocess
import sys


def _copy_out(src: str, dest_dir: str, name: str, suffixes=('', '-wal', '-journal')) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, name)
    for s in suffixes:
        if os.path.exists(src + s):
            shutil.copyfile(src + s, out + s)
    return out


def wal_db(folder: str, name: str = 'msgstore.db') -> tuple[str, dict]:
    """A WAL-mode database copied while the app had it open: 20 rows were
    checkpointed into the main file, then three commits went to the WAL
    (an insert, an update of row 3, a delete of rows 5 and 6).
    Returns (path, truth) with truth = {'checkpoint': {id: text},
    'committed': {id: text}, 'deleted': {id: text}, 'old_versions': {id: text}}."""
    work = os.path.join(folder, 'work')
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, name)
    c = sqlite3.connect(src, isolation_level=None)
    c.execute('pragma journal_mode=wal')
    c.execute('pragma wal_autocheckpoint=0')
    c.execute('create table messages(id integer primary key, body text, ts integer)')
    base = {i: f'message number {i} ' + 'x' * 40 for i in range(1, 21)}
    for i, b in base.items():
        c.execute('insert into messages values(?,?,?)', (i, b, 1700000000 + i))
    c.execute('pragma wal_checkpoint(truncate)')
    c.execute('insert into messages values(21, ?, 1700000021)', ('added in the wal',))
    c.execute("update messages set body='edited body of three' where id=3")
    c.execute('delete from messages where id in (5, 6)')
    path = _copy_out(src, os.path.join(folder, 'phone'), name)
    c.close()
    committed = dict(base)
    committed[21] = 'added in the wal'
    committed[3] = 'edited body of three'
    del committed[5], committed[6]
    return path, {'checkpoint': base, 'committed': committed,
                  'deleted': {5: base[5], 6: base[6]}, 'old_versions': {3: base[3]}}


def wal_mode_without_wal(folder: str, name: str = 'notes.db') -> str:
    """A WAL-mode database extracted without its -wal (everything was
    checkpointed, or the extraction left the sidecar out)."""
    path, _ = wal_db(folder, name)
    os.remove(path + '-wal')
    return path


def hot_journal_db(folder: str, name: str = 'Favicons',
                   change: str = "update favicons set url='changed'||url") -> tuple[str, int]:
    """A rollback-journal database copied while the app was mid-change: a
    child process updates every row inside an open transaction, the files
    are copied, and the child dies without committing. The committed state
    has 5000 rows with url 'x'*200; the in-flight main file has some pages
    already holding 'changed...'. Returns (path, row count)."""
    work = os.path.join(folder, 'work')
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, 'src.db')
    c = sqlite3.connect(src)
    c.execute('create table favicons(id integer primary key, url text)')
    c.executemany('insert into favicons(url) values(?)', [('x' * 200,) for _ in range(5000)])
    c.commit()
    c.close()
    out_dir = os.path.join(folder, 'phone')
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name)
    code = (
        "import sqlite3, shutil, os\n"
        f"c = sqlite3.connect({src!r}, isolation_level=None)\n"
        "c.execute('PRAGMA cache_size=5'); c.execute('begin')\n"
        f"c.execute({change!r})\n"
        f"shutil.copy({src!r}, {out!r})\n"
        f"shutil.copy({src + '-journal'!r}, {out + '-journal'!r})\n"
        "os._exit(0)\n")
    subprocess.run([sys.executable, '-c', code], check=True)
    return out, 5000


def deleted_rows_db(folder: str, name: str = 'chat.db', secure_delete: bool = False,
                    n: int = 300, delete=range(40, 60)) -> tuple[str, dict]:
    """A rollback-journal database with *n* rows, of which *delete* were
    deleted (ordinary DELETE, so their bytes stay in free space and freed
    pages unless *secure_delete*). Returns (path, {'deleted': {id: body},
    'live': {id: body}})."""
    out_dir = os.path.join(folder, 'phone')
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    c = sqlite3.connect(path, isolation_level=None)
    if secure_delete:
        c.execute('pragma secure_delete=on')
    c.execute('create table chat(id integer primary key, sender text, body text, ts integer)')
    rows = {i: f'body of chat line {i:04d} ' + 'y' * (i % 50) for i in range(1, n + 1)}
    for i, b in rows.items():
        c.execute('insert into chat values(?,?,?,?)', (i, f'sender{i % 7}', b, 1700000000 + i))
    for i in delete:
        c.execute('delete from chat where id=?', (i,))
    c.close()
    deleted = {i: rows[i] for i in delete}
    return path, {'deleted': deleted, 'live': {i: b for i, b in rows.items() if i not in deleted}}
