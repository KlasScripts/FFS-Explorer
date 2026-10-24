"""The shipped embedded-media skip list reaches every install: copied into
the editable list next to the exe, new entries added on upgrade, and an
entry the examiner removed stays removed."""
import json
import os
import sys

import embedded_media_skip_list as sl

MOM = 'com.toyopagroup.picaboo/Documents/gallery_data_object/*/scdb-27.sqlite3.mom'


def _install(tmp_path, monkeypatch, shipped):
    exe_dir = tmp_path / 'ffs-explorer'
    seed_dir = exe_dir / '_internal' / 'config'
    seed_dir.mkdir(parents=True, exist_ok=True)
    (seed_dir / 'embedded_media_skip_list_ios.json').write_text(
        json.dumps({'version': 1, 'platform': 'ios', 'entries': shipped}))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(exe_dir / '_internal'), raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe_dir / 'ffs-explorer.exe'))
    sl._seed_merged['ios'] = False          # a new run of the app
    sl._cache['ios'] = {'stat': None, 'data': None}
    return exe_dir / 'embedded_media_skip_list_ios.json'


def test_new_install_gets_the_shipped_list(tmp_path, monkeypatch):
    user_file = _install(tmp_path, monkeypatch, [MOM])
    assert sl.load('ios') == [MOM]
    assert user_file.exists()


def test_removed_entry_stays_removed_and_upgrade_adds_new(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, [MOM])
    sl.load('ios')
    sl.add_entry('my_own.db', 'ios')
    sl.remove_entry(MOM, 'ios')
    _install(tmp_path, monkeypatch, [MOM])                 # restart, same release
    assert sl.load('ios') == ['my_own.db']
    _install(tmp_path, monkeypatch, [MOM, 'new_in_release.db'])   # upgrade
    assert sl.load('ios') == ['my_own.db', 'new_in_release.db']


def test_not_frozen_uses_config_and_no_seed():
    assert sl.bundled_seed_path('ios') is None
    assert sl.store_path('ios').endswith(os.path.join('config', 'embedded_media_skip_list_ios.json'))


def test_user_media_ignore_list_seed(tmp_path, monkeypatch):
    import user_media_ignore_list as ul
    exe_dir = tmp_path / 'ffs-explorer'
    seed_dir = exe_dir / '_internal' / 'config'
    seed_dir.mkdir(parents=True)
    (seed_dir / 'user_media_ignore_list.json').write_text(
        json.dumps({'version': 1, 'entries': ['*@2x.png*', '*.9.png']}))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(exe_dir / '_internal'), raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe_dir / 'ffs-explorer.exe'))

    def restart():
        monkeypatch.setattr(ul, '_seed_merged', False)
        ul._cache.update(stat=None, data=None)

    restart()
    assert ul.load() == ['*.9.png', '*@2x.png*']        # new install
    ul.remove_entry('*.9.png')
    ul.add_entries(['private/var/mine'])
    restart()
    assert ul.load() == ['*@2x.png*', 'private/var/mine']   # removal sticks
