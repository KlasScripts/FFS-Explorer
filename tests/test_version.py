"""app/version.py: the release shown in the title and the startup log line."""
import sys

import version


def _fresh(monkeypatch):
    monkeypatch.setattr(version, '_cached', None)


def test_frozen_build_reads_bundled_version_txt(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    (tmp_path / 'version.txt').write_text('v0.50.22\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    assert version.app_version() == 'v0.50.22'
    assert version.window_title() == 'FFS Explorer v0.50.22'


def test_frozen_build_without_version_txt_shows_plain_title(tmp_path, monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    assert version.app_version() == ''
    assert version.window_title() == 'FFS Explorer'


def test_source_run_asks_git(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delattr(sys, 'frozen', raising=False)
    calls = []

    class Done:
        returncode, stdout = 0, 'v0.50.21-2-gabc1234\n'

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(version.subprocess, 'run', fake_run)
    assert version.app_version() == 'v0.50.21-2-gabc1234'
    assert calls == [['git', 'describe', '--tags', '--dirty']]
    version.app_version()
    assert len(calls) == 1          # asked once per session


def test_source_run_without_git_shows_plain_title(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delattr(sys, 'frozen', raising=False)

    def no_git(cmd, **kw):
        raise FileNotFoundError('git')
    monkeypatch.setattr(version.subprocess, 'run', no_git)
    assert version.window_title() == 'FFS Explorer'
