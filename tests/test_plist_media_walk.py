"""embedded_media_scan reads keyed archives (NSKeyedArchiver) from their
object table, each object once, instead of expanding them: the same media
with the same labels as the expanded tree (nska_deserialize, as reports
decode them) for every object the tree shows, plus media the expansion
never showed (objects nothing in $top reaches, and values more than 8
levels down, where the old walk stopped). IOS17 2026-11-19: 3,150 keyed
archives, the same media in 3,148, 30 more in the other 2, 23.4 s -> 9.0 s
on one process."""
import plistlib
from plistlib import UID

import embedded_media_scan as ems
from artifact_runner import decode_plist_blob

PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x01' * 6000
PNG2 = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'\x02' * 6000
JPG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x03' * 6000

DICT = {'$classname': 'NSDictionary', '$classes': ['NSDictionary', 'NSObject']}
ARR = {'$classname': 'NSArray', '$classes': ['NSArray', 'NSObject']}
MDATA = {'$classname': 'NSMutableData', '$classes': ['NSMutableData', 'NSData', 'NSObject']}
STR = {'$classname': 'NSMutableString', '$classes': ['NSMutableString', 'NSString', 'NSObject']}
CUSTOM = {'$classname': 'Thing', '$classes': ['Thing', 'NSObject']}


def _archive(objects, top=None):
    return plistlib.dumps({'$version': 100000, '$archiver': 'NSKeyedArchiver',
                           '$top': top or {'root': UID(1)}, '$objects': objects},
                          fmt=plistlib.FMT_BINARY)


def _expanded(raw):
    """(path, blob) set from the expanded tree: the reference."""
    out = set()

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, path + (str(k),))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + (f'[{i}]',))
        elif isinstance(o, bytes):
            if o.startswith(b'bplist00') and decode_plist_blob(o) is not None:
                walk(decode_plist_blob(o), path + ('<nested-bplist>',))
            elif ems.classify_media_blob(o):
                out.add(('.'.join(path), o))
    walk(decode_plist_blob(raw), ())
    return out


def _scanned(raw):
    return {(h['plist_path'], h['blob']) for h in ems.scan_plist_bytes(raw)}


def _rich():
    # root: Thing{photo: PNG, data: NSMutableData{JPG}, items: [PNG2, 'x'],
    #             meta: {'icon': PNG2, 'name': 'n'}, inner: bplist(Thing{img: PNG})}
    inner = _archive(['$null', {'$class': UID(3), 'img': UID(2)}, PNG, CUSTOM])
    objs = ['$null',
            {'$class': UID(2), 'photo': UID(3), 'data': UID(4), 'items': UID(6),
             'meta': UID(9), 'inner': UID(14)},
            CUSTOM, PNG,
            {'$class': UID(5), 'NS.data': JPG}, MDATA,
            {'$class': UID(8), 'NS.objects': [UID(7), UID(13)]}, PNG2, ARR,
            {'$class': UID(12), 'NS.keys': [UID(10), UID(11)], 'NS.objects': [UID(7), UID(13)]},
            'icon', 'name', DICT, 'x', inner]
    return _archive(objs)


def test_same_media_and_labels_as_the_expanded_tree():
    raw = _rich()
    got = _scanned(raw)
    ref = _expanded(raw)
    # PNG2 is shared (items[0] and meta.icon): found once, where the tree
    # shows it first; every other (label, media) exactly as the tree gives it
    assert got == ref - {('meta.icon', PNG2)}
    assert ('photo', PNG) in got and ('data.NS.data', JPG) in got
    assert ('items.[0]', PNG2) in got and ('inner.<nested-bplist>.img', PNG) in got


def test_top_level_names_as_nska_sets_them_out():
    objs = ['$null', {'$class': UID(2), 'photo': UID(3)}, CUSTOM, PNG]
    for top in ({'root': UID(1)}, {'other': UID(1)}, {'root': UID(1), 'pic': UID(3)},
                {'pic': UID(3)}):
        raw = _archive(objs, top)
        got, ref = _scanned(raw), _expanded(raw)
        # each object once, at its first place in the tree; with two roots
        # the PNG is both $top.pic and root.photo, and is listed at the first
        assert got and got <= ref, top
        assert {b for _p, b in got} == {b for _p, b in ref}, top
        assert len(got) == len({b for _p, b in ref}), top


def test_unreferenced_object_found_and_named():
    objs = ['$null', {'$class': UID(2), 'photo': UID(3)}, CUSTOM, PNG, JPG]   # JPG: nothing refers to it
    raw = _archive(objs)
    assert _expanded(raw) == {('photo', PNG)}
    assert _scanned(raw) == {('photo', PNG), ('<unreferenced $objects[4]>', JPG)}
    assert ems.compute_display_name('a.plist', 'plist', '<unreferenced $objects[4]>',
                                    {'ext': '.jpg'}) == 'unreferenced.jpg'


def test_deep_values_found():
    # 12 levels down: the old walk stopped at 8
    deep = PNG
    for i in range(12):
        deep = {f'k{i}': deep}
    raw = plistlib.dumps(deep, fmt=plistlib.FMT_BINARY)
    assert [h['plist_path'] for h in ems.scan_plist_bytes(raw)] == [
        '.'.join(f'k{i}' for i in reversed(range(12)))]
    # a chain of 12 objects, each holding the next; the last holds the PNG
    n = 12
    objs = ['$null'] + [{'$class': UID(n + 2), 'next': UID(i + 2)} for i in range(n - 1)]
    objs += [{'$class': UID(n + 2), 'img': UID(n + 1)}, PNG, CUSTOM]
    hits = list(ems.scan_plist_bytes(_archive(objs)))
    assert [(h['plist_path'], h['blob']) for h in hits] == [('.'.join(['next'] * (n - 1) + ['img']), PNG)]


def test_not_a_plist_and_plain_plist():
    assert list(ems.scan_plist_bytes(b'not a plist')) == []
    raw = plistlib.dumps({'a': [1, {'b': PNG}], 'c': JPG})
    assert _scanned(raw) == {('a.[1].b', PNG), ('c', JPG)}
