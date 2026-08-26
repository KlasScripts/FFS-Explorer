"""leapp_evidence_fixtures.py — ground-truth fixtures for VALIDATING
app_intelligence.find_evidence_databases' ranking mechanism, not a runtime
dependency of it.

Moved here from app/known_evidence_patterns.py 2026-08-25 after a design
question: an earlier version had this data feeding a `known_real_store`
field directly into find_evidence_databases' live output. That was removed
the same day it shipped — baking a per-app answer key into live ranking
risks testing whether the answer key is right, not whether the general
mechanism (size/WAL comparison, noise filtering, magic-byte detection for
extensionless files) actually works on its own. iLEAPP and ALEAPP are also
themselves LIVE, actively-maintained GitHub projects — people add and change
parsers constantly — so an answer key baked into shipped runtime code goes
stale in a way a periodically-rerun validation script doesn't. See
validate_evidence_ranking.py in this directory, which loads a real case and
checks whether find_evidence_databases' unaided ranking surfaces each fixture
path within the top N candidates for its app — that's the actual test this
data exists for now.

NOT vendored code — nothing here is copied from either project's source;
only the glob PATTERNS their own artifact-extraction scripts target are
extracted as plain data, individually cited per entry. Mined by hand
2026-08-24 from two permissively-licensed (MIT, Alexis Brignoni) public
tools' own scripts/artifacts/*.py: iLEAPP (github.com/abrignoni/iLEAPP, iOS)
and ALEAPP (github.com/abrignoni/ALEAPP, Android), checked out locally at
/Users/klastveita/script/iLEAPP-main and /Users/klastveita/script/ALEAPP-main
respectively as of that date — a SNAPSHOT of those live projects, not a
frozen spec; re-pull and re-check before trusting an entry here is still
current if much time has passed. A handful of entries cite this project's
own prior real-casework findings (see CLAUDE.md) instead, where no
iLEAPP/ALEAPP module covers that app.

Deliberately NOT limited to messaging apps — extended 2026-08-24 to cover
vault (hide-and-lock) and cloud-storage apps too.

Deliberately NOT exhaustive — covers only the apps this project has
concretely cross-checked so far, and only where the exact bundle id /
package name is independently confirmed (either against real case data, or
a literal match in the source tool's own sample_data comments) — never
guessed. Extend this table by hand as more apps get worked.

Two tables:
  KNOWN_EVIDENCE_PATTERNS  — {platform: {app_id: [{pattern, source}]}},
    checked first: precise, tied to a confirmed bundle id/package name.
  UNSCOPED_EVIDENCE_PATTERNS — {platform: [{pattern, source}]}, checked as a
    fallback regardless of which container app_id the path was found under.
    Exists because a real, confirmed source tool pattern doesn't always come
    with a confirmed owning app_id to key it by — two different reasons this
    happens, both real, not a design shortcut:
      1. A vault app's whole point is to look like something else. iLEAPP's
         own nsVault.py notes its identity is "inferred from the
         FolderLockAdvanced.sqlite filename... not bundle-specific" — the
         SOURCE TOOL itself doesn't scope by bundle id here, so scoping our
         own cross-reference more tightly than iLEAPP's own would just be
         inventing false precision.
      2. iLEAPP/ALEAPP scan the whole filesystem and never needed per-app
         container context the way this project's container-walk does, so
         several of their own patterns (Chrome/Drive/OneDrive path globs)
         are written bare, with no app-container prefix, even though a real
         confirmed owning app does exist — the pattern is real and citable,
         the id mapping just isn't independently confirmed here yet.

Patterns use iLEAPP/ALEAPP's own glob syntax verbatim (fnmatch-compatible;
matched here against the ui_path with a leading '/' prepended, since their
patterns assume a rooted filesystem view, e.g.
'*/AppGroup/*/ChatStorage.sqlite*')."""

import fnmatch

# Local checkout paths as of 2026-08-24 — used by validate_evidence_ranking.py
# and leapp_coverage_report.py to locate the live source for re-scanning.
# Both are snapshots of live GitHub projects; re-clone/pull periodically.
ILEAPP_PATH = '/Users/klastveita/script/iLEAPP-main'
ALEAPP_PATH = '/Users/klastveita/script/ALEAPP-main'

KNOWN_EVIDENCE_PATTERNS: dict[str, dict[str, list[dict]]] = {
    'ios': {
        'net.whatsapp.WhatsApp': [
            {'pattern': '*/AppGroup/*/ChatStorage.sqlite*',
             'source': 'iLEAPP:whatsApp.py:whatsAppMessages'},
        ],
        'com.zhiliaoapp.musically': [
            {'pattern': '*AwemeIM.db*',
             'source': 'iLEAPP:tikTok.py:tiktok_messages (messages/contacts)'},
            {'pattern': '*/Application/*/Library/Application Support/ChatFiles/*/db.sqlite*',
             'source': 'iLEAPP:tikTok.py:tiktok_messages'},
        ],
        'com.burbn.instagram': [
            {'pattern': '*/Application/*/Library/Application Support/DirectSQLiteDatabase/*.db*',
             'source': 'iLEAPP:instagramThreads.py:instagram_threads'},
        ],
        'com.toyopagroup.picaboo': [
            {'pattern': '*/Application/*/Documents/user_scoped/*/arroyo/arroyo.db*',
             'source': 'iLEAPP:snapchat.py:snapchatMessages'},
        ],
        'org.whispersystems.signal': [
            {'pattern': '*/AppGroup/*/grdb*/signal.sqlite*',
             'source': 'iLEAPP:signalIOS.py:get_signalIOSMessages'},
        ],
        'ch.threema.iapp': [
            {'pattern': '*/AppGroup/*/ThreemaData.sqlite*',
             'source': 'iLEAPP:Threema.py:threema_chats'},
        ],
        'com.google.chrome.ios': [
            {'pattern': '*/Chrome/Default/History*',
             'source': 'iLEAPP:chrome.py (bundle id confirmed via its own sample_data)'},
        ],
        'ph.telegra.Telegraph': [
            # Bundle id confirmed against this project's own real casework
            # (iOS 16.5 CTF23 Cellebrite), not iLEAPP's sample_data. The
            # file itself has NO extension ('db_sqlite', not '.sqlite') —
            # invisible to find_evidence_databases until the magic-byte
            # fallback (2026-08-24) was added specifically because of this
            # app; confirmed real via get_sqlite_schema (opens cleanly,
            # substantial row counts) and matches iLEAPP's telegramMesssages.py
            # path shape ('telegram-data/account-*/postbox/db/db_sqlite')
            # exactly, found independently before this entry was added.
            {'pattern': '*/telegram-data/account-*/postbox/db/db_sqlite',
             'source': 'iLEAPP:telegramMesssages.py + ios-ffs-browser casework '
                      '2026-08-24 (iOS 16.5 CTF23 Cellebrite)'},
        ],
    },
    'android': {
        'com.whatsapp': [
            {'pattern': '*/com.whatsapp/databases/msgstore.db*',
             'source': 'ALEAPP:WhatsApp.py (messages)'},
            {'pattern': '*/com.whatsapp/databases/wa.db*',
             'source': 'ALEAPP:WhatsApp.py (contacts)'},
        ],
        'com.snapchat.android': [
            {'pattern': '*/com.snapchat.android/databases/arroyo.db*',
             'source': 'ALEAPP:snapchat.py'},
            {'pattern': '*/com.snapchat.android/databases/main.db*',
             'source': 'ALEAPP:snapchat.py'},
        ],
        'org.thoughtcrime.securesms': [
            {'pattern': '*/org.thoughtcrime.securesms/databases/signal.db*',
             'source': 'ALEAPP:signalAndroid.py'},
        ],
        'org.telegram.messenger': [
            {'pattern': '*/org.telegram.messenger*/files/cache4.db*',
             'source': 'ALEAPP:telegramAndroid.py'},
        ],
        'ch.threema.app': [
            # No Threema module in ALEAPP — this project's own confirmed
            # real-casework finding instead (see CLAUDE.md's noise-exclusion
            # section): threema4.db beat Mapbox's mbgl-offline.db on size
            # (1.98MB vs 282KB) before mbgl-offline.db was excluded,
            # 2026-08-23, Android 14 JoshHickman.
            {'pattern': '*/ch.threema.app/databases/threema4.db*',
             'source': 'ios-ffs-browser casework 2026-08-23 (Android 14 JoshHickman)'},
        ],
        # Vault (hide-and-lock) apps — package names confirmed literally in
        # each module's own path pattern (Android package names double as
        # the on-disk data/data/<package> folder name).
        'com.thinkyeah.galleryvault': [
            {'pattern': '*/com.thinkyeah.galleryvault/databases/galleryvault.db*',
             'source': 'ALEAPP:galleryVault.py'},
        ],
        'com.theronrogers.vaultyfree': [
            {'pattern': '*/com.theronrogers.vaultyfree/databases/media.db*',
             'source': 'ALEAPP:vaulty_files.py'},
        ],
        'com.calculator.lock.hide.photo.video': [
            {'pattern': '*/com.calculator.lock.hide.photo.video/databases/note_contact.db*',
             'source': 'ALEAPP:calculatorLockVault.py'},
        ],
        # Cloud storage — package names confirmed the same way.
        'com.dropbox.android': [
            {'pattern': '*/com.dropbox.android/databases/*-db.db*',
             'source': 'ALEAPP:dropbox.py'},
        ],
        'com.microsoft.skydrive': [
            {'pattern': '*/com.microsoft.skydrive/files/QTMetadata.db*',
             'source': 'ALEAPP:OneDrive_Metadata.py'},
        ],
        'me.proton.android.drive': [
            {'pattern': '*/me.proton.android.drive/databases/db-drive*',
             'source': 'ALEAPP:ProtonDrive.py'},
        ],
        'com.google.android.apps.docs': [
            {'pattern': '*/com.google.android.apps.docs/app_cello/*/cello.db*',
             'source': 'ALEAPP:Cello.py'},
            {'pattern': '*/com.google.android.apps.docs/databases/DocList.db*',
             'source': 'ALEAPP:DocList.py'},
        ],
    },
}

# See the module docstring's "Two tables" section for why these can't be
# scoped by app_id the same way as KNOWN_EVIDENCE_PATTERNS above.
UNSCOPED_EVIDENCE_PATTERNS: dict[str, list[dict]] = {
    'ios': [
        {'pattern': '*/Library/FolderLockAdvanced.sqlite*',
         'source': "iLEAPP:nsVault.py (a vault app; iLEAPP's own notes say "
                   "this identity is inferred from the filename, not a "
                   "bundle id, so this project can't scope it by app_id "
                   "either)"},
        {'pattern': '*/Documents/drivekit/users/*/*cello/cello.db*',
         'source': 'iLEAPP:googleDrive.py:google_drive_accounts (Google '
                   'Drive iOS — its own pattern has no app-container prefix '
                   'either; real bundle id not independently confirmed here)'},
        {'pattern': '*/DatabaseQ[Tt]/QTMetadata.db*',
         'source': 'iLEAPP:oneDrive.py:one_drive_files (OneDrive iOS — same '
                   'caveat as Google Drive above)'},
    ],
    'android': [],
}


def match_known_pattern(platform: str, app_id: str, ui_path: str) -> str | None:
    """Return the citing source string if *ui_path* matches one of app_id's
    known-real patterns, else None. Falls back to UNSCOPED_EVIDENCE_PATTERNS
    (checked regardless of app_id) if no scoped match is found.

    Used only by validate_evidence_ranking.py now — NOT imported by
    app/app_intelligence.py. See module docstring."""
    probe = ui_path if ui_path.startswith('/') else '/' + ui_path
    entries = KNOWN_EVIDENCE_PATTERNS.get(platform, {}).get(app_id) or []
    for entry in entries:
        if fnmatch.fnmatch(probe, entry['pattern']):
            return entry['source']
    for entry in UNSCOPED_EVIDENCE_PATTERNS.get(platform, []):
        if fnmatch.fnmatch(probe, entry['pattern']):
            return entry['source']
    return None


def iter_fixtures():
    """Yield (platform, app_id, pattern, source) for every scoped fixture —
    the flat form validate_evidence_ranking.py iterates over. Unscoped
    entries are excluded (no app_id to check a case's app rows against)."""
    for platform, apps in KNOWN_EVIDENCE_PATTERNS.items():
        for app_id, entries in apps.items():
            for entry in entries:
                yield platform, app_id, entry['pattern'], entry['source']
