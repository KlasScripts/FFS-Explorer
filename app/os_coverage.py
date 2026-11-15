"""os_coverage.py — which OS versions this app's evidence locations have
been checked on, and the warning shown when an extraction is outside
them. Qt-free.

Evidence moves between OS versions (EVIDENCE_LOCATIONS.md). The jump menu
and the evidence folders (scan_rules) were checked by path on real
extractions of the versions in CHECKED; public sources cover up to
DOCUMENTED. On any other version, an empty or missing folder may mean the
location moved, not that there is no evidence, and the examiner is told
so rather than left to assume.

Update CHECKED only after checking a real extraction of that version
against EVIDENCE_LOCATIONS.md, and record it there.
"""
import re

# Major versions checked by path on real extractions (EVIDENCE_LOCATIONS.md,
# 2026-11-15): Android 14 (two phones) and 15; iOS 17.
CHECKED = {'Android': (14, 15), 'iOS': (17, 17)}
# Highest major version the public sources in EVIDENCE_LOCATIONS.md cite.
DOCUMENTED = {'Android': 16, 'iOS': 26}

_FIELD = {'Android': 'Android Version', 'iOS': 'iOS Version'}


def _major(version: str):
    m = re.match(r'\s*(\d+)', version or '')
    return int(m.group(1)) if m else None


def version_of(fields, platform: str) -> str:
    """The OS version string from device-info rows [(field, data, source)]."""
    name = _FIELD[platform]
    return next((d for f, d, _s in fields or () if f == name and d), '')


def warning(platform: str, version: str):
    """The banner text for an extraction of *platform* ('Android' or
    'iOS') at *version*, or None when that major version was checked."""
    lo, hi = CHECKED[platform]
    checked = f"{platform} {lo}" if lo == hi else f"{platform} {lo}–{hi}"
    sources = f"public sources cite up to {platform} {DOCUMENTED[platform]}"
    major = _major(version)
    if major is None:
        return (f"⚠ The {platform} version of this extraction could not be read, so it is "
                f"not known whether its evidence locations were checked (real images checked: "
                f"{checked}; {sources}). An empty or missing folder may mean the location "
                f"moved, not that there is no evidence.")
    if lo <= major <= hi:
        return None
    side = "newer" if major > hi else "older"
    return (f"⚠ This extraction is {platform} {version}, {side} than the versions whose "
            f"evidence locations were checked on real images ({checked}; {sources}). "
            f"An empty or missing folder may mean the location moved, not that there is "
            f"no evidence. See EVIDENCE_LOCATIONS.md.")
