"""file_table.py — the case's file list and folder contents as one
memory-mapped sidecar file in the case folder.

Added 2026-11-18. Before this, the file list (ui_metadata: path -> a dict of
its metadata) and the folder contents (folder_map: folder -> list of child
paths) were rebuilt on every open from the snapshot blob in casecache.db as
Python objects: ~1,600 bytes per file, 2.9 GB resident and ~4 s for a
2.5M-entry iPhone, and every all-files loop walked millions of dicts. Here
they are written once (when the archive is parsed) the same way
zip_index.py writes the zip directory: columns of numbers, one pool of path
names, an on-disk CRC-32 hash table for path -> row, all mapped read-only,
so an open costs a few milliseconds and the system loads only the pages
used. Measured (2026-11-18, scratch prototype): IOS17 0.86 GB -> 122 MB on
disk and ~5 MB resident; the 2.5M-entry synthetic iPhone 2.9 GB -> 621 MB on
disk; total size of every file 1.76 s -> 0.017 s from the size column.

Exactness (what the app reads back is what it would have held):
  * every value of an entry, its type (None stays None, 0 stays 0, an int
    is never a bool), the order of the keys in each entry, and the order of
    the file list and of every folder's children are kept;
  * the 11 number fields are int64 columns with a null bit per field; xattr
    (a dict, usually empty) is msgpack in a pool;
  * an entry with other keys, another key order, or a value that is not a
    plain int, is stored whole as msgpack, if msgpack gives it back
    exactly; if any entry or folder would not come back exactly, write()
    refuses (ValueError) and the caller keeps the old dicts.

Reading: FileList and FolderMap are MutableMappings over the mapped file
with a small dict on top for what the app adds or replaces while a case is
open (files unpacked from nested archives, embedded media). Differences
from a dict, on purpose:
  * an entry (or child list) read from the file is a new object each time:
    changing it in place changes nothing. Assign it back, or use
    setdefault(), which copies a stored entry into the top dict and returns
    that copy (the embedded-media container size, ffs-explorer.py, relies
    on this);
  * copy() is cheap: it shares the mapped file and copies only the top dict.

Qt-free. The mapped file is never replaced while open: each write gets a
new name (files-<token>.ffsfiles, the token kept in the load snapshot), as
Windows cannot replace a file that is mapped.
"""
from __future__ import annotations

import glob
import mmap
import os
import struct
import uuid
import zlib
from array import array
from collections.abc import MutableMapping

import msgpack

from zip_index import _hash_table

MAGIC = b'FFSFILE\x02'   # 2: a NUL after each name
FIELDS = ('atime', 'btime', 'ctime', 'gid', 'inode', 'links', 'mode', 'mtime',
          'prot', 'size', 'uid')
_FIELD_SET = frozenset(FIELDS) | {'xattr'}
_I64_MIN, _I64_MAX = -(1 << 63), (1 << 63) - 1
_U64_MAX = (1 << 64) - 1
WHOLE = 1 << 15                # flags bit: the entry is stored whole (msgpack)
_HEADER = struct.Struct('<8sQ')
_SECTION = struct.Struct('<16sQQ')
SUFFIX = '.ffsfiles'


# ── Writing ──────────────────────────────────────────────────────────────────

def _plain(x) -> bool:
    """True if msgpack gives *x* back exactly (same values, same types)."""
    t = type(x)
    if x is None or t is bool or t is str or t is bytes or t is float:
        return True
    if t is int:
        return _I64_MIN <= x <= _U64_MAX
    if t is list:
        return all(_plain(v) for v in x)
    if t is dict:
        return all(type(k) is str and _plain(v) for k, v in x.items())
    return False


def _columnar(v, order) -> bool:
    if type(v) is not dict or len(v) != len(order) or tuple(v) != order:
        return False
    for k in FIELDS:
        x = v[k]
        if x is not None and (type(x) is not int or not _I64_MIN <= x <= _I64_MAX):
            return False
    xa = v['xattr']
    return type(xa) is dict and _plain(xa)


def new_path(case_dir: str) -> str:
    return os.path.join(case_dir, f'files-{uuid.uuid4().hex}{SUFFIX}')


def write(ui_metadata: dict, folder_map: dict, path: str) -> None:
    """Write the file list and folder contents to *path* (through a temporary
    file). ValueError if anything would not read back exactly."""
    names: list[str] = list(ui_metadata)
    row: dict[str, int] = {n: i for i, n in enumerate(names)}
    n_files = len(names)
    if any(type(n) is not str for n in names):
        raise ValueError('a file-list key is not a str')

    def rid(p):
        i = row.get(p)
        if i is None:
            if type(p) is not str:
                raise ValueError('a folder or child path is not a str')
            i = row[p] = len(names)
            names.append(p)
        return i

    folder_rows, child_start, child_len, children = array('i'), array('Q'), array('I'), array('i')
    for f, kids in folder_map.items():
        if type(kids) is not list:
            raise ValueError('a folder\'s children are not a list')
        folder_rows.append(rid(f))
        child_start.append(len(children))
        child_len.append(len(kids))
        children.extend(rid(k) for k in kids)

    first = next((v for v in ui_metadata.values()
                  if type(v) is dict and v.keys() == _FIELD_SET), None)
    order = tuple(first) if first is not None else FIELDS + ('xattr',)
    cols = {k: array('q', bytes(8 * n_files)) for k in FIELDS}
    flags = array('H', bytes(2 * n_files))
    p_start, p_len = array('Q', bytes(8 * n_files)), array('I', bytes(4 * n_files))
    pool = bytearray()
    for i in range(n_files):
        v = ui_metadata[names[i]]
        if _columnar(v, order):
            fl = 0
            for b, k in enumerate(FIELDS):
                x = v[k]
                if x is None:
                    fl |= 1 << b
                else:
                    cols[k][i] = x
            if v['xattr']:
                blob = msgpack.packb(v['xattr'], use_bin_type=True)
                p_start[i], p_len[i] = len(pool), len(blob)
                pool += blob
            flags[i] = fl
        else:
            if not _plain(v):
                raise ValueError(f'file-list entry {i} would not read back exactly')
            blob = msgpack.packb(v, use_bin_type=True)
            p_start[i], p_len[i] = len(pool), len(blob)
            pool += blob
            flags[i] = WHOLE

    enc = [n.encode('utf-8', 'surrogatepass') for n in names]
    if any(b'\x00' in b for b in enc):
        raise ValueError('a path holds a NUL character')
    name_start, name_len = array('Q'), array('I')
    off = 0
    for b in enc:
        name_start.append(off)
        name_len.append(len(b))
        off += len(b) + 1          # each name is followed by a NUL (names_run)
    sections = [('counts', struct.pack('<QQ', n_files, len(names))),
                ('order', ','.join(order).encode('ascii')),
                ('names', b'\x00'.join(enc) + b'\x00'), ('name_start', name_start.tobytes()),
                ('name_len', name_len.tobytes()), ('table', _hash_table(row).tobytes()),
                ('flags', flags.tobytes()), ('p_start', p_start.tobytes()),
                ('p_len', p_len.tobytes()), ('pool', bytes(pool)),
                ('folder_rows', folder_rows.tobytes()), ('child_start', child_start.tobytes()),
                ('child_len', child_len.tobytes()), ('children', children.tobytes())]
    sections += [('c_' + k, cols[k].tobytes()) for k in FIELDS]
    head = _HEADER.size + _SECTION.size * len(sections)
    offset = (head + 7) & ~7
    table, blobs = [], []
    for name, blob in sections:
        table.append(_SECTION.pack(name.encode('ascii'), offset, len(blob)))
        pad = (-len(blob)) & 7
        blobs.append(blob + b'\x00' * pad)
        offset += len(blob) + pad
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(_HEADER.pack(MAGIC, len(sections)))
        f.writelines(table)
        f.write(b'\x00' * (((head + 7) & ~7) - head))
        f.writelines(blobs)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remove_others(case_dir: str, keep: str | None) -> None:
    """Delete this case's other file tables (best effort: one still mapped by
    a running copy of the app, on Windows, stays until a later write)."""
    for p in glob.glob(os.path.join(case_dir, 'files-*' + SUFFIX)) + \
            glob.glob(os.path.join(case_dir, 'files-*' + SUFFIX + '.tmp')):
        if keep is None or os.path.basename(p) != os.path.basename(keep):
            try:
                os.remove(p)
            except OSError:
                pass


# ── Reading ──────────────────────────────────────────────────────────────────

class FileTable:
    """A mapped file table: rows (file-list rows first), find(path), entry(row)."""

    def __init__(self, path: str):
        self.path = path
        fh = open(path, 'rb')
        try:
            self._mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        finally:
            fh.close()
        mm = self._mm
        magic, n = _HEADER.unpack_from(mm, 0)
        if magic != MAGIC:
            raise ValueError('not an FFS Explorer file table')
        mv = memoryview(mm)
        s = {}
        for k in range(n):
            name, off, ln = _SECTION.unpack_from(mm, _HEADER.size + k * _SECTION.size)
            if off + ln > len(mm):
                raise ValueError('file table is cut short')
            s[name.rstrip(b'\x00').decode('ascii')] = mv[off:off + ln]
        self.n_files, self.n_rows = struct.unpack('<QQ', s['counts'])
        self.order = tuple(bytes(s['order']).decode('ascii').split(','))
        self._names = s['names']
        self.name_start, self.name_len = s['name_start'].cast('Q'), s['name_len'].cast('I')
        self._table = s['table'].cast('i')
        self._mask = len(self._table) - 1
        self.flags = s['flags'].cast('H')
        self._p_start, self._p_len, self._pool = s['p_start'].cast('Q'), s['p_len'].cast('I'), s['pool']
        self.columns = {k: s['c_' + k].cast('q') for k in FIELDS}
        self._col_list = [self.columns[k] for k in FIELDS]
        self._pos = [(k, FIELDS.index(k) if k != 'xattr' else -1) for k in self.order]
        self.folder_rows = s['folder_rows'].cast('i')
        self._child_start, self._child_len = s['child_start'].cast('Q'), s['child_len'].cast('I')
        self._children = s['children'].cast('i')
        if len(self.name_start) != self.n_rows or len(self.flags) != self.n_files:
            raise ValueError('file table counts do not match')
        self._folder_index = None
        self._whole = None

    def name(self, i: int) -> str:
        st = self.name_start[i]
        return str(self._names[st:st + self.name_len[i]], 'utf-8', 'surrogatepass')

    def names_run(self, a: int, b: int) -> list:
        """Names of rows a..b-1, decoded in one call (rows are stored in
        order, each name followed by a NUL)."""
        if b <= a:
            return []
        end = self.name_start[b - 1] + self.name_len[b - 1]
        return str(self._names[self.name_start[a]:end], 'utf-8', 'surrogatepass').split('\x00')

    def find(self, name) -> int:
        """Row of *name*, or -1."""
        if type(name) is not str:
            return -1
        nb = name.encode('utf-8', 'surrogatepass')
        h = zlib.crc32(nb) & self._mask
        table, starts, lens, pool, mask = self._table, self.name_start, self.name_len, self._names, self._mask
        ln = len(nb)
        while True:
            i = table[h]
            if i == -1:
                return -1
            if lens[i] == ln:
                st = starts[i]
                if pool[st:st + ln] == nb:
                    return i
            h = (h + 1) & mask

    def entry(self, i: int) -> dict:
        """File-list row *i* as the dict the app held."""
        fl = self.flags[i]
        if fl & WHOLE:
            st = self._p_start[i]
            return msgpack.unpackb(self._pool[st:st + self._p_len[i]], raw=False,
                                   strict_map_key=False)
        d = {}
        cols = self._col_list
        for k, b in self._pos:
            if b < 0:
                ln = self._p_len[i]
                if ln:
                    st = self._p_start[i]
                    d[k] = msgpack.unpackb(self._pool[st:st + ln], raw=False, strict_map_key=False)
                else:
                    d[k] = {}
            else:
                d[k] = None if fl >> b & 1 else cols[b][i]
        return d

    def value(self, i: int, field: str):
        """One field of file-list row *i* without building the dict
        (KeyError if the entry has no such field)."""
        fl = self.flags[i]
        if fl & WHOLE or field == 'xattr':
            return self.entry(i)[field]
        b = FIELDS.index(field)
        return None if fl >> b & 1 else self._col_list[b][i]

    def whole_rows(self) -> list:
        """File-list rows stored whole (not in the columns)."""
        if self._whole is None:
            f = self.flags
            self._whole = [i for i in range(self.n_files) if f[i] & WHOLE]
        return self._whole

    def folder_position(self, row: int) -> int:
        if self._folder_index is None:
            self._folder_index = {r: j for j, r in enumerate(self.folder_rows)}
        return self._folder_index.get(row, -1)

    def children(self, j: int) -> list:
        st = self._child_start[j]
        name = self.name
        return [name(c) for c in self._children[st:st + self._child_len[j]]]

    def close(self) -> None:
        try:
            self._mm.close()
        except (BufferError, ValueError):
            pass     # views still in use: closed when the last is dropped


_UNSET = object()
_RUN = 50_000             # names decoded per call when iterating


class _Layered(MutableMapping):
    """A read-only mapping in the file table with a dict on top: *top*
    holds what was set (new keys and replaced stored ones), *gone* the
    stored keys deleted. Subclasses say how to find and read a stored key."""

    def __init__(self, table: FileTable, top: dict | None = None, gone: set | None = None):
        self.table = table
        self.top = top if top is not None else {}
        self.gone = gone if gone is not None else set()
        self._added = {k for k in self.top if self._locate(k) < 0}

    # subclasses: _locate(k) -> position or -1; _load(pos); _stored() -> (name, pos)*

    def __getitem__(self, k):
        try:
            return self.top[k]
        except KeyError:
            pass
        except TypeError:
            raise KeyError(k) from None
        if self.gone and k in self.gone:
            raise KeyError(k)
        j = self._locate(k)
        if j < 0:
            raise KeyError(k)
        return self._load(j)

    def get(self, k, default=None):
        try:
            return self.top[k]
        except KeyError:
            pass
        except TypeError:
            return default
        if self.gone and k in self.gone:
            return default
        j = self._locate(k)
        return default if j < 0 else self._load(j)

    def __contains__(self, k):
        try:
            if k in self.top:
                return True
        except TypeError:
            return False
        if self.gone and k in self.gone:
            return False
        return self._locate(k) >= 0

    def __setitem__(self, k, v):
        if k not in self.top and self._locate(k) < 0:
            self._added.add(k)
        self.top[k] = v
        self.gone.discard(k)

    def __delitem__(self, k):
        stored = not (k in self.gone) and self._locate(k) >= 0
        if k in self.top:
            del self.top[k]
            self._added.discard(k)
        elif not stored:
            raise KeyError(k)
        if stored:
            self.gone.add(k)

    def setdefault(self, k, default=None):
        """As dict.setdefault; a stored value is copied into the top dict,
        so changing the returned object in place is kept."""
        try:
            return self.top[k]
        except KeyError:
            pass
        j = self._locate(k)
        if j >= 0 and k not in self.gone:
            v = self._load(j)
        else:
            v = default
            if j < 0:
                self._added.add(k)     # a deleted stored key comes back in its place
        self.top[k] = v
        self.gone.discard(k)
        return v

    def _live(self):
        gone = self.gone
        for n, j in self._stored():
            if not gone or n not in gone:
                yield n, j

    def __iter__(self):
        for n, _j in self._live():
            yield n
        added = self._added
        for n in list(self.top):
            if n in added:
                yield n

    def _iter_items(self):
        top, load = self.top, self._load
        for n, j in self._live():
            v = top.get(n, _UNSET) if top else _UNSET
            yield n, (load(j) if v is _UNSET else v)
        added = self._added
        for n in list(top):
            if n in added:
                yield n, top[n]

    def items(self):
        return _Items(self)

    def values(self):
        return _Values(self)

    def __len__(self):
        return self._n_stored() - len(self.gone) + len(self._added)

    def __bool__(self):
        return len(self) > 0

    def copy(self):
        """Cheap: shares the mapped table, copies only the changes."""
        for _ in range(5):
            try:
                return type(self)(self.table, dict(self.top), set(self.gone))
            except RuntimeError:     # changed size during the copy (another thread)
                pass
        return type(self)(self.table, dict(self.top), set(self.gone))

    def __reduce__(self):            # pickled by path: a worker maps the same file
        return (_reopen, (type(self).__name__, self.table.path, dict(self.top), set(self.gone)))

    def __repr__(self):
        return f'<{type(self).__name__} {len(self):,} from {os.path.basename(self.table.path)}>'


class FileList(_Layered):
    """ui_metadata: path -> metadata dict."""

    def _locate(self, k) -> int:
        i = self.table.find(k)
        return i if 0 <= i < self.table.n_files else -1

    def _load(self, i):
        return self.table.entry(i)

    def _stored(self):
        run, n = self.table.names_run, self.table.n_files
        for a in range(0, n, _RUN):
            for k, nm in enumerate(run(a, min(a + _RUN, n))):
                yield nm, a + k

    def _n_stored(self):
        return self.table.n_files


class FolderMap(_Layered):
    """folder_map: folder path -> list of child paths."""

    def _locate(self, k) -> int:
        r = self.table.find(k)
        return self.table.folder_position(r) if r >= 0 else -1

    def _load(self, j):
        return self.table.children(j)

    def _stored(self):
        name = self.table.name
        for j, r in enumerate(self.table.folder_rows):
            yield name(r), j

    def _n_stored(self):
        return len(self.table.folder_rows)


class _Items:
    def __init__(self, m):
        self._m = m

    def __iter__(self):
        return self._m._iter_items()

    def __len__(self):
        return len(self._m)


class _Values:
    def __init__(self, m):
        self._m = m

    def __iter__(self):
        return (v for _k, v in self._m._iter_items())

    def __len__(self):
        return len(self._m)


# ── Fast reads for loops over every file (work on a plain dict too) ─────────

def fields_of(ui_metadata, fields: tuple):
    """(path, (entry.get(f) for f in fields)) for every file, in order:
    what `for p, m in ui_metadata.items(): m.get(f)` gives, without building
    each entry from the file table (a column read instead). An entry that
    is not a dict gives Nones."""
    if not isinstance(ui_metadata, FileList):
        for p, m in ui_metadata.items():
            yield p, (tuple(m.get(f) for f in fields) if isinstance(m, dict)
                      else (None,) * len(fields))
        return
    t, top = ui_metadata.table, ui_metadata.top
    order = set(t.order)
    cols = [(t.columns[f] if f in t.columns and f in order else None) for f in fields]
    bits = [FIELDS.index(f) if c is not None else -1 for f, c in zip(fields, cols)]
    direct = all(c is not None or (f not in order) for f, c in zip(fields, cols))
    flags = t.flags
    for p, i in ui_metadata._live():
        v = top.get(p, _UNSET) if top else _UNSET
        if v is not _UNSET:
            yield p, (tuple(v.get(f) for f in fields) if isinstance(v, dict) else (None,) * len(fields))
            continue
        fl = flags[i]
        if fl & WHOLE or not direct:
            m = t.entry(i)
            yield p, tuple(m.get(f) for f in fields)
        else:
            yield p, tuple((None if (c is None or fl >> b & 1) else c[i]) for c, b in zip(cols, bits))
    for p in list(top):
        if p in ui_metadata._added:
            v = top[p]
            yield p, (tuple(v.get(f) for f in fields) if isinstance(v, dict) else (None,) * len(fields))


def any_set(ui_metadata, field: str) -> bool:
    """any(m.get(field) for m in ui_metadata.values() if m), from the
    column where it can be (a missing value is stored as 0, which is falsy
    there too)."""
    if not isinstance(ui_metadata, FileList):
        return any(v.get(field) for v in ui_metadata.values() if v)
    t, top, gone = ui_metadata.table, ui_metadata.top, ui_metadata.gone
    if any(v.get(field) for v in list(top.values()) if v):
        return True
    skip = gone | set(top) if (gone or top) else None

    def live(i):
        return skip is None or t.name(i) not in skip
    if any(t.entry(i).get(field) for i in t.whole_rows() if live(i)):
        return True
    if field not in t.order:
        return False
    vals = t._p_len if field == 'xattr' else t.columns[field]
    flags = t.flags
    for i, x in enumerate(vals):
        if x and not flags[i] & WHOLE and live(i):
            return True
    return False


_OPEN: dict[str, FileTable] = {}


def open_table(path: str) -> FileTable:
    """The FileTable for *path*, mapped once per process."""
    t = _OPEN.get(path)
    if t is None:
        t = _OPEN[path] = FileTable(path)
    return t


def _reopen(kind, path, top, gone):
    return {'FileList': FileList, 'FolderMap': FolderMap}[kind](open_table(path), top, gone)


def open_maps(path: str) -> tuple[FileList, FolderMap]:
    t = open_table(path)
    return FileList(t), FolderMap(t)
