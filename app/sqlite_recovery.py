"""sqlite_recovery.py — deleted and older records of a SQLite database, one
way for every caller. Qt-free.

Reports (`recoverable_tables`, via sqlite_carve.recover_deleted_rows) and
the File Browser's Database tab both ask this module, so they find the
same records with the same labels. The byte-level decoders stay in
sqlite_carve.py; this module decides which candidates belong to which
table and how sure that is.

Work is split the way it costs:
- per FILE, done once and kept on the FileRecovery object: decoding every
  freed page, every WAL frame, every rollback-journal page, the overflow
  search, and each leaf page's free space. Each page image is decoded once
  however many tables are asked about (memo by page number + image hash),
  and an image the committed view shares with the main file is not
  decoded twice.
- per TABLE, cheap: which pages are this table's, its live rows (to keep a
  live row out of the results by construction), the record pattern learned
  from its own live rows, the column count, dedupe.

Methods ('method' on each record), strongest first:
- checkpoint_deleted / checkpoint_older: a row in the checkpoint view (the
  main file without its -wal) that the committed view no longer has, or
  has with other values. Whole rows from a consistent database: certain.
- wal_frame: a version in a WAL frame of one of this table's pages that is
  neither the committed nor the checkpoint version. 'older_wal' when the
  frame's salt is from an earlier WAL generation (the page may then have
  belonged to another table).
- journal: a version in a rollback-journal page (the page as it was before
  a change). The page may have belonged to another table.
- unfinished_write: with a hot journal (committed = rolled back), the
  in-flight main file's version of a page the unfinished change touched.
  Never committed: what the app was writing when the phone was acquired.
- unfinished_delete: a live (committed) row that the unfinished change had
  already removed from its page: the app was deleting it, or moving it to
  a page not yet written. The row itself is the committed one.
- freeblock / page_gap: a cell with an intact length and rowid in free
  space on one of this table's pages.
- freed_page: a cell on a page now on the freelist. The page is no longer
  any table's: matched by column count and an unused rowid only.
- overflow: a deleted record whose payload continued on overflow pages
  (sqlite_carve.carve_overflowing_records); 'complete' or partial.
- header_signature: a record header in free space matching this table's
  record pattern (learned from its live rows). No rowid.
- header_rebuilt: an ordinary single-row delete whose freeblock link (4
  bytes) overwrote the cell's length, rowid AND the start of its record
  header (short rows, low rowids). At most one serial type is lost; it is
  taken from this table's pattern, else tried against every type the
  column's declared type allows (so wiped and number-only tables work),
  and a rebuild whose length equals the freeblock link's size wins. No
  rowid.
- header_schema: a record header in free space of a table with no live
  rows, matched by its declared column types (text columns must hold
  readable text).

Not recovered: rows of DROPPED tables (owner decision 2026-11-17: phone
apps delete, update and insert rows; dropping a table is rare, e.g. a
schema migration that rebuilds a table). FQLite 5.21 does this (corpus 0A:
99/100) by carving old sqlite_master entries.

Scored on the SQLite Forensic Corpus by tests/test_sqlite_corpus.py (with
the same-day comparison with FQLite and DC3 sqlite_dissect in its header).

A record equal to the committed or checkpoint version of its row is never
returned, nor a pattern match equal to a live row: those are copies, not
lost data (counted in the notes). Nothing is dropped for being doubtful;
it is labelled ('table_certain', 'truncated', the report's confidence
gate). WITHOUT ROWID tables are not carved.

RECOVERY_VERSION changes whenever what a given set of bytes yields changes
(discovery, decoding, attribution or labels), so cached results made by an
older version are not reused and a report records which version made it.
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
from dataclasses import dataclass, field

import sqlite_carve as sc
import sqlite_reader

RECOVERY_VERSION = 14

CERTAIN_METHODS = {'checkpoint_deleted', 'checkpoint_older', 'wal_frame', 'freeblock',
                   'page_gap', 'unfinished_write'}


@dataclass
class TableInfo:
    """What attribution needs to know about one table (rowid tables only)."""
    name: str
    columns: list
    id_col: str | None
    root: int | None                       # in the committed view
    live: dict                             # rowid -> values tuple (committed)
    before: dict | None = None             # rowid -> values (checkpoint view), if any
    before_root: int | None = None
    notnull: set = field(default_factory=set)
    declared: list = field(default_factory=list)   # declared column types, record order
    same_width: int = 1        # tables in this database with this many columns (itself included)

    @property
    def ncols(self) -> int:
        return len(self.columns)


def _rows_by_rowid(conn: sqlite3.Connection, table: str) -> dict | None:
    """{rowid: values} of the table's live rows, or None if it cannot be
    read. TEXT that is not valid UTF-8 (WebKit IndexedDB's Records.key
    holds binary keys) is read as the carver decodes carved text
    (sqlite_carve.decode_value: UTF-8 with U+FFFD for bad bytes), so live
    and carved versions compare equal; before 2026-11-19 one such value
    made the whole table uncarved (IOS17: every WebKit IndexedDB Records
    table)."""
    try:
        return {r[0]: tuple(r[1:]) for r in conn.execute(f'SELECT rowid, * FROM "{table}"')}
    except sqlite3.OperationalError as exc:
        if 'decode' not in str(exc).lower():
            return None
    except sqlite3.Error:
        return None
    old = conn.text_factory
    conn.text_factory = lambda b: b.decode('utf-8', errors='replace')
    try:
        return {r[0]: tuple(r[1:]) for r in conn.execute(f'SELECT rowid, * FROM "{table}"')}
    except sqlite3.Error:
        return None
    finally:
        conn.text_factory = old


def _root(conn, table):
    row = conn.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone()
    return row[0] if row and row[0] else None


def table_info(conn: sqlite3.Connection, table: str,
               checkpoint_conn: sqlite3.Connection | None = None) -> TableInfo | None:
    """TableInfo for *table* from the committed connection (and the
    checkpoint view's, when there is one), or None when the table does not
    exist or has no rowid."""
    try:
        root = _root(conn, table)
        if root is None:
            return None
        columns = sc.record_column_names(conn, table)
        live = _rows_by_rowid(conn, table)
        if live is None:
            return None
        id_col = sc.rowid_alias_column(conn, table)
        notnull = sc.notnull_columns(conn, table) - ({id_col} if id_col else set())
        declared = [r[2] or '' for r in conn.execute(f'PRAGMA table_info("{table}")')]
        same_width = 0
        for (other,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            try:
                if len(conn.execute(f'PRAGMA table_info("{other}")').fetchall()) == len(columns):
                    same_width += 1
            except sqlite3.Error:
                continue
    except sqlite3.Error:
        return None
    before = before_root = None
    if checkpoint_conn is not None:
        try:
            before_root = _root(checkpoint_conn, table)
            before = _rows_by_rowid(checkpoint_conn, table) if before_root else {}
        except sqlite3.Error:
            before = before_root = None
    return TableInfo(table, columns, id_col, root, live, before, before_root, notnull, declared,
                     max(1, same_width))


def _kind(v) -> str:
    """A value's storage class as the shape check compares it: an integer and
    a real are both 'num' (SQLite stores a REAL that is whole as an integer
    on disk, and returns it as a float)."""
    if v is None:
        return 'null'
    if isinstance(v, (int, float)):
        return 'num'
    if isinstance(v, str):
        return 'text'
    return 'blob'


MIN_SHAPE_ROWS = 3        # fewer live rows than this: no shape to check against


def _shape(rows, ncols) -> list | None:
    """Per column, the storage classes its rows hold, or None when there are
    too few rows to tell."""
    rows = list(rows)
    if len(rows) < MIN_SHAPE_ROWS:
        return None
    out = [set() for _ in range(ncols)]
    for r in rows:
        for i, v in enumerate(r[:ncols]):
            out[i].add(_kind(v))
    return out


def _affinity(decl: str) -> str:
    """SQLite's column affinity for a declared type (sqlite.org/datatype3.html 3.1)."""
    d = (decl or '').upper()
    if 'INT' in d:
        return 'INTEGER'
    if 'CHAR' in d or 'CLOB' in d or 'TEXT' in d:
        return 'TEXT'
    if 'BLOB' in d or not d:
        return 'BLOB'
    if 'REAL' in d or 'FLOA' in d or 'DOUB' in d:
        return 'REAL'
    return 'NUMERIC'


def _type_allowed(t: int, affinity: str) -> bool:
    """Whether serial type *t* (not NULL) can be stored in a column of this
    affinity by an ordinary insert."""
    if t in (10, 11):
        return False
    is_int, is_real = t in (1, 2, 3, 4, 5, 6, 8, 9), t == 7
    is_text, is_blob = t >= 13 and t % 2 == 1, t >= 12 and t % 2 == 0
    if affinity in ('INTEGER', 'REAL'):
        return is_int or is_real
    if affinity == 'TEXT':
        return is_text
    if affinity == 'NUMERIC':
        return is_int or is_real or is_text
    return True


def _h(image: bytes) -> bytes:
    return hashlib.blake2b(image, digest_size=16).digest()


def _varint_len(v: int) -> int:
    n = 1
    while v > 0x7f and n < 9:
        v >>= 7
        n += 1
    return n


class FileRecovery:
    """One database file's recovery sources, decoded once and shared by
    every table asked about. *main*: the main file's bytes as extracted
    (the in-flight main under a hot journal). *committed*: the committed
    view's image (sqlite_reader.committed_image) when it differs from
    *main* (a -wal applied or a hot journal rolled back), else None."""

    def __init__(self, main: bytes, wal: bytes | None = None, journal: bytes | None = None,
                 committed: bytes | None = None):
        self.main = main
        self.wal = wal if wal and sqlite_reader.is_wal(wal) else None
        self.journal = journal or None
        self.committed = committed if committed and committed != main else None
        try:
            self.header = sc.parse_db_header(main)
            self.page_size = self.header['page_size']
        except ValueError:
            self.header = None
            self.page_size = (struct.unpack('>I', self.wal[8:12])[0] if self.wal else 0)
        self._memo: dict = {}

    # ── page images ─────────────────────────────────────────────────────
    def _page(self, raw: bytes, page_no: int) -> bytes:
        return sc._page_bytes(raw, self.page_size, page_no)

    def _sources(self):
        """(name, raw) for every whole-file image: main, and committed."""
        out = [('main', self.main)]
        if self.committed:
            out.append(('committed', self.committed))
        return out

    def _wal_frames(self) -> list[dict]:
        if 'frames' not in self._memo:
            frames = []
            if self.wal:
                salt = self.wal[16:24]
                for f in sc.iter_wal_frames(self.wal, self.page_size):
                    off = f['frame_offset']
                    f['older_wal'] = self.wal[off + 8:off + 16] != salt
                    frames.append(f)
            self._memo['frames'] = frames
        return self._memo['frames']

    def _wal_images(self) -> dict:
        if 'wal_images' not in self._memo:
            self._memo['wal_images'] = ({f['page']: f['image'] for f in self._wal_frames()}
                                        if self.wal else {})
        return self._memo['wal_images']

    # ── which table a page belonged to when a WAL transaction committed ──
    def _commit_of(self, frame_index: int) -> int | None:
        """Index of the commit frame that ends *frame_index*'s transaction,
        in the current WAL generation; None for a frame of an earlier
        generation (its base, the main file before a later checkpoint, is
        gone) or one whose transaction never committed."""
        if 'commit_of' not in self._memo:
            frames = self._wal_frames()
            commit_of, nxt = {}, None
            for f in reversed(frames):
                if f['older_wal']:
                    continue
                if f['is_commit']:
                    nxt = f['frame_index']
                commit_of[f['frame_index']] = nxt
            self._memo['commit_of'] = commit_of
        return self._memo['commit_of'].get(frame_index)

    def _page_at(self, upto: int):
        """page_no -> that page as of the commit frame *upto*: its latest
        current-generation frame up to there, else the main file (the
        database at the last checkpoint). How SQLite itself reads it."""
        key = ('by_page',)
        if key not in self._memo:
            by_page: dict = {}
            for f in self._wal_frames():
                if not f['older_wal']:
                    by_page.setdefault(f['page'], []).append((f['frame_index'], f['image']))
            self._memo[key] = by_page
        by_page = self._memo[key]

        def page(pn):
            for k, image in reversed(by_page.get(pn, ())):
                if k <= upto:
                    return image
            return self._page(self.main, pn)
        return page

    def _owners_at(self, upto: int) -> dict:
        """{leaf page: table name} in the database as of commit frame *upto*:
        each table's root read from sqlite_master as it was then (name,
        rootpage; the CREATE text is not needed, so a long one that spills
        onto overflow pages does not hide the table), each b-tree walked
        through that commit's pages. Memoised per commit."""
        key = ('owners', upto)
        if key in self._memo:
            return self._memo[key]
        page = self._page_at(upto)
        owners: dict = {}

        def leaves_of(root):
            out, stack, seen = [], [root], set()
            while stack:
                pn = stack.pop()
                if pn in seen or pn < 1:
                    continue
                seen.add(pn)
                img = page(pn)
                ho = 100 if pn == 1 else 0
                if len(img) < ho + 12:
                    continue
                kind = img[ho]
                if kind == sc._LEAF_TABLE_PAGE:
                    out.append(pn)
                elif kind == sc._INTERIOR_TABLE_PAGE:
                    n = struct.unpack('>H', img[ho + 3:ho + 5])[0]
                    stack.append(struct.unpack('>I', img[ho + 8:ho + 12])[0])
                    for i in range(n):
                        co = struct.unpack('>H', img[ho + 12 + 2 * i:ho + 14 + 2 * i])[0]
                        if co and co + 4 <= len(img):
                            stack.append(struct.unpack('>I', img[co:co + 4])[0])
            return out
        try:
            roots = {}
            for pn in leaves_of(1):                 # sqlite_master
                img = page(pn)
                ho = 100 if pn == 1 else 0
                n = struct.unpack('>H', img[ho + 3:ho + 5])[0]
                for i in range(n):
                    co = struct.unpack('>H', img[ho + 8 + 2 * i:ho + 10 + 2 * i])[0]
                    if not co or co >= len(img):
                        continue
                    plen, a = sc.read_varint(img, co)
                    _rid, b = sc.read_varint(img, co + a)
                    start = co + a + b
                    payload = img[start:start + plen]
                    try:
                        types, hlen = sc.parse_record_header(payload, 0)
                        values, _cut = sc.decode_body(payload, types[:4], hlen)
                    except ValueError:
                        continue
                    if (len(values) >= 4 and values[0] == 'table' and isinstance(values[1], str)
                            and isinstance(values[3], int) and values[3] > 0):
                        roots[values[1]] = values[3]
            for name, root in roots.items():
                for pn in leaves_of(root):
                    owners.setdefault(pn, name)
        except (ValueError, IndexError, struct.error):
            owners = {}
        self._memo[key] = owners
        return owners

    def _owner_when_written(self, frame_index: int, page_no: int) -> tuple[str | None, int | None]:
        """(table name, commit frame index): the table whose leaf *page_no*
        was when the transaction holding *frame_index* committed; (None,
        None) when that cannot be shown."""
        upto = self._commit_of(frame_index)
        if upto is None:
            return None, None
        return self._owners_at(upto).get(page_no), upto

    def _journal_records(self) -> list[tuple[int, bytes, int]]:
        if 'journal' not in self._memo:
            out = []
            j = self.journal
            if j and len(j) >= 28 and j[:8] == sqlite_reader.JOURNAL_MAGIC:
                _n, _nonce, _orig, sector, page_size = struct.unpack('>IIIII', j[8:28])
                if page_size and sector and not page_size & (page_size - 1):
                    off = sector
                    step = page_size + 8
                    while off + step <= len(j):
                        pno = struct.unpack('>I', j[off:off + 4])[0]
                        if pno:
                            out.append((pno, j[off + 4:off + 4 + page_size], off + 4))
                        off += step
            self._memo['journal'] = out
        return self._memo['journal']

    def _origin(self, source: str, page_no: int, image: bytes) -> tuple[str, int]:
        """(file, file offset of the page's first byte) holding *image*:
        main for a main-file page; for a committed page, the WAL frame or
        journal record whose image it is (else main if identical)."""
        base_main = (page_no - 1) * self.page_size
        if source == 'main':
            return 'main', base_main
        key = ('origin', page_no, _h(image))
        if key not in self._memo:
            where = None
            for f in reversed(self._wal_frames()):
                if f['page'] == page_no and f['image'] == image:
                    where = ('wal', f['frame_offset'] + 24)
                    break
            if where is None:
                for pno, img, off in self._journal_records():
                    if pno == page_no and img == image:
                        where = ('journal', off)
                        break
            if where is None:
                where = ('main', base_main)
            self._memo[key] = where
        return self._memo[key]

    def _cells(self, page_no: int, image: bytes) -> list[dict]:
        key = ('cells', page_no, _h(image))
        if key not in self._memo:
            ho = 100 if page_no == 1 else 0
            try:
                cells = (sc.decode_leaf_page_cells(image, page_no, ho)
                         if len(image) > ho and image[ho] == sc._LEAF_TABLE_PAGE else [])
            except (ValueError, IndexError):
                cells = []
            self._memo[key] = cells
        return self._memo[key]

    def _free_space_cells(self, page_no: int, image: bytes) -> list[dict]:
        key = ('free', page_no, _h(image))
        if key not in self._memo:
            ho = 100 if page_no == 1 else 0
            out = []
            if len(image) > ho and image[ho] == sc._LEAF_TABLE_PAGE:
                for start, end, kind in sc._free_byte_ranges(image, ho):
                    out.extend(sc._brute_force_records(image[start:end], start, page_no, kind))
            self._memo[key] = out
        return self._memo[key]

    def _freed_cells(self) -> list[tuple[str, dict]]:
        """(source, cell) for every cell on a freed page of main and of
        committed (a page image both share is decoded once)."""
        if 'freed' not in self._memo:
            out, done = [], set()
            for source, raw in self._sources():
                try:
                    hdr = sc.parse_db_header(raw)
                except ValueError:
                    continue
                for pno in sc.iter_freelist_pages(raw, self.page_size, hdr):
                    page = self._page(raw, pno)
                    if len(page) < self.page_size or page[0] != sc._LEAF_TABLE_PAGE:
                        continue
                    k = (pno, _h(page))
                    if k in done:
                        continue
                    done.add(k)
                    try:
                        cells = sc.decode_leaf_page_cells(page, pno)
                    except (ValueError, IndexError):
                        continue
                    out.extend((source, dict(c, _image=page)) for c in cells)
            self._memo['freed'] = out
        return self._memo['freed']

    def _overflow(self) -> list[dict]:
        """Overflowing deleted records, each with its record header and
        values decoded once (keys 'types', 'values', 'truncated'); records
        whose header does not decode are left out."""
        if 'overflow' not in self._memo:
            try:
                found = sc.carve_overflowing_records(self.main, self.wal)
            except Exception:
                found = []
            out = []
            for o in found:
                try:
                    types, hlen = sc.parse_record_header(o['payload'], 0)
                    values, truncated = sc.decode_body(o['payload'], types, hlen)
                except ValueError:
                    continue
                out.append(dict(o, types=types, values=list(values), truncated=truncated))
            self._memo['overflow'] = out
        return self._memo['overflow']

    def leaves(self, t: TableInfo) -> set:
        """This table's leaf pages in main and in committed (a table created
        after the last checkpoint has pages only in the WAL)."""
        out: set = set()
        roots = {r for r in (t.root, t.before_root) if r}
        for _source, raw in self._sources():
            for r in roots:
                try:
                    out |= set(sc.walk_table_leaf_pages(raw, self.page_size, r, self._wal_images()))
                except Exception:
                    continue
        return out

    def _leaf_images(self, leaves) -> list[tuple[str, int, bytes]]:
        """(source, page, image) for each distinct image of each leaf page."""
        out, done = [], set()
        for source, raw in self._sources():
            for pno in sorted(leaves):
                page = self._page(raw, pno)
                if len(page) < self.page_size:
                    page = self._wal_images().get(pno, b'')
                    if not page:
                        continue
                k = (pno, _h(page))
                if k in done:
                    continue
                done.add(k)
                out.append((source, pno, page))
        return out

    # ── the per-table answer ────────────────────────────────────────────
    def records(self, t: TableInfo) -> tuple[list[dict], list[str]]:
        """(records, notes) for table *t*. Each record: method, rowid
        (None when lost), values (list, len <= t.ncols: a cut-off record has
        fewer), truncated, file ('main'|'wal'|'journal'), offset (in that
        file), length, page, table_certain, older_version (an earlier
        version of a row that is still live), older_wal, frame_index,
        overflow_status, in_unfinished_write (found on a page the unfinished
        change had already written: part of that change, never committed)."""
        if not self.page_size:
            return [], []
        ncols = t.ncols
        id_idx = t.columns.index(t.id_col) if t.id_col in t.columns else None
        live = t.live
        before = t.before or {}
        leaves = self.leaves(t)
        out: list[dict] = []
        seen: set = set()
        skipped = {'live_copy': 0, 'implausible_text': 0, 'ambiguous_rebuild': 0,
                   'wrong_shape': 0}
        shape = _shape(list(live.values()) + list(before.values()), ncols)

        def odd(v):
            """Mechanical signs of random bytes: text that was not valid
            UTF-8 (decoded with U+FFFD), or a real number no app writes
            (not finite, or a magnitude outside 1e-30 .. 1e30)."""
            if isinstance(v, str):
                return '\ufffd' in v
            if isinstance(v, float):
                a = abs(v)
                return a != a or a == float('inf') or (a != 0 and (a < 1e-30 or a > 1e30))
            return False
        live_odd = [any(odd(r[i]) for r in list(live.values()) + list(before.values())
                        if i < len(r)) for i in range(ncols)]

        def shape_ok(values):
            for i, v in enumerate(values):
                if i != id_idx and i < ncols and odd(v) and not live_odd[i]:
                    skipped['wrong_shape'] += 1
                    return False
            return _shape_ok(values)

        def _shape_ok(values):
            """False when a carved candidate holds, in some column, a kind of
            value (number / text / blob / null) none of the table's live rows
            hold there: random bytes that happened to decode, not a record of
            this table (corpus test 2026-11-17: every false record failed
            this). The id column is not checked (the rowid stands in)."""
            if shape is None:
                return True
            for i, v in enumerate(values):
                if i == id_idx or i >= len(shape):
                    continue
                if _kind(v) not in shape[i]:
                    skipped['wrong_shape'] += 1
                    return False
            return True

        def empty(vals):
            return all(v in (None, '', b'') for i, v in enumerate(vals) if i != id_idx)

        def plausible(values) -> bool:
            """The checks a carved record must pass, without counting: not
            empty, readable text, no impossible number, the table's value
            kinds. Used to choose among the ways an overwritten header can be
            rebuilt before deciding it is ambiguous."""
            if empty(list(values) + [None] * (ncols - len(values))):
                return False
            for i, v in enumerate(values):
                if isinstance(v, str) and not sc._text_plausible(v):
                    return False
                if i != id_idx and i < ncols and odd(v) and not live_odd[i]:
                    return False
                if shape is not None and i != id_idx and i < len(shape) and _kind(v) not in shape[i]:
                    return False
            return True

        damaged = set()
        found_noid: list = []          # every version listed so far, for pattern dedupe

        def guarded(fn, pno, *a):
            """fn(pno, ...) or [] when that page's structures are too damaged
            to search; the page is counted, so one bad page never stops the
            rest of the table being carved."""
            try:
                return fn(pno, *a)
            except (ValueError, IndexError, struct.error, KeyError, TypeError):
                damaged.add(pno)
                return []

        def rec(method, rowid, values, file, offset, length, page, **extra):
            r = {'method': method, 'rowid': rowid, 'values': values, 'truncated': False,
                 'file': file, 'offset': offset, 'length': length, 'page': page,
                 'table_certain': method in CERTAIN_METHODS,
                 'older_version': (rowid is not None and rowid in live
                                   and method != 'unfinished_write'),
                 'older_wal': False, 'frame_index': None, 'overflow_status': None,
                 'in_unfinished_write': method == 'unfinished_write'}
            r.update(extra)
            if r['older_wal']:
                r['table_certain'] = False
            # A real record partly overwritten since (e.g. the page's own
            # header or a later cell written over its tail): zero bytes in
            # its text. Kept, labelled, never cleaned up.
            r['partly_overwritten'] = any(isinstance(v, str) and '\x00' in v for v in values)
            out.append(r)

        def keep(method, cell, file, base, carved=False, **extra):
            values = list(cell['values'])
            if len(values) != ncols:
                return
            rowid = cell['rowid']
            if carved and (rowid in live or empty(values)):
                return
            if carved and not shape_ok(values):
                return
            if id_idx is not None:
                values[id_idx] = rowid
            version = tuple(values)
            if live.get(rowid) == version or before.get(rowid) == version:
                return
            if (rowid, version) in seen:
                return
            seen.add((rowid, version))
            found_noid.append(version)
            rec(method, rowid, values, file, base + cell['offset'],
                cell.get('cell_len') or cell.get('length'), cell['page'], **extra)

        # checkpoint view vs committed: deleted or changed since the checkpoint
        if t.before is not None:
            where = {}
            for _s, pno, image in self._leaf_images(leaves):
                if _s != 'main':
                    continue
                for c in self._cells(pno, image):
                    where.setdefault(c['rowid'], c)
            for rowid, v in t.before.items():
                if live.get(rowid) == v:
                    continue
                c = where.get(rowid)
                method = 'checkpoint_deleted' if rowid not in live else 'checkpoint_older'
                seen.add((rowid, tuple(v)))
                found_noid.append(tuple(v))
                rec(method, rowid, list(v), 'main',
                    ((c['page'] - 1) * self.page_size + c['offset']) if c else None,
                    c.get('cell_len') if c else None, c['page'] if c else None)

        # every WAL frame of one of this table's pages
        for f in self._wal_frames():
            if f['page'] not in leaves:
                continue
            for c in guarded(self._cells, f['page'], f['image']):
                keep('wal_frame', c, 'wal', f['frame_offset'] + 24,
                     older_wal=f['older_wal'], frame_index=f['frame_index'])

        # WAL frames of a page that is free now but was one of this table's
        # leaves when the frame's transaction committed: the database as of
        # that commit (main file + the WAL frames up to it, as SQLite reads
        # it) shows the page in this table's b-tree, so its records are this
        # table's, older versions of live rows included: table certain.
        # A page in use by any table now is not read this way (the rule
        # below); nor a frame of an earlier WAL round or an uncommitted one.
        # Before 2026-11-19 these were missed: IOS17 AddressBookImages, three
        # earlier contact photos of live rows, found whole in the WAL.
        in_use_now = self._pages_in_use()
        for f in self._wal_frames():
            if f['page'] in leaves or f['page'] in in_use_now or f['older_wal']:
                continue
            owner, upto = self._owner_when_written(f['frame_index'], f['page'])
            if owner != t.name:
                continue
            for c in guarded(self._cells, f['page'], f['image']):
                keep('wal_frame', c, 'wal', f['frame_offset'] + 24,
                     frame_index=f['frame_index'], leaf_at_commit=upto)

        # WAL frames of a page that is free now (in no table's b-tree): it
        # left its table, typically when its rows were deleted, so like a
        # freed page its records are matched by column count, an unused
        # rowid and this table's value kinds, table not certain. A page in
        # use by any table now is never read this way (iPhone Viber and
        # Life360 deleted messages, Josh Hickman ground truth, 2026-11-17).
        # Column count alone let every table with as many columns claim the
        # same records (Pixel: 15,276 records, 10,800 in one Messenger
        # database), so a record must also match this table's own record
        # pattern, learned from its live rows: a table with fewer than five
        # live rows (no pattern) gets none of these.
        in_use = self._pages_in_use()
        free_sig = None
        try:
            present = [p for p in leaves if len(self._page(self.committed or self.main, p)) == self.page_size]
            free_sig = sc.sample_header_signature(self.committed or self.main, self.page_size, present)
        except Exception:
            free_sig = None
        # No pattern (a wiped table): only when no other table in the database
        # has this many columns, and the record's types fit the declared
        # column types (iPhone Viber / Life360 messages: their table had been
        # emptied, the only 23-column table, Josh Hickman ground truth).
        has_sig = bool(free_sig and free_sig['anchors'] and free_sig['length'] == ncols)
        free_tpl = None if has_sig else (self._schema_template(t, need_text=False)
                                         if t.same_width == 1 else None)

        def free_ok(types):
            if has_sig:
                return sc._matches_signature(types, free_sig)
            if free_tpl is None or len(types) != ncols:
                return False
            for i, ty in enumerate(types):
                if i == free_tpl['id_idx']:
                    if ty != 0:
                        return False
                elif ty == 0:
                    if i in free_tpl['notnull']:
                        return False
                elif not _type_allowed(ty, free_tpl['affinity'][i]):
                    return False
            return True
        if has_sig or free_tpl:
            for f in self._wal_frames():
                if f['page'] in leaves or f['page'] in in_use:
                    continue
                if not f['older_wal'] and self._owner_when_written(f['frame_index'], f['page'])[0]:
                    # whose leaf it was at its commit is known: this table's
                    # records are taken above, another table's are not this
                    # table's (two tables of one shape each took the other's
                    # rows here before 2026-11-19: ground-truth test)
                    continue
                for c in guarded(self._cells, f['page'], f['image']):
                    if not free_ok(c.get('types') or []):
                        continue
                    keep('wal_frame', c, 'wal', f['frame_offset'] + 24, carved=True,
                         older_wal=f['older_wal'], frame_index=f['frame_index'],
                         table_certain=False, page_now_free=True)

        # rollback journal pages (the page before a change)
        journal = self._journal_records()
        for pno, image, off in journal:
            if pno in leaves:
                for c in self._cells(pno, image):
                    keep('journal', c, 'journal', off, table_certain=False)

        # pages the unfinished change had written in the in-flight main file
        # (hot journal rolled back): every page whose main-file image is not
        # the committed one, including pages it added past the committed
        # end (rollback cuts those off; the journal has no record of them).
        # Anything found on their main-file image is part of that change.
        inflight = self._inflight_pages() if journal else set()

        # the unfinished change itself: its versions of this table's rows
        for pno in sorted(inflight & leaves):
            for c in self._cells(pno, self._page(self.main, pno)):
                keep('unfinished_write', c, 'main', (pno - 1) * self.page_size)

        # rows the unfinished change had removed: on a page it wrote, the
        # committed image holds the row and no in-flight page of this table
        # does. Either the app was deleting it, or moving it to a page it
        # had not written yet (the in-flight file is only part-written).
        if inflight:
            inflight_rowids = set()
            for pno in leaves:
                for c in self._cells(pno, self._page(self.main, pno)):
                    inflight_rowids.add(c['rowid'])
            for pno in sorted(inflight & leaves):
                image = self._page(self.committed, pno)
                file, base = self._origin('committed', pno, image)
                for c in self._cells(pno, image):
                    rowid = c['rowid']
                    if rowid in inflight_rowids or rowid not in live:
                        continue
                    if ('udel', rowid) in seen:
                        continue
                    seen.add(('udel', rowid))
                    rec('unfinished_delete', rowid, list(live[rowid]), file, base + c['offset'],
                        c.get('cell_len'), pno, older_version=False, in_unfinished_write=True)

        def unfinished(source, pno):
            return source == 'main' and pno in inflight

        # free space on this table's pages (cells with an intact length and rowid)
        leaf_images = self._leaf_images(leaves)
        for source, pno, image in leaf_images:
            file, base = self._origin(source, pno, image)
            for c in guarded(self._free_space_cells, pno, image):
                keep(c['source'], c, file, base, carved=True,
                     in_unfinished_write=unfinished(source, pno))

        # freed pages: no table's any more, matched by column count only
        for source, c in self._freed_cells():
            file, base = self._origin(source, c['page'], c['_image'])
            if file == 'wal' and self.page_size:
                # its image is a WAL frame: whose leaf the page was at that
                # frame's commit, when known, settles it (the WAL frames
                # above took this table's; another table's are not this one's)
                # (nothing is known for an earlier WAL round's or an
                # uncommitted frame: _owner_when_written gives None)
                fi = (base - 24 - 32) // (24 + self.page_size)
                if self._owner_when_written(fi, c['page'])[0]:
                    continue
            keep('freed_page', c, file, base, carved=True, table_certain=False)

        # overflowing deleted records
        live_rows = list(live.values()) + list(before.values())
        live_set = None
        for o in self._overflow():
            if len(o['types']) != ncols:
                continue
            src = o.get('source', '')
            on_table = o['page'] in leaves
            leaf_at_commit = None
            if not on_table and o.get('wal_frame_offset') is not None and self.page_size:
                # a WAL page free now that was this table's leaf when its
                # transaction committed (see the WAL frames above)
                fi = (o['wal_frame_offset'] - 32) // (24 + self.page_size)
                if o['page'] not in self._pages_in_use():
                    owner, upto = self._owner_when_written(fi, o['page'])
                    if owner == t.name:
                        on_table, leaf_at_commit = True, upto
            if not (on_table or src.startswith('freed_page')):
                continue
            values = list(o['values'])[:ncols]
            rowid = o.get('rowid')
            if rowid is not None and id_idx is not None and id_idx < len(values):
                values[id_idx] = rowid
            full = len(values) == ncols and o.get('status') == 'complete'
            if full and (live.get(rowid) == tuple(values) or before.get(rowid) == tuple(values)):
                continue
            if live_set is None and rowid is None and full:
                live_set = set(live_rows)
            if rowid is None and full and tuple(values) in live_set:
                skipped['live_copy'] += 1
                continue
            key = ('ovf', o['page'], o['offset'])
            if key in seen:
                continue
            seen.add(key)
            if o.get('wal_frame_offset') is not None:
                file, offset = 'wal', o['wal_frame_offset'] + 24 + o['offset']
            else:
                file, offset = 'main', (o['page'] - 1) * self.page_size + o['offset']
            # offset: the record header on the page; length: the record's
            # on-page part plus its 4-byte overflow pointer (the rest is on
            # overflow pages elsewhere in the file), so a hex view of
            # offset+length shows this record's bytes and nothing else
            usable = self.page_size - ((self.header or {}).get('reserved_bytes', 0) or 0)
            local, _ovf = sc._cell_local_payload_size(o.get('payload_len') or 0, usable)
            rec('overflow', rowid, values, file, offset, local + 4, o['page'],
                truncated=not full, overflow_status=o.get('status'),
                table_certain=on_table and not src.startswith('freed_page'),
                leaf_at_commit=leaf_at_commit)

        # record-pattern searches (no rowid): copies of live rows are dropped
        def without_id(vals):
            return tuple(v for i, v in enumerate(vals) if i != id_idx)
        prefixes: dict = {}

        def known_prefix(n):
            if n not in prefixes:
                prefixes[n] = {without_id(list(v)[:n]) for v in live_rows}
            return prefixes[n]

        def already_listed(version, n):
            """A pattern match equal (on the columns it has) to a record already
            listed with its rowid is the same record found again."""
            return any(without_id(list(v)[:n]) == version for v in found_noid)

        def keep_pattern(method, values, read, truncated, file, offset, length, page, page_off,
                         in_unfinished_write=False):
            if empty(values + [None] * (ncols - len(values))):
                return
            if read < ncols and read < 2:
                return                       # one cut-off value proves nothing
            if any(isinstance(v, str) and not sc._text_plausible(v) for v in values):
                skipped['implausible_text'] += 1
                return
            if not shape_ok(values):
                return
            if id_idx is not None and id_idx < len(values):
                values[id_idx] = None
            version = without_id(values[:read])
            if version in known_prefix(read):
                skipped['live_copy'] += 1
                return
            if already_listed(version, read):
                return
            if ('sig', read, version) in seen or ('ovf', page, page_off) in seen:
                return
            seen.add(('sig', read, version))
            rec(method, None, values, file, offset, length, page,
                truncated=truncated or read < ncols, in_unfinished_write=in_unfinished_write)

        signatures = {}
        for source, raw in self._sources():
            present = [p for p in leaves if len(self._page(raw, p)) == self.page_size]
            try:
                signatures[source] = sc.sample_header_signature(raw, self.page_size, present)
            except Exception:
                signatures[source] = None
        if not any(sig and sig['anchors'] for sig in signatures.values()):
            # No live rows to learn this table's record layout from (a wiped
            # table): use its declared column types instead, but only for a
            # table with a text column, whose every text column must then hold
            # readable text (an all-number layout matches random bytes).
            template = self._schema_template(t)
            if template:
                for source, pno, image in leaf_images:
                    file, base = self._origin(source, pno, image)
                    for m in guarded(self._schema_matches, pno, image, template):
                        keep_pattern('header_schema', m['values'], len(m['values']),
                                     m['truncated'], file, base + m['header_offset'], m['length'],
                                     pno, m['header_offset'], unfinished(source, pno))
        rebuild_template = self._schema_template(t, need_text=False)
        for source, pno, image in leaf_images:
            sig = signatures.get(source) or signatures.get('main')
            file, base = self._origin(source, pno, image)
            if sig and sig['anchors']:
                for m in guarded(self._signature_matches, pno, image, sig):
                    values = list(m['values'])[:ncols]
                    keep_pattern('header_signature', values, len(values), m['truncated'],
                                 file, base + m['header_offset'], m['length'], pno,
                                 m['header_offset'], unfinished(source, pno))
            # overwritten headers: rebuilt from the live-row pattern when it
            # fixes the lost type, else from the declared column types
            for m in guarded(self._rebuilt_matches, pno, image, sig, ncols, skipped, plausible,
                             repr(shape), rebuild_template):
                keep_pattern('header_rebuilt', m['values'], len(m['values']), m['truncated'],
                             file, base + m['cell_offset'], m['length'], pno, m['cell_offset'],
                             unfinished(source, pno))

        notes = []
        if damaged:
            notes.append(f"{len(damaged)} page(s) had damaged structures and were not fully "
                         f"searched: {', '.join(map(str, sorted(damaged)[:10]))}"
                         f"{' …' if len(damaged) > 10 else ''}.")
        if skipped['live_copy']:
            notes.append(f"{skipped['live_copy']} record(s) found by pattern were copies of live "
                         "rows and are not listed.")
        if skipped['implausible_text']:
            notes.append(f"{skipped['implausible_text']} pattern match(es) decoded as unreadable "
                         "text and are not listed.")
        if skipped['wrong_shape']:
            notes.append(f"{skipped['wrong_shape']} candidate(s) held a kind of value (number, "
                         "text, blob, empty) this table's live rows never hold in that column, "
                         "text that is not valid UTF-8, or an impossible number, so are taken "
                         "to be random bytes, and are not listed.")
        if skipped['ambiguous_rebuild']:
            notes.append(f"{skipped['ambiguous_rebuild']} overwritten record header(s) could be "
                         "rebuilt more than one way and are not listed.")
        return out, notes

    def _pages_in_use(self) -> set:
        """Pages any b-tree (table, index, sqlite_master), live overflow
        chain or freelist trunk uses in the committed view (else main)."""
        if 'in_use' not in self._memo:
            raw = self.committed or self.main
            try:
                self._memo['in_use'] = sc.allocated_pages(raw, sc.parse_db_header(raw))
            except Exception:
                self._memo['in_use'] = None
        got = self._memo['in_use']
        return got if got is not None else set(range(1, 1 << 31))   # unknown: treat all as in use

    def _inflight_pages(self) -> set:
        if 'inflight' not in self._memo:
            out = set()
            if self.committed and self.journal and self.page_size:
                n = len(self.main) // self.page_size
                for p in range(1, n + 1):
                    if self._page(self.main, p) != self._page(self.committed, p):
                        out.add(p)
            self._memo['inflight'] = out
        return self._memo['inflight']

    @staticmethod
    def _schema_template(t: TableInfo, need_text: bool = True) -> dict | None:
        affs = [_affinity(d) for d in t.declared]
        if len(affs) != t.ncols or (need_text and 'TEXT' not in affs):
            return None
        id_idx = t.columns.index(t.id_col) if t.id_col in t.columns else None
        return {'affinity': affs, 'notnull': {i for i, c in enumerate(t.columns) if c in t.notnull},
                'id_idx': id_idx, 'ncols': t.ncols}

    def _schema_matches(self, page_no: int, image: bytes, tpl: dict) -> list[dict]:
        """Record headers in free space whose column count and serial types
        fit the table's declared column types (see records())."""
        key = ('schema', page_no, _h(image), repr(tpl['affinity']), repr(sorted(tpl['notnull'])))
        if key in self._memo:
            return self._memo[key]
        out = []
        ho = 100 if page_no == 1 else 0
        size = len(image)
        if size > ho and image[ho] == sc._LEAF_TABLE_PAGE:
            affs, ncols, id_idx = tpl['affinity'], tpl['ncols'], tpl['id_idx']
            text_cols = [i for i, a in enumerate(affs) if a == 'TEXT']
            seen_body = set()
            for start, end, _k in sc._free_byte_ranges(image, ho):
                end = min(end, size)
                bounded = image[:end]
                for offset in range(start, end):
                    try:
                        types, header_len = sc.parse_record_header(bounded, offset, max_cols=ncols + 1)
                    except ValueError:
                        continue
                    if len(types) != ncols:
                        continue
                    ok = True
                    for i, ty in enumerate(types):
                        if i == id_idx:
                            ok = ty == 0
                        elif ty == 0:
                            ok = i not in tpl['notnull'] and i not in text_cols
                        else:
                            ok = _type_allowed(ty, affs[i])
                        if not ok:
                            break
                    if not ok:
                        continue
                    body_start = offset + header_len
                    if body_start in seen_body:
                        continue
                    values, truncated = sc.decode_body(bounded, types, body_start)
                    if truncated or len(values) != ncols:
                        continue
                    if not all(isinstance(values[i], str) and len(values[i]) >= 1
                               and sc._text_plausible(values[i]) for i in text_cols):
                        continue
                    seen_body.add(body_start)
                    out.append({'header_offset': offset, 'values': list(values), 'truncated': False,
                                'length': header_len + sum(sc._serial_type_size(x) for x in types)})
        self._memo[key] = out
        return out

    # ── pattern searches on one page image ───────────────────────────────
    def _signature_matches(self, page_no: int, image: bytes, sig: dict) -> list[dict]:
        key = ('sig', page_no, _h(image), repr(sorted(sig['anchors'].items())), sig['length'])
        if key not in self._memo:
            ho = 100 if page_no == 1 else 0
            out = []
            if len(image) > ho and image[ho] == sc._LEAF_TABLE_PAGE:
                seen_body = set()
                for start, end, _k in sc._free_byte_ranges(image, ho):
                    bounded = image[:end]
                    for offset in range(start, end):
                        try:
                            types, header_len = sc.parse_record_header(bounded, offset)
                        except ValueError:
                            continue
                        if not sc._matches_signature(types, sig):
                            continue
                        body_start = offset + header_len
                        if body_start in seen_body:
                            continue
                        seen_body.add(body_start)
                        values, truncated = sc.decode_body(bounded, types, body_start)
                        if not values:
                            continue
                        length = header_len + sum(sc._serial_type_size(t) for t in types)
                        if truncated:
                            length = min(length, end - offset)
                        out.append({'header_offset': offset, 'values': values,
                                    'truncated': truncated, 'length': length})
            self._memo[key] = out
        return self._memo[key]

    def _rebuilt_matches(self, page_no: int, image: bytes, sig: dict, ncols: int,
                         skipped: dict, plausible=lambda v: True, shape_key: str = '',
                         template: dict | None = None) -> list[dict]:
        """Deleted records whose first 4 bytes were overwritten by a
        freeblock link, losing the payload length, the rowid AND the first
        header bytes (short rows, low rowids). When neighbouring rows are
        deleted the freeblocks merge, and each deleted row keeps the link
        that was written at its own start, so every offset in free space
        whose 4 bytes look like a freeblock link (size >= 4; next 0 or a
        later offset in the page) is a candidate start, not only the
        current freeblocks' starts.

        The 4 lost bytes always include at least one byte of length and one
        of rowid, so at most the header-length byte and ONE serial type are
        lost. For each way they can split (length varint 1-3 bytes, rowid
        1-4), the lost type is this table's pattern anchor when it has one,
        else every one-byte type the column's declared type allows
        (*template*: works with no live rows, and for number-only tables).
        A way is valid when every type is valid, every anchor matches, the
        rebuilt length takes exactly the varint size assumed, the cell lies
        inside the free range, and the values pass *plausible*. Among valid
        ways, the ones whose freeblock-link size equals the rebuilt cell's
        length exactly (the record was freed on its own) win. One distinct
        result: listed; several: not listed (counted)."""
        anchors = (sig or {}).get('anchors') or {}
        key = ('rebuilt', page_no, _h(image), repr(sorted(anchors.items())),
               (sig or {}).get('length'), shape_key, repr(template))
        if key in self._memo:
            out, amb = self._memo[key]
            skipped['ambiguous_rebuild'] += amb
            return out
        out, amb = [], 0
        ho = 100 if page_no == 1 else 0
        size = len(image)
        shape_len_ok = sig is None or sig.get('length') in (None, ncols)
        if (shape_len_ok and (anchors or template) and size > ho
                and image[ho] == sc._LEAF_TABLE_PAGE):
            # A freeblock's own 4-byte link is part of its range here.
            # Clamped to the page: a damaged freeblock can claim a size or
            # offset past its end (corpus 17-10, 17-11).
            ranges = [(max(0, start - (4 if kind == 'freeblock' else 0)), min(end, size))
                      for start, end, kind in sc._free_byte_ranges(image, ho)]
            taken_until = -1
            for lo, hi in ranges:
                for cell in range(lo, hi - 4):
                    if cell < taken_until or cell + 4 > size:
                        continue
                    nxt, fsize = struct.unpack('>HH', image[cell:cell + 4])
                    if fsize < 4 or not (nxt == 0 or cell < nxt < size):
                        continue
                    found = []
                    for a in (1, 2, 3):                     # payload-length varint size
                        for b in (1, 2, 3, 4):              # rowid varint size
                            lost = 4 - a - b                # header bytes overwritten
                            if lost < 1:
                                continue
                            for r in self._rebuild_ways(image, cell, a, b, lost, anchors,
                                                        ncols, hi, template):
                                if plausible(r['values']):
                                    found.append(r)
                    exact = [r for r in found if r['length'] == fsize]
                    pick = exact or found
                    distinct = {tuple(map(repr, r['values'])) for r in pick}
                    if len(distinct) == 1:
                        out.append(pick[0])
                        taken_until = cell + pick[0]['length']
                    elif len(distinct) > 1:
                        amb += 1
        self._memo[key] = (out, amb)
        skipped['ambiguous_rebuild'] += amb
        return out

    @staticmethod
    def _rebuild_ways(image, cell, a, b, lost, anchors, ncols, fb_end, template):
        """Every valid rebuild of the record at *cell* for one split."""
        n_lost_types = lost - 1                     # 0 or 1 (see _rebuilt_matches)
        if n_lost_types > 1:
            return []
        options = [[]]
        if n_lost_types == 1:
            if 0 in anchors:
                t = anchors[0]
                options = [[t]] if t <= 0x7f else []
            elif template:
                aff = template['affinity'][0]
                id_col = template['id_idx'] == 0
                nullable = 0 not in template['notnull']
                ts = [0] if id_col else ([0] if nullable else [])
                if not id_col:
                    ts += [t for t in range(1, 0x80) if _type_allowed(t, aff)]
                options = [[t] for t in ts]
            else:
                return []
        out = []
        for lost_types in options:
            r = FileRecovery._rebuild_one(image, cell, a, b, lost_types, anchors, ncols,
                                          fb_end, template)
            if r:
                out.append(r)
        return out

    @staticmethod
    def _rebuild_one(image, cell, a, b, lost_types, anchors, ncols, fb_end, template=None):
        """One rebuild of the record at *cell* with the given lost types, or None."""
        types = list(lost_types)
        pos_byte = cell + 4                             # first surviving header byte
        try:
            while len(types) < ncols:
                t, n = sc.read_varint(image, pos_byte)
                if t in (10, 11) or n > 9:
                    return None
                types.append(t)
                pos_byte += n
        except (ValueError, IndexError):
            return None
        for pos, t in anchors.items():
            if pos < len(types) and types[pos] != t:
                return None
        if template:
            for i, t in enumerate(types):
                if i == template['id_idx']:
                    if t != 0:
                        return None
                elif t == 0:
                    if i in template['notnull']:
                        return None
                elif not _type_allowed(t, template['affinity'][i]):
                    return None
        header_len = 1 + sum(_varint_len(t) for t in types)
        if header_len > 0x7f:
            return None
        header_start = cell + a + b
        if header_start + header_len != pos_byte:
            return None
        body_len = sum(sc._serial_type_size(t) for t in types)
        payload_len = header_len + body_len
        if _varint_len(payload_len) != a:
            return None
        cell_len = a + b + payload_len
        if cell + cell_len > fb_end:
            return None
        values, truncated = sc.decode_body(image[:fb_end], types, pos_byte)
        if truncated or len(values) != ncols:
            return None
        return {'cell_offset': cell, 'values': list(values), 'truncated': False,
                'length': cell_len}


def for_connection(main: bytes, wal: bytes | None, journal: bytes | None,
                   conn: sqlite3.Connection) -> FileRecovery:
    """FileRecovery whose committed image comes from *conn* (a committed-view
    connection) when a -wal or -journal makes it differ from *main*."""
    committed = None
    if wal or journal:
        try:
            committed = sqlite_reader.committed_image(conn)
        except Exception:
            committed = None
    return FileRecovery(main, wal, journal, committed)


_DESCRIBE = {
    'checkpoint_deleted': "deleted since the last checkpoint: whole row from the main file",
    'checkpoint_older': "earlier version: changed since the last checkpoint",
    'wal_frame': "older version in the WAL",
    'journal': "older page in the rollback journal; table not certain",
    'unfinished_write': "unfinished write: never committed",
    'unfinished_delete': ("live row the unfinished write had removed (being deleted, or "
                          "moved to a page not yet written); never committed"),
    'freeblock': "",
    'page_gap': "",
    'freed_page': "freed page; table not certain, matched by column count",
    'overflow': "overflowing record",
    'header_signature': "unverified match",
    'header_rebuilt': "record header rebuilt from this table's pattern; unverified match",
    'header_schema': ("found by the table's declared column types (no live rows to learn "
                      "its pattern from); unverified match"),
}


def describe(r: dict) -> str:
    """Plain words for how a record was found and how sure that is ('' for
    an ordinary deleted cell found intact in free space)."""
    text = _DESCRIBE.get(r['method'], r['method'])
    if r['method'] == 'wal_frame' and r.get('page_now_free'):
        text = ("version in the WAL of a page that is free now; table not certain, "
                "matched by column count")
    elif r['method'] == 'wal_frame' and r.get('older_wal'):
        text = "older version in an earlier WAL round; table not certain"
    if r.get('leaf_at_commit') is not None:
        text = (text + "; " if text else "") + (
            f"page free now, this table's when WAL frame {r['leaf_at_commit']} committed")
    if r['method'] == 'overflow':
        status = r.get('overflow_status') or ''
        text += '' if status == 'complete' else f" (partial: {status})"
        if not r.get('table_certain'):
            text += "; table not certain"
    if r.get('in_unfinished_write') and r['method'] not in ('unfinished_write',
                                                              'unfinished_delete'):
        text = (text + "; " if text else "") + "part of the unfinished write, never committed"
    if r.get('partly_overwritten'):
        text = (text + "; " if text else "") + "partly overwritten: zero bytes in its text"
    if r.get('older_version') and r['method'] not in ('checkpoint_older',):
        text = (text + "; " if text else "") + "an earlier version of a row that is still live"
    return text


# ── carving in a worker process (the Database tab) ──────────────────────────
# Measured 2026-11-17 in the real window (Pixel Snapchat simple_db_helper.db):
# carving on a QThread froze the window for 2.8-4.6 s although it was "in the
# background": it is pure-Python decoding, it holds the GIL, and the GUI
# thread, which re-enters Python thousands of times while painting, waits
# for the GIL each time. So the tab carves in one spawned worker process
# that keeps each file's FileRecovery (per-file decoding still done once),
# the same pattern as the embedded-media sweep and Keyword Search pools.

_POOL = None
_CHILD_FILES: dict = {}
_CHILD_MAX = 3


def _pool():
    global _POOL
    if _POOL is None:
        import atexit
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        _POOL = ProcessPoolExecutor(1, mp_context=mp.get_context('spawn'))
        atexit.register(lambda: _POOL.shutdown(wait=False, cancel_futures=True))
    return _POOL


def records_for_files(files: dict, table: str) -> tuple[list, list, int]:
    """In the worker process: (records, notes, ncols) for *table* of the
    prepared database described by *files* ({'key', 'main', 'wal',
    'journal', 'committed', 'db', 'before'}: file paths, None when absent).
    Each file's FileRecovery is kept between calls."""
    def rd(p):
        if not p:
            return None
        with open(p, 'rb') as f:
            return f.read()
    fr = _CHILD_FILES.get(files['key'])
    if fr is None:
        fr = FileRecovery(rd(files['main']), rd(files.get('wal')), rd(files.get('journal')),
                          rd(files.get('committed')))
        while len(_CHILD_FILES) >= _CHILD_MAX:
            _CHILD_FILES.pop(next(iter(_CHILD_FILES)))
        _CHILD_FILES[files['key']] = fr
    conn = sqlite3.connect(f"file:{files['db']}?immutable=1", uri=True)
    ck = (sqlite3.connect(f"file:{files['before']}?immutable=1", uri=True)
          if files.get('before') else None)
    try:
        t = table_info(conn, table, ck)
        if t is None:
            return [], [], 0
        recs, notes = fr.records(t)
        return recs, notes, t.ncols
    finally:
        conn.close()
        if ck is not None:
            ck.close()


def records_in_process(files: dict, table: str) -> tuple[list, list, int]:
    """records_for_files() run in the worker process; blocks the calling
    (non-GUI) thread until done without holding the GIL."""
    return _pool().submit(records_for_files, files, table).result()


# ── kept results (casecache.db recovered_records) ───────────────────────
# JSON, never pickle: the case folder must not be able to run code when its
# cache is read. Bytes are base64 under {"__b__": ...}.

CACHE_MAX_BYTES = 32 * 1024 * 1024       # one table's results larger than this are not kept


def _enc(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        import base64
        return {'__b__': base64.b64encode(bytes(v)).decode('ascii')}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    if isinstance(v, set):
        return sorted(_enc(x) for x in v)
    return v


def _dec(v):
    if isinstance(v, dict):
        if set(v) == {'__b__'}:
            import base64
            return base64.b64decode(v['__b__'])
        return {k: _dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


def encode_records(records: list) -> bytes:
    import json
    import zlib
    return zlib.compress(json.dumps(_enc(records), separators=(',', ':')).encode('utf-8'), 6)


def decode_records(blob: bytes) -> list:
    import json
    import zlib
    return _dec(json.loads(zlib.decompress(blob).decode('utf-8')))


def cache_get(case_dir: str, db_key: str, table: str):
    """(records, notes, ncols) kept for this database and table by this
    RECOVERY_VERSION, or None."""
    if not case_dir:
        return None
    try:
        from db_utils import _open_cache_db
        conn = _open_cache_db(case_dir)
        try:
            row = conn.execute(
                'SELECT records, notes, ncols FROM recovered_records '
                'WHERE db_key = ? AND table_name = ? AND recovery_version = ?',
                (db_key, table, RECOVERY_VERSION)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        import json
        return decode_records(row[0]), json.loads(row[1]), row[2]
    except Exception:
        return None


def cache_put(case_dir: str, db_key: str, table: str, records: list, notes: list,
              ncols: int) -> bool:
    """Keep one table's results; False when not kept (too large, no case)."""
    if not case_dir:
        return False
    try:
        import json
        blob = encode_records(records)
        if len(blob) > CACHE_MAX_BYTES:
            return False
        from db_utils import _open_cache_db
        conn = _open_cache_db(case_dir)
        try:
            conn.execute('INSERT OR REPLACE INTO recovered_records VALUES (?,?,?,?,?,?)',
                         (db_key, table, RECOVERY_VERSION, ncols, blob, json.dumps(notes)))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


# ── where a LIVE row's bytes are (Record mode) ─────────────────────────────

def committed_wal_images(main: bytes, wal: bytes) -> dict:
    """{page: (image, file offset of the image)} for the WAL frames SQLite
    would apply: the current salt, up to the last commit frame. A later
    frame of a page replaces an earlier one."""
    out: dict = {}
    if not wal or not sqlite_reader.is_wal(wal):
        return out
    try:
        page_size = struct.unpack('>I', wal[8:12])[0]
    except struct.error:
        return out
    salt = wal[16:24]
    pending: dict = {}
    for f in sc.iter_wal_frames(wal, page_size):
        off = f['frame_offset']
        if wal[off + 8:off + 16] != salt:
            continue                                  # an earlier WAL generation
        pending[f['page']] = (f['image'], off + 24)
        if f['is_commit']:
            out.update(pending)
            pending = {}
    return out                                        # frames after the last commit: not applied


def locate_committed_row(main: bytes, wal: bytes | None, table: str, rowid: int) -> dict | None:
    """Where the committed version of *table*'s row *rowid* is: {'file':
    'main' | 'wal', 'abs_offset', 'length', 'overflows'}, or None. A page
    the WAL replaced is read from the WAL (its latest committed frame), so a
    row only in the WAL is found and a row the WAL changed is never cited
    at its stale bytes in the main file."""
    try:
        header = sc.parse_db_header(main)
    except ValueError:
        return None
    page_size = header['page_size']
    usable = page_size - (header.get('reserved_bytes', 0) or 0)
    images = committed_wal_images(main, wal) if wal else {}
    st = sqlite_reader.stage('db', main, wal=wal)
    try:
        conn, _ = sqlite_reader.connect_committed(st.path)
        try:
            row = conn.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None

    def page(pno):
        if pno in images:
            return images[pno][0], 'wal', images[pno][1]
        return sc._page_bytes(main, page_size, pno), 'main', (pno - 1) * page_size

    stack, seen = [row[0]], set()
    while stack:
        pno = stack.pop()
        if pno in seen or pno < 1:
            continue
        seen.add(pno)
        img, file, base = page(pno)
        ho = 100 if pno == 1 else 0
        if len(img) < ho + 8:
            continue
        kind = img[ho]
        n = struct.unpack('>H', img[ho + 3:ho + 5])[0]
        if kind == sc._INTERIOR_TABLE_PAGE:
            stack.append(struct.unpack('>I', img[ho + 8:ho + 12])[0])
            for i in range(n):
                p = ho + 12 + 2 * i
                cell = struct.unpack('>H', img[p:p + 2])[0]
                if cell:
                    stack.append(struct.unpack('>I', img[cell:cell + 4])[0])
        elif kind == sc._LEAF_TABLE_PAGE:
            for i in range(n):
                p = ho + 8 + 2 * i
                try:
                    cell = struct.unpack('>H', img[p:p + 2])[0]
                    plen, c1 = sc.read_varint(img, cell)
                    rid, c2 = sc.read_varint(img, cell + c1)
                except (ValueError, struct.error, IndexError):
                    continue
                if sc._to_signed_rowid(rid) != rowid:
                    continue
                local, overflows = sc._cell_local_payload_size(plen, usable)
                return {'file': file, 'abs_offset': base + cell,
                        'length': c1 + c2 + local + (4 if overflows else 0),
                        'overflows': overflows}
    return None
