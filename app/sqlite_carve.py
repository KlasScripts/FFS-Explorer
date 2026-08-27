"""sqlite_carve.py — low-level SQLite record recovery: freeblocks, freed
(freelist) pages, and full WAL frame history (not just the current valid
chain a normal `sqlite3.connect()` would replay).

This operates below the SQL layer entirely — it reads raw page bytes and
decodes SQLite's on-disk record format directly, because none of that is
reachable through `SELECT`. A `DELETE`d row's bytes are not erased: the
cell is unlinked from the page's cell-pointer array (freeblock) or the
whole page is added to the freelist, but the payload usually stays until
something else reuses that space. A WAL file similarly keeps old frames
physically present after a checkpoint logically resets which frames are
"current" — the bytes aren't overwritten until new frames wrap around and
reuse that space.

Results here are candidates, not certainties: anything decoded from a
freeblock, a freed page, or a superseded WAL frame is unlabeled/dangling by
construction (its owning table isn't recorded anywhere once freed) and is
matched to a target table only by "this many columns decoded plausibly" —
state that explicitly, always cite page number/offset/frame index so an
examiner can verify, and never claim recovered content is complete or that
absence here proves the data was never there (page reuse and checkpoint
timing both destroy recoverability without leaving a trace that it happened).

No evidence is ever opened read-write: every function takes bytes already
in memory (or a path opened by the caller in 'rb' mode). Nothing here
performs a `sqlite3.connect()` on anything — the whole point is to look
below/around what that API can see.
"""

import os
import sqlite3
import struct
import tempfile

_LEAF_TABLE_PAGE = 0x0D
_INTERIOR_TABLE_PAGE = 0x05


# ── varint / record decoding (SQLite's own on-disk format) ─────────────────

def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a SQLite varint at *offset*. Returns (value, bytes_consumed)."""
    result = 0
    for i in range(9):
        if offset + i >= len(data):
            raise ValueError('varint runs past end of buffer')
        b = data[offset + i]
        if i == 8:
            result = (result << 8) | b
            return result, 9
        result = (result << 7) | (b & 0x7F)
        if not (b & 0x80):
            return result, i + 1
    raise ValueError('unreachable')


def _serial_type_size(t: int) -> int:
    return {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}.get(
        t, (t - 12) // 2 if t >= 12 and t % 2 == 0 else (t - 13) // 2)


def decode_value(data: bytes, offset: int, serial_type: int):
    if serial_type == 0:
        return None
    if serial_type in (8, 9):
        return serial_type - 8
    size = _serial_type_size(serial_type)
    chunk = data[offset:offset + size]
    if len(chunk) < size:
        raise ValueError('record body runs past end of buffer')
    if serial_type == 7:
        return struct.unpack('>d', chunk)[0]
    if serial_type in (1, 2, 3, 4, 5, 6):
        return int.from_bytes(chunk, 'big', signed=True)
    if serial_type >= 12 and serial_type % 2 == 0:
        return chunk  # BLOB
    if serial_type >= 13 and serial_type % 2 == 1:
        return chunk.decode('utf-8', errors='replace')  # TEXT
    raise ValueError(f'reserved/unknown serial type {serial_type}')


def parse_record_header(payload: bytes, offset: int = 0, max_cols: int = 200) -> tuple[list, int]:
    """Parse just a record header at *offset* into (serial_types,
    header_length) — header-length varint followed by one serial-type
    varint per column. Raises ValueError on anything implausible. Shared
    by decode_record (called at offset 0, immediately after a payload-
    length/rowid pair) and the header-signature scanner below (called at
    arbitrary candidate offsets, deliberately with no such pair expected —
    see the module docstring on why that pair often doesn't survive a
    single-row deletion while the header right after it can)."""
    header_len, n = read_varint(payload, offset)
    if header_len < n or offset + header_len > len(payload):
        raise ValueError('implausible header length')
    types = []
    pos = offset + n
    end = offset + header_len
    while pos < end:
        t, consumed = read_varint(payload, pos)
        types.append(t)
        pos += consumed
        if len(types) > max_cols:
            raise ValueError('too many columns — not a real record')
    if pos != end:
        raise ValueError('header length mismatch')
    return types, header_len


def decode_body(payload: bytes, types: list, body_start: int) -> tuple[list, bool]:
    """Decode field values given already-parsed serial types and where
    the body starts. Returns (values, truncated) — truncated=True means
    the buffer ran out partway through (e.g. a freeblock/page boundary
    cut off a long TEXT field); values covers whatever *did* fit rather
    than being discarded, since a partial row beats no row."""
    values = []
    body_pos = body_start
    for t in types:
        size = _serial_type_size(t)
        if size < 0:
            raise ValueError('reserved serial type')
        if body_pos + size > len(payload):
            return values, True
        values.append(decode_value(payload, body_pos, t))
        body_pos += size
    return values, False


def decode_record(payload: bytes, max_cols: int = 200) -> list:
    """Decode one SQLite record (header + body) into a list of Python
    values. Raises ValueError if it doesn't look like a valid record —
    callers use that to reject garbage while scanning free space."""
    types, header_len = parse_record_header(payload, 0, max_cols)
    values, truncated = decode_body(payload, types, header_len)
    if truncated:
        raise ValueError('body runs past end of payload — likely overflow or garbage')
    return values


# ── page-level structures ────────────────────────────────────────────────

def parse_db_header(raw: bytes) -> dict:
    if raw[:16] != b'SQLite format 3\x00':
        raise ValueError('not a SQLite database')
    page_size = struct.unpack('>H', raw[16:18])[0]
    if page_size == 1:
        page_size = 65536
    return {
        'page_size': page_size,
        'first_freelist_trunk': struct.unpack('>I', raw[32:36])[0],
        'n_freelist_pages': struct.unpack('>I', raw[36:40])[0],
    }


def _page_bytes(raw: bytes, page_size: int, page_no: int) -> bytes:
    """Page numbers are 1-based; page 1 includes the 100-byte file header."""
    start = (page_no - 1) * page_size
    return raw[start:start + page_size]


def decode_leaf_page_cells(page: bytes, page_no: int, header_offset: int = 0) -> list[dict]:
    """Decode every cell in a table-leaf b-tree page (type 0x0D). Works
    whether the page is currently live, freed-but-intact, or a historical
    WAL frame image — the on-disk layout is identical either way.
    *header_offset* is 100 for page 1 (past the file header), else 0."""
    if page[header_offset] != _LEAF_TABLE_PAGE:
        raise ValueError(f'page {page_no} is not a table-leaf page '
                         f'(type byte {page[header_offset]:#x})')
    n_cells = struct.unpack('>H', page[header_offset + 3:header_offset + 5])[0]
    ptr_array_start = header_offset + 8
    out = []
    for i in range(n_cells):
        ptr_off = ptr_array_start + i * 2
        cell_off = struct.unpack('>H', page[ptr_off:ptr_off + 2])[0]
        if cell_off == 0 or cell_off >= len(page):
            continue
        payload_len, c1 = read_varint(page, cell_off)
        rowid, c2 = read_varint(page, cell_off + c1)
        payload_start = cell_off + c1 + c2
        # No overflow-page handling: only the on-page-local portion is
        # read, which for a 4096-byte page comfortably covers ordinary
        # short text fields (message bodies etc.) — large BLOBs/long TEXT
        # may come back truncated rather than wrong.
        payload = page[payload_start:payload_start + payload_len]
        try:
            types, header_len = parse_record_header(payload)
            values, truncated = decode_body(payload, types, header_len)
            if truncated:
                continue
        except ValueError:
            continue
        out.append({'page': page_no, 'offset': cell_off, 'rowid': rowid,
                    'values': values, 'types': types,
                    'cell_len': c1 + c2 + payload_len})
    return out


def walk_table_leaf_pages(raw: bytes, page_size: int, root_page: int,
                         wal_images: dict[int, bytes] | None = None) -> list[int]:
    """Follow a table b-tree from *root_page* down to every leaf page
    currently belonging to it (root itself may be a leaf for a small
    table). Needed because carving freeblocks/WAL history only makes
    sense scoped to the pages a table actually owns right now.

    *wal_images*, if given (see _wal_latest_page_images), maps
    page_no -> that page's most recent image still present in a WAL file —
    consulted ONLY as a fallback when a page this walk needs doesn't
    physically exist in *raw* at all. That is a real, unremarkable state:
    a table created after the database's last checkpoint has a root (and
    any interior) page that was NEVER written to the main file, so a
    plain walk of *raw* finds zero leaves for it — and every one of that
    table's real pages then gets silently skipped by
    carve_wal_history_for_table too, since it only ever searches the page
    numbers this function already found. Confirmed on a real Chrome
    segmentation_platform/ukm_db: the schema named page 8 as the `urls`
    table's root, but the checkpointed main file was only 1 page long —
    see chrome_segmentation_platform.py's description for the full case.
    Never used to override a page that DOES exist in *raw* — the WAL may
    hold a newer version of an already-checkpointed page, but this
    function's job is topology discovery, not content, and existing
    behavior for a table that was already checkpointed must not change."""
    leaves = []
    stack = [root_page]
    seen = set()
    while stack:
        page_no = stack.pop()
        if page_no in seen:
            continue
        seen.add(page_no)
        header_offset = 100 if page_no == 1 else 0
        page = _page_bytes(raw, page_size, page_no)
        if len(page) < page_size and wal_images and page_no in wal_images:
            page = wal_images[page_no]
        if len(page) < header_offset + 12:
            continue
        page_type = page[header_offset]
        if page_type == _LEAF_TABLE_PAGE:
            leaves.append(page_no)
        elif page_type == _INTERIOR_TABLE_PAGE:
            n_cells = struct.unpack('>H', page[header_offset + 3:header_offset + 5])[0]
            right_child = struct.unpack('>I', page[header_offset + 8:header_offset + 12])[0]
            stack.append(right_child)
            ptr_array_start = header_offset + 12
            for i in range(n_cells):
                ptr_off = ptr_array_start + i * 2
                cell_off = struct.unpack('>H', page[ptr_off:ptr_off + 2])[0]
                if cell_off:
                    child = struct.unpack('>I', page[cell_off:cell_off + 4])[0]
                    stack.append(child)
    return leaves


def locate_live_row(raw: bytes, table: str, rowid: int) -> dict | None:
    """Find the on-disk (page, cell-offset, cell-length) of a currently
    LIVE row by its rowid — for the Artifact Viewer's "jump the hex view
    to where this record lives" feature, the opposite case from
    recover_deleted_rows (which only ever looks at freed/unlinked space).

    The rootpage lookup is the one place this function opens an actual
    sqlite3 connection, and only against a throwaway temp copy of *raw*
    (never the archive or a persistent extracted file) opened `mode=ro` —
    same reasoning as record_column_names/rowid_alias_column elsewhere in
    this module: a real SQLite reader correctly follows overflow pages for
    a long `sqlite_master.sql` CREATE TABLE text, which the raw cell
    decoder below deliberately does not attempt (decode_leaf_page_cells
    skips a cell it can't decode fully on-page). The connection is
    read-only, touches nothing but sqlite_master, and the temp file is
    removed immediately after.

    Returns None (never raises) if the table doesn't exist, the rowid
    isn't found in the table's CURRENT live b-tree (it may only exist in
    an unmerged WAL frame — not checked here — or may genuinely be
    deleted, which is recover_deleted_rows' job, not this one's), or
    anything else about the lookup fails."""
    try:
        header = parse_db_header(raw)
    except Exception:
        return None

    fd, tmp_path = tempfile.mkstemp(suffix='.sqlite')
    root_page = None
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        conn = sqlite3.connect(f'file:{tmp_path}?mode=ro', uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            root_page = row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if root_page is None:
        return None

    try:
        for page_no in walk_table_leaf_pages(raw, header['page_size'], root_page):
            header_offset = 100 if page_no == 1 else 0
            page = _page_bytes(raw, header['page_size'], page_no)
            if len(page) < header_offset + 12 or page[header_offset] != _LEAF_TABLE_PAGE:
                continue
            for cell in decode_leaf_page_cells(page, page_no, header_offset):
                if cell['rowid'] == rowid:
                    return {
                        'page':        page_no,
                        'offset':      cell['offset'],
                        'length':      cell['cell_len'],
                        'abs_offset':  (page_no - 1) * header['page_size'] + cell['offset'],
                        'page_size':   header['page_size'],
                    }
    except Exception:
        return None
    return None


def iter_freelist_pages(raw: bytes, page_size: int, header: dict):
    """Yield page numbers currently on the freelist — pages SQLite has
    marked reusable but not yet overwritten. Trunk pages themselves are
    also yielded (their own leftover content, past the trunk header, can
    still hold old cell bytes from before they became a freelist trunk)."""
    trunk_no = header['first_freelist_trunk']
    seen = set()
    while trunk_no and trunk_no not in seen and trunk_no * page_size <= len(raw):
        seen.add(trunk_no)
        yield trunk_no
        trunk = _page_bytes(raw, page_size, trunk_no)
        next_trunk = struct.unpack('>I', trunk[0:4])[0]
        n_leaves = struct.unpack('>I', trunk[4:8])[0]
        for i in range(n_leaves):
            off = 8 + i * 4
            if off + 4 > len(trunk):
                break
            leaf_no = struct.unpack('>I', trunk[off:off + 4])[0]
            if leaf_no and leaf_no not in seen:
                seen.add(leaf_no)
                yield leaf_no
        trunk_no = next_trunk


def iter_freeblocks(page: bytes, header_offset: int = 0):
    """Yield (offset, size) of every freeblock in a b-tree page — slack
    space from a deleted cell that hasn't been reclaimed by a later insert.
    The freeblock's own 4-byte link header is skipped; only the potentially
    -stale content past it is returned."""
    first = struct.unpack('>H', page[header_offset + 1:header_offset + 3])[0]
    seen = set()
    off = first
    while off and off not in seen and off + 4 <= len(page):
        seen.add(off)
        size = struct.unpack('>H', page[off + 2:off + 4])[0]
        if size < 4:
            break
        yield off + 4, size - 4
        off = struct.unpack('>H', page[off:off + 2])[0]


def _brute_force_records(chunk: bytes, base_offset: int, page_no: int,
                         source: str) -> list[dict]:
    """Try every byte offset in *chunk* as a candidate cell start
    (payload_len varint, rowid varint, record). Used for any free-space
    byte range regardless of kind — a formally-linked freeblock or the
    single unallocated page-gap (see _free_byte_ranges) — since a deleted
    cell's start doesn't necessarily align with the range's own start
    (page compaction can leave old bytes anywhere inside a merged region)."""
    out = []
    for start in range(len(chunk)):
        try:
            payload_len, c1 = read_varint(chunk, start)
            if payload_len <= 0 or payload_len > len(chunk) - start:
                continue
            rowid, c2 = read_varint(chunk, start + c1)
            payload_start = start + c1 + c2
            payload = chunk[payload_start:payload_start + payload_len]
            if len(payload) < payload_len:
                continue
            values = decode_record(payload)
            if not values:
                continue
            out.append({'page': page_no, 'offset': base_offset + start,
                       'length': c1 + c2 + payload_len,
                       'rowid': rowid, 'values': values, 'source': source})
        except ValueError:
            continue
    return out


def carve_unallocated_region(raw: bytes, page_size: int, leaf_pages: list[int]) -> list[dict]:
    """Scan every free-space byte range on each of *leaf_pages* — the
    unallocated page-gap and every individually linked freeblock, see
    _free_byte_ranges — for a residual (payload_len varint, rowid varint,
    record) triple left behind by a deleted cell that a later insert
    hasn't overwritten yet. SQLite does not zero either kind of range on
    delete/defragment, so both are worth scanning the same way."""
    out = []
    for page_no in leaf_pages:
        page = _page_bytes(raw, page_size, page_no)
        header_offset = 100 if page_no == 1 else 0
        if not page or page[header_offset] != _LEAF_TABLE_PAGE:
            continue
        for start, end, source in _free_byte_ranges(page, header_offset):
            out.extend(_brute_force_records(page[start:end], start, page_no, source))
    return out


def carve_freed_pages(raw: bytes, page_size: int, header: dict) -> list[dict]:
    """Scan every currently-freelisted page: if it still structurally
    looks like an intact table-leaf page (freeing a page only unlinks it
    from the b-tree — SQLite does not zero it), decode all of its cells
    directly. Cleaner and more reliable than freeblock scanning when it
    applies, because the whole page's structure (not just fragments) is
    usually still there."""
    out = []
    for page_no in iter_freelist_pages(raw, page_size, header):
        page = _page_bytes(raw, page_size, page_no)
        if len(page) < page_size or page[0] != _LEAF_TABLE_PAGE:
            continue  # not a recognizable leaf page anymore (reused/trunk/corrupt)
        try:
            out.extend(decode_leaf_page_cells(page, page_no))
        except ValueError:
            continue
    for row in out:
        row['source'] = 'freed_page'
    return out


# ── header signature: recovering a single deleted row ───────────────────────
#
# The other carving paths above (freeblocks-as-a-linked-list, freed whole
# pages, WAL history) all depend on a payload-length varint and a rowid
# varint immediately preceding the record header being intact. They
# usually are NOT for the single most common real case: an ordinary
# DELETE of one row on a page that otherwise stays live. SQLite's own
# freeSpace() writes a 4-byte freeblock link header — [next-offset][size]
# — into the START of exactly the bytes that used to be the payload-length
# and rowid varints, the moment the row is deleted. That is not corruption
# or bad luck; it is what every ordinary single-row delete does.
#
# What usually survives past those clobbered 4 bytes is the record HEADER
# and BODY — SQLite has no reason to touch them. So instead of requiring
# the (payload_length, rowid) pair, search for the header directly: derive
# a signature from live rows of the same table (per column position, is
# the serial type the same across samples — true for NULL/small-INTEGER
# columns, and for TEXT/BLOB columns whose length happens to be constant
# in practice even though "TEXT is variable-length" in general, e.g. a
# GUID column is always exactly 36 bytes), then scan for a header whose
# serial types match on every one of those fixed positions and can be
# anything at the genuinely-variable ones. Once a match is found, its
# OWN serial types (not the training sample's) decode the body normally —
# a genuinely long message body decodes at whatever length it actually is.
#
# The tradeoff for using this path: there is no rowid. It was in the 4
# bytes that didn't survive. A row recovered this way is real content with
# no citable row identity — report it as such, never invent one.
#
# (Methodology cross-checked against sqbrite (github.com/mattboyer/sqbrite,
# MIT-licensed prior art for this exact problem): it searches freeblocks
# for a similar fixed-byte "magic" pattern per table, hand-curated per app
# in a YAML file. The header-serial-type insight matches; the difference
# here is deriving the signature automatically from the case's own live
# data instead of requiring a human to pre-supply it per app, since this
# project needs to work on apps nobody has looked at before.)

def sample_header_signature(raw: bytes, page_size: int, leaf_pages: list,
                            max_samples: int = 50, min_agreement: float = 0.9):
    """Derive a column signature from up to *max_samples* live cells
    across *leaf_pages*. Returns {'length': N, 'anchors': {pos: serial_type},
    'n_samples': M}, or None if there weren't enough live rows to derive
    anything trustworthy (an anchor set built from 2 rows is not worth
    scanning a whole page with — it will false-positive constantly)."""
    from collections import Counter
    samples = []
    for page_no in leaf_pages:
        if len(samples) >= max_samples:
            break
        page = _page_bytes(raw, page_size, page_no)
        header_offset = 100 if page_no == 1 else 0
        if not page or page[header_offset] != _LEAF_TABLE_PAGE:
            continue
        for cell in decode_leaf_page_cells(page, page_no, header_offset):
            samples.append(cell['types'])
            if len(samples) >= max_samples:
                break
    if len(samples) < 5:
        return None
    length = len(samples[0])
    if not all(len(s) == length for s in samples):
        # Column count actually differs between sampled rows -- most often
        # an ALTER TABLE ADD COLUMN happened partway through this table's
        # history. A signature needs one fixed shape; bail out rather than
        # silently picking one arbitrary shape and mismatching the rest.
        return None
    anchors = {}
    for pos in range(length):
        counts = Counter(s[pos] for s in samples)
        best_type, best_count = counts.most_common(1)[0]
        if best_count / len(samples) >= min_agreement:
            anchors[pos] = best_type
    return {'length': length, 'anchors': anchors, 'n_samples': len(samples)}


def _matches_signature(types: list, signature: dict) -> bool:
    if len(types) != signature['length']:
        return False
    return all(types[pos] == expected for pos, expected in signature['anchors'].items())


def _free_byte_ranges(page: bytes, header_offset: int) -> list[tuple[int, int, str]]:
    """Every byte range in a table-leaf page that is NOT live cell content,
    as (start, end, source): the gap between the cell-pointer array and
    the compacted content area ('page_gap' — the "big gap" a full page
    defragment produces, or space a table simply hasn't grown into yet),
    plus each individually linked freeblock within that content area
    ('freeblock' — slack from a deleted cell a later insert hasn't
    reclaimed). Both are candidates for the SAME reason: SQLite does not
    zero either region on delete/defragment. Deliberately excludes live
    cells — the header-signature scanner must never re-report a row the
    parser's own query already returned; scoping the scan to only-ever-
    free bytes is what guarantees that structurally, rather than relying
    on a rowid-based dedup that path's matches don't have (see
    recover_deleted_rows for why: it finds rows with no surviving rowid
    at all)."""
    n_cells = struct.unpack('>H', page[header_offset + 3:header_offset + 5])[0]
    content_start = struct.unpack('>H', page[header_offset + 5:header_offset + 7])[0]
    if content_start == 0:
        content_start = 65536
    scan_start = header_offset + 8 + n_cells * 2
    ranges = []
    if scan_start < content_start:
        ranges.append((scan_start, content_start, 'page_gap'))
    for fb_off, fb_size in iter_freeblocks(page, header_offset):
        ranges.append((fb_off, fb_off + fb_size, 'freeblock'))
    return ranges


def carve_by_header_signature(raw: bytes, page_size: int, leaf_pages: list,
                              signature: dict) -> list[dict]:
    """Search only the free-space byte ranges of each of *leaf_pages* —
    never live cell content, see _free_byte_ranges — for a record header
    matching *signature*, with no payload-length/rowid pair required
    before it. This is the path that actually covers an ordinary
    single-row deletion; see the section docstring above for why the
    other carving functions in this module usually can't. Every result
    has `rowid: None` — recovered content with no row identity to cite,
    not a row this function is guessing at."""
    if not signature or not signature['anchors']:
        return []  # no fixed positions -- would match almost anything, too unsafe to scan with
    out = []
    for page_no in leaf_pages:
        page = _page_bytes(raw, page_size, page_no)
        header_offset = 100 if page_no == 1 else 0
        if not page or page[header_offset] != _LEAF_TABLE_PAGE:
            continue
        seen_body_starts = set()
        for start, end, _range_source in _free_byte_ranges(page, header_offset):
            # Bound the buffer to this free range's own end: a live cell
            # sits immediately past it, and without this a record header
            # that starts in free space but extends past `end` would read
            # straight into that live cell's bytes and misreport them as
            # its own field values instead of correctly truncating.
            bounded = page[:end]
            for offset in range(start, end):
                try:
                    types, header_len = parse_record_header(bounded, offset)
                except ValueError:
                    continue
                if not _matches_signature(types, signature):
                    continue
                body_start = offset + header_len
                if body_start in seen_body_starts:
                    continue  # same header already matched at an earlier offset
                seen_body_starts.add(body_start)
                values, truncated = decode_body(bounded, types, body_start)
                if not values:
                    continue
                # No payload-length varint precedes a header-signature match
                # (that's the whole point — see the section docstring), so
                # there's no single number to read the record's length from
                # the way the other carving paths have; reconstructed instead
                # from the header + each field's own serial-type size. A
                # truncated match ran past the free-space range's own end
                # (bounded == page[:end]), so the reconstructed length is
                # capped there rather than claiming bytes past what was
                # actually available to decode.
                length = header_len + sum(_serial_type_size(t) for t in types)
                if truncated:
                    length = min(length, end - offset)
                out.append({'page': page_no, 'header_offset': offset, 'rowid': None,
                           'values': values, 'types': types, 'truncated': truncated,
                           'length': length, 'source': 'header_signature'})
    return out


# ── WAL frame history (every frame ever written that's still on disk) ──────

def iter_wal_frames(wal: bytes, page_size: int):
    """Yield every frame physically present in *wal*, in file order,
    regardless of checksum validity or whether it belongs to the chain a
    normal SQLite connection would currently replay. A checkpoint logically
    resets which frames matter but does not erase the file — old frames
    for a since-superseded transaction, or from a wrapped-around earlier
    WAL generation, can still be sitting in this file's tail/slack."""
    if len(wal) < 32 or wal[0:4] not in (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83'):
        return
    frame_size = 24 + page_size
    n = (len(wal) - 32) // frame_size
    for i in range(n):
        off = 32 + i * frame_size
        header = wal[off:off + 24]
        page_no = struct.unpack('>I', header[0:4])[0]
        db_size_after_commit = struct.unpack('>I', header[4:8])[0]
        if page_no == 0:
            continue
        page_image = wal[off + 24:off + 24 + page_size]
        yield {'frame_index': i, 'page': page_no,
              'is_commit': db_size_after_commit != 0,
              'image': page_image,
              # Absolute byte offset of this frame's own 24-byte header in
              # the WAL FILE (not the main db) — needed so a recovered row
              # sourced from WAL history can be hex-jumped to in the -wal
              # sidecar directly. See carve_wal_history_for_table.
              'frame_offset': off}


def _wal_latest_page_images(wal: bytes, page_size: int) -> dict[int, bytes]:
    """Every page number's most-recent image still physically present in
    *wal*, keyed by page number — walk_table_leaf_pages' wal_images
    fallback is the only consumer. This is topology discovery for a table
    whose pages were never checkpointed into the main db file, not a
    general "read the current db from its WAL" API. iter_wal_frames yields
    frames in file order, so letting a later dict-set win here always
    keeps the newest image — an outdated intermediate version of a page
    could misreport a leaf as still being an interior page, or vice
    versa, and either would corrupt the topology walk that consumes this."""
    images = {}
    for frame in iter_wal_frames(wal, page_size):
        images[frame['page']] = frame['image']
    return images


def blob_safe(v, _max=64):
    """BLOB-typed record values decode as raw bytes, same as a live BLOB
    column read through sqlite3 — callers writing carved output to JSON,
    a DB TEXT column, or an MCP response need this same treatment as
    mcp_server._blob_safe (kept as a separate copy: this module has no
    dependency on mcp_server, and forensic carving output must not require
    the MCP server to even be importable)."""
    if isinstance(v, (bytes, bytearray)):
        return f'<blob {len(v)} bytes, hex: {v[:_max].hex()}{"…" if len(v) > _max else ""}>'
    return v


def flatten_json_fields(fields: dict) -> dict:
    """Some apps (Room's TypeConverter pattern, common on Android) store an
    entire row as one JSON blob in a single TEXT column rather than as
    normal SQL columns — Burner's MessageEntity is exactly this shape:
    real columns are just value/burnerId/contactId/id, and "direction",
    "text", "dateCreated" etc. all live inside the JSON string in `value`.
    Detect any field whose value parses as a JSON object and merge its
    top-level keys in as "{column}.{jsonkey}" — readable named fields
    instead of an opaque blob an examiner has to parse by hand. The
    original column is kept too (nothing is removed), so this only adds
    information, never hides where a flattened value actually came from."""
    import json as _json
    extra = {}
    for col, val in fields.items():
        if not isinstance(val, str) or not val.strip().startswith('{'):
            continue
        try:
            obj = _json.loads(val)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                v = _json.dumps(v)
            extra[f'{col}.{k}'] = v
    return extra


def record_column_names(conn, table: str) -> list[str]:
    """Column names in `PRAGMA table_info` order — which is also the
    on-disk record-body order, including a single-column `INTEGER PRIMARY
    KEY` (a rowid alias). Verified against real data: a rowid-alias column
    is NOT omitted from the record body the way an earlier version of this
    function assumed — SQLite still reserves it a position in the header,
    almost always serial type 0 (NULL, 0 bytes), and transparently
    substitutes the cell's own rowid for it at query time. Get this wrong
    (as before) and every decoded value is silently one column off. `conn`
    only needs read access for one PRAGMA — never opens or touches raw
    page bytes itself."""
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in info]


def rowid_alias_column(conn, table: str) -> str | None:
    """The column name that a bare cell rowid transparently stands in
    for (single-column INTEGER PRIMARY KEY), or None if this table has
    no such column. Used to backfill that field with the real rowid on a
    carved row that has one — its record-body value is otherwise always
    a placeholder NULL, never the actual id."""
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    pk_cols = [row[1] for row in info if row[5]]
    if len(pk_cols) == 1 and any(row[1] == pk_cols[0] and 'int' in (row[2] or '').lower()
                                 for row in info):
        return pk_cols[0]
    return None


def carve_wal_history_for_table(wal: bytes, page_size: int, leaf_pages: set[int],
                                current_rowids: set[int]) -> list[dict]:
    """Decode every historical WAL frame image for any page number in
    *leaf_pages*, and return rows that appear in some frame but are absent
    from *current_rowids* — candidates for "existed at some point in this
    WAL's history, gone from the final state.\""""
    out = []
    for frame in iter_wal_frames(wal, page_size):
        if frame['page'] not in leaf_pages:
            continue
        image = frame['image']
        if not image or image[0] != _LEAF_TABLE_PAGE:
            continue
        try:
            cells = decode_leaf_page_cells(image, frame['page'])
        except ValueError:
            continue
        for cell in cells:
            if cell['rowid'] not in current_rowids:
                cell['source'] = 'wal_frame'
                cell['frame_index'] = frame['frame_index']
                # Absolute offset of this cell's own bytes within the WAL
                # FILE itself: past the frame's 24-byte header, then the
                # cell's already-known offset within the page image.
                cell['wal_offset'] = frame['frame_offset'] + 24 + cell['offset']
                out.append(cell)
    return out


# ── top-level orchestration: what artifact_runner.py actually calls ────────

def _find_table_file(extracted_paths: dict, table: str) -> str | None:
    """Given every file artifact_runner.py extracted for one parser run,
    return whichever one's sqlite_master actually defines *table* — a
    parser script declaring `recoverable_tables` names only the table, not
    which of its (possibly several) database files holds it."""
    for path in extracted_paths.values():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'rb') as f:
                if f.read(16) != b'SQLite format 3\x00':
                    continue
            conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5)
            try:
                hit = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
            finally:
                conn.close()
            if hit:
                return path
        except Exception:
            continue
    return None


def _try_link_foreign_keys(conn: sqlite3.Connection, table: str, fields: dict) -> dict:
    """Best-effort: for each of *table*'s declared foreign keys, if the
    recovered row carries a non-NULL value for that column, look the
    target row up in its (still-live, undeleted) table and fold in a short
    label. Same-database FKs only — deliberately not worth the complexity
    of cross-database ATTACH for a bonus, best-effort pass. Adds nothing
    for a column with no match: an unresolved FK is left as the bare
    value, never replaced with a guess."""
    linked = {}
    try:
        fks = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    except sqlite3.Error:
        return linked
    for fk in fks:
        to_table, from_col, to_col = fk[2], fk[3], fk[4]
        value = fields.get(from_col)
        if value is None:
            continue
        try:
            row = conn.execute(
                f'SELECT * FROM "{to_table}" WHERE "{to_col}" = ? LIMIT 1', (value,)).fetchone()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        # First non-empty text-ish value in the linked row makes a
        # reasonable display label without knowing that table's schema.
        label = next((str(v) for v in row if isinstance(v, str) and v.strip()), None)
        linked[f'{from_col}_linked'] = f'{to_table}: {label}' if label else f'{to_table} row found'
    return linked


def recover_deleted_rows(paths: dict, table: str, field_notes: dict = None) -> list[dict]:
    """The one function artifact_runner.py calls. *paths* is the same
    dict `module.run(paths)` received (extracted-file paths, WAL/SHM
    sidecars included when the parser declared them as optional_files).
    *table* is one entry from the parser's `recoverable_tables`.
    *field_notes* is {column: caveat_string} — the parser's own
    cross-checked findings about a specific recovered column being
    unreliable (e.g. a direction flag that turned out constant regardless
    of truth). Attached to every recovered row as `{column}_caveat` rather
    than dropping the field or silently correcting it to a guess — the raw
    recovered value is never altered, just labeled.

    Never raises: this is a bonus pass layered on top of a parser's own
    (already-succeeded) SQL query, and a carving bug must never take down
    a real parse. Returns [] on any failure, same as "nothing recoverable
    found" — callers cannot tell the two apart from the return value alone,
    which is fine here since this function logs nothing evidentiary either
    way (see artifact_runner.py for where that distinction, if it matters,
    gets surfaced instead)."""
    field_notes = field_notes or {}
    try:
        db_path = _find_table_file(paths, table)
        if db_path is None:
            return []
        with open(db_path, 'rb') as f:
            raw = f.read()
        header = parse_db_header(raw)

        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=5)
        try:
            root_row = conn.execute(
                "SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if root_row is None:
                return []
            live_rowids = {r[0] for r in conn.execute(f'SELECT rowid FROM "{table}"')}
            cols = record_column_names(conn, table)
            id_col = rowid_alias_column(conn, table)

            leaves = walk_table_leaf_pages(raw, header['page_size'], root_row[0])

            # Candidates WITH a rowid — require an exact column-count match,
            # since these also required an intact (payload_length, rowid)
            # pair to decode at all; a count mismatch here means it isn't
            # really a row of this table, not a truncated one.
            rowid_candidates = carve_unallocated_region(raw, header['page_size'], leaves)
            rowid_candidates += carve_freed_pages(raw, header['page_size'], header)

            wal_path = db_path + '-wal'
            if os.path.isfile(wal_path):
                with open(wal_path, 'rb') as f:
                    wal = f.read()
                # `leaves` alone (from the main file) misses a table whose
                # root/interior pages were never checkpointed at all — see
                # walk_table_leaf_pages' own docstring for the real case
                # this was found against. Re-walking with a WAL-image
                # fallback recovers the correct leaf set for exactly that
                # case, and is a no-op for a table that WAS already
                # checkpointed (the fallback only ever fires for a page
                # missing from the main file).
                wal_images = _wal_latest_page_images(wal, header['page_size'])
                wal_leaves = walk_table_leaf_pages(raw, header['page_size'], root_row[0], wal_images)
                rowid_candidates += carve_wal_history_for_table(
                    wal, header['page_size'], set(leaves) | set(wal_leaves), live_rowids)

            out, seen, content_seen = [], set(), set()
            for c in rowid_candidates:
                if c['rowid'] in live_rowids or len(cols) != len(c['values']):
                    continue  # still live elsewhere, or not a real row of this table
                key = (c['rowid'], c['page'], c['offset'])
                if key in seen:
                    continue
                seen.add(key)
                fields = {name: blob_safe(v) for name, v in zip(cols, c['values'])}
                if id_col:
                    # The record body's own value for the rowid-alias
                    # column is a placeholder NULL (see record_column_names)
                    # — the real value only exists as the cell's rowid,
                    # which this candidate has. Backfill it.
                    fields[id_col] = c['rowid']
                fields.update(flatten_json_fields(fields))
                # Exact-duplicate content (e.g. the same logical row
                # captured unchanged across several WAL frames) is pure
                # noise -- collapse it. A row whose fields genuinely
                # evolved between frames (e.g. a "read" flag flipping) is
                # NOT a duplicate by this check and is kept: that's real
                # timeline signal, not noise, and collapsing it would lose
                # information rather than just tidying the output.
                content_key = tuple(sorted(fields.items()))
                if content_key in content_seen:
                    continue
                content_seen.add(content_key)
                is_wal = c['source'] == 'wal_frame'
                row = {
                    'recovered': True,
                    'recovery_method': c['source'],
                    'source_table': table,
                    'raw_page': c['page'],
                    'raw_rowid': c['rowid'],
                    # Exact on-disk location of THIS candidate's own bytes,
                    # for the Artifact Viewer's Hex-panel Record mode to
                    # jump straight to — no live-b-tree search needed (that
                    # would never find a carved row anyway); the carving
                    # pass already knows precisely where it found this one.
                    # 'main' = the primary db file record_source's file_key
                    # already resolves; 'wal' = that same key's "_wal"
                    # sidecar (see resolve_module_file_ui_path's caller).
                    'raw_file':   'wal' if is_wal else 'main',
                    'raw_offset': c['wal_offset'] if is_wal else c['offset'],
                    'raw_length': c.get('length') or c.get('cell_len'),
                }
                row.update(fields)
                row.update(_try_link_foreign_keys(conn, table, fields))
                row.update({f'{col}_caveat': note for col, note in field_notes.items()
                           if col in fields})
                out.append(row)

            # Header-signature candidates — no rowid to compare against
            # live_rowids at all (see the section docstring above
            # carve_by_header_signature for why), so every match here is,
            # by construction, not one of the rows the live query already
            # returned. A short match (fewer values than `cols`) is a
            # genuine truncation, not a mismatch — keep it, missing fields
            # simply absent from the row rather than padded with a guess.
            signature = sample_header_signature(raw, header['page_size'], leaves)
            for c in carve_by_header_signature(raw, header['page_size'], leaves, signature):
                key = ('header_signature', c['page'], c['header_offset'])
                if key in seen:
                    continue
                seen.add(key)
                fields = {name: blob_safe(v) for name, v in zip(cols, c['values'])}
                fields.update(flatten_json_fields(fields))
                content_key = tuple(sorted(fields.items()))
                if content_key in content_seen:
                    continue
                content_seen.add(content_key)
                row = {
                    'recovered': True,
                    'recovery_method': c['source'],
                    'source_table': table,
                    'raw_page': c['page'],
                    'raw_rowid': None,
                    # header-signature matches only ever come from the main
                    # db file's free space (see carve_by_header_signature —
                    # it's never run against WAL frames), so this is always
                    # 'main'.
                    'raw_file':   'main',
                    'raw_offset': c['header_offset'],
                    'raw_length': c.get('length'),
                    'truncated': c['truncated'],
                }
                row.update(fields)
                row.update(_try_link_foreign_keys(conn, table, fields))
                row.update({f'{col}_caveat': note for col, note in field_notes.items()
                           if col in fields})
                out.append(row)

            return out
        finally:
            conn.close()
    except Exception:
        return []
