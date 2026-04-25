#!/usr/bin/env python3

#!/usr/bin/env python3
# Based on gkls.py by slo-sleuth
# https://github.com/slo-sleuth/gkls/blob/master/gkls.py
# Original work Copyright (c) [year] slo-sleuth, MIT Licence

"""
graykey.py — extract metadata from a Graykey full_files zip into a
Cellebrite-compatible structure, optionally saved as msgpack.

Also used for Android zips that carry UT/UX extra fields but no GrayKey
block (e.g. Cellebrite Android FFS) via extract_with_prefix().

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
from struct import Struct, error as StructError

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

    # UT block: flags(1B) then up to 4 × uint32 timestamps, each present only
    # if the corresponding flag bit is set.  The central-directory copy of UT
    # only carries flags + mtime (5 bytes); the local-file-header copy carries
    # all timestamps that are flagged.  We parse flag-by-flag so both forms work.
    ut = _find_block(extra, _TAG_UT)
    mtime = atime = ctime = btime = 0
    has_ut = ut is not None and len(ut) >= 1
    if has_ut:
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
        import calendar
        dt = f.date_time   # (year, month, day, hour, min, sec)
        try:
            mtime = int(calendar.timegm(dt + (0, 0, -1)))
        except (ValueError, OverflowError):
            mtime = 0

    # UID/GID from UX block: version(1B) uid_sz(1B) uid(uid_sz B) gid_sz(1B) gid(gid_sz B)
    ux = _find_block(extra, _TAG_UX)
    if ux is not None:
        uid_sz = ux[1]
        uid    = int.from_bytes(ux[2:2 + uid_sz], 'little')
        gid_off = 2 + uid_sz
        gid    = int.from_bytes(ux[gid_off + 1:gid_off + 1 + ux[gid_off]], 'little')
    else:
        uid = gid = 0

    # Inode from IN block: inode(Q=8B) + additional fields (ignored)
    in_block = _find_block(extra, _TAG_IN)
    inode = int.from_bytes(in_block[:8], 'little') if in_block is not None else 0

    # Graykey block: version(1B) flags(1B) [prot_class(4B)] [xattrs]
    gk = _find_gk_block(extra)
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


def extract(zip_path: str) -> dict:
    """
    Parse a Graykey full_files zip. Returns a Cellebrite-compatible metadata
    dict keyed by full entry path (no leading slash).

    Raises TypeError if not a valid zip or not a Graykey archive.
    """
    if not zipfile.is_zipfile(zip_path):
        raise TypeError(f'{zip_path!r} is not a valid zip file')

    with zipfile.ZipFile(zip_path, 'r') as z:
        if not _is_graykey(z):
            raise TypeError(f'{zip_path!r} does not appear to be a Graykey archive')

        return {
            f.filename.rstrip('/'): _parse_entry(f)
            for f in z.infolist()
        }


def extract_with_prefix(zip_path: str, strip_prefix: str) -> dict:
    """
    Parse a zip whose entries carry UT/UX extra fields but no GrayKey block.
    Strips *strip_prefix* (e.g. 'Dump/') from entry names to produce ui_paths.
    Returns a Cellebrite-compatible metadata dict.

    Reads the local file header extra field directly so that all UT timestamps
    (mtime, atime, ctime) are available — the central-directory copy of UT only
    carries mtime, so using ZipInfo.extra alone would lose atime and ctime.
    """
    slash = strip_prefix if strip_prefix.endswith('/') else strip_prefix + '/'
    entries: list[tuple[str, zipfile.ZipInfo]] = []
    with zipfile.ZipFile(zip_path, 'r') as z:
        for f in z.infolist():
            name = f.filename.rstrip('/')
            if name.startswith(slash):
                name = name[len(slash):]
            elif name == strip_prefix.rstrip('/'):
                continue  # the prefix dir itself — skip
            else:
                continue  # entry outside the prefix — not part of this extraction
            if not name:
                continue  # stripping the prefix produced an empty path — skip
            entries.append((name, f))

    # Probe up to 8 file entries to decide whether local file header extras
    # carry more UT data than the central directory copy.  If every probe shows
    # a local UT block no longer than the CD UT block, the archive only stores
    # mtime (Cellebrite Android pattern) — skip 100K+ raw seeks and use f.extra.
    _need_local = False
    with open(zip_path, 'rb') as rf:
        for _, f in entries[:8]:
            cd_ut = _find_block(f.extra, _TAG_UT)
            cd_len = len(cd_ut) if cd_ut else 0
            rf.seek(f.header_offset + 26)
            fname_len, extra_len = struct.unpack('<HH', rf.read(4))
            rf.seek(f.header_offset + 30 + fname_len)
            local_ut = _find_block(rf.read(extra_len), _TAG_UT)
            local_len = len(local_ut) if local_ut else 0
            if local_len > cd_len:
                _need_local = True
                break

    result = {}
    if not _need_local:
        # Central directory has everything available — no raw seeks needed.
        for name, f in entries:
            result[name] = _parse_entry(f)
    else:
        with open(zip_path, 'rb') as rf:
            for name, f in entries:
                rf.seek(f.header_offset + 26)
                fname_len, extra_len = struct.unpack('<HH', rf.read(4))
                rf.seek(f.header_offset + 30 + fname_len)
                local_extra = rf.read(extra_len)
                result[name] = _parse_entry(f, local_extra if local_extra else None)
    return result


def save(metadata: dict, out_path: str) -> None:
    """Serialise metadata dict to a msgpack file."""
    with open(out_path, 'wb') as fh:
        fh.write(msgpack.packb(metadata, use_bin_type=True))


def load(msgpack_path: str) -> dict:
    """Load a msgpack metadata file and return the dict."""
    with open(msgpack_path, 'rb') as fh:
        return msgpack.unpackb(fh.read(), raw=False, strict_map_key=False)


def extract_metadata(zip_path: str) -> dict:
    """
    Parse a Graykey archive and return the metadata dict in memory.
    Nothing is written to disk — evidence-derived data must not be
    left as residual files subject to data-retention obligations (e.g. MoPI).
    """
    return extract(zip_path)


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
