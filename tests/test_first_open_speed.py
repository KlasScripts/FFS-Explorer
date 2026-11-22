"""First-open speed-ups give the same results as before (2026-11-22):
the archive-offset sample read from the case's index, the User Media list
from a walk of its folders (and saved until a Process Case run), and the
header-scan counts made in the parse process."""
import os
import sqlite3
import struct
import sys
import time
import zipfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, 'app'), ROOT]


def _zip_with_times(path, names):
    """A stored zip whose entries carry a UT mtime, folders included."""
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as z:
        for i, name in enumerate(names):
            ut = 1704067200 + i * 997
            info = zipfile.ZipInfo(name, date_time=time.gmtime(ut - 5 * 3600)[:6])
            info.extra = struct.pack('<HHBI', 0x5455, 5, 1, ut)
            z.writestr(info, b'' if name.endswith('/') else b'x')


def test_archive_offset_sample_from_the_index_is_the_same(tmp_path):
    import archive_offset
    import zip_cd_cache
    names = []
    for d in range(40):
        names.append(f'd{d}/')
        names += [f'd{d}/f{k}.txt' for k in range(60)]
    zp = str(tmp_path / 'a.zip')
    _zip_with_times(zp, names)
    case = str(tmp_path / 'case')
    os.makedirs(case)
    assert zip_cd_cache.save(zp, case)
    infos = zip_cd_cache.load(zp, case)
    assert hasattr(infos, '_index')
    plain = [SimpleNamespace(filename=i.filename, date_time=i.date_time, extra=i.extra)
             for i in infos]
    for limit in (5, 50, 4000):
        assert archive_offset.sample_pairs(infos, limit) == archive_offset.sample_pairs(plain, limit)


class _Files(dict):
    """A file list with the file table's runtime-additions dict (top)."""
    def __init__(self, base, top, table_path):
        super().__init__(base)
        self.update(top)
        self.top = dict(top)
        self.table = SimpleNamespace(path=table_path)


def test_user_media_walk_matches_the_full_scan_and_is_saved(tmp_path):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import importlib.util
    spec = importlib.util.spec_from_file_location('ffs_main_speed', os.path.join(ROOT, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    from db_utils import _open_cache_db, _open_results_db, start_run_log
    case = str(tmp_path / 'case')
    os.makedirs(case)
    _open_cache_db(case).close()
    _open_results_db(case).close()
    one = {'size': 10}
    files = {
        'media/DCIM/100APPLE/IMG_1.JPG': one, 'media/DCIM/100APPLE/IMG_2.HEIC': one,
        'media/DCIM/100APPLE/notes.txt': one, 'media/DCIM/x.mov': one,
        'media/DCIM/empty.jpg': {'size': 0},          # a 0-byte file is not media
        'other/pic.jpg': one, 'media/DCIMX/pic.jpg': one,
    }
    added = {'media/DCIM/carved/blob.png': one}
    fm = {'': ['media', 'other'], 'media': ['media/DCIM', 'media/DCIMX'],
          'media/DCIM': ['media/DCIM/100APPLE', 'media/DCIM/x.mov', 'media/DCIM/carved',
                         'media/DCIM/empty.jpg'],
          'media/DCIM/100APPLE': ['media/DCIM/100APPLE/IMG_1.JPG', 'media/DCIM/100APPLE/IMG_2.HEIC',
                                  'media/DCIM/100APPLE/notes.txt'],
          'media/DCIM/carved': ['media/DCIM/carved/blob.png'],
          'media/DCIMX': ['media/DCIMX/pic.jpg'], 'other': ['other/pic.jpg']}
    win = m.FastZipBrowser.__new__(m.FastZipBrowser)
    win.full_metadata = _Files(files, added, os.path.join(case, 'files-x.ffsfiles'))
    win.folder_map = fm
    win._case_dir = case
    win._header_type_overrides = {}
    win._user_media_include_prefixes = lambda: ['media/DCIM']
    want = sorted(p for p in win.full_metadata
                  if p.startswith('media/DCIM/') and p not in fm and win._is_media_file(p))
    assert want == ['media/DCIM/100APPLE/IMG_1.JPG', 'media/DCIM/100APPLE/IMG_2.HEIC',
                    'media/DCIM/carved/blob.png', 'media/DCIM/x.mov']
    got = m.FastZipBrowser._user_media_candidate_paths(win)
    assert got == want
    # saved: a fresh window reads it back without walking
    win2 = m.FastZipBrowser.__new__(m.FastZipBrowser)
    win2.__dict__.update({k: v for k, v in win.__dict__.items() if k != '_um_candidates_memo'})
    win2._user_media_files_under = lambda *a: (_ for _ in ()).throw(AssertionError('walked'))
    assert m.FastZipBrowser._user_media_candidate_paths(win2) == want
    # a Process Case run (a new run_log row) makes it walk again
    with sqlite3.connect(os.path.join(case, 'caseresults.db')) as db:
        start_run_log(db, 'header_scan', total=1)
    win3 = m.FastZipBrowser.__new__(m.FastZipBrowser)
    win3.__dict__.update({k: v for k, v in win.__dict__.items() if k != '_um_candidates_memo'})
    walked = []
    real = m.FastZipBrowser._user_media_files_under
    win3._user_media_files_under = lambda *a: walked.append(1) or real(win3, *a)
    assert m.FastZipBrowser._user_media_candidate_paths(win3) == want and walked


def test_header_counts_saved_at_parse_are_what_the_window_reads(tmp_path):
    import file_table
    import header_counts
    import zip_cd_cache
    from adapters import FfsAdapter
    from ffs_metadata import _save_header_tier_counts
    from db_utils import _open_cache_db
    names = ['filesystem1/private/var/mobile/Containers/Data/Application/',
             'filesystem1/private/var/mobile/Media/DCIM/a.jpg',
             'filesystem1/private/var/mobile/Media/DCIM/b']
    zp = str(tmp_path / 'b.zip')
    _zip_with_times(zp, names)
    case = str(tmp_path / 'case')
    os.makedirs(case)
    _open_cache_db(case).close()
    assert zip_cd_cache.save(zp, case)
    infos = zip_cd_cache.load(zp, case)
    view = zip_cd_cache.CachedZipView(zp, infos)
    adapter = FfsAdapter.detect(view, frozenset(view.namelist()))
    ui = {adapter.ui_path_from_physical(n) if hasattr(adapter, 'ui_path_from_physical') else n: {}
          for n in names if not n.endswith('/')}
    ui = {k: {} for k in ui if k}
    fm = {'': sorted(ui)}
    path = file_table.new_path(case)
    file_table.write(ui, fm, path)
    _save_header_tier_counts(zp, case, adapter)
    ui_list, _ = file_table.open_maps(path)
    try:
        want = header_counts._header_tier_counts([(p, None) for p in ui], adapter, view)
        calls = []
        real = header_counts._header_tier_counts
        header_counts._header_tier_counts = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            got = header_counts._header_tier_counts_cached(ui_list, adapter, view, False, case)
        finally:
            header_counts._header_tier_counts = real
        assert got == want and not calls          # read back, not counted again
    finally:
        ui_list.table.close()
