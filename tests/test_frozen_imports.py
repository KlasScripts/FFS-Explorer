"""ffs_explorer.spec bundles every app/ module and everything artifacts/
scripts import (scripts/frozen_imports.py): a report-only import was left
out of the Windows build ("No module named 'files_by_google_glide'",
2026-11-17). Also: each name a report imports exists here, so a typo or an
uninstalled package is caught before a build."""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import frozen_imports  # noqa: E402


def test_report_only_helpers_are_bundled():
    names = set(frozen_imports.hidden_imports(ROOT))
    assert {'files_by_google_glide', 'chrome_cache', 'chrome_shared', 'artifact_runner'} <= names
    assert set(frozen_imports.app_modules(ROOT)) <= names


def test_spec_uses_the_list():
    with open(os.path.join(ROOT, 'ffs_explorer.spec'), encoding='utf-8') as f:
        spec = f.read()
    assert 'from frozen_imports import hidden_imports' in spec and '+ _AUTO_HIDDEN' in spec


def test_every_report_import_exists():
    missing = [n for n in frozen_imports.artifact_imports(ROOT)
               if importlib.util.find_spec(n.split('.')[0]) is None]
    assert not missing, f"artifacts import modules that aren't installed/in app/: {missing}"
