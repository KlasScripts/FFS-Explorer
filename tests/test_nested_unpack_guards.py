"""Process Case unpacks every archive it finds: the skip list and the
zip-bomb / disk-space guards in nested_archive."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import nested_archive  # noqa: E402


def test_skip_list_is_developer_content_only():
    assert nested_archive.skip_reason('data/x/anim/confetti.lottie')
    assert nested_archive.skip_reason('data/x/libs/sdk.AAR')
    for kept in ('data/x/logs/app.log.gz', 'data/x/export.zip',
                 'data/x/files/backup', 'data/x/chat.zip.1'):
        assert nested_archive.skip_reason(kept) is None


def test_size_over_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(nested_archive, 'MAX_UNPACKED', 1000)
    with pytest.raises(RuntimeError, match='zip bomb'):
        nested_archive._check_unpacked_size(1001, 1000, str(tmp_path))
    nested_archive._check_unpacked_size(1000, 1000, str(tmp_path))


def test_ratio_is_refused_only_when_big(tmp_path, monkeypatch):
    monkeypatch.setattr(nested_archive, 'BOMB_MIN', 10_000)
    nested_archive._check_unpacked_size(9_000, 1, str(tmp_path))     # small: fine
    with pytest.raises(RuntimeError, match='1000:1'):
        nested_archive._check_unpacked_size(20_000, 10, str(tmp_path))


def test_disk_space_is_kept(tmp_path, monkeypatch):
    import shutil
    usage = shutil.disk_usage(str(tmp_path))
    monkeypatch.setattr(nested_archive, 'MAX_UNPACKED', usage.free * 2)
    monkeypatch.setattr(nested_archive, 'BOMB_RATIO', 10 ** 12)
    with pytest.raises(RuntimeError, match='free'):
        nested_archive._check_unpacked_size(usage.free, usage.free, str(tmp_path))
