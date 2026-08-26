#!/usr/bin/env python3
"""leapp_coverage_report.py — live comparison of which apps this project has
a real artifact parser for (artifacts/ios|android/*.py) against which apps
iLEAPP/ALEAPP support, by scanning both LOCAL CHECKOUTS directly rather than
a hardcoded list.

Why generated, not hardcoded: iLEAPP (github.com/abrignoni/iLEAPP) and
ALEAPP (github.com/abrignoni/ALEAPP) are live, actively-maintained GitHub
projects — people add and change parsers constantly. A static "apps they
support" list committed to this repo would start going stale the moment
either project's next PR merges. Re-run this script after re-pulling the
checkouts (see leapp_evidence_fixtures.ILEAPP_PATH/ALEAPP_PATH for the
paths) to get a current picture rather than trusting an old one.

This is the "in the fullness of time, get all the artifacts they support
into this app" roadmap tool — run it to see the gap, not to read a snapshot
someone remembered to update.

Usage:
    venv/bin/python3 scripts/leapp_coverage_report.py [--ileapp PATH] [--aleapp PATH]
"""

import argparse
import re
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent / 'app'
_ARTIFACTS_DIR = Path(__file__).resolve().parent.parent / 'artifacts'
sys.path.insert(0, str(_APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import leapp_evidence_fixtures as fixtures  # noqa: E402 — for the default checkout paths


def _this_project_coverage() -> dict[str, list[dict]]:
    """{'ios'|'android': [{'file', 'name', 'app_path_or_group'}, ...]} from
    this project's own artifacts/ios|android/*.py module-level attributes."""
    out: dict[str, list[dict]] = {'ios': [], 'android': []}
    for platform in ('ios', 'android'):
        d = _ARTIFACTS_DIR / platform
        if not d.is_dir():
            continue
        for f in sorted(d.glob('*.py')):
            if f.name.startswith('_'):
                continue
            text = f.read_text(errors='replace')
            name_m = re.search(r'^name\s*=\s*["\']([^"\']+)["\']', text, re.M)
            path_m = re.search(r'^app_path\s*=\s*["\']([^"\']+)["\']', text, re.M)
            group_m = re.search(r'^app_group\s*=\s*["\']([^"\']+)["\']', text, re.M)
            out[platform].append({
                'file': f.name,
                'name': name_m.group(1) if name_m else f.stem,
                'app_path_or_group': (path_m or group_m).group(1) if (path_m or group_m) else None,
            })
    return out


def _leapp_categories(checkout_path: str) -> dict[str, set]:
    """{category_name: {script_filenames}} scanned live from a local
    iLEAPP/ALEAPP checkout's scripts/artifacts/*.py — its own declared
    'category' field per module, the closest thing either project
    publishes to an app name. NOT filtered to third-party comms apps only
    — some categories are OS/system-level (Accounts, OS Updates, etc.),
    left in deliberately rather than guessing which are "real apps."""
    root = Path(checkout_path) / 'scripts' / 'artifacts'
    out: dict[str, set] = {}
    if not root.is_dir():
        return out
    for f in sorted(root.glob('*.py')):
        text = f.read_text(errors='replace')
        for cat in re.findall(r'''["\']category["\']\s*:\s*["\']([^"\']+)["\']''', text):
            out.setdefault(cat, set()).add(f.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ileapp', default=fixtures.ILEAPP_PATH)
    ap.add_argument('--aleapp', default=fixtures.ALEAPP_PATH)
    args = ap.parse_args()

    ours = _this_project_coverage()
    ileapp_cats = _leapp_categories(args.ileapp)
    aleapp_cats = _leapp_categories(args.aleapp)

    print("ios-ffs-browser real parser coverage (artifacts/ios|android/*.py)")
    print("=" * 70)
    for platform in ('ios', 'android'):
        print(f"\n[{platform}] — {len(ours[platform])} parser(s)")
        for p in ours[platform]:
            target = p['app_path_or_group'] or '(no app_path/app_group declared)'
            print(f"  {p['name']:45s} {target}")

    print(f"\n\niLEAPP local checkout: {args.ileapp}")
    print(f"  ({len(ileapp_cats)} declared categories across "
         f"{sum(len(v) for v in ileapp_cats.values())} script references — "
         "live snapshot, re-pull the checkout before trusting this is current)")
    print(f"\nALEAPP local checkout: {args.aleapp}")
    print(f"  ({len(aleapp_cats)} declared categories across "
         f"{sum(len(v) for v in aleapp_cats.values())} script references — "
         "same staleness caveat)")

    our_ios_names = {p['name'].lower() for p in ours['ios']}
    our_android_names = {p['name'].lower() for p in ours['android']}

    def _not_covered(cats: dict, our_names: set) -> list[str]:
        return sorted(c for c in cats if not any(n in c.lower() or c.lower() in n
                                                  for n in our_names))

    print("\n\niLEAPP categories with no obvious match in our iOS coverage "
         "(roadmap candidates, not a precise gap — name matching is fuzzy):")
    for c in _not_covered(ileapp_cats, our_ios_names):
        print(f"  {c}  ({len(ileapp_cats[c])} script(s))")

    print("\nALEAPP categories with no obvious match in our Android coverage:")
    for c in _not_covered(aleapp_cats, our_android_names):
        print(f"  {c}  ({len(aleapp_cats[c])} script(s))")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
