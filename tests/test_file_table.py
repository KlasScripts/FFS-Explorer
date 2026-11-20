"""file_table: the file list and folder contents read back exactly (values,
types, None vs 0, key order, list order) from the mapped sidecar; the
mappings behave as the dicts they replace under random changes; copies are
independent; pickling maps the same file; anything that would not read
back exactly is refused; a snapshot that names a file table reads it back,
and one whose file table is missing or another one fails (so the case is
parsed again) rather than giving a wrong file list."""
import gc
import os
import pickle
import random

import msgpack
import pytest

import file_table as ft
import ffs_metadata

IOS_ORDER = ('atime', 'uid', 'btime', 'size', 'gid', 'inode', 'ctime', 'links',
             'mtime', 'mode', 'prot', 'xattr')


def _ios_entry(rnd, i):
    v = {k: rnd.randrange(0, 1 << 40) for k in IOS_ORDER if k != 'xattr'}
    v['mtime'] = (1 << 63) - 1 if i % 97 == 0 else v['mtime']     # the largest stored value
    v['xattr'] = {'com.apple.x': bytes([i % 256]) * 3} if i % 10 == 0 else {}
    return {k: v[k] for k in IOS_ORDER}


def _sample(n=3000, seed=1):
    rnd = random.Random(seed)
    ui, fm = {}, {}
    for i in range(n):
        folder = f'private/var/mobile/F{i % 37}'
        p = f'{folder}/file {i} é☃.db'
        if i % 50 == 0:
            v = {'size': 0, 'mtime': None, '_display_name': 'x', 'capture': '1 of 2'}   # other keys
        elif i % 51 == 0:
            v = {'atime': 0, 'btime': 0, 'ctime': 0, 'mtime': 5, 'uid': 0, 'gid': 0,     # Android: Nones
                 'inode': 0, 'links': None, 'mode': None, 'prot': None, 'size': 0, 'xattr': {}}
        else:
            v = _ios_entry(rnd, i)
        ui[p] = v
        fm.setdefault(folder, []).append(p)
    fm['private/var/mobile'] = [f'private/var/mobile/F{k}' for k in range(37)]
    fm['private/var/mobile/Empty'] = []
    return ui, fm


def _typed(d):
    return [(k, type(v), v) for k, v in d.items()]


@pytest.fixture
def table(tmp_path):
    ui, fm = _sample()
    path = ft.new_path(str(tmp_path))
    ft.write(ui, fm, path)
    yield ui, fm, path
    ft._OPEN.clear()


def test_reads_back_exactly(table):
    ui, fm, path = table
    FL, FM = ft.FileList(ft.FileTable(path)), ft.FolderMap(ft.FileTable(path))
    assert list(FL) == list(ui) and len(FL) == len(ui)
    for k, v in ui.items():
        assert _typed(FL[k]) == _typed(v)
    assert [(k, _typed(v)) for k, v in FL.items()] == [(k, _typed(v)) for k, v in ui.items()]
    assert list(FM) == list(fm) and len(FM) == len(fm)
    assert all(FM[k] == v for k, v in fm.items())
    assert 'nope' not in FL and FL.get('nope', 7) == 7 and 'private/var/mobile/F1' not in FL
    assert 'private/var/mobile/F1/file 1 é☃.db' not in FM     # a file is not a folder
    t = FL.table
    # stored whole: 1 in 50 has other keys, and the Android-style ones another key order
    want = sum(1 for v in ui.values() if tuple(v) != IOS_ORDER)
    assert want > 60 and sum(1 for i in range(t.n_files) if t.flags[i] & ft.WHOLE) == want
    assert t.value(1, 'size') == ui[t.name(1)]['size']


def test_behaves_like_a_dict(table):
    ui, fm, path = table
    rnd = random.Random(5)
    for ref, m, mk in ((dict(ui), ft.FileList(ft.FileTable(path)), lambda i: {'size': i}),
                       (dict(fm), ft.FolderMap(ft.FileTable(path)), lambda i: [f'v/{i}'])):
        keys = list(ref)
        for step in range(3000):
            op = rnd.random()
            k = rnd.choice(keys) if rnd.random() < 0.7 else f'new/{rnd.randrange(400)}'
            if op < 0.3:
                ref[k] = m[k] = mk(step)
            elif op < 0.45:
                assert (k in ref) == (k in m)
                if k in ref:
                    del ref[k]; del m[k]
                else:
                    with pytest.raises(KeyError):
                        del m[k]
            elif op < 0.6:
                d = mk(-step)
                a, b = ref.setdefault(k, d), m.setdefault(k, d)
                assert a == b
            else:
                assert ref.get(k) == m.get(k) and (k in ref) == (k in m)
            if step % 500 == 0:
                assert len(ref) == len(m) and set(ref) == set(m)
        assert len(ref) == len(m) and set(ref) == set(m) and dict(m.items()) == ref


def test_setdefault_change_is_kept(table):
    # ffs-explorer.py's embedded-media container: setdefault(...)['size'] = total
    ui, fm, path = table
    FL = ft.FileList(ft.FileTable(path))
    k = next(iter(ui))
    FL.setdefault(k, {})['size'] = 123
    assert FL[k]['size'] == 123 and FL[k]['mtime'] == ui[k]['mtime']
    assert list(FL)[0] == k            # still in its place
    # a stored entry read with get() is a copy: changing it in place does nothing
    FL.get(list(ui)[1])['size'] = -1
    assert FL[list(ui)[1]]['size'] == ui[list(ui)[1]]['size']


def test_copy_is_independent_and_pickle_maps_same_file(table):
    ui, fm, path = table
    FL = ft.FileList(ft.open_table(path))
    FL['added/x'] = {'size': 1}
    c = FL.copy()
    c['added/y'] = {'size': 2}
    assert 'added/y' not in FL and 'added/x' in c and len(c) == len(FL) + 1
    r = pickle.loads(pickle.dumps(c))
    assert r.table.path == path and dict(r.items()) == dict(c.items())


def test_refuses_what_would_not_read_back(tmp_path):
    for bad in ({'a': {'size': (1, 2)}},          # a tuple comes back a list
                {'a': {'size': 1 << 70}},         # too big for msgpack
                {'a': {'size': 1.5, 'x': {1: 2}}}):  # an int key
        with pytest.raises(ValueError):
            ft.write(bad, {'': ['a']}, str(tmp_path / 'x.ffsfiles'))
    with pytest.raises(ValueError):
        ft.write({'a': {}}, {'': ('a',)}, str(tmp_path / 'x.ffsfiles'))


def test_bool_is_not_stored_as_an_int(tmp_path):
    ui = {'a': {k: 0 for k in IOS_ORDER[:-1]} | {'xattr': {}}}
    ui['a']['size'] = True
    p = str(tmp_path / 'b.ffsfiles')
    ft.write(ui, {'': ['a']}, p)
    assert ft.FileList(ft.FileTable(p))['a']['size'] is True


def _snapshot(case_dir, ui, fm):
    return ffs_metadata.pack_snapshot(['cellebrite', '', '', 1], None, ['z'], ['z'],
                                      ui, fm, {'m'}, file_table_dir=case_dir)


def test_snapshot_round_trip_and_stale_table(tmp_path):
    ui, fm = _sample(500)
    case = str(tmp_path)
    blob = _snapshot(case, ui, fm)
    tables = [n for n in os.listdir(case) if n.endswith(ft.SUFFIX)]
    assert len(tables) == 1
    snap = ffs_metadata.unpack_snapshot(blob, case_dir=case)
    assert isinstance(snap['ui_metadata'], ft.FileList)
    assert dict(snap['ui_metadata'].items()) == ui and dict(snap['folder_map'].items()) == fm
    assert snap['metadata_only'] == ['m'] and snap['zip_names'] == ['z']
    # a second write replaces the first table; the first snapshot is then refused.
    # Let go of the first mapping first: Windows can't delete a mapped file
    # (remove_others leaves it for a later write).
    del snap
    ft._OPEN.clear()
    gc.collect()
    ui2 = dict(list(ui.items())[:-1])
    blob2 = _snapshot(case, ui2, fm)
    assert len([n for n in os.listdir(case) if n.endswith(ft.SUFFIX)]) == 1
    with pytest.raises(OSError):
        ffs_metadata.unpack_snapshot(blob, case_dir=case)
    assert len(ffs_metadata.unpack_snapshot(blob2, case_dir=case)['ui_metadata']) == len(ui2)


def test_snapshot_without_case_dir_keeps_dicts():
    ui, fm = _sample(200)
    blob = ffs_metadata.pack_snapshot(['c', '', '', 1], None, [], None, ui, fm, set())
    snap = ffs_metadata.unpack_snapshot(blob)
    assert type(snap['ui_metadata']) is dict and snap['ui_metadata'] == ui


def test_field_helpers_match_the_dict_way(table):
    ui, fm, path = table
    FL = ft.FileList(ft.FileTable(path))
    ref = dict(ui)
    keys = list(ui)
    for k in keys[5:9]:
        ref[k] = FL[k] = {'size': 7, 'capture': '2 of 2'}       # replaced
    for k in keys[20:23]:
        del ref[k]; del FL[k]                                   # deleted
    ref['new/a'] = FL['new/a'] = {'mtime': 9, 'devid': 3}      # added
    ref['new/b'] = FL['new/b'] = None
    for fields in (('size',), ('inode', 'devid', 'sha256'), ('mtime', 'xattr', 'capture'), ('nope',)):
        want = [(p, tuple(m.get(f) for f in fields) if isinstance(m, dict) else (None,) * len(fields))
                for p, m in ref.items()]
        assert sorted(ft.fields_of(FL, fields), key=repr) == sorted(want, key=repr), fields
        assert [p for p, _ in ft.fields_of(FL, fields)] == list(FL)
    for f in ('size', 'xattr', 'capture', 'devid', 'nope', 'btime', 'links'):
        assert ft.any_set(FL, f) == any(v.get(f) for v in ref.values() if v), f
        assert ft.any_set(ref, f) == ft.any_set(FL, f)


def test_in_zip_bits_answer_like_the_set(tmp_path):
    ui, fm = _sample(400)
    in_zip = frozenset(list(ui)[::3])
    p = str(tmp_path / 'z.ffsfiles')
    ft.write(ui, fm, p, in_zip=in_zip)
    zp = ft.ZipPaths(ft.FileTable(p))
    assert len(zp) == len(in_zip) and set(zp) == in_zip
    assert all((k in zp) == (k in in_zip) for k in list(ui) + ['nope', 'private/var/mobile/F1'])
    with pytest.raises(ValueError):                      # a path that is not in the file list
        ft.write(ui, fm, str(tmp_path / 'y.ffsfiles'), in_zip={'not/a/file'})


def _small_zip_case(tmp_path):
    import zipfile, zip_cd_cache
    zpath = tmp_path / 'EXTRACTION_FFS.zip'
    with zipfile.ZipFile(zpath, 'w') as zf:
        for n in ('d/', 'd/a.db', 'd/b.txt', 'e/c.plist'):
            zf.writestr(n, b'' if n.endswith('/') else b'x')
    case = tmp_path / 'case'
    case.mkdir()
    assert zip_cd_cache.save(str(zpath), str(case))
    assert zip_cd_cache.open_index(str(zpath), str(case)) is not None
    return str(zpath), str(case)


def test_snapshot_names_the_zip_index_and_keeps_in_zip_bits(tmp_path):
    zpath, case = _small_zip_case(tmp_path)
    names = ['d/', 'd/a.db', 'd/b.txt', 'e/c.plist']
    ui = {'d/a.db': {'size': 1}, 'd/b.txt': {'size': 1}, 'e/c.plist': {'size': 1}, 'meta/only': {'size': 0}}
    fm = {'': ['d', 'e', 'meta'], 'd': ['d/a.db', 'd/b.txt'], 'e': ['e/c.plist'], 'meta': ['meta/only']}
    zui = frozenset(['d/a.db', 'd/b.txt', 'e/c.plist'])
    blob = ffs_metadata.pack_snapshot(['cellebrite', '', '', 1], None, names, zui, ui, fm, set(),
                                      file_table_dir=case, zip_path=zpath)
    head = msgpack.Unpacker(raw=False, strict_map_key=False)
    head.feed(blob)
    h = next(head)
    assert h['zip_names'] is None and h['zip_names_index']['n'] == 4 and h['zip_ui_in_table']
    snap = ffs_metadata.unpack_snapshot(blob, case_dir=case)
    assert set(snap['zip_names']) == set(names) and len(snap['zip_names']) == 4
    assert isinstance(snap['zip_ui_paths'], ft.ZipPaths) and set(snap['zip_ui_paths']) == zui
    assert 'meta/only' not in snap['zip_ui_paths']
    # names that are not exactly the index's: stored in the snapshot as before
    ft._OPEN.clear()
    blob2 = ffs_metadata.pack_snapshot(['cellebrite', '', '', 1], None, names[:3], zui, ui, fm, set(),
                                       file_table_dir=case, zip_path=zpath)
    h2 = next(iter(msgpack.Unpacker(__import__('io').BytesIO(blob2), raw=False, strict_map_key=False)))
    assert h2['zip_names'] == names[:3] and h2['zip_names_index'] is None
    # an index that is not the one the snapshot named: refused (the case is parsed again)
    ft._OPEN.clear()
    blob3 = ffs_metadata.pack_snapshot(['cellebrite', '', '', 1], None, names, zui, ui, fm, set(),
                                       file_table_dir=case, zip_path=zpath)
    h3 = next(iter(msgpack.Unpacker(__import__('io').BytesIO(blob3), raw=False, strict_map_key=False)))
    h3['zip_names_index']['cd_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        ffs_metadata._names_from_index(h3['zip_names_index'], case)
