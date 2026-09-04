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
        # QtWebEngineWidgets (artifact_media.py's MediaFullViewDialog,
        # added 2026-09-01 to render Chrome Offline Pages .mhtml archives)
        # is only ever imported lazily inside a method, same pattern
        # QtMultimedia/QtMultimediaWidgets already use for video playback
        # a few lines down in that same file — those two work in this
        # frozen build today WITHOUT being listed here at all (PyInstaller's
        # own PySide6 hook auto-detects a function-body import the same as
        # a module-level one), so this entry is likely redundant in
        # practice, not required. Listed anyway out of caution: unlike
        # QtMultimedia, QtWebEngine bundles a genuinely separate helper
        # process (QtWebEngineProcess.exe) plus its own resource/locale
        # .pak files, a real, previously-reported class of PyInstaller
        # packaging gap for this specific Qt module — NOT verified against
        # an actual frozen Windows build as of this comment (dev-mode/venv
        # only). Check the next Windows CI run actually renders an .mhtml
        # archive, not just that the exe launches, before trusting this.
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        # Artifact parser scripts (artifacts/) are loaded via importlib at
        # runtime, so PyInstaller's import analysis never sees their imports.
        # Any stdlib module used ONLY inside an artifact must be listed here or
        # the script fails to load and the parser silently never appears.
        # photos_metadata.py needs these:
        'unicodedata',
        'uuid',
        'struct',
        'plistlib',
        # app/chrome_cache.py and app/chrome_shared.py are themselves only
        # ever reached via a dynamic `import chrome_cache`/`import
        # chrome_shared` INSIDE a dynamically-loaded artifact script
        # (chrome_cache_media.py/chrome_cache_pages.py; chrome_login_data.py,
        # chrome_cookies.py, chrome_favicons.py, chrome_autofill.py, etc. for
        # chrome_shared) — never a static top-level import anywhere
        # PyInstaller's own analysis walks, so — same reason as every other
        # entry in this list — they're invisible to it and never get bundled
        # without being listed here explicitly. Confirmed the real failure
        # this causes when omitted: a frozen build's Chrome Cache/Favicons/
        # Login Data/etc. parsers fail at runtime with "No module named
        # 'chrome_cache'"/'chrome_shared', not just a theoretical risk.
        'chrome_cache',
        'chrome_shared',
        # chrome_cache.py itself lazy-imports these two only inside
        # _decompress_body, for real 'br'/'zstd' Content-Encoding values
        # (confirmed common on real casework — see requirements.txt).
        'brotli',
        'zstandard',
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
