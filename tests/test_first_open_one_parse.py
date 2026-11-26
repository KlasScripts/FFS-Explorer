"""The first open has one parse (ffs-notes case-creation review, 2026-11-26):
parse_archive_metadata in a child process, the same function in the load
thread if the child cannot run, and nothing else. If that function throws,
the open ends with the error and no tree."""
import importlib.util
import os
import sqlite3
import struct
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, 'app'), ROOT]

_NAMES = ['Dump/', 'Dump/data/', 'Dump/data/media/', 'Dump/data/media/0/',
          'Dump/data/media/0/DCIM/'] + [f'Dump/data/media/0/DCIM/f{i}.txt' for i in range(30)]


def _main():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    spec = importlib.util.spec_from_file_location('ffs_main_oneparse',
                                                  os.path.join(ROOT, 'ffs-explorer.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _case(tmp_path, name):
    zp = str(tmp_path / f'{name}.zip')
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_STORED) as z:
        for i, n in enumerate(_NAMES):
            # UT times: the Android (Dump/, zip extras) layout
            ut = 1704067200 + i * 997
            info = zipfile.ZipInfo(n, date_time=time.gmtime(ut)[:6])
            info.extra = struct.pack('<HHBI', 0x5455, 5, 1, ut)
            z.writestr(info, b'' if n.endswith('/') else b'content ' + n.encode())
    case = str(tmp_path / f'{name}_case')
    os.makedirs(case)
    return zp, case


def _open(m, zp, case):
    w = m.ZipMetadataWorker(zp, case_dir=case)
    got, status = [], []
    w.metadata_ready.connect(lambda *a: got.append(a))
    w.status_update.connect(status.append)
    w.run()                       # on this thread: signals arrive at once
    deadline = time.time() + 30   # the system check runs on its own thread
    while getattr(w, '_system_check_done', False) is False and time.time() < deadline:
        time.sleep(0.05)
    return got, status


def _saved(case):
    db = sqlite3.connect(os.path.join(case, 'casecache.db'))
    try:
        keys = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        blobs = {r[0] for r in db.execute('SELECT key FROM blobs')} if 'blobs' in keys else set()
        return keys, blobs
    finally:
        db.close()


def test_child_and_in_thread_parse_save_the_same_case(tmp_path, monkeypatch):
    m = _main()
    zp, case = _case(tmp_path, 'child')
    got, _ = _open(m, zp, case)
    assert len(got) == 1
    child_tree = sorted(got[0][1])                       # folder_map keys
    child_saved = _saved(case)

    zp2, case2 = _case(tmp_path, 'thread')
    monkeypatch.setattr(m.ZipMetadataWorker, '_parse_in_subprocess', lambda self: False)
    got2, _ = _open(m, zp2, case2)
    assert len(got2) == 1
    assert sorted(got2[0][1]) == child_tree
    assert _saved(case2) == child_saved

    # a reopen reads the saved case, with no parse at all
    monkeypatch.setattr(m, 'parse_archive_metadata',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('parsed again')))
    got3, _ = _open(m, zp2, case2)
    assert len(got3) == 1 and sorted(got3[0][1]) == child_tree


def test_a_failed_parse_ends_the_open_without_a_tree(tmp_path, monkeypatch):
    m = _main()
    zp, case = _case(tmp_path, 'fail')
    monkeypatch.setattr(m.ZipMetadataWorker, '_parse_in_subprocess', lambda self: False)

    def boom(*a, **k):
        raise RuntimeError('bad archive')
    monkeypatch.setattr(m, 'parse_archive_metadata', boom)
    got, status = _open(m, zp, case)
    assert got == []
    assert any('Metadata parse error: bad archive' in s for s in status)


def test_the_old_parse_steps_are_gone():
    src = open(os.path.join(ROOT, 'ffs-explorer.py'), encoding='utf-8').read()
    start = src.index('class ZipMetadataWorker(QThread):')
    body = src[start:src.index('\nclass ', start + 10)]
    run = body[body.index('    def run(self):'):]
    for gone in ('build_ui_metadata', 'pack_snapshot', '_scan_headers_once_per_copy',
                 'repair_guid_map'):
        assert gone not in run, gone
