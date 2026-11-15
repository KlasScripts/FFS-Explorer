"""Header Scan reads files proven identical once (duplicate_scan.proven_copies):
proof is the same GrayKey SHA-256 or the same trusted inode, on top of the
same size and CRC-32. A copy that differs, or is not proven, is read."""
import duplicate_scan


def test_sha256_proves_copies_and_the_canonical_path_is_read():
    m = [('data/user/0/a/x.db', 10, 7, 0, 0, 'S'),
         ('data/data/a/x.db', 10, 7, 0, 0, 'S'),
         ('data_mirror/data_ce/null/0/a/x.db', 10, 7, 0, 0, 'S')]
    assert duplicate_scan.proven_copies(m) == {
        'data/user/0/a/x.db': 'data/data/a/x.db',
        'data_mirror/data_ce/null/0/a/x.db': 'data/data/a/x.db'}


def test_a_differing_copy_is_never_mapped():
    m = [('data/data/a/x.db-wal', 10, 7, 0, 0, 'S1'),
         ('data/user/0/a/x.db-wal', 12, 9, 0, 0, 'S2')]      # changed during capture
    assert duplicate_scan.proven_copies(m) == {}
    # same SHA-256 recorded but a different size/CRC: not trusted as a copy
    m = [('a', 10, 7, 0, 0, 'S'), ('b', 10, 8, 0, 0, 'S')]
    assert duplicate_scan.proven_copies(m) == {}


def test_size_and_crc_alone_are_not_proof():
    m = [('data/data/a', 10, 7, 0, 0, ''), ('data/user/0/a', 10, 7, 0, 0, '')]
    assert duplicate_scan.proven_copies(m) == {}
    assert duplicate_scan.proven_copies(m, inode_trusted=True) == {}   # no inode either


def test_inode_is_proof_only_when_trusted():
    m = [('data/data/a', 10, 7, 55, 3, ''), ('data/user/0/a', 10, 7, 55, 3, '')]
    assert duplicate_scan.proven_copies(m) == {}
    assert duplicate_scan.proven_copies(m, inode_trusted=True) == {'data/user/0/a': 'data/data/a'}
    # same inode on another device id: a different file
    m2 = [('x', 10, 7, 55, 3, ''), ('y', 10, 7, 55, 4, '')]
    assert duplicate_scan.proven_copies(m2, inode_trusted=True) == {}
