"""duplicate_scan.py — size + CRC32 candidate grouping with MD5 confirmation.

Added 2026-10-16, direct request: "i want to use the size and crc and
then md5 check to confirm. hopefully only do the md5 for the duplicates
will make it not too long."

Size and CRC32 come free from the zip's already-parsed central directory
(every zipfile.ZipInfo already carries both — no I/O to read them). Only
files inside a multi-member (size, CRC32) group are actually opened and
MD5-hashed — CRC32 is a non-cryptographic checksum, and while a collision
between two unrelated real files is astronomically rare, it is not
impossible, so MD5 is what actually confirms two files are byte-identical.

Extended same day, direct follow-up: "if we already know it has the same
inode can we not shortcut the check[?] do not bother with the md5." When
the caller's archive format makes (inode, devid) trustworthy (see
FfsAdapter.inode_trusted's own docstring — POSIX guarantees (device,
inode) uniquely identifies a file, so this is provable, not a heuristic),
members of a (size, CRC32) group that additionally share a real nonzero
(inode, devid) collapse into ONE "hash unit" — only its first member is
actually read; the digest is applied to every member that shares it.
Confirmed on a real archive: 117,737 of 121,382 candidate files were
hardlink siblings this way — a 97% reduction in actual file reads/hashes,
with zero loss of correctness (same-device-same-inode is definitionally
the same file, a stronger guarantee than a matching MD5 even gives,
since MD5 only proves identical content, not that it is literally the
same underlying data).

Extended 2026-10-20, direct request: GrayKey records each file's SHA-256
at acquisition (ASSUMPTIONS.md A-73). A (size, CRC32) group whose every
member carries one is grouped on it with no reads at all. Those results
store the SHA-256 hex (64 chars) in the same 'md5' field — the table's
schema is unchanged, so casecache.db isn't rebuilt — and the File
Browser's own "MD5" column shows only real 32-char MD5 values."""

import hashlib

from zip_entry import ZipEntry


def find_duplicate_groups(zip_path, candidates, inode_trusted=False,
                          progress_cb=None, cancel_check=None):
    """*candidates* — [(ui_path, physical_path, zinfo, inode, devid)] for
    every real file to consider, where *zinfo* is the entry's
    zipfile.ZipInfo (already parsed from the central directory — no new
    I/O for this step) and *inode*/*devid* are 0 when absent/not
    meaningful for this entry's format.

    *inode_trusted* — True when the caller has confirmed (inode, devid)
    reliably identifies one file within this archive (see
    FfsAdapter.inode_trusted). When True, (size, CRC32) group members
    sharing a real (nonzero) (inode, devid) are POSIX-guaranteed the same
    file and collapse into one hash unit, skipping their own read/hash
    entirely — see this module's own docstring for the real-archive
    numbers. Devid is irrelevant (always 0) for formats with no device
    concept at all (Cellebrite) — grouping on (inode, 0) there is exactly
    equivalent to grouping on bare inode.

    Returns {ui_path: {'md5': str, 'size': int, 'group_size': int}} for
    every file CONFIRMED byte-identical to (or, via the inode shortcut,
    provably the same underlying file as) at least one other file in
    *candidates*. A (size, CRC32) group with only one member is never
    opened or hashed at all.

    progress_cb(done, total) — *total* is the number of files that will
    actually be read (real candidates, further reduced by the inode
    shortcut when it applies), not len(candidates) — so a caller's
    progress bar reflects the real, usually much smaller, amount of work.

    cancel_check() — checked before every hash; returns whatever has been
    confirmed so far if it becomes true (a partial, still-correct result,
    never a half-hashed group)."""
    by_size_crc: dict[tuple[int, int], list] = {}
    # Optional 6th field: the SHA-256 the acquisition tool recorded ('' if
    # none). Optional 7th: a zero-arg callable returning the file's bytes,
    # for files that aren't in the main zip (unpacked from a nested archive,
    # see nested_archive.unpacked_duplicate_candidates); zinfo then only
    # needs file_size and CRC.
    for ui_path, phys, zinfo, inode, devid, *rest in candidates:
        by_size_crc.setdefault((zinfo.file_size, zinfo.CRC), []).append(
            (ui_path, phys, zinfo, inode, devid,
             rest[0] if rest else '', rest[1] if len(rest) > 1 else None))

    results: dict[str, dict] = {}
    to_process = []
    for group in by_size_crc.values():
        if len(group) < 2:
            continue
        # Every member carries the SHA-256 the acquisition tool recorded
        # (GrayKey, ASSUMPTIONS.md A-73): group on it — no file is read.
        # Otherwise the whole group takes the MD5 path below, so a group
        # never mixes the two digest types.
        if all(m[5] for m in group):
            by_sha: dict[str, list] = {}
            for m in group:
                by_sha.setdefault(m[5], []).append(m)
            for digest, members in by_sha.items():
                if len(members) < 2:
                    continue
                for m in members:
                    results[m[0]] = {'md5': digest, 'size': m[2].file_size,
                                     'group_size': len(members)}
        else:
            to_process.append(group)

    # Collapse same-(inode,devid) members within each (size, CRC32) group
    # into one hash unit — (representative_member, [ui_path, ...]).  When
    # inode_trusted is False, or a member's inode is 0 (absent), every
    # member is its own standalone unit, same behaviour as before this
    # shortcut existed.
    grouped_units: list[list] = []
    for group in to_process:
        if not inode_trusted:
            grouped_units.append([(m, [m[0]]) for m in group])
            continue
        by_inode: dict[tuple[int, int], list] = {}
        units = []
        for member in group:
            inode = member[3]
            if inode:
                by_inode.setdefault((inode, member[4]), []).append(member)
            else:
                units.append((member, [member[0]]))
        for members in by_inode.values():
            units.append((members[0], [m[0] for m in members]))
        grouped_units.append(units)

    total = sum(len(units) for units in grouped_units)
    if progress_cb:
        progress_cb(0, total)
    if total == 0:
        return results

    done = 0
    for units in grouped_units:
        by_md5: dict[str, list] = {}
        for (ui_path, phys, zinfo, inode, devid, _sha, reader), member_paths in units:
            if cancel_check and cancel_check():
                return results
            data = reader() if reader is not None else ZipEntry(zip_path, phys, zinfo).read()
            done += 1
            if progress_cb:
                progress_cb(done, total)
            if data is None:
                continue     # unreadable: can't confirm, so never reported as a duplicate
            digest = hashlib.md5(data).hexdigest()
            by_md5.setdefault(digest, []).extend(
                (p, zinfo.file_size) for p in member_paths)
        for digest, members in by_md5.items():
            if len(members) < 2:
                continue  # CRC32 collided but MD5 didn't — not a real duplicate
            for ui_path, size in members:
                results[ui_path] = {
                    'md5': digest,
                    'size': size,
                    'group_size': len(members),
                }
    return results
