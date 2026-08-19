#!/usr/bin/env python3
"""Pre-commit check: verify CLAUDE.md's file:line anchors and module table
against the actual code. Auto-fixes precise single-symbol anchors in place;
blocks the commit (prints a report) for anything that needs human/LLM
judgment: a symbol that moved files, one that vanished, or a module-table
mismatch.

The old line-number-range "section map" check was removed along with the
line numbers it checked (2026-08-19) — that map is symbol-name-only now
(see CLAUDE.md), specifically because a *stored* number can drift silently
between the rare commits this project makes, while a name only goes stale
on a rename (rare, deliberate, and this script's single-anchor check still
catches that).

Deliberately conservative: only touches things it can verify unambiguously,
so it never blocks on noise. Exit 0 = clean (or auto-fixed and safe to
commit). Exit 1 = needs a human/Claude look before committing.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
    ).stdout.strip()
)
CLAUDE_MD = ROOT / "CLAUDE.md"
MAIN_FILE = "ffs-explorer.py"

# Directories under app/ whose contents are vendored / not individually
# documented in the module table.
VENDORED = {"ccl_segb", "__pycache__"}

SINGLE_ANCHOR_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`\s*\((?:`)?([\w./-]+\.py):(\d+)(?:`)?\)")
TABLE_ROW_PY_RE = re.compile(r"`([\w./-]+\.py)`")


def find_symbol(root: Path, symbol: str):
    """Return list of (relpath, lineno) where `class symbol` or `def symbol` is defined."""
    pattern = rf"^\s*(class|def)\s+{re.escape(symbol)}\b"
    hits = []
    for py_file in root.rglob("*.py"):
        rel = py_file.relative_to(root)
        if any(part in VENDORED for part in rel.parts) or "venv" in rel.parts:
            continue
        try:
            text = py_file.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if re.match(pattern, line):
                hits.append((str(rel), i))
    return hits


def check_single_anchors(text: str, blocking: list, fixed: list):
    def repl(m):
        symbol, claimed_file, claimed_line = m.group(1), m.group(2), int(m.group(3))
        hits = find_symbol(ROOT, symbol)
        if not hits:
            blocking.append(f"`{symbol}` anchored at {claimed_file}:{claimed_line} no longer "
                             f"exists anywhere in the tree (removed/renamed?).")
            return m.group(0)
        same_file = [h for h in hits if h[0] == claimed_file or h[0] == Path(claimed_file).name]
        if same_file:
            actual_line = same_file[0][1]
            if actual_line != claimed_line:
                fixed.append(f"`{symbol}`: {claimed_file}:{claimed_line} -> {claimed_line}"
                              f" corrected to {actual_line}")
                return f"`{symbol}` ({claimed_file}:{actual_line})"
            return m.group(0)
        # Symbol exists, but not in the file CLAUDE.md claims -> moved files.
        moved_to = ", ".join(f"{f}:{ln}" for f, ln in hits)
        blocking.append(f"`{symbol}` was anchored to {claimed_file} but now only found in: "
                         f"{moved_to}. Anchor + surrounding prose need review (responsibility "
                         f"may have moved, not just the line).")
        return m.group(0)

    return SINGLE_ANCHOR_RE.sub(repl, text)


def check_module_table(text: str, blocking: list):
    in_table = False
    documented = set()
    for line in text.splitlines():
        if line.strip().startswith("## app/ modules"):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table:
            continue
        if not line.strip().startswith("|"):
            continue
        cell = line.split("|", 2)[1] if line.count("|") >= 2 else ""
        for m in TABLE_ROW_PY_RE.finditer(cell):
            documented.add(m.group(1))

    actual = set()
    app_dir = ROOT / "app"
    for py_file in app_dir.rglob("*.py"):
        rel = py_file.relative_to(app_dir)
        if any(part in VENDORED for part in rel.parts) or rel.name == "__init__.py":
            continue
        actual.add(str(rel))

    missing_from_doc = actual - documented
    stale_in_doc = documented - actual
    for f in sorted(missing_from_doc):
        blocking.append(f"app/{f} exists but isn't listed in the '## app/ modules' table.")
    for f in sorted(stale_in_doc):
        blocking.append(f"'## app/ modules' table lists app/{f}, which no longer exists.")


def main():
    if not CLAUDE_MD.exists():
        return 0
    text = CLAUDE_MD.read_text()

    blocking = []
    fixed = []

    new_text = check_single_anchors(text, blocking, fixed)
    check_module_table(new_text, blocking)

    if new_text != text:
        CLAUDE_MD.write_text(new_text)
        print("CLAUDE.md: auto-corrected line-number drift:")
        for f in fixed:
            print(f"  - {f}")

    if blocking:
        print("\nCLAUDE.md needs a manual/Claude review before this commit (structural drift, "
              "not just a line number):")
        for b in blocking:
            print(f"  ! {b}")
        print("\nFix the relevant section(s) in CLAUDE.md, `git add CLAUDE.md`, and re-commit.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
