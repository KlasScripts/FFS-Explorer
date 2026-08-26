# ios_ffs_browser.spec
# -*- mode: python ; coding: utf-8 -*-

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

a = Analysis(
    ['ffs-explorer.py'],
    pathex=['app'],
    binaries=[],
    datas=[
        # Bundle config JSON files under config/ next to the exe
        ('config/hardware_models.json', 'config'),
        ('config/photo_flags.json', 'config'),
        # Bundle the icons so resource_path() can find them at runtime
        ('resources', 'resources'),
        # Artifact parser scripts — loaded dynamically so PyInstaller can't
        # detect them via import analysis; must be listed explicitly.
        ('artifacts', 'artifacts'),
    ] + collect_data_files('blackboxprotobuf')
      # zoneinfo has no system tz database to fall back to on Windows;
      # tzdata ships it as package data, never imported by name so PyInstaller's
      # static analysis can't discover it on its own (see device_timezone.py).
      + collect_data_files('tzdata'),
    hiddenimports=[
        # msgpack sometimes needs explicit nudging
        'msgpack',
        'msgpack.fallback',
        # PySide6 platform plugin — needed on Windows
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        # Artifact parser scripts (artifacts/) are loaded via importlib at
        # runtime, so PyInstaller's import analysis never sees their imports.
        # Any stdlib module used ONLY inside an artifact must be listed here or
        # the script fails to load and the parser silently never appears.
        # photos_metadata.py needs these:
        'unicodedata',
        'uuid',
        'struct',
        'plistlib',
        # nska_deserialize (NSKeyedArchiver plist decoding, artifact_runner.py's
        # decode_plist_blob helper) and its own biplist dependency — imported
        # at artifact_runner.py's module level, not inside a dynamically-loaded
        # artifact script, so static analysis should already catch these; listed
        # explicitly anyway to match this project's existing defensive pattern
        # for every other pip dependency here.
        'nska_deserialize',
        'biplist',
    ] + collect_submodules('blackboxprotobuf')
      + collect_submodules('ccl_segb')
      # sms_messages.py imports this lazily (attributedBody typedstream
      # fallback decode) — same "artifact scripts are invisible to static
      # analysis" reason as the stdlib modules above.
      + collect_submodules('typedstream')
      # MCP server (Tools → Enable AI Access) is lazy-imported; uvicorn loads
      # its event-loop/protocol modules dynamically, so both need collecting.
      # mcp.cli is excluded: it's the standalone `mcp` CLI tool (never used —
      # this app only imports mcp.server.fastmcp.FastMCP directly) and its
      # import requires the optional `typer` extra (`pip install mcp[cli]`),
      # which we don't install; collecting it crashes the PyInstaller build.
      + collect_submodules('mcp', filter=lambda name: not name.startswith('mcp.cli'))
      + collect_submodules('uvicorn')
      + ['google.protobuf', 'six'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim things you definitely don't need
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
        'tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_UPX_EXCLUDE = [
    # UPX-packing Qt, Python, and MSVC runtime DLLs is the classic source of
    # antivirus false positives and occasional broken Qt plugin loads, and
    # Defender re-scans the unpacked images on every launch.
    'Qt6*.dll',
    'PySide6*',
    'python*.dll',
    'vcruntime*.dll',
    'msvcp*.dll',
    'ucrtbase.dll',
]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir mode — keeps Qt DLLs alongside exe
    name='ffs-explorer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                       # compress binaries — set False if UPX causes AV flags
    upx_exclude=_UPX_EXCLUDE,
    console=False,                  # no console window (GUI app)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='resources/icon.ico' if sys.platform == 'win32' else 'resources/icon.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=_UPX_EXCLUDE,
    name='ffs-explorer',         # output folder name inside dist/
)
