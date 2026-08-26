"""app_intelligence.py — per-app coverage/category/permission intelligence
and a deterministic "interest score" for apps with no artifact parser yet.

Qt-free (imported by the MCP server as well as, potentially, the GUI).
Every score component is stored individually alongside the total — never a
bare number — so an examiner or an AI client can see exactly why an app
scored the way it did, not just trust it.

Raw-content reads (iOS plists, Android packages.xml) reuse
CaseContext.read_bytes — the same raw-byte reader the Tier-3 SQLite tools
already use — gated the same way, by the caller checking
ctx.raw_content_enabled before calling read_ios_app_metadata /
read_android_permissions. This module never checks that flag itself; the
caller (mcp_server.py's list_apps tool) decides whether raw content is
allowed and simply omits calling these when it isn't.
"""

import hashlib
import os
import plistlib
import time
import unicodedata
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone

import artifact_runner
import ccl_abx
from db_utils import _open_cache_db, _open_results_db, load_app_registry, load_case_setting
from header_scan import sniff_media_kind


def scan_logic_version() -> str:
    """Short content hash of this module's own source — folded into both
    callers' app_intelligence cache keys (artifact_viewer.py's
    _art_show_apps, mcp_server.py's list_apps) alongside the existing
    files_indexed/raw_content_enabled components.

    Found necessary 2026-08-26: the cache key previously reflected only
    facts about the ARCHIVE (file count, raw_content_enabled) — nothing
    about this module's OWN scan logic — so a genuine bug fix here (the
    data_created/preferences_modified/splash_snapshot_modified rework, and
    separately a display_name fix) silently kept serving a stale cached
    scan on an already-scanned case, with no user-visible signal that
    anything was wrong; only re-running against a NEWLY-scanned case (or
    manually clearing the cache) would show the fix. Same auto-derived
    technique parser_versions.py already uses for artifact parser scripts
    (never hand-authored, so it can't drift from what's actually on disk) —
    not that module's own store/changelog machinery, which is scoped to
    per-script versioning UI this cache-busting use has no need for."""
    try:
        with open(__file__, 'rb') as f:
            return hashlib.blake2b(f.read(), digest_size=8).hexdigest()
    except OSError:
        return 'unknown'

# Known comms/social apps this project has already had to identify by eye
# during triage (case reviews 2026-08-23) — bundle-id/app_group SUBSTRINGS,
# matched case-insensitively. Exact-string matching against every vendor's
# full reverse-DNS id would be more "precise" but brittle (regional bundle
# id variants, iOS vs Android naming differing per vendor); a substring
# against each vendor's distinctive root is the same tradeoff this project
# already made informally when eyeballing list_app_containers by hand.
_KNOWN_COMMS_SUBSTRINGS = (
    'whatsapp', 'telegram', 'graph.telegra',  # Telegram's iOS app_group root
    'kik.chat', 'hammerandchisel.discord', 'viber',
    'skype', 'groupme', 'signal', 'adhoclabs.burner',
    'toyopagroup.picaboo',  # Snapchat
    'imo.android.imous', 'threema', 'naver.line',
    'bereal', 'zhiliaoapp.musically',  # TikTok (has DMs)
    'instagram', 'facebook.Messenger', 'facebook.orca',
    'google.android.apps.messaging', 'microsoft.teams',
    'tencent.mm',  # WeChat
    'clubhouse', 'mewe',
)

# Known photo/file "vault" (hide-and-lock) apps and cloud-storage apps —
# added 2026-08-24 after a user design question pointed out the score
# formula actively WORKED AGAINST vault-app detection (see the
# category_noncomms branch below): a vault app deliberately self-
# categorizes as boring (Utilities/Calculator) and declares minimal
# permissions specifically to look uninteresting, which is exactly the
# profile that branch used to score 0. Bundle ids/packages confirmed via
# iLEAPP's nsVault.py (iOS — notes its own app identity is "inferred from
# the FolderLockAdvanced.sqlite filename... not bundle-specific", so no iOS
# vault id is listed here) and ALEAPP's galleryVault.py, vaulty_files.py,
# calculatorLockVault.py, NQ_Vault.py, playgroundVault.py (Android).
_KNOWN_VAULT_SUBSTRINGS = (
    'thinkyeah.galleryvault', 'theronrogers.vaultyfree',
    'calculator.lock.hide.photo.video', 'netqin.ps',
    'playground.develop.applocker',
)
# Known cloud-storage/sync apps — same day, same design question. These
# apps are worth flagging for a DIFFERENT reason than comms/vault apps:
# not because they hold a conversation, but because their local cache can
# reveal files/folders the device owner stored remotely and may believe
# were never on-device at all. Confirmed via iLEAPP's googleDrive.py/
# oneDrive.py and ALEAPP's dropbox.py/OneDrive_Metadata.py/ProtonDrive.py.
_KNOWN_CLOUD_STORAGE_SUBSTRINGS = (
    'dropbox.android', 'microsoft.skydrive',  # OneDrive's real Android package
    'proton.android.drive',
    'google.android.apps.docs',  # Google Drive's real Android package (named
                                 # for Google Docs historically, confirmed via
                                 # ALEAPP's Cello.py/DocList.py)
)

# The exact permission signal this project's score formula asks for —
# iOS usage-description keys and Android manifest permission names for the
# same four capabilities. Deliberately NOT full permission lists (e.g. photo
# library, network state) — those weren't part of the approved score design.
_IOS_SENSITIVE_KEYS = (
    'NSCameraUsageDescription', 'NSMicrophoneUsageDescription',
    'NSContactsUsageDescription', 'NSLocationWhenInUseUsageDescription',
    'NSLocationAlwaysAndWhenInUseUsageDescription',
    'NSLocationAlwaysUsageDescription',
)
_ANDROID_SENSITIVE_PERMS = (
    'android.permission.CAMERA', 'android.permission.RECORD_AUDIO',
    'android.permission.READ_CONTACTS', 'android.permission.ACCESS_FINE_LOCATION',
)

_ACTIVITY_MIN_BYTES = 5 * 1024 * 1024      # 5MB — matches the approved score formula
_ACTIVITY_RECENT_WINDOW_S = 30 * 24 * 3600  # 30 days relative to the case's own max mtime
# Found 2026-08-26, while verifying the archive_max_mtime future-timestamp
# fix above: last_activity/archive_max_mtime are nanosecond epoch values
# throughout this project (confirmed: _format_last_activity divides by
# 1_000_000_000, adapters/ffs.py's own extraction multiplies by _S_TO_NS),
# but compute_interest_score compared their delta directly against this
# SECONDS constant with no conversion — the "recent" branch could only ever
# pass for a delta under ~2.6 MILLISECONDS, so recently_used/activity_signal
# has been silently False for essentially every app, in every case, since
# this feature shipped — not specific to the future-timestamp bug above.
# Verified against this case's own real data: WhatsApp's last real activity,
# ~58 minutes before the case's own acquisition instant, still read
# recently_used=False before this fix.
_ACTIVITY_RECENT_WINDOW_NS = _ACTIVITY_RECENT_WINDOW_S * 1_000_000_000

# Cross-app library/telemetry database noise, confirmed by manual
# case-review 2026-08-23 (Android 14 JoshHickman) to appear across many
# unrelated apps and never hold message content: Firebase Data Transport,
# WorkManager/Google-notification tables (varying numeric/account prefix,
# hence a suffix match), App Center telemetry, ExoPlayer's media-playback
# cache, an emoji-picker library, and Kik's own A/B-testing tables (also
# prefixed per-install, hence suffix match). NOT exhaustive — a genuinely
# new noise pattern will still surface as a false "evidence" hit until
# someone adds it here; that's a real, honest limitation, not a bug.
# Names with no recognized db extension (e.g. 'androidx.work.workdb',
# 'gnp_database') are already excluded by _DB_EXTENSIONS below and don't
# need listing here too.
_DB_NOISE_EXACT = {
    'ariastorage.db', 'exoplayer_internal.db',
    'mbgl-offline.db',  # Mapbox GL's offline map-tile cache — confirmed
                        # this beat Threema's real threema4.db on size
                        # (1.98MB vs 282KB) before this was added
    'cache.db',  # Apple's standard NSURLCache/WebKit HTTP-response cache
                 # filename — confirmed beating the real evidence file (or
                 # standing in for a missing one) for Discord, Kik, BeReal,
                 # and Viber simultaneously on the SAME iOS case; this one
                 # name alone was the single highest-value noise entry
                 # found this session, appearing across unrelated apps
                 # exactly the way a platform-standard cache filename would
    'observations.db',  # Apple WebKit's Private Click Measurement store
                        # (under .../WebKit/WebsiteData/), confirmed showing
                        # up under Discord's SafariViewService subtree once
                        # cache.db was excluded — also a platform artifact,
                        # not app content
    'heimdallr.db',  # ByteDance's own in-house crash/performance-monitoring
                     # SDK (TikTok, CapCut, other ByteDance apps) — confirmed
                     # 2026-08-24 against a real iOS 16.5 case: this 25MB
                     # file beat TikTok's real message/contacts store
                     # (Documents/AwemeIM.db, 135KB) on size alone. Not a
                     # message store in any app that carries it; cross-
                     # checked against iLEAPP's tikTok.py, which never
                     # targets this file
    'cache_controller.db',  # Snapchat's own local cache-orchestration
                            # store, not chat content — confirmed 2026-08-24
                            # on the same case beating Snapchat's real
                            # message store (Documents/user_scoped/*/arroyo/
                            # arroyo.db) on size (960KB vs 377KB); cross-
                            # checked against iLEAPP's snapchat.py, which
                            # targets arroyo.db, never this file
    'unifystorage.sqlite',  # ByteDance's own generic SDK storage layer
                            # (under Library/AWEStorage/) — confirmed
                            # 2026-08-24 on the same case beating TikTok's
                            # real message store (Documents/AwemeIM.db) on
                            # size (10.4MB vs 135KB) even after heimdallr.db
                            # was excluded above; iLEAPP's tikTok.py never
                            # targets it either
}
_DB_NOISE_SUFFIXES = ('notifications.db', '.abtesting.db', '.alternatestable', '.assetentries.db')
# Substring (not suffix) match — safe here because a legitimate primary
# message database is never going to have these words in its own filename.
# 'emoji' generalized from Threema's 'emoji-search-index.db' after the
# identical emoji-picker-library pattern reappeared as plain 'emoji.db' in
# MeWe. 'analytics' added after Instagram's real evidence slot was filled
# by Meta's own 'FBAnalyticsUnifiedEventStore.sqlite' telemetry store —
# same class of problem (an SDK-injected file, not app content), broadened
# to the class rather than naming every analytics SDK's own filename.
_DB_NOISE_SUBSTRINGS = (
    'emoji', 'analytics',
    'remoteconfig',  # Firebase Remote Config SDK's own local cache (Kik)
    'mixpanel',      # Mixpanel analytics SDK — doesn't contain "analytics"
                     # in its own filename, so the substring above missed it
)
_DB_EXTENSIONS = ('.db', '.sqlite', '.sqlite3')
# A dot-less file over this size isn't worth a full read just to check its
# 16-byte header — real dot-less SQLite stores found so far (Chrome's
# bare names, Telegram's db_sqlite at 9.4MB) are all well under this.
_MAGIC_CHECK_MAX_BYTES = 64 * 1024 * 1024

# Apps confirmed (Signal) or reasonably inferred from shared codebase
# lineage (Session, a Signal-protocol fork) to encrypt their local database
# at rest — a standard SQLite parse cannot read these without the device's
# own key material first. A short, explicitly-sourced list, not a general
# rule — most messaging apps do NOT do this (confirmed: Threema, Telegram,
# Kik, WhatsApp's local stores were all directly readable in real casework
# this session). Matched the same way _KNOWN_COMMS_SUBSTRINGS is, against
# app_id substrings.
_KNOWN_ENCRYPTED_APPS = {
    'thoughtcrime.securesms': (
        "Signal — local database (signal.db) is SQLCipher-encrypted at "
        "rest; not readable via a standard SQLite parse without the "
        "device's Android Keystore-derived key."),
    'loki.messenger': (
        "Session — Signal-protocol-derived (not independently confirmed "
        "this session, inferred from shared codebase lineage with Signal, "
        "which IS confirmed) — likely also encrypted at rest."),
    'whispersystems.signal': (
        "Signal (iOS) — inherits the same SQLCipher-at-rest design as "
        "Signal Android; not independently confirmed on iOS this session."),
}


    # Whole-subtree prunes, checked at the FOLDER level — TRUE excludes
    # (never a candidate for ANY app, WebView-based or native) vs.
    # deprioritized (still a candidate, just a fallback of last resort).
    # Split 2026-08-23 after a design discussion raised a real risk: a
    # blanket WebKit exclusion would silently hide real content for an app
    # that's actually BUILT as a wrapped web view (a Cordova/Ionic-style
    # "simple web app"), where WebKit's own LocalStorage genuinely IS the
    # app's real data store — a false NEGATIVE, worse than the false
    # positives this filtering exists to catch, since it makes a real app
    # look empty instead of merely cluttering the result with cache noise.
_DB_TRUE_EXCLUDE_PATH_SUBSTRINGS = (
    'com.apple.safariviewservice',  # Apple's own system browser silo for
                                     # SFSafariViewController-presented
                                     # links — this is a SEPARATE, OS-owned
                                     # container from the host app's own
                                     # WebKit storage regardless of the
                                     # app's architecture, so always safe
                                     # to exclude outright (confirmed: this
                                     # is where Discord's WebKit noise
                                     # actually lived)
    'library/httpstorages',  # NSURLSession's own cookie/credential cache —
                             # network-stack-level, never app-authored
                             # content even for a WebView-based app
    '.tipkit/',  # Apple's TipKit framework (onboarding tips) — its own
                 # tiny database, never app content (BeReal)
    'logstoreprovider',  # a generic per-install-numbered SDK event/log
                         # folder confirmed under both Instagram's main app
                         # AND its own NotificationExtension container —
                         # confirmed 2026-08-24 beating Instagram's real
                         # Direct-Messages store (Library/Application
                         # Support/DirectSQLiteDatabase/<id>.db, per
                         # iLEAPP's instagramThreads.py) on size (3.8MB vs
                         # 1.4MB combined with its own -wal); no exact
                         # filename to match since 'events.sqlite' alone is
                         # too generic (matches the notification extension's
                         # own separate, equally-noise events.sqlite too)
)
# Deprioritized, NOT excluded: an app's OWN WebKit storage (outside the
# SafariViewService silo above) — only surfaced as a candidate if nothing
# non-WebKit survives the normal filter elsewhere, and returned with a
# `note` rather than the same unqualified confidence as a native SQLite hit.
_DB_DEPRIORITIZED_PATH_SUBSTRINGS = ('/webkit/',)


def find_evidence_databases(container_path: str, folder_map: dict, ui_metadata: dict,
                            limit: int = 5, read_bytes=None) -> tuple[list[dict], int]:
    """Returns (candidates, total_found) — up to *limit* candidate database
    files anywhere under *container_path*, plus the TRUE total that survived
    filtering before the *limit* cutoff was applied. Reporting the total
    separately (added 2026-08-25) matters: confirmed on real casework
    (TikTok, iOS 16.5 CTF23 Cellebrite) that a small fixed limit can
    silently cut off the real evidence entirely — 34 candidates existed in
    one container, and the two real message stores ranked #7 and #12 by
    size, both invisible at limit=5 even though they're real SQLite files a
    schema check would immediately confirm. A caller that only sees 5
    candidates and total_found=5 knows it saw everything; total_found=34
    tells it there's more to check, e.g. via a higher limit.

    Name/size-based (Tier 2, no raw content read) so this works even with
    raw_content_enabled off, PLUS a Tier-3 content-based fallback for
    extensionless files when raw content access is on (see *read_bytes*
    below). Deliberately NOT scoped to a databases/ subfolder — confirmed
    against real Telegram data that its actual message store
    (files/account1/cache4.db) sits outside the conventional databases/
    folder entirely.

    *read_bytes*, optional — (ui_path) -> bytes | None, the same Tier-3 raw
    reader CaseContext already exposes for get_sqlite_schema/
    sample_sqlite_rows. When given (i.e. ctx.raw_content_enabled), a file
    with NO EXTENSION AT ALL that survived exclusion but didn't match
    _DB_EXTENSIONS gets its header magic-byte checked (header_scan.
    classify_magic) rather than being silently dropped. This SUPERSEDES an
    earlier, narrower fix (an exact-name allowlist for Chrome's bare
    'History'/'Cookies'/etc. files, added and then removed the same day) —
    a hardcoded list of known bare filenames is the same kind of whack-a-
    mole this project moved away from for noise-filtering; checking actual
    file content is the general fix, not another specific name to add.
    Content-based detection is real evidence, not a filename guess — a
    dot-less file's magic bytes either are or aren't SQLite's, no judgment
    call involved. Confirmed both cases now covered without any per-app name list:
    Chrome's bare 'History' AND Telegram's underscore-named 'db_sqlite'
    (confirmed missing entirely from candidates, not just outranked, before
    this fallback existed) are both plain content matches. The one real
    cost of dropping the Tier-2 allowlist: Chrome's bare files are only
    found when raw_content_enabled is on — acceptable, since that's true
    of category/permissions scoring already, and Tier 2 still keeps every
    normally-extensioned file working with no opt-in needed at all.
    Deliberately scoped to dot-less filenames only
    (not every non-matching file) to bound the cost — a real container can
    hold thousands of files, and nearly every one of them (media, plists,
    JSON, caches) already carries a self-describing extension; a name
    with no extension separator at all is the specific, narrow class this
    bug belongs to, not an excuse to content-scan everything. A file over
    _MAGIC_CHECK_MAX_BYTES is skipped even then — a huge dot-less file is
    unlikely to be a database and not worth a full read just to check 16
    bytes of it.

    Replaced the older single-winner find_evidence_database 2026-08-24 after
    it was confirmed, on a real case (iOS 16.5 CTF23 Cellebrite), to be an
    unbounded whack-a-mole: even after excluding four confirmed-noise SDK
    files by name (see _DB_NOISE_EXACT/_DB_TRUE_EXCLUDE_PATH_SUBSTRINGS
    below), TWO MORE layers of unrelated third-party SDK telemetry
    (tracker_v3.sqlite, time_in_app_*.db) still outranked the real message
    store on size for the same two apps. Telling real app content apart
    from a bundled SDK's own analytics/telemetry file by filename alone is
    a genuinely open-ended judgment call — better made by whoever is
    calling this (an LLM, optionally verifying via get_sqlite_schema, or an
    examiner) than guessed at here. This function's job narrows to: don't
    return zero-ambiguity platform noise, and surface everything else with
    the facts needed to triage it.

    Deliberately does NOT cite iLEAPP/ALEAPP or any other external source
    inline (removed 2026-08-24, the same day it was added, after a design
    question: baking a per-app answer key into the LIVE ranking risks
    testing whether the answer key is right, not whether this general
    mechanism — size/WAL/noise-filtering/magic-byte detection — actually
    works. That cross-reference data still exists, as validation fixtures
    a SEPARATE script checks this function's output against — see
    scripts/leapp_evidence_fixtures.py and scripts/validate_evidence_ranking.py
    — but it's deliberately not imported here. iLEAPP and ALEAPP are themselves live, actively-
    maintained GitHub projects (people add/change parsers constantly), so
    baking a snapshot of their answers into permanent runtime ranking
    logic would also go stale in a way a periodically-rerun validation
    script wouldn't.

    Each candidate dict carries: path, bytes (base file only), wal_bytes,
    wal_present, shm_present, and note (non-null only for a deprioritized
    WebKit-storage hit — see below).

    wal_bytes is reported SEPARATELY, not merged into bytes — confirmed
    necessary against a real case: Instagram's real Direct-Messages store
    (per iLEAPP's instagramThreads.py) sat at a nearly-empty 4KB base file
    with all its actual content in a 1.4MB -wal sidecar; a caller ranking
    candidates needs to see that split to understand what it's looking at,
    not have it silently pre-summed. SHM is detected (shm_present) but never
    sized — it's a fixed ~32KB shared-memory index, never a store of real
    content.

    Candidates rank by bytes + wal_bytes descending. Normal (non-WebKit)
    hits are listed ahead of deprioritized WebKit-storage hits UNLESS no
    normal hit exists at all — an app's own WebKit LocalStorage genuinely
    IS the app's data for a Cordova/Ionic-style wrapped web view, so it's
    demoted, never dropped. Returns ([], 0) if nothing survives filtering —
    never guessed.

    Deliberately does NOT interpret a candidate's own filename beyond the
    extension/magic-byte check above — a purely-numeric or opaque filename
    stem (e.g. Instagram's real store, '<account-id>.db' under
    DirectSQLiteDatabase/) can be a strong relevance signal when it matches
    an ID already seen elsewhere for the same app (an account/user/thread
    id decoded from another file, or seen in a sibling container's path),
    but recognizing that match needs context this function doesn't have —
    it only sees one container's file list, not the app's already-decoded
    content. That reasoning step is the caller's job; see the STANDING
    TRIAGE STEP note on mcp_server.py's list_apps evidence_databases field
    for the full instruction and the confirmed Instagram example."""
    candidates: list[dict] = []
    fallback: list[dict] = []
    stack = [container_path]
    while stack:
        cur = stack.pop()
        lower_cur = cur.lower()
        if any(s in lower_cur for s in _DB_TRUE_EXCLUDE_PATH_SUBSTRINGS):
            continue
        deprioritized = any(s in lower_cur for s in _DB_DEPRIORITIZED_PATH_SUBSTRINGS)
        children = folder_map.get(cur)
        if children is None:
            name = cur.rsplit('/', 1)[-1].lower()
            recognized = name.endswith(_DB_EXTENSIONS)
            if not recognized and read_bytes is not None and '.' not in name:
                size_hint = (ui_metadata.get(cur) or {}).get('size', 0)
                if 0 < size_hint <= _MAGIC_CHECK_MAX_BYTES:
                    header = read_bytes(cur)
                    if header and header[:16] == b'SQLite format 3\x00':
                        recognized = True
            if not recognized:
                continue
            if (name in _DB_NOISE_EXACT
                    or any(name.endswith(s) for s in _DB_NOISE_SUFFIXES)
                    or any(s in name for s in _DB_NOISE_SUBSTRINGS)):
                continue
            size = (ui_metadata.get(cur) or {}).get('size', 0)
            wal_size = (ui_metadata.get(cur + '-wal') or {}).get('size', 0)
            shm_present = (ui_metadata.get(cur + '-shm')) is not None
            entry = {
                'path': cur, 'bytes': size,
                'wal_bytes': wal_size, 'wal_present': wal_size > 0,
                'shm_present': shm_present,
                'note': ("Found only inside WebKit's own storage — could be "
                         "this app's real data if it's built as a wrapped "
                         "web view, or just generic browser-engine cache. "
                         "Lower confidence than a native SQLite hit; verify "
                         "manually.") if deprioritized else None,
            }
            (fallback if deprioritized else candidates).append(entry)
            continue
        stack.extend(children)

    def _rank(e: dict) -> int:
        return e['bytes'] + e['wal_bytes']

    candidates.sort(key=_rank, reverse=True)
    fallback.sort(key=_rank, reverse=True)
    chosen = candidates or fallback
    return chosen[:limit], len(chosen)


# Chromium-style WebView storage folder names — a COMPLETELY DIFFERENT
# storage format from SQLite (a folder of .ldb/.log/MANIFEST-*/CURRENT
# files, not a single file), so find_evidence_databases' extension-based
# scan can never see it at all, real content or not.
_WEBVIEW_STORAGE_FOLDER_NAMES = ('indexeddb', 'leveldb')


def find_webview_storage(container_path: str, folder_map: dict, ui_metadata: dict) -> dict | None:
    """Detect (NOT read) Chromium-style WebView local storage anywhere
    under *container_path* — common on Android under
    app_webview/Default/{IndexedDB,Local Storage/leveldb}/ for any app
    using the system WebView to render some or all of its UI (confirmed
    present in real Android 14 casework, e.g.
    com.google.android.apps.wear.companion's own
    app_webview/Default/Local Storage/leveldb/). For an app actually BUILT
    as a wrapped web view, this folder can hold the app's real content —
    this function only detects its presence/size, it does not parse it.
    Actually reading LevelDB's raw key-value format would need vendoring a
    real reader (iLEAPP/ALEAPP both carry ccl_leveldb.py, MIT/CCL
    Forensics, same lineage as this project's ccl_abx.py — a credible
    future addition); making Chromium's IndexedDB encoding ON TOP of that
    meaningful needs a much larger, separately-maintained tool
    (mister_skinnylegs) — both out of scope here, by design, per the
    2026-08-23 decision to do presence-detection now and treat full
    parsing as a distinct future decision. Returns the largest matching
    folder found (by total bytes) plus how many OTHER such folders exist,
    or None."""
    matches = []
    stack = [container_path]
    while stack:
        cur = stack.pop()
        children = folder_map.get(cur)
        name = cur.rsplit('/', 1)[-1].lower()
        if children is not None and name in _WEBVIEW_STORAGE_FOLDER_NAMES:
            total = 0
            substack = [cur]
            while substack:
                sc = substack.pop()
                sub_children = folder_map.get(sc)
                if sub_children is None:
                    total += (ui_metadata.get(sc) or {}).get('size', 0)
                else:
                    substack.extend(sub_children)
            matches.append({'path': cur, 'bytes': total})
            continue  # a matched storage folder's own contents aren't separately walked
        if children is not None:
            stack.extend(children)
    if not matches:
        return None
    matches.sort(key=lambda m: m['bytes'], reverse=True)
    best = matches[0]
    return {'path': best['path'], 'bytes': best['bytes'], 'other_stores': len(matches) - 1}


# Known vault-app hidden-storage folder PATH patterns — presence-only
# detection (not a read), added 2026-08-24 for the same reason
# find_webview_storage exists: a vault app's raw media stash typically has
# NO SQLite index at all (files inside are renamed/extensionless by
# design specifically to defeat a database- or extension-based scan), so
# find_evidence_databases can never see it no matter how its noise
# filtering or bare-filename list is extended. Path-substring (not exact
# folder-name) match, since these span more than one segment
# ('applocker/vault') or carry a per-install-varying suffix
# ('.galleryvault_<id>'). Confirmed via ALEAPP's calculatorLockVault.py
# ('.Calculator_Lock'), galleryVault.py ('.galleryvault_'), and
# playgroundVault.py ('applocker/vault'); iOS nsVault's own hidden video
# folder ('Documents/FolderLockAdvanced/Videos/Movies') matched via
# 'folderlockadvanced' — same caveat as its scripts/leapp_evidence_fixtures.py
# entry: iLEAPP itself notes this app's identity isn't bundle-specific,
# so this is a folder-name signature, not an app_id lookup.
_VAULT_STORAGE_PATH_SUBSTRINGS = (
    '.calculator_lock', '.galleryvault_', 'applocker/vault',
    'folderlockadvanced',
)


def find_hidden_vault_storage(container_path: str, folder_map: dict, ui_metadata: dict) -> dict | None:
    """Presence-only detection of a vault app's raw hidden-media folder —
    matched by a confirmed vault-storage PATH signature (see
    _VAULT_STORAGE_PATH_SUBSTRINGS), not by reading file contents (the
    files inside are typically renamed/extensionless by design, defeating
    both a database-format scan and a media-extension scan equally).
    Returns {path, bytes, other_stores} for the largest match by total
    bytes, same shape as find_webview_storage, or None. Deliberately NOT
    exhaustive — an unrecognized vault app's storage folder naming won't
    be caught here; that's the entire point of a vault app's naming
    convention, and no name-based heuristic can close that gap in
    general — only a content-based read (out of scope for this Tier-2,
    no-raw-content function) could, and even then only by recognizing
    what the hidden content actually IS, not by any name it's been given."""
    matches = []
    stack = [container_path]
    while stack:
        cur = stack.pop()
        children = folder_map.get(cur)
        lower_cur = cur.lower()
        if children is not None and any(
                p in lower_cur for p in _VAULT_STORAGE_PATH_SUBSTRINGS):
            total = 0
            substack = [cur]
            while substack:
                sc = substack.pop()
                sub_children = folder_map.get(sc)
                if sub_children is None:
                    total += (ui_metadata.get(sc) or {}).get('size', 0)
                else:
                    substack.extend(sub_children)
            matches.append({'path': cur, 'bytes': total})
            continue  # a matched vault folder's own contents aren't separately walked
        if children is not None:
            stack.extend(children)
    if not matches:
        return None
    matches.sort(key=lambda m: m['bytes'], reverse=True)
    best = matches[0]
    return {'path': best['path'], 'bytes': best['bytes'], 'other_stores': len(matches) - 1}


def find_encryption_caveat(app_id: str) -> str | None:
    lower_id = (app_id or '').lower()
    for substr, caveat in _KNOWN_ENCRYPTED_APPS.items():
        if substr in lower_id:
            return caveat
    return None


# ── iOS: plist reads ────────────────────────────────────────────────────────

def find_bundle_container_parent(folder_map: dict) -> str | None:
    """Locate the archive's Bundle/Application container parent path,
    whatever its exact prefix/casing (observed once as root-level lowercase
    'containers/Bundle/Application' in a real Cellebrite case, vs. the
    Data-container parent's 'mobile/Containers/Data/Application' —
    these are NOT the same prefix convention in every extraction, so this
    scans for the real path rather than hardcoding one. Call once per scan,
    not per app."""
    for key in folder_map:
        if key.lower().endswith('bundle/application'):
            return key
    return None


def _clean_display_name(name: str | None) -> str | None:
    """Strip Unicode formatting/invisible characters (category 'Cf' —
    bidi marks, zero-width joiners, etc.) from a plist-sourced name.
    Confirmed on real casework (2026-08-26): WhatsApp's own
    CFBundleDisplayName carries a leading U+200E LEFT-TO-RIGHT MARK — a
    real, legitimate Apple App Store metadata convention, invisible when
    rendered, but present in the string, which would sort/filter/copy
    oddly in the Apps table. NOT applied to a genuinely visible character
    that's simply unusual (BeReal's own CFBundleDisplayName is literally
    'BeReal.', trailing period included — real branding, left as-is)."""
    if not name:
        return name
    cleaned = ''.join(c for c in name if unicodedata.category(c) != 'Cf')
    return cleaned.strip() or None


def scan_ios_bundle_containers(ctx, bundle_parent: str | None, folder_map: dict) -> dict:
    """{bundle_id: {category, permissions_declared, display_name}} for every
    app under the Bundle container parent — one pass, reading each
    container's iTunesMetadata.plist + Info.plist exactly once (not twice:
    identity and content come from the same reads, no separate lookup
    needed).

    display_name (added 2026-08-26, after a user-flagged real example:
    org.mozilla.ios.Firefox's app_registry/LaunchServices-sourced name —
    see _load_group_owner_index below — reads 'Client', and com.viber's
    reads blank) prefers Info.plist's CFBundleDisplayName — the actual
    on-device home-screen name — over app_registry's csstore-sourced name.
    Confirmed against this case's own real data why the two diverge:
    Firefox's own .app bundle folder is literally named 'Client.app'
    (CFBundleExecutable='Client' — Mozilla's iOS build target name, a real,
    documented quirk of that codebase) while Info.plist's own
    CFBundleDisplayName is 'Firefox' — the LaunchServices registry's name
    field is apparently keyed closer to the internal executable/bundle name
    than the display name Apple shows the user, and was simply empty for
    Viber. iTunesMetadata.plist's itemName (the full App Store listing
    title, e.g. 'Firefox: Private, Safe Browser') is the fallback when
    Info.plist has no CFBundleDisplayName (not App-Store-installed, or the
    field is genuinely absent) — a real name, just closer to marketing copy
    than the plain in-hand name CFBundleDisplayName gives when available.

    Cross-checked against iLEAPP's appItunesmeta.py (@AlexisBrignoni) before
    building this — it independently confirmed the same two files/fields
    for iOS app metadata, and revealed a simpler identity path than an
    earlier version of this function used: guid_to_bundle does NOT cover
    Bundle containers at all (confirmed empty for a real Bundle-container
    GUID against real casework — it only covers container_parents(), which
    is Data/PluginKitPlugin/Shared-AppGroup), but there's no need to
    reverse-engineer identity from guid_to_bundle or a third plist at all:
    iTunesMetadata.plist's own 'softwareVersionBundleId' and Info.plist's
    'CFBundleIdentifier' both give the bundle id directly (confirmed
    matching, both 'com.hammerandchisel.discord', for Discord in real
    casework) — the same files already read for genre/category and
    permissions respectively.

    category prefers iTunesMetadata.plist's App-Store-assigned `genre`
    (present for App Store installs) and falls back to Info.plist's
    developer-declared `LSApplicationCategoryType`. Both are flat files
    directly in the archive — no zip-within-zip, unlike Android's manifest
    (see module docstring / CLAUDE.md for why Android has no equivalent).
    A container with neither file, or with a bundle id from neither
    source, contributes nothing — never guessed."""
    out = {}
    if not bundle_parent:
        return out
    for child in folder_map.get(bundle_parent, []):
        bundle_id = None
        category = None
        display_name = None

        raw = ctx.read_bytes(f'{child}/iTunesMetadata.plist')
        if raw:
            try:
                meta = plistlib.loads(raw)
                bundle_id = meta.get('softwareVersionBundleId') or None
                category = meta.get('genre') or None
                display_name = meta.get('itemName') or None
            except Exception:
                pass

        app_dir = folder_map.get(child, [])
        app_bundle = next((p for p in app_dir if p.endswith('.app')), None)
        permissions = []
        if app_bundle:
            raw = ctx.read_bytes(f'{app_bundle}/Info.plist')
            if raw:
                try:
                    info = plistlib.loads(raw)
                    bundle_id = bundle_id or info.get('CFBundleIdentifier') or None
                    if category is None and info.get('LSApplicationCategoryType'):
                        category = info['LSApplicationCategoryType']
                    permissions = sorted(k for k in _IOS_SENSITIVE_KEYS if k in info)
                    # Preferred over iTunesMetadata's itemName above — the
                    # actual home-screen name, not the (often longer,
                    # marketing-flavored) App Store listing title. See this
                    # function's own docstring for the confirmed Firefox/
                    # Viber examples that motivated capturing this at all.
                    if info.get('CFBundleDisplayName'):
                        display_name = info['CFBundleDisplayName']
                except Exception:
                    pass

        if bundle_id:
            out[bundle_id] = {'category': category, 'permissions_declared': permissions,
                              'display_name': _clean_display_name(display_name)}
    return out


# ── Android: packages.xml ───────────────────────────────────────────────────

def _load_packages_xml(ctx) -> ET.Element | None:
    """packages.xml is plain text XML on older Android, but confirmed ABX
    (binary-encoded XML — see app/ccl_abx.py) on real Android 14 casework —
    checked by magic bytes, not by version number, since that's what
    actually varies per device/build, not a clean OS-version cutoff."""
    raw = ctx.read_bytes('data/system/packages.xml')
    if not raw:
        return None
    if ccl_abx.is_abx(raw):
        try:
            return ccl_abx.abx_bytes_to_xml_root(raw)
        except Exception:
            return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


_RUNTIME_PERMISSIONS_PATH = 'data/misc_de/0/apexdata/com.android.permission/runtime-permissions.xml'

# Public, stable Android SDK constants (developer.android.com/reference/
# android/content/pm/ApplicationInfo) — confirmed against real casework:
# packages.xml's categoryHint="4" on com.whatsapp decoded to CATEGORY_SOCIAL,
# correctly matching a known comms app. -1 (CATEGORY_UNDEFINED) is common and
# not itself informative — most apps never set this.
_ANDROID_CATEGORY_HINTS = {
    0: 'Game', 1: 'Audio', 2: 'Video', 3: 'Image', 4: 'Social',
    5: 'News', 6: 'Maps', 7: 'Productivity', 8: 'Accessibility',
}


def _load_runtime_permissions_xml(ctx) -> ET.Element | None:
    """Per-user granted-permission grants — NOT in packages.xml at all
    (confirmed against real casework: packages.xml's <package> elements
    carry signing/install metadata only, no <perms>/<item> children;
    granted permissions live in this separate file instead, as flat
    <package name=..><permission name=.. granted=../></package> — no
    wrapper tag). May be plain XML or ABX depending on device/build, same
    as packages.xml — checked by magic bytes either way."""
    raw = ctx.read_bytes(_RUNTIME_PERMISSIONS_PATH)
    if not raw:
        return None
    if ccl_abx.is_abx(raw):
        try:
            return ccl_abx.abx_bytes_to_xml_root(raw)
        except Exception:
            return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def read_android_permissions(ctx, package_id: str, root: ET.Element | None = None) -> list:
    """Granted runtime permissions for one package — see
    _load_runtime_permissions_xml for why this isn't packages.xml. Pass a
    pre-parsed *root* when scanning many packages in one pass. Returns []
    if the package or file isn't found, or the app was never granted any
    of the four permissions this project's score formula checks for —
    never guessed."""
    if root is None:
        root = _load_runtime_permissions_xml(ctx)
    if root is None:
        return []
    for pkg in root.iter('package'):
        if pkg.get('name') == package_id:
            granted = {perm.get('name') for perm in pkg.iter('permission')
                      if perm.get('granted') == 'true'}
            return sorted(granted & set(_ANDROID_SENSITIVE_PERMS))
    return []


def read_android_category(package_id: str, packages_root: ET.Element | None) -> str | None:
    """Android's on-device category hint for one package, from
    packages.xml's <package categoryHint="N">. None if absent or
    CATEGORY_UNDEFINED (-1, the common default) — not itself informative,
    so treated the same as absent rather than a confirmed 'no category'."""
    if packages_root is None:
        return None
    for pkg in packages_root.iter('package'):
        if pkg.get('name') == package_id:
            hint = pkg.get('categoryHint')
            try:
                return _ANDROID_CATEGORY_HINTS.get(int(hint))
            except (TypeError, ValueError):
                return None
    return None


# ── Parser coverage ──────────────────────────────────────────────────────────

def resolve_parser_coverage(platform: str) -> dict:
    """{app_id: [script_name, ...]} for every artifact parser that targets a
    real per-app container on *platform* ('ios' or 'android').

    Android parsers declare app_path='data/data/<package>' — app_id is the
    last path segment, matching adapters.ffs.FfsAdapter.container_bundle_id's
    own Android branch exactly. iOS per-app parsers declare app_group (the
    App Group's own stable identifier, e.g. 'group.net.whatsapp.WhatsApp.shared')
    which IS what list_app_containers already reports as that container's
    bundle_id (via guid_to_bundle) — used verbatim, no reverse GUID lookup
    needed here. An iOS app_path pointing at an OS path (mobile/Library/SMS,
    mobile/Media/PhotoData — sms_messages.py, photos_metadata.py) isn't a
    per-app container at all and correctly resolves to nothing here."""
    coverage: dict = {}
    modules, _errors = artifact_runner.load_artifacts(platform)
    for script_name, mod in modules:
        app_id = None
        if platform == 'android':
            app_path = getattr(mod, 'app_path', None)
            if app_path and app_path.startswith('data/data/'):
                app_id = app_path.rsplit('/', 1)[-1]
        else:
            app_id = getattr(mod, 'app_group', None)
        if app_id:
            coverage.setdefault(app_id, []).append(script_name)
    return coverage


def resolve_parser_locations(platform: str) -> dict:
    """{app_id: {'app_path_or_group': str, 'has_media_fields': bool}} for
    every app resolve_parser_coverage() covers — added 2026-08-25 after a
    user point: for an app that already HAS a real parser, this project
    already knows exactly where its database is (the same app_path/
    app_group resolve_parser_coverage reads) and, if the parser declares
    media_fields (see Conventions), probably where its media/attachments
    live too — that confirmed knowledge should be surfaced on a has_parser
    row, not just a bare boolean that implies "nothing more to say here."
    Deliberately a separate function sharing the same
    artifact_runner.load_artifacts() call shape rather than folded into
    resolve_parser_coverage() itself — that function's existing
    {app_id: [script_name]} return shape is depended on elsewhere
    (has_parser, artifact_tables, row_counts) and changing it would be a
    wider, riskier edit than adding a sibling lookup."""
    out: dict = {}
    modules, _errors = artifact_runner.load_artifacts(platform)
    for _script_name, mod in modules:
        app_id = None
        location = None
        if platform == 'android':
            app_path = getattr(mod, 'app_path', None)
            if app_path and app_path.startswith('data/data/'):
                app_id = app_path.rsplit('/', 1)[-1]
                location = app_path
        else:
            app_id = getattr(mod, 'app_group', None)
            location = app_id
        if not app_id:
            continue
        has_media = bool(getattr(mod, 'media_fields', None))
        entry = out.setdefault(app_id, {'app_path_or_group': location, 'has_media_fields': False})
        entry['has_media_fields'] = entry['has_media_fields'] or has_media
    return out


# ── Scoring ───────────────────────────────────────────────────────────────────

def compute_interest_score(app_id: str, *, has_parser: bool, total_bytes: int,
                           last_activity, archive_max_mtime,
                           category: str | None, permissions_declared: list,
                           raw_content_enabled: bool) -> tuple:
    """Deterministic 0-10 score. Returns (total, breakdown, recently_used)
    where breakdown names every component and why it scored what it did —
    an examiner or AI client should never have to trust a bare number.
    recently_used is surfaced as its own clear top-level field in
    scan_apps' output too, not just buried in the activity_signal
    breakdown — added 2026-08-23 after testing showed an offline LLM
    doing its own open-ended investigation performed much better once it
    didn't have to derive "was this recently used" from raw timestamps
    itself."""
    breakdown = {}

    lower_id = (app_id or '').lower()
    known_comms = any(s in lower_id for s in _KNOWN_COMMS_SUBSTRINGS)
    known_vault = any(s in lower_id for s in _KNOWN_VAULT_SUBSTRINGS)
    known_cloud = any(s in lower_id for s in _KNOWN_CLOUD_STORAGE_SUBSTRINGS)
    category_comms = bool(category) and any(
        w in category.lower() for w in ('social', 'photo'))
    category_noncomms = bool(category) and any(
        w in category.lower() for w in ('game', 'utilit'))
    if known_comms or known_vault or known_cloud or category_comms:
        comms_score = 4
        comms_reason = (
            'matches known vault/hide-and-lock app list' if known_vault else
            'matches known cloud-storage/sync app list' if known_cloud else
            'matches known comms/social app list' if known_comms else
            f'declared category {category!r} is comms-adjacent')
    elif category_noncomms and not permissions_declared:
        # Changed 2026-08-24 (was: comms_score = 0, "confirmed non-comms").
        # That actively PENALIZED exactly the profile a vault app is built
        # to present — Utilities/Calculator category, no camera/mic/
        # contacts/location permission declared, specifically so it looks
        # uninteresting. A declared category the examiner can't verify
        # against real content (raw_content_enabled or not, "Utilities" is
        # still just a self-reported App Store label) is not evidence an
        # app has nothing worth investigating — treat it as undetermined,
        # same as the true-unknown branch below, not as confirmed-boring.
        comms_score = 2
        comms_reason = (f'declared category {category!r} with no sensitive '
                        'permission declared is not itself evidence this app '
                        'has nothing worth investigating (e.g. a vault app '
                        'deliberately looks like this) — treated as undetermined')
    else:
        comms_score = 2
        comms_reason = ('category/identity undetermined' if raw_content_enabled
                        else 'raw content disabled — category unknown')
    # Renamed from 'comms_signal' 2026-08-24 — this axis now also covers
    # known vault and cloud-storage apps, not just comms/social, so the old
    # name undersold what it measures.
    breakdown['high_interest_category'] = {'score': comms_score, 'max': 4, 'reason': comms_reason}

    if permissions_declared:
        perm_score = 3
        perm_reason = f'declares access to: {", ".join(permissions_declared)}'
    elif raw_content_enabled:
        perm_score = 0
        perm_reason = 'no camera/mic/contacts/location permission declared'
    else:
        perm_score = 0
        perm_reason = 'raw content disabled — permissions not checked'
    breakdown['sensitive_permission'] = {'score': perm_score, 'max': 3, 'reason': perm_reason}

    gap_score = 0 if has_parser else 2
    breakdown['coverage_gap'] = {
        'score': gap_score, 'max': 2,
        'reason': 'no artifact parser covers this app' if gap_score else 'already parsed'}

    recent = (last_activity is not None and archive_max_mtime is not None
             and (archive_max_mtime - last_activity) <= _ACTIVITY_RECENT_WINDOW_NS)
    active_score = 1 if (total_bytes > _ACTIVITY_MIN_BYTES and recent) else 0
    breakdown['activity_signal'] = {
        'score': active_score, 'max': 1,
        'reason': (f'>{_ACTIVITY_MIN_BYTES // (1024*1024)}MB with activity in the '
                   "case's active window" if active_score else
                   'below size threshold or no recent activity relative to this case')}

    total = comms_score + perm_score + gap_score + active_score
    return total, breakdown, recent


# ── Orchestration ─────────────────────────────────────────────────────────────

def _format_last_activity(mtime_ns: int | None) -> str | None:
    """Labeled, human-readable UTC string for a raw nanosecond-epoch mtime
    (the unit ui_metadata stores throughout this project — confirmed in
    adapters/ffs.py's own extraction code), or None if there's no activity
    to report. Added 2026-08-25 after a user question about whether an
    examiner could actually tell "last used date" from this tool's output
    — the raw last_activity field alone is an unlabeled nanosecond epoch
    integer, exactly the ambiguous-timestamp shape this project's own
    Conventions section warns against (every evidence timestamp must say
    which category it's in and be UTC-labeled). This is Tier 2 — the same
    'evidence timestamp, always UTC, always labeled' rule from
    timestamp_display.py's Qt-coupled formatter, reimplemented here
    Qt-free (app_intelligence.py has no PySide6 dependency and shouldn't
    gain one) rather than reused directly."""
    if not mtime_ns:
        return None
    dt = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc)
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')


def _walk_container(path: str, folder_map: dict, ui_metadata: dict,
                    read_bytes=None, future_ceiling_ns: int | None = None) -> tuple:
    """(file_count, max_mtime, media_file_count) across every file under
    *path*, walking folder_map once — each file in the archive belongs to
    at most one container, so total cost across ALL containers combined
    is bounded by files_indexed once, not multiplied per container.

    *future_ceiling_ns*, when given (the case's own acquisition_dt — see
    scan_apps' archive_max_mtime comment), excludes an mtime past it from
    max_mtime — a real device file cannot postdate its own imaging moment,
    so a later value is a fabricated cache-library timestamp, not evidence
    of activity. file_count/media_file_count are unaffected — the file
    itself is still real, only its mtime is untrustworthy as an activity
    signal.

    media_file_count classifies each file via header_scan.sniff_media_kind
    (image/video only — matches this project's own 'media' vocabulary
    everywhere else: media_fields, the Media Browser tab), added
    2026-08-25 per direct user request for an accurate per-app media
    count as part of automatic case-load processing (not an opt-in/
    on-demand step). Extension-recognized files (the vast majority — real
    photo/video files almost always keep a real extension) cost NOTHING
    extra: sniff_media_kind's extension branches never touch the `data`
    argument, so passing b'' is enough to resolve them without a single
    byte read. Only a file with NO recognized media/pdf/text extension
    falls through to sniff_media_kind's magic-byte fallback, and even
    then ONLY when *read_bytes* is given and the file is within
    _MAGIC_CHECK_MAX_BYTES — same bounded-cost pattern
    find_evidence_databases already established for its own magic-byte
    fallback, so this never reads every file in a 300k+-file case just to
    classify a handful of ambiguous ones."""
    count = 0
    max_mtime = None
    media_count = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        children = folder_map.get(cur)
        if children is None:
            m = ui_metadata.get(cur)
            if m:
                count += 1
                mt = m.get('mtime')
                if (mt and (future_ceiling_ns is None or mt <= future_ceiling_ns)
                        and (max_mtime is None or mt > max_mtime)):
                    max_mtime = mt
                ext = os.path.splitext(cur)[1].lower()
                kind = sniff_media_kind(ext, b'')
                if kind is None and read_bytes is not None:
                    size_hint = m.get('size', 0)
                    if 0 < size_hint <= _MAGIC_CHECK_MAX_BYTES:
                        data = read_bytes(cur)
                        if data:
                            kind = sniff_media_kind(ext, data[:64])
                if kind in ('image', 'video'):
                    media_count += 1
            continue
        stack.extend(children)
    return count, max_mtime, media_count


def _load_group_owner_index(ctx) -> tuple[dict, dict]:
    """({app_group_id: owning_bundle_id}, {bundle_id: display_name}) from
    the app_registry table (built once at case-load time via
    adapters.ffs.FfsAdapter.build_app_registry — see CLAUDE.md), one
    read shared for both — added 2026-08-24 specifically to fix a real
    scoring gap found this session: an App-Group container row's own
    app_id IS the group identifier (e.g. 'group.ch.threema'), which is
    never a key in ios_bundle_info (keyed by real bundle ids only), so the
    row scored as if it were an identity-less app with unknown category/
    permissions even though its owning app's real category/permissions are
    known. display_name added 2026-08-25 after a user question pointed out
    list_apps only ever showed bundle ids (e.g. 'com.zhiliaoapp.musically')
    with no human-readable app name at all — app_registry already computes
    this from the same LaunchServices read, just wasn't being surfaced.
    Returns ({}, {}) if app_registry hasn't been built yet (an older case,
    opened before this feature existed) — degrades to the previous
    behavior, never raises."""
    group_owner, display_names = {}, {}
    try:
        with closing(_open_cache_db(ctx.case_dir)) as db:
            for row in load_app_registry(db):
                for gid in row['app_group_paths']:
                    group_owner[gid] = row['bundle_id']
                if row.get('display_name'):
                    display_names[row['bundle_id']] = row['display_name']
    except Exception:
        pass
    return group_owner, display_names


def _load_acquisition_ceiling_ns(ctx) -> int | None:
    """The case's own acquisition instant, as a UTC nanosecond epoch, or
    None if never recorded (GrayKey, or an older case). A real device file
    cannot postdate the moment its own device was imaged, so this is a
    genuine ceiling for excluding a fabricated future mtime — see
    scan_apps' archive_max_mtime comment.

    case_settings' acquisition_dt (caseresults.db, set best-effort at first
    case-load from the .ufd, Cellebrite only) is the acquisition
    WORKSTATION's own naive LOCAL clock reading, not UTC — confirmed via
    device_timezone.detect_acquisition_offset's own docstring ("acquired_at
    (naive, no tzinfo — it IS the local reading)"). acquisition_offset_hours
    (stored alongside it, same source line) is the signed UTC offset at
    that moment, so true UTC = local − offset (offset −4 ⇒ UTC = local + 4h)
    — verified against this case's own real data: applying it lands ~2m37s
    BEFORE WhatsApp's own last real activity, exactly where the acquisition
    instant should sit; treating acquisition_dt as UTC directly would have
    wrongly excluded a 4-hour window of genuine same-day activity instead.

    Best-effort like every other lookup in this module: any parse failure
    just means no cap gets applied, never raises."""
    try:
        with closing(_open_results_db(ctx.case_dir)) as db:
            raw_dt = load_case_setting(db, 'acquisition_dt')
            raw_offset = load_case_setting(db, 'acquisition_offset_hours')
        if not raw_dt or not raw_offset:
            return None
        local_dt = datetime.fromisoformat(raw_dt).replace(tzinfo=timezone.utc)
        utc_dt = local_dt - timedelta(hours=float(raw_offset))
        return int(utc_dt.timestamp() * 1_000_000_000)
    except Exception:
        return None


def scan_apps(ctx) -> list:
    """Full per-app intelligence sweep. Tier 2 fields (file_count,
    last_activity, has_parser, row_count) are always computed; category/
    permissions_declared are only filled when ctx.raw_content_enabled —
    otherwise left None and scored as 'unknown', never as confirmed-absent.
    """
    platform = 'android' if (ctx.adapter and
        ctx.adapter.format == ctx.adapter.FORMAT_ZIP_EXTRAS) else 'ios'
    coverage = resolve_parser_coverage(platform)
    parser_locations = resolve_parser_locations(platform)

    guid_map = ctx.get_guid_to_bundle()
    folder_map = ctx.get_folder_map()
    ui_metadata = ctx.get_ui_metadata()
    sizes = ctx.get_folder_sizes()
    parents = ctx.adapter.container_parents() if ctx.adapter else []

    # A file's own mtime can legitimately postdate this analysis machine's
    # clock (an old case reopened later) but can NEVER legitimately postdate
    # the moment the device was imaged — confirmed on real casework
    # (2026-08-26, IOS17 JoshHickman) that several unrelated apps' own
    # third-party disk-cache libraries (a Realm/disk-cache-style store, and
    # separately Kingfisher, a common Swift image-cache library) write cache
    # entries with a fabricated far-future mtime (one app's cache uniformly
    # stamped 2037-12-15, another's landed after the case's own acquisition
    # date) — not corruption, a deliberate "don't evict" cache convention,
    # but poisonous to a plain max(): it silently overstated that one app's
    # last_activity by 13 years, AND (since this is a single case-wide
    # value every app's recently_used flag is compared against) made every
    # OTHER app's real, same-day activity read as "not recent" too. Capped
    # at the case's own acquisition_dt (case_settings, set best-effort at
    # first case-load from the .ufd — Cellebrite only, see device_timezone.py)
    # when known; a device file cannot postdate its own imaging moment, so
    # this is a real ceiling, not a guess. Silently unavailable (GrayKey, or
    # an older case from before acquisition_dt was tracked) simply skips the
    # cap rather than raising — same degrade-gracefully convention as the
    # rest of this module.
    _acquisition_ceiling_ns = _load_acquisition_ceiling_ns(ctx)

    def _real_mtime(m: dict) -> int | None:
        mt = m.get('mtime')
        if not mt:
            return None
        if _acquisition_ceiling_ns is not None and mt > _acquisition_ceiling_ns:
            return None
        return mt

    archive_max_mtime = max(
        (v for v in (_real_mtime(m) for m in ui_metadata.values()) if v),
        default=None)

    packages_root = _load_packages_xml(ctx) if (
        platform == 'android' and ctx.raw_content_enabled) else None
    runtime_perms_root = _load_runtime_permissions_xml(ctx) if (
        platform == 'android' and ctx.raw_content_enabled) else None
    ios_bundle_info = {}
    if platform == 'ios' and ctx.raw_content_enabled:
        ios_bundle_info = scan_ios_bundle_containers(
            ctx, find_bundle_container_parent(folder_map), folder_map)
    group_owner, display_names = (
        _load_group_owner_index(ctx) if platform == 'ios' else ({}, {}))

    row_counts = {}
    if coverage:
        import os
        import sqlite3
        try:
            conn = sqlite3.connect(
                f"file:{os.path.join(ctx.case_dir, 'caseresults.db')}?mode=ro",
                uri=True, timeout=5)
            for app_id, scripts in coverage.items():
                total = 0
                for s in scripts:
                    try:
                        total += conn.execute(
                            f'SELECT COUNT(*) FROM "artifact_{s}"').fetchone()[0]
                    except sqlite3.OperationalError:
                        pass  # parser loaded but never run on this case
                row_counts[app_id] = total
            conn.close()
        except Exception:
            pass

    # Pass 1: resolve every container to its OWN app_id, tagged by which
    # root it came from. Data/Application and Shared/AppGroup containers
    # for the SAME app are then merged into one row (2026-08-24, prompted
    # by a user question after the Telegram gap below): previously each
    # container became its own row keyed by its own resolved app_id, and
    # since an App-Group container's own app_id IS the opaque group
    # identifier (e.g. 'group.ph.telegra.Telegraph') rather than the real
    # bundle id ('ph.telegra.Telegraph'), an LLM reading list_apps had no
    # way to know two unrelated-looking rows were the same app — confirmed
    # concretely: Telegram's real store lives entirely in its App-Group
    # container, while its Data/Application row (which an LLM would
    # naturally check first, since it matches the bundle id it already
    # knows) showed nothing at all. Grouped via group_owner (already built
    # above for the scoring identity fix — no second lookup mechanism
    # needed). PluginKitPlugin containers (extensions)
    # deliberately stay as their OWN separate rows — they're genuinely
    # separate components, and unlike an AppGroup's opaque id, an
    # extension's own app_id is already self-describing (a dotted suffix
    # of the host app's, e.g. 'net.whatsapp.WhatsApp.ServiceExtension').
    containers = []  # (own_app_id, child_path, kind)
    for parent in parents:
        kind = ('app_group' if parent.endswith('AppGroup')
                else 'plugin' if parent.endswith('PluginKitPlugin')
                else 'data')
        for child in folder_map.get(parent, []):
            own_app_id = ctx.adapter.container_bundle_id(child, guid_map) if ctx.adapter else None
            if own_app_id:
                containers.append((own_app_id, child, kind))

    groups: dict[str, list[tuple[str, str, str]]] = {}  # identity -> [(own_app_id, child, kind)]
    for own_app_id, child, kind in containers:
        identity = (group_owner.get(own_app_id, own_app_id)
                   if platform == 'ios' and kind != 'plugin' else own_app_id)
        groups.setdefault(identity, []).append((own_app_id, child, kind))

    out = []
    for app_id, members in groups.items():
        member_ids = {m[0] for m in members}
        file_count = 0
        last_activity = None
        total_bytes = 0
        # Per-kind split alongside the merged value above (added
        # 2026-08-25, GUI Apps table): a 'data' container (the app's own
        # sandbox) and an 'app_group' container (Shared/AppGroup) can have
        # very different last-touched times — an app whose OWN data is
        # stale but whose shared/App-Group store is fresh (or vice versa)
        # loses that distinction once collapsed into one merged max, which
        # is what `last_activity` above still does for backward
        # compatibility with existing MCP list_apps callers. 'plugin' kind
        # never appears here — see the identity-grouping comment above
        # this loop: a PluginKitPlugin container always forms its OWN
        # separate group/row, never a member of its host app's.
        # last_activity_data/last_activity_shared (2026-08-25, GUI Apps
        # table) were REMOVED 2026-08-26, same day investigated, rather
        # than patched: both were a per-container max() over every file's
        # own mtime — exactly the shape the archive_max_mtime comment above
        # describes as poisoned by a real, recurring case of third-party
        # disk-cache libraries writing files with a fabricated far-future
        # mtime (confirmed on real casework — BeReal's own cache library
        # stamped 65 files 2037-12-15; a second, unrelated app's Kingfisher
        # image cache landed after the case's own acquisition date). Capping
        # at the acquisition ceiling (as archive_max_mtime now does) would
        # have fixed the worst case, but a max-of-every-file-in-a-container
        # approach has no way to tell a real "quietly wrote a cache entry
        # this session" write from an app simply being installed and never
        # opened again — the whole design was a ballpark with no honest way
        # to label its own error bars. Replaced with three narrower,
        # individually-labeled signals per direct user design guidance
        # (2026-08-26): data_created/shared_created (each container's own
        # creation time — btime, NOT mtime; confirmed byte-exact against
        # this case's own ground-truth documentation for install date/time
        # on 3 independently-checked apps) as an honest "first seen on this
        # device" data point, plus preferences_modified/
        # splash_snapshot_modified — two specific, generically-present iOS
        # files (not container-wide maxes) whose own mtime was confirmed
        # against the same ground-truth documentation to closely track
        # (within single minutes on 3/3 apps for the SplashBoard snapshot;
        # within minutes on 2/3, ~50min undershoot on the 3rd, for the
        # Preferences plist) each app's real last-used date — each
        # labeled by its own source in the GUI so an examiner can judge
        # its reliability directly, rather than trusting one opaque merged
        # number. See ArtifactViewerMixin's "Application Report Notes"
        # node for the caveats shown to the examiner.
        data_created = None
        shared_created = None
        preferences_modified = None
        splash_snapshot_modified = None
        media_file_count = 0
        # Shared with the evidence-database block further below — computed
        # once here since _walk_container's own accurate media count now
        # needs it too, not just find_evidence_databases' magic-byte
        # fallback. None (no raw reads at all) when raw_content_enabled is
        # off — the GUI's own automatic case-load scan always passes True
        # here (see artifact_viewer.py's _art_show_apps), so this only
        # actually degrades an AI client's own consent-restricted
        # list_apps call, never the app's own internal processing.
        _rb = ctx.read_bytes if ctx.raw_content_enabled else None
        for _own_id, child, _kind in members:
            fc, la, mc = _walk_container(child, folder_map, ui_metadata,
                                         read_bytes=_rb,
                                         future_ceiling_ns=_acquisition_ceiling_ns)
            file_count += fc
            media_file_count += mc
            if la is not None and (last_activity is None or la > last_activity):
                last_activity = la
            container_meta = ui_metadata.get(child)
            cbt = container_meta.get('btime') if container_meta else None
            if _kind == 'data':
                if cbt and (data_created is None or cbt < data_created):
                    data_created = cbt
                # bundle id filename convention (Library/Preferences/<bundle
                # id>.plist) — confirmed against real casework; _own_id is
                # always == app_id for a 'data' member (group_owner only
                # remaps an app_group id, never a plain bundle id — see the
                # identity-grouping comment above), app_id used directly for
                # clarity rather than relying on that always-equal identity.
                pm = ui_metadata.get(f'{child}/Library/Preferences/{app_id}.plist')
                if pm and pm.get('mtime') and (preferences_modified is None
                                               or pm['mtime'] > preferences_modified):
                    preferences_modified = pm['mtime']
                _sfc, sla, _smc = _walk_container(
                    f'{child}/Library/SplashBoard/Snapshots/sceneID:{app_id}-default',
                    folder_map, ui_metadata, future_ceiling_ns=_acquisition_ceiling_ns)
                if sla is not None and (splash_snapshot_modified is None
                                        or sla > splash_snapshot_modified):
                    splash_snapshot_modified = sla
            elif _kind == 'app_group':
                if cbt and (shared_created is None or cbt < shared_created):
                    shared_created = cbt
            total_bytes += sizes.get(child, 0)
        has_parser = bool(member_ids & set(coverage))
        artifact_tables = next((coverage[mid] for mid in member_ids if mid in coverage), [])
        row_count = next((row_counts[mid] for mid in member_ids if mid in row_counts), None)
        known_location = next(
            (parser_locations[mid] for mid in member_ids if mid in parser_locations), None)

        category = permissions = plist_display_name = None
        if ctx.raw_content_enabled:
            if platform == 'ios':
                meta = ios_bundle_info.get(app_id)
                if not meta:
                    # This identity itself may still be an unresolved App
                    # Group id (group_owner had no entry for it — an older
                    # case, or app_registry missing this app), never a key
                    # in ios_bundle_info either way. See _load_group_owner_index.
                    meta = ios_bundle_info.get(group_owner.get(app_id))
                if meta:
                    category = meta['category']
                    permissions = meta['permissions_declared']
                    plist_display_name = meta.get('display_name')
            else:
                permissions = read_android_permissions(ctx, app_id, runtime_perms_root)
                category = read_android_category(app_id, packages_root)

        score, breakdown, recently_used = compute_interest_score(
            app_id, has_parser=has_parser, total_bytes=total_bytes,
            last_activity=last_activity, archive_max_mtime=archive_max_mtime,
            category=category, permissions_declared=permissions or [],
            raw_content_enabled=ctx.raw_content_enabled)

        # Evidence-database/encryption-caveat lookups only matter for
        # triaging an UNPARSED app (an already-parsed app's real store is
        # already known) — skipping them for has_parser=True apps saves
        # the folder_map walk for the ~90% of containers that don't need
        # it. Pooled across every member container, then re-ranked
        # together, so a real store in the App-Group container is never
        # shadowed by noise in the Data container or vice versa.
        evidence_dbs = []
        evidence_dbs_total = 0
        webview_matches = []
        vault_matches = []
        caveat = None
        if not has_parser:
            # _rb already computed above, before the file-count walk.
            for _own_id, child, _kind in members:
                # limit high here (not the row's final 5) so total_found
                # reflects the TRUE count before this row's own cutoff —
                # see find_evidence_databases' docstring for why that
                # matters (TikTok: 34 real candidates, 2 real files ranked
                # #7/#12, both invisible if truncated per-container first).
                cands, total = find_evidence_databases(
                    child, folder_map, ui_metadata, limit=1000, read_bytes=_rb)
                evidence_dbs.extend(cands)
                evidence_dbs_total += total
                wv = find_webview_storage(child, folder_map, ui_metadata)
                if wv:
                    webview_matches.append(wv)
                vs = find_hidden_vault_storage(child, folder_map, ui_metadata)
                if vs:
                    vault_matches.append(vs)
            evidence_dbs.sort(key=lambda e: e['bytes'] + e['wal_bytes'], reverse=True)
            evidence_dbs = evidence_dbs[:5]
            caveat = find_encryption_caveat(app_id)
        webview_storage = None
        if webview_matches:
            webview_matches.sort(key=lambda m: m['bytes'], reverse=True)
            best = webview_matches[0]
            other = sum(m.get('other_stores', 0) for m in webview_matches) + len(webview_matches) - 1
            webview_storage = {'path': best['path'], 'bytes': best['bytes'], 'other_stores': other}
        hidden_vault_storage = None
        if vault_matches:
            vault_matches.sort(key=lambda m: m['bytes'], reverse=True)
            best = vault_matches[0]
            other = sum(m.get('other_stores', 0) for m in vault_matches) + len(vault_matches) - 1
            hidden_vault_storage = {'path': best['path'], 'bytes': best['bytes'], 'other_stores': other}

        out.append({
            'platform': platform, 'app_id': app_id,
            # plist_display_name (Info.plist's CFBundleDisplayName, or
            # iTunesMetadata's itemName — see scan_ios_bundle_containers)
            # preferred over app_registry's csstore-sourced name: confirmed
            # on real casework (2026-08-26) that the LaunchServices name
            # field can be the app's internal executable/bundle name rather
            # than its home-screen name (org.mozilla.ios.Firefox read
            # 'Client', its actual .app folder name) or simply blank
            # (com.viber) — only reachable when raw_content_enabled, so
            # falls back to the always-available csstore name, then to the
            # bare bundle id (unchanged final fallback, artifact_viewer.py's
            # _flatten_app_intelligence_row).
            'display_name': plist_display_name or display_names.get(app_id),
            'containers': [{'app_id': mid, 'path': child, 'kind': kind}
                          for mid, child, kind in members],
            'total_bytes': total_bytes, 'file_count': file_count,
            'media_file_count': media_file_count,
            'last_activity': last_activity,
            'last_activity_utc': _format_last_activity(last_activity),
            'data_created': data_created,
            'data_created_utc': _format_last_activity(data_created),
            'shared_created': shared_created,
            'shared_created_utc': _format_last_activity(shared_created),
            'preferences_modified': preferences_modified,
            'preferences_modified_utc': _format_last_activity(preferences_modified),
            'splash_snapshot_modified': splash_snapshot_modified,
            'splash_snapshot_modified_utc': _format_last_activity(splash_snapshot_modified),
            'has_parser': has_parser,
            'artifact_tables': artifact_tables,
            'row_count': row_count,
            'category': category, 'permissions_declared': permissions or [],
            'score': score, 'score_breakdown': breakdown,
            'recently_used': recently_used,
            'evidence_databases': evidence_dbs,
            'evidence_databases_total': evidence_dbs_total,
            'known_location': known_location,
            'webview_storage': webview_storage,
            'hidden_vault_storage': hidden_vault_storage,
            'encryption_caveat': caveat,
            'scanned_at': int(time.time()),
        })

    # De-dup by (platform, app_id) — app_intelligence's PRIMARY KEY assumes
    # one container per app, which real devices can violate: confirmed on
    # the iOS 18 CTF25 Magnet image, a system extension
    # (com.apple.poirot.SearchAnalyticsWorker) has its own PluginKitPlugin
    # container AND an App-Group container whose metadata plist names that
    # same bundle id directly rather than a 'group.*' identifier — both
    # resolve to the same app_id here, which used to crash
    # save_app_intelligence's INSERT. Keep the larger container (more
    # total_bytes, tie-broken by file_count) as the representative row.
    if len(out) != len({(d['platform'], d['app_id']) for d in out}):
        best: dict[tuple, dict] = {}
        for d in out:
            key = (d['platform'], d['app_id'])
            cur = best.get(key)
            if cur is None or (d['total_bytes'], d['file_count']) > (cur['total_bytes'], cur['file_count']):
                best[key] = d
        out = list(best.values())

    # Tie-break beyond raw score so the FRONT of the list needs minimal
    # further thinking, per the explicit design goal (2026-08-23), updated
    # 2026-08-24 for the multi-candidate evidence_databases list and again
    # the same day for hidden_vault_storage: a HIDDEN VAULT FOLDER match
    # ranks highest of all — a confirmed vault-storage folder signature is
    # about as strong a signal as this project computes without opening a
    # file (deliberately built to hide something, found anyway). Below
    # that: a clean (no-note) evidence_databases hit, then a lower-
    # confidence WebKit-fallback hit or a mere webview_storage detection,
    # then nothing at all. A known encryption caveat is pushed down (real,
    # but not a quick-win parser target); then recently-used; then size.
    # Deliberately does NOT rank by an external citation (known_real_store
    # was removed from evidence_databases 2026-08-24) — this ordering
    # reflects only what the mechanism itself computed, not an answer key.
    def _evidence_rank(d):
        if d['hidden_vault_storage']:
            return 3
        dbs = d['evidence_databases']
        if not dbs:
            return 1 if d['webview_storage'] else 0
        if not dbs[0]['note']:
            return 2
        return 1

    out.sort(key=lambda d: (
        d['score'],
        _evidence_rank(d),
        0 if d['encryption_caveat'] else 1,
        1 if d['recently_used'] else 0,
        d['total_bytes'],
    ), reverse=True)
    return out
