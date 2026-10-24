"""A densely cross-linked NSKeyedArchiver plist (like a Core Data .mom)
must not hang decode_plist_blob or the embedded-media sweep (found
2026-10-24 on Snapchat's scdb-27.sqlite3.mom)."""
import plistlib
import time
from plistlib import UID

import nska_deserialize

from artifact_runner import decode_plist_blob
from embedded_media_scan import scan_plist_bytes

PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x00' * 6000


def _archive(objects, root):
    return plistlib.dumps({'$version': 100000, '$archiver': 'NSKeyedArchiver',
                           '$top': {'root': UID(root)}, '$objects': objects},
                          fmt=plistlib.FMT_BINARY)


def _cross_linked(n=60):
    """Each node holds two references onward, so expanding it as a tree
    takes ~fib(n) steps; the last one also holds a PNG."""
    cls = n + 2
    objects = ['$null']
    for i in range(1, n + 1):
        nxt = [UID(min(i + 1, n)), UID(min(i + 2, n))] if i < n else [UID(n + 1)]
        objects.append({'$class': UID(cls), 'NS.objects': nxt})
    objects.append(PNG)
    objects.append({'$classname': 'NSArray', '$classes': ['NSArray', 'NSObject']})
    return _archive(objects, 1)


def test_cross_linked_archive_stops_and_sweep_still_finds_blob():
    raw = _cross_linked()
    t = time.time()
    assert decode_plist_blob(raw) is None
    hits = list(scan_plist_bytes(raw, label='test.mom'))
    assert time.time() - t < 30
    assert [h['plist_path'] for h in hits] == [f'$objects[{61}]']
    assert hits[0]['blob'] == PNG
    assert nska_deserialize.rec_uids == []          # library state reset


def test_normal_archive_still_decodes_after_a_stopped_run():
    decode_plist_blob(_cross_linked())
    small = _archive(['$null', {'NS.keys': [UID(2)], 'NS.objects': [UID(3)], '$class': UID(4)},
                      'photo', PNG, {'$classname': 'NSDictionary',
                                     '$classes': ['NSDictionary', 'NSObject']}], 1)
    assert decode_plist_blob(small) == {'photo': PNG}
    assert [h['plist_path'] for h in scan_plist_bytes(small)] == ['photo']
