"""version.py — which release of FFS Explorer is running, e.g. 'v0.50.21'.

Shown in the window title and the startup log line. Qt-free.

- Frozen build: version.txt next to the bundled files (sys._MEIPASS), written
  by ffs_explorer.spec from the release tag CI builds (FFS_VERSION).
- Source run: `git describe --tags --dirty` in the repository, e.g.
  'v0.50.21', or 'v0.50.21-2-gabc1234' two commits later ('-dirty' with
  uncommitted changes).
- '' when neither is available; callers then show no version.

Before this, the only clue was the startup log's "frozen build" time, which
is the exe file's modified time: when it was unzipped, not which release.
"""

import os
import subprocess
import sys

_cached: str | None = None


def app_version() -> str:
    global _cached
    if _cached is None:
        _cached = _frozen_version() if getattr(sys, 'frozen', False) else _git_version()
    return _cached


def _frozen_version() -> str:
    base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    try:
        with open(os.path.join(base, 'version.txt'), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def _git_version() -> str:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(
            ['git', 'describe', '--tags', '--dirty'], cwd=repo,
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def window_title(base: str = 'FFS Explorer') -> str:
    v = app_version()
    return f'{base} {v}' if v else base
