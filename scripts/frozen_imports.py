"""Modules the frozen (PyInstaller) build must bundle although its import
analysis can't see them, for ffs_explorer.spec's hiddenimports.

Report scripts in artifacts/ are loaded with importlib while the app runs,
so PyInstaller never sees what they import: a module only a report imports
was left out, and the report failed in the Windows build with "No module
named ..." (chrome_cache / chrome_shared, then files_by_google_glide for
the three Files by Google reports, 2026-11-17). Hand-kept lists missed each
new one, so this lists them from the source instead:

  - every module and package in app/ (all are the app's own code), and
  - every module any artifacts/ script imports (read with ast, never run).

A name PyInstaller can't find is only a build warning, never a failure.
"""
import ast
import glob
import os


def app_modules(root: str) -> list:
    app = os.path.join(root, 'app')
    names = {os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(app, '*.py'))}
    names |= {d for d in os.listdir(app)
              if os.path.isfile(os.path.join(app, d, '__init__.py'))}
    names.discard('__init__')
    return sorted(names)


def artifact_imports(root: str) -> list:
    names = set()
    for path in glob.glob(os.path.join(root, 'artifacts', '**', '*.py'), recursive=True):
        try:
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
    return sorted(names)


def hidden_imports(root: str) -> list:
    return sorted(set(app_modules(root)) | set(artifact_imports(root)))


if __name__ == '__main__':
    import sys
    print('\n'.join(hidden_imports(sys.argv[1] if len(sys.argv) > 1 else '.')))
