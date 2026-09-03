#!/usr/bin/env python3
"""validate_evidence_ranking.py — checks whether
app_intelligence.find_evidence_databases' UNAIDED ranking mechanism (no
external citation — that field was deliberately removed from the live tool,
see app_intelligence.py's own docstring) surfaces each known-real file from
leapp_evidence_fixtures.py within its top-N candidates, for every fixture
app actually present in a given case.

This is the review process leapp_evidence_fixtures.py exists to feed:
independent ground truth (iLEAPP/ALEAPP's own path patterns, mined by hand)
checking whether the MECHANISM works — size/WAL comparison, noise
filtering, magic-byte detection for extensionless files — not whether a
hardcoded answer key matches itself. See both files' module docstrings, and
leapp_evidence_fixtures.py's ILEAPP_PATH/ALEAPP_PATH for the local checkout
locations this data was mined from (both live, actively-maintained GitHub
projects — re-pull before trusting a stale-looking result).

Usage:
    venv/bin/python3 scripts/validate_evidence_ranking.py <zip_path> [--top N] [--raw]

    --top N   how many of find_evidence_databases' top candidates count as
              "found" (default 5, matching its own default limit)
    --raw     also enable the magic-byte fallback for extensionless files
              (the Tier-3 half of the mechanism) by reading real file bytes

Reads zip STRUCTURE via zipfile (central directory only — same as the rest
of this project: zip_cd_cache.py, adapters/ffs.py, ffs_metadata.py all do
this too). Reads actual file BYTES via ZipEntry (bypasses zipfile's
decompression path, matching production's own read primitive) — never a
raw zipfile.ZipFile.read() call for data.
"""

import argparse
import sys
import zipfile
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent / 'app'
sys.path.insert(0, str(_APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters.ffs import FfsAdapter          # noqa: E402
from ffs_metadata import _build_folder_tree  # noqa: E402
from zip_entry import ZipEntry               # noqa: E402
import app_intelligence                      # noqa: E402
import leapp_evidence_fixtures as fixtures   # noqa: E402


def _make_read_bytes(zip_path: str, adapter: FfsAdapter):
    with zipfile.ZipFile(zip_path) as z:
        infos = {i.filename: i for i in z.infolist()}

    def read_bytes(ui_path: str) -> bytes | None:
        physical = adapter.resolve(ui_path)
        zinfo = infos.get(physical)
        if zinfo is None:
            return None
        return ZipEntry(zip_path, physical, zinfo).read()

    return read_bytes


def _group_owner_from_registry(app_registry_rows: list) -> dict:
    """Same logic as app_intelligence._load_group_owner_index, but built
    directly from an in-memory app_registry rows list — no casecache.db
    round-trip needed for a one-shot validation run."""
    out = {}
    for row in app_registry_rows:
        for gid in row.get('app_group_paths', {}):
            out[gid] = row['bundle_id']
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('zip_path')
    ap.add_argument('--top', type=int, default=5,
                    help='how many top candidates count as "found" (default: 5)')
    ap.add_argument('--raw', action='store_true',
                    help='enable the magic-byte fallback for extensionless files')
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip_path) as z:
        names = frozenset(z.namelist())
        adapter = FfsAdapter.detect(z, names)
        ui_metadata, guid_to_bundle, _zip_ui_paths = adapter.build_ui_metadata(
            args.zip_path, names, z=z)
    folder_map = _build_folder_tree(ui_metadata)
    app_registry_rows, ls_guid_map = adapter.build_app_registry(args.zip_path, names)
    if ls_guid_map:
        guid_to_bundle = {**guid_to_bundle, **ls_guid_map}
    group_owner = _group_owner_from_registry(app_registry_rows)

    platform = 'android' if adapter.format == adapter.FORMAT_ZIP_EXTRAS else 'ios'
    read_bytes = _make_read_bytes(args.zip_path, adapter) if args.raw else None

    # {identity_app_id: [container_path, ...]} — same Data/Application +
    # Shared/AppGroup merge-by-owning-bundle-id scan_apps does, so this
    # test exercises exactly what list_apps' live mechanism would see.
    containers_by_app: dict[str, list[str]] = {}
    for parent in adapter.container_parents():
        for child in folder_map.get(parent, []):
            own_id = adapter.container_bundle_id(child, guid_to_bundle)
            if not own_id:
                continue
            identity = group_owner.get(own_id, own_id) if platform == 'ios' else own_id
            containers_by_app.setdefault(identity, []).append(child)

    fixture_apps = {}  # app_id -> [(pattern, source), ...]
    for fx_platform, app_id, pattern, source in fixtures.iter_fixtures():
        if fx_platform == platform:
            fixture_apps.setdefault(app_id, []).append((pattern, source))

    results = []
    for app_id, patterns in sorted(fixture_apps.items()):
        container_paths = containers_by_app.get(app_id)
        if not container_paths:
            results.append((app_id, 'not in case', None, None))
            continue
        # Always pull the FULL pool (limit=1000, not args.top) so the
        # top-N pass/fail check and the "would list_evidence_candidates
        # catch it deeper down" check both work off true, untruncated
        # data — matches what scan_apps/list_evidence_candidates
        # actually do internally (2026-08-25).
        candidates = []
        for cp in container_paths:
            cands, _total, _archives = app_intelligence.find_evidence_databases(
                cp, folder_map, ui_metadata, limit=1000, read_bytes=read_bytes)
            candidates.extend(cands)
        candidates.sort(key=lambda e: e['bytes'] + e['wal_bytes'], reverse=True)

        matched_rank = None
        matched_source = None
        for i, cand in enumerate(candidates):
            for pattern, source in patterns:
                if fixtures.match_known_pattern(platform, app_id, cand['path']):
                    matched_rank = i
                    matched_source = source
                    break
            if matched_rank is not None:
                break

        if matched_rank is not None and matched_rank < args.top:
            results.append((app_id, 'FOUND', matched_rank, matched_source))
        elif matched_rank is not None:
            results.append((app_id, f'FOUND ONLY VIA ESCALATION (rank {matched_rank}, '
                                    f'not in top {args.top} — list_evidence_candidates would '
                                    'catch this)', matched_rank, matched_source))
        elif not candidates:
            results.append((app_id, 'NO CANDIDATES AT ALL', None, None))
        else:
            results.append((app_id, f'NOT FOUND ANYWHERE ({len(candidates)} candidates checked)',
                            None, None))

    found = sum(1 for r in results if r[1] == 'FOUND')
    escalation_ok = sum(1 for r in results if str(r[1]).startswith('FOUND ONLY VIA ESCALATION'))
    checked = sum(1 for r in results if r[1] != 'not in case')
    print(f"{args.zip_path}\n{'=' * 60}")
    for app_id, status, rank, source in results:
        if status == 'FOUND':
            print(f"  PASS       {app_id:45s} rank {rank}  ({source})")
        elif status == 'not in case':
            print(f"  --         {app_id:45s} not present in this case")
        elif str(status).startswith('FOUND ONLY VIA ESCALATION'):
            print(f"  ESCALATE   {app_id:45s} {status}")
        else:
            print(f"  FAIL       {app_id:45s} {status}")
    print(f"\n{found}/{checked} fixture apps present in this case had their known-real "
         f"file surfaced in the top {args.top} candidates, unaided.")
    if escalation_ok:
        print(f"{escalation_ok}/{checked} more were findable via list_evidence_candidates "
             "(the top-N ranking alone missed them, but nothing was silently lost).")
    true_fails = checked - found - escalation_ok
    if true_fails:
        print(f"{true_fails}/{checked} were NOT findable at all — a real gap, not just a "
             "ranking/visibility issue.")
    return 0 if true_fails == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
