#!/bin/sh
# Run once after cloning: installs the tracked hooks in scripts/ into
# .git/hooks (git doesn't version .git/hooks itself, so this can't be
# automatic on clone).
set -e
cd "$(git rev-parse --show-toplevel)"
cp scripts/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
echo "Installed pre-commit hook (CLAUDE.md anchor check)."
