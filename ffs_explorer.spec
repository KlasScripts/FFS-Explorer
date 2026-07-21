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
    ] + collect_data_files('blackboxprotobuf'),
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
    ] + collect_submodules('blackboxprotobuf')
      + collect_submodules('ccl_segb')
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
