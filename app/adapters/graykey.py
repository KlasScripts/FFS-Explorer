#!/usr/bin/env python3

#!/usr/bin/env python3
# Based on gkls.py by slo-sleuth
# https://github.com/slo-sleuth/gkls/blob/master/gkls.py
# Original work Copyright (c) [year] slo-sleuth, MIT Licence

"""
graykey.py — extract metadata from a Graykey full_files zip into a
Cellebrite-compatible structure, optionally saved as msgpack.

The UT/UX/inode extra-field parsers here are also used by FfsAdapter for
Cellebrite Android zips (format detection via _has_ut_extras, and mtime
from the UT block — the only timestamp that format carries).

Schema (keyed by full entry path, no leading slash):
    {
        "path/to/entry": {
            "atime": <nanoseconds int>,
            "btime": <nanoseconds int>,
            "ctime": <nanoseconds int>,
            "mtime": <nanoseconds int>,
            "uid":   int,
            "gid":   int,
            "inode": int,
            "links": None,   # not stored in Graykey
            "mode":  None,   # not stored in Graykey
            "prot":  None,   # not stored in Graykey
            "size":  int,
            "xattr": { "key": bytes, ... }
        }
    }

Dependency: msgpack
"""

import zipfile
from pathlib import Path
import struct
from struct import Struct

import msgpack

# Extra-field block tags (little-endian 16-bit IDs)
_TAG_UT     = 0x5455   # Unix Timestamp  — flags + mtime/atime/ctime/btime
_TAG_UX     = 0x7875   # Info-ZIP Unix   — version + uid_sz + uid + gid_sz + gid
_TAG_IN     = 0x4e49   # Inode number    — inode(Q) + devID(L) + ...
_TAG_GK     = 0x4b47   # Graykey block (newer)  — tag bytes b'GK'
_TAG_GK_OLD = 0x0004   # Graykey block (older)  — original format tag

_ST_TLV = Struct('<HH')   # block tag + length
_ST_U32 = Struct('<I')    # single uint32 (xattr count / length)

_S_TO_NS = 1_000_000_000

# Bind unpack_from methods to locals — avoids attribute lookup per call
_unpack_tlv = _ST_TLV.unpack_from
_unpack_u32 = _ST_U32.unpack_from


def _find_block(extra: bytes, tag: int) -> bytes | None:
    """Scan the extra field TLV chain and return the data payload for *tag*, or None."""
    off = 0
    while off + 4 <= len(extra):
        t, length = _unpack_tlv(extra, off)
        off += 4
        if t == tag:
            return extra[off:off + length]
        off += length
    return None


def _find_gk_block(extra: bytes) -> bytes | None:
    """Return the Graykey block data, checking both the new and old tag."""
    return _find_block(extra, _TAG_GK) or _find_block(extra, _TAG_GK_OLD)


def _is_graykey(z: zipfile.ZipFile) -> bool:
    for info in z.infolist()[:20]:
        if _find_gk_block(info.extra) is not None:
            return True
    return False


def _parse_xattrs(extra: bytes, off: int) -> dict:
    count = _unpack_u32(extra, off)[0]
    off  += 4
    xattrs = {}
    for _ in range(count):
        length = _unpack_u32(extra, off)[0]
        off   += 4
        chunk  = extra[off:off + length]
        off   += length
        null   = chunk.find(b'\x00')
        if null == -1:
            continue  # malformed entry — skip
        xattrs[chunk[:null].decode()] = chunk[null + 1:]
    return xattrs


def _parse_entry(f: zipfile.ZipInfo, extra: bytes | None = None) -> dict:
    if extra is None:
        extra = f.extra

    # Single-pass TLV scan — collect all needed blocks in one traversal
    # instead of calling _find_block separately for each tag.
    ut = ux = in_block = gk = None
    off = 0
    _unpack_tlv_local = _unpack_tlv
    while off + 4 <= len(extra):
        t, length = _unpack_tlv_local(extra, off)
        off += 4
        if   t == _TAG_UT:
            ut       = extra[off:off + length]
        elif t == _TAG_UX:
            ux       = extra[off:off + length]
        elif t == _TAG_IN:
            in_block = extra[off:off + length]
        elif t == _TAG_GK or t == _TAG_GK_OLD:
            gk       = extra[off:off + length]
        off += length

    # UT block: flags(1B) then up to 4 × uint32 timestamps, each present only
    # if the corresponding flag bit is set.  The central-directory copy of UT
    # only carries flags + mtime (5 bytes); the local-file-header copy carries
    # all timestamps that are flagged.  We parse flag-by-flag so both forms work.
    mtime = atime = ctime = btime = 0
    if ut is not None and len(ut) >= 1:
        flags = ut[0]
        off   = 1
        if (flags & 1) and off + 4 <= len(ut):
            mtime = struct.unpack_from('<I', ut, off)[0]; off += 4
        if (flags & 2) and off + 4 <= len(ut):
            atime = struct.unpack_from('<I', ut, off)[0]; off += 4
        if (flags & 4) and off + 4 <= len(ut):
            ctime = struct.unpack_from('<I', ut, off)[0]; off += 4
        if (flags & 8) and off + 4 <= len(ut):   # GrayKey btime extension
            btime = struct.unpack_from('<I', ut, off)[0]
    else:
        # No UT block at all — fall back to DOS mod time (always present,
        # 2-second precision).  Only used when the entry carries no Unix
        # timestamp whatsoever; a UT block with mtime=0 is honoured as-is
        # (displayed as "---") rather than being overwritten with the DOS date.
        # DOS year 1980 is the epoch zero for the format (meaning "not set").
        import calendar
        dt = f.date_time   # (year, month, day, hour, min, sec)
        if dt[0] <= 1980:
            mtime = 0
        else:
            try:
                mtime = int(calendar.timegm(dt + (0, 0, -1)))
            except (ValueError, OverflowError):
                mtime = 0

    # UID/GID from UX block: version(1B) uid_sz(1B) uid(uid_sz B) gid_sz(1B) gid(gid_sz B)
    if ux is not None:
        uid_sz  = ux[1]
        uid     = int.from_bytes(ux[2:2 + uid_sz], 'little')
        gid_off = 2 + uid_sz
        gid     = int.from_bytes(ux[gid_off + 1:gid_off + 1 + ux[gid_off]], 'little')
    else:
        uid = gid = 0

    # Inode from IN block: inode(Q=8B) + additional fields (ignored)
    inode = int.from_bytes(in_block[:8], 'little') if in_block is not None else 0

    # Graykey block: version(1B) flags(1B) [prot_class(4B)] [xattrs]
    xattrs = {}
    if gk is not None and len(gk) >= 2:
        gver, gflag = gk[0], gk[1]
        if gver != 1:
            raise ValueError(f'Unsupported Graykey version {gver} in {f.filename!r}')
        off = 2  # past version + flags bytes within gk data
        if gflag & 1:
            off += 4  # skip data protection class
        if gflag & 2:
            xattrs = _parse_xattrs(gk, off)

    return {
        'atime': atime * _S_TO_NS,
        'btime': btime * _S_TO_NS,
        'ctime': ctime * _S_TO_NS,
        'mtime': mtime * _S_TO_NS,
        'uid':   uid,
        'gid':   gid,
        'inode': inode,
        'links': None,
        'mode':  None,
        'prot':  None,
        'size':  f.file_size,
        'xattr': xattrs,
    }


def _has_ut_extras(z: zipfile.ZipFile) -> bool:
    """Return True if the first entries carry UT extra-field blocks (Unix timestamps)."""
    for info in z.infolist()[:20]:
        if _find_block(info.extra, _TAG_UT) is not None:
            return True
    return False


def extract(zip_path: str, z: zipfile.ZipFile | None = None) -> dict:
    """
    Parse a Graykey full_files zip. Returns a Cellebrite-compatible metadata
    dict keyed by full entry path (no leading slash).

    If *z* is an already-open ZipFile it is used directly (no second open).
    Raises TypeError if not a valid zip or not a Graykey archive.
    """
    if z is not None:
        return {f.filename.rstrip('/'): _parse_entry(f) for f in z.infolist()}
    if not zipfile.is_zipfile(zip_path):
        raise TypeError(f'{zip_path!r} is not a valid zip file')
    with zipfile.ZipFile(zip_path, 'r') as z:
        if not _is_graykey(z):
            raise TypeError(f'{zip_path!r} does not appear to be a Graykey archive')
        return {f.filename.rstrip('/'): _parse_entry(f) for f in z.infolist()}


def save(metadata: dict, out_path: str) -> None:
    """Serialise metadata dict to a msgpack file (used by the CLI mode below)."""
    with open(out_path, 'wb') as fh:
        fh.write(msgpack.packb(metadata, use_bin_type=True))


def extract_metadata(zip_path: str, z: zipfile.ZipFile | None = None) -> dict:
    """
    Parse a Graykey archive and return the metadata dict in memory.
    Nothing is written to disk — evidence-derived data must not be
    left as residual files subject to data-retention obligations (e.g. MoPI).

    If *z* is an already-open ZipFile it is used directly (no second open).
    """
    return extract(zip_path, z)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print('Usage: graykey.py <zip> [output.msgpack]')
        sys.exit(1)

    zip_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else str(Path(zip_path).with_suffix('.msgpack'))

    print(f'Extracting: {zip_path}')
    data = extract(zip_path)
    save(data, out_path)
    print(f'Saved {len(data)} entries → {out_path}')
