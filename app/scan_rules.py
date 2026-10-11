"""scan_rules.py — the one shared ruleset of folder-prefix rules consumed
by all four "where does real user/app data live, vs. system/app noise"
features in this app: the header-scan tiers, nested-archive discovery,
the embedded sqlite/plist sweep, and the Media Browser's "User Media"
filter. Added 2026-10-10, direct request/design discussion: these four
features had independently drifted (archive_discovery_folders() was
missing folders scan_folders() had, purely by accident), and a header-
scan-only Caches include/exclude mechanism was built then found to not
generalize cleanly to the other three — the real fix is ONE list, not
N hand-maintained ones.

Platform-split (scan_rules_ios.json / scan_rules_android.json), same
reasoning as embedded_media_skip_list.py's own platform split: iOS and
Android paths share no vocabulary at all ('mobile/Containers/...' vs
'data/data/...'), so a platform column on a single merged list would
always be a trivial 1:1 mirror of which file a row lives in — a case is
also always exactly one platform. Shipped, git-tracked defaults in
config/ (unlike embedded_media_skip_list's empty-until-populated files)
since this ruleset is the app's own built-in definition of "what's user
data," not something an examiner builds up by hand over time.

Each rule: {"path": str, "include": 1|0, "user_media": str}.
  - "path" is CANONICAL (Cellebrite-format for iOS, no "private/var/"
    prefix) — FfsAdapter applies prefix_shortcut() per-path at use time
    for a GrayKey case (see FfsAdapter.scan_folders()), the same
    mechanism already used for FORENSIC_SHORTCUTS and the old
    _user_media_include_prefixes(). Android has no equivalent prefix
    quirk between its two acquisition tools.
  - "include": 1 = in scope for header-scan/nested-archive-discovery/
    embedded-media-scan; 0 = excluded. An include=0 row is always a
    subfolder nested under some include=1 row above it (e.g. a known-
    noise Caches subfolder carved out of the broader Caches include) —
    matching is a simple "matches an include prefix AND not an exclude
    prefix" check, not a general longest-prefix-wins resolver, because
    that nesting invariant always holds by construction.
  - "user_media": "not_user_media" | "user_media_only" | "also_user_media"
    — an INDEPENDENT dimension from "include", consulted only by the
    Media Browser's own "User Media" filter (user_media_prefixes()),
    which is deliberately narrower/precision-focused rather than a
    coverage tool — it ignores "include"/exclude entirely and just asks
    "is this row tagged relevant to Browse-as-user-media." A row can in
    principle be "user_media_only" (relevant to User Media's own display
    but not meant to be scanned by the other three features) even though
    no real row needs that today.
  - "embedded_media_exclude": 1 (optional, default absent/0) — carves a
    row OUT of the embedded sqlite/plist sweep specifically, while it
    stays in scope for header-scan/nested-archive-discovery (see
    embedded_media_excludes() below). One-directional ("too expensive
    here"), unlike "user_media"'s tri-state — no row uses this yet.
    There is deliberately NO equivalent "user_media_exclude" field —
    folder-level excludes from the User Media filter already have a
    real, working, UI-wired mechanism (user_media_ignore_list.py's own
    folder-prefix entries), so this ruleset doesn't duplicate it.

Deliberately NOT the same thing as embedded_media_skip_list.py's own
per-case/per-platform "this one database is never worth scanning" list —
that's a different, narrower, user-editable exception list layered
*within* whatever this module says is in scope, not a replacement for it.
"""

import json
import os
import sys

_PLATFORMS = ('ios', 'android')
_cache = {p: {"stat": None, "rules": None} for p in _PLATFORMS}


def _check_platform(platform: str) -> str:
    if platform not in _PLATFORMS:
        raise ValueError(f"platform must be 'ios' or 'android', got {platform!r}")
    return platform


def store_path(platform: str) -> str:
    """Location of config/scan_rules_<platform>.json — same dev/frozen
    path convention as hardware_models.json (resource_path() in
    ffs-explorer.py), re-derived here since this module has no Qt/GUI
    dependency and shouldn't import from ffs-explorer.py."""
    _check_platform(platform)
    name = f"scan_rules_{platform}.json"
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "config", name)


def load_rules(platform: str) -> list[dict]:
    """Return the raw rule dicts for *platform*, cached by file mtime+size.
    A missing/unreadable file returns [] rather than raising — every real
    caller goes through include_prefixes()/exclude_prefixes()/
    user_media_prefixes() below, for which an empty list is a safe (if
    maximally conservative) fallback."""
    _check_platform(platform)
    path = store_path(platform)
    cache = _cache[platform]
    try:
        st = os.stat(path)
        stat = (st.st_mtime_ns, st.st_size)
    except OSError:
        cache["stat"], cache["rules"] = None, []
        return []
    if cache["stat"] == stat and cache["rules"] is not None:
        return cache["rules"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        rules = [r for r in raw.get("rules", [])
                 if isinstance(r, dict) and r.get("path")]
    except Exception:
        rules = []
    cache["stat"], cache["rules"] = stat, rules
    return rules


def include_prefixes(platform: str) -> tuple[str, ...]:
    """Canonical include=1 path prefixes for *platform* — the base scope
    for header-scan/nested-archive-discovery/embedded-media-scan, before
    exclude_prefixes() is applied."""
    return tuple(r["path"] for r in load_rules(platform) if r.get("include") == 1)


def exclude_prefixes(platform: str) -> tuple[str, ...]:
    """Canonical include=0 path prefixes for *platform* — subfolders of
    include_prefixes() confirmed to hold no real user data; a path in
    scope must match an include prefix and NOT match one of these."""
    return tuple(r["path"] for r in load_rules(platform) if r.get("include") == 0)


def embedded_media_excludes(platform: str) -> tuple[str, ...]:
    """Canonical prefixes carved OUT of the embedded sqlite/plist sweep
    specifically — added 2026-10-10, direct request: a folder can stay
    in scope for header-scan/nested-archive-discovery (cheap: extension
    check, or a magic-byte read) while being excluded from embedded-
    media (expensive: opens and parses every sqlite/plist inside it)
    because the examiner has judged that tradeoff not worth the time for
    THIS folder specifically. A plain additional denylist on top of
    exclude_prefixes() — not a tri-state like user_media, because this
    is a one-directional "too expensive here" carve-out, never a "scan
    this ADDITIONALLY, even though it's excluded everywhere else" case.
    No rows use this yet — see the "embedded_media_exclude" key on a
    rule in scan_rules_ios.json/scan_rules_android.json's own schema
    note to add one once a real, confirmed-safe-to-skip folder exists;
    never invented speculatively."""
    return tuple(r["path"] for r in load_rules(platform)
                 if r.get("embedded_media_exclude") == 1)


def user_media_prefixes(platform: str) -> tuple[str, ...]:
    """Canonical prefixes tagged relevant to the Media Browser's "User
    Media" filter ("user_media_only" or "also_user_media") — independent
    of include/exclude above; see this module's own docstring.

    Folder-level EXCLUDES from this filter are NOT part of this ruleset
    — they belong to user_media_ignore_list.py (its own folder-prefix
    entries, added via the "User Media" row's right-click menu, a
    working mechanism that predates this file). A "user_media_exclude"
    field was briefly added here 2026-10-10 then reverted the same day,
    direct correction: it duplicated that already-built, already-wired
    mechanism instead of pointing the UI at it."""
    return tuple(r["path"] for r in load_rules(platform)
                 if r.get("user_media") in ("user_media_only", "also_user_media"))
