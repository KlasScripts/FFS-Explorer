"""sqlite_reader.py — the one way an evidence SQLite file is opened. Qt-free.

Every feature that reads an evidence database (reports, the File Browser's
Database tab, Keyword Search, the MCP server, the scans) opens it here, so
they all see the same thing and say the same things about it.

The versions of a database ("views"):

- committed: what the app itself would read next time it started. A -wal
  beside the file is applied; an unfinished change (a "hot" rollback
  journal: the app was mid-write at acquisition) is undone on a throwaway
  copy. The default view everywhere.
- checkpoint: the main file alone, without its -wal: the state at the last
  checkpoint. Only exists when a real -wal is present. A whole, consistent
  database, so any query runs on it unchanged.
- in-flight main: the main file's bytes as extracted when a hot journal is
  present. Not consistent (pages of an unfinished change), so it is never
  queried with SQL: it is only carved (sqlite_recovery.py).
- carved: deleted or older records (sqlite_recovery.py).

How a file is opened (tested 2026-11-16 on SQLite 3.50.4):
- `mode=ro` when a -wal or -journal is beside the file. With a real -wal,
  SQLite applies it and creates a -shm beside the file (its index, rebuilt
  from the WAL; the device's own -shm is never needed to read).
- `mode=ro&immutable=1` when the header says WAL mode (bytes 18/19 = 2)
  and neither sidecar is beside it. `mode=ro` there creates an empty -wal
  and -shm beside the file. `immutable=1` reads the file alone and creates
  nothing. It is never used with a real -wal beside the file: it would
  silently ignore the WAL (8 rows with the WAL, 5 without, in the test).
- SQLite refusing a read-only open because it must write first (a hot
  journal to roll back, a WAL index to rebuild): the file and its sidecars
  are copied to a temporary folder and opened there read-write, so SQLite
  finishes the job as the app would; then `query_only`. The copy given is
  never changed.

Each open returns notes (plain sentences) saying what was done, for the
caller to show: a parser's run log, the Database tab's status line, an MCP
reply.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field

SQLITE_MAGIC = b'SQLite format 3\x00'
WAL_MAGICS = (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83')
JOURNAL_MAGIC = bytes.fromhex('d9d505f920a163d7')
SIDECARS = ('-wal', '-journal')        # what is ever placed beside a copy
NEEDS_WRITE_TO_OPEN = {'SQLITE_READONLY_ROLLBACK', 'SQLITE_READONLY_RECOVERY',
                       'SQLITE_READONLY_CANTINIT', 'SQLITE_READONLY_CANTLOCK'}

_temp_dirs: list[str] = []


def _cleanup_temp_dirs() -> None:
    for d in _temp_dirs:
        shutil.rmtree(d, ignore_errors=True)
    _temp_dirs.clear()


def new_temp_dir(prefix: str = 'ffs_db_') -> str:
    """A temporary folder removed when the process exits."""
    if not _temp_dirs:
        atexit.register(_cleanup_temp_dirs)
    d = tempfile.mkdtemp(prefix=prefix)
    _temp_dirs.append(d)
    return d


def is_sqlite(data: bytes) -> bool:
    return bool(data) and data[:16] == SQLITE_MAGIC


def is_wal(data: bytes) -> bool:
    return bool(data) and len(data) >= 32 and data[:4] in WAL_MAGICS


def wal_mode_header(path: str) -> bool:
    """True when the database header declares WAL mode (bytes 18 and 19)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(20)
    except OSError:
        return False
    return head[:16] == SQLITE_MAGIC and head[18:20] == b'\x02\x02'


def _uri(path: str, query: str) -> str:
    # '?' and '#' end the path in a URI and '%' starts an escape: a case
    # folder named "Case #12" would otherwise open the wrong file.
    p = path.replace('%', '%25').replace('?', '%3f').replace('#', '%23')
    return f'file:{p}?{query}'


def open_query(path: str, label: str | None = None) -> tuple[str, str | None]:
    """(URI query, note or None) for a read-only open of *path*. *label*
    names the file in the note (default: its file name)."""
    beside = [s for s in SIDECARS if os.path.exists(path + s)]
    if not beside and wal_mode_header(path):
        return ('mode=ro&immutable=1',
                f"{label or os.path.basename(path)} is in WAL mode with no -wal beside it: read the "
                "database file alone (no WAL was extracted with it).")
    return 'mode=ro', None


def connect_committed(path: str, factory=sqlite3.Connection,
                      label: str | None = None) -> tuple[sqlite3.Connection, list[str]]:
    """The committed view of the database at *path*, read-only.
    Returns (connection, notes). Raises sqlite3 errors as connect would.
    *label* names the file in the notes (default: its file name)."""
    notes: list[str] = []
    query, note = open_query(path, label)
    if note:
        notes.append(note)
    conn = sqlite3.connect(_uri(path, query), uri=True, factory=factory)
    try:
        conn.execute('PRAGMA schema_version').fetchone()     # opens the file now
    except sqlite3.OperationalError as e:
        reason = getattr(e, 'sqlite_errorname', '')
        if reason not in NEEDS_WRITE_TO_OPEN:
            conn.close()
            raise
        conn.close()
        conn, note = _finished_copy(path, reason, factory, label)
        notes.append(note)
    conn.execute('PRAGMA query_only = 1')
    return conn, notes


def _finished_copy(path: str, reason: str, factory,
                   label: str | None = None) -> tuple[sqlite3.Connection, str]:
    tmp = new_temp_dir()
    copy = os.path.join(tmp, os.path.basename(path))
    for suffix in ('', '-journal', '-wal', '-shm'):
        if os.path.exists(path + suffix):
            shutil.copyfile(path + suffix, copy + suffix)
    conn = sqlite3.connect(_uri(copy, 'mode=rw'), uri=True, factory=factory)
    conn.execute('PRAGMA schema_version').fetchone()           # SQLite finishes here
    what = ("an unfinished change in its -journal (the app was writing when the device "
            "was acquired)" if reason == 'SQLITE_READONLY_ROLLBACK'
            else "a -wal whose index had to be rebuilt")
    return conn, (f"{label or os.path.basename(path)} had {what}. SQLite won't read it without first "
                  "undoing/finishing that, so it was read from a temporary copy where SQLite "
                  "did so, as the app itself would on its next start. The extracted copy is "
                  f"unchanged. ({reason})")


def connect_checkpoint(path: str) -> sqlite3.Connection | None:
    """The checkpoint view (main file alone, -wal ignored), or None when
    there is no -wal beside *path* (then committed is the only view) or a
    -journal is beside it (immutable would read the unfinished change)."""
    if not os.path.exists(path + '-wal') or os.path.exists(path + '-journal'):
        return None
    conn = sqlite3.connect(_uri(path, 'mode=ro&immutable=1'), uri=True)
    conn.execute('PRAGMA query_only = 1')
    return conn


def committed_image(conn: sqlite3.Connection) -> bytes:
    """The committed view's page images as one database file's bytes
    (WAL applied / unfinished change undone), via sqlite3_serialize. Free
    space and freelist pages are copied as they are, so it can be carved."""
    return bytes(conn.serialize())


def readable_image(image: bytes) -> bytes:
    """*image* with its header set to rollback-journal mode, so a plain
    open of a file holding it needs no -wal (the page content is unchanged;
    only header bytes 18 and 19 differ)."""
    if len(image) >= 20 and image[18:20] != b'\x01\x01':
        image = image[:18] + b'\x01\x01' + image[20:]
    return image


@dataclass
class Staged:
    """A database (and its sidecars) written to a folder of its own."""
    dir: str
    path: str
    has_wal: bool = False
    has_journal: bool = False
    notes: list = field(default_factory=list)


def stage(name: str, main: bytes, wal: bytes | None = None, journal: bytes | None = None,
          parent: str | None = None) -> Staged:
    """Write *main* (and a real -wal / -journal) to a new folder so it can be
    opened. A sidecar that is really the database itself (some readers fall
    back to the whole file when an entry is missing) or a -wal without the
    WAL header is left out, with a note. The device's -shm is never staged:
    SQLite rebuilds the index from the WAL."""
    d = tempfile.mkdtemp(prefix='ffs_db_', dir=parent) if parent else new_temp_dir()
    path = os.path.join(d, os.path.basename(name) or 'db')
    with open(path, 'wb') as f:
        f.write(main)
    st = Staged(d, path)
    if wal:
        if is_sqlite(wal) or not is_wal(wal):
            st.notes.append(f"{os.path.basename(name)}-wal has no WAL header: not applied.")
        else:
            with open(path + '-wal', 'wb') as f:
                f.write(wal)
            st.has_wal = True
    if journal and not is_sqlite(journal):
        with open(path + '-journal', 'wb') as f:
            f.write(journal)
        st.has_journal = True
    return st


def journal_pages(journal: bytes) -> list[tuple[int, bytes]]:
    """(page number, page image) for each record in a rollback journal: each
    holds a page as it was before the change the journal protects.

    The header is 28 bytes: magic, record count, nonce, original page
    count, sector size, page size. Records start at the sector size and
    are page number (4), the page as it was before the change, and a
    checksum (4). Records are read to the end of the file, so the count
    in the header (often 0 in a journal left behind) is not relied on.
    (Moved from sqlite_viewer._journal_pages unchanged.)"""
    import struct
    if len(journal) < 28 or journal[:8] != JOURNAL_MAGIC:
        return []
    _n, _nonce, _orig, sector, page_size = struct.unpack('>IIIII', journal[8:28])
    if not page_size or not sector or page_size & (page_size - 1):
        return []
    out = []
    off = sector
    step = page_size + 8
    while off + step <= len(journal):
        page_no = struct.unpack('>I', journal[off:off + 4])[0]
        if page_no:
            out.append((page_no, journal[off + 4:off + 4 + page_size]))
        off += step
    return out
