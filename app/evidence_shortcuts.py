"""evidence_shortcuts.py — the "Jump to ▾" menu's locations, and how each one
is found in the open extraction. Qt-free.

Where evidence lives moves between OS versions (EVIDENCE_LOCATIONS.md, with
sources). So a shortcut lists every location it can be in, newest first,
and the menu opens the first one this extraction has. If it has none, the
entry is shown as "not in this extraction" with the paths that were tried.
An empty or missing folder can mean the location moved in an OS version
not checked yet, not that there is no evidence.

Each leaf is (label, candidates). *candidates* is one path or a tuple of
them. A candidate is a folder or file ui_path written the Cellebrite way
(iOS: no 'private/var/'; FfsAdapter.prefix_shortcut adds it on GrayKey).
A candidate containing '*' is a file pattern (fnmatch): the jump opens the
folder of the first matching file, e.g. Notes, whose NoteStore.sqlite sits
in an app group container with a different UUID on every phone.

Version labels are given only where a source supports them
(EVIDENCE_LOCATIONS.md); unverified boundaries are left out of the label.
"""
from fnmatch import fnmatchcase

IOS_SHORTCUTS = [
    ("App Data", "mobile/Containers/Data/Application"),
    ("App Data Plugins", "mobile/Containers/Data/PluginKitPlugin"),
    ("App Data Shared", "mobile/Containers/Shared/AppGroup"),
    None,  # separator
    ("Communications", [
        ("SMS / iMessage",          "mobile/Library/SMS"),
        # iOS 8+ CallHistoryDB; call_history.db in wireless/ before that.
        ("Call History",            ("mobile/Library/CallHistoryDB",
                                     "wireless/Library/CallHistory")),
        ("Contacts",                "mobile/Library/AddressBook"),
        ("Mail",                    "mobile/Library/Mail"),
        ("Voicemail",               "mobile/Library/Voicemail"),
    ]),
    ("Media", [
        ("Photos (DCIM)",           "mobile/Media/DCIM"),
        ("Photo Library Data",      "mobile/Media/PhotoData"),
        ("Voice Memos",             "mobile/Media/Recordings"),
    ]),
    ("Browsing & Web", [
        ("Safari",                  "mobile/Library/Safari"),
        ("Safari Downloads — iOS 13+","mobile/Library/Safari/Downloads"),
        ("Safari Reading List",     "mobile/Library/Safari/ReadingListArchives"),
    ]),
    ("System & Device", [
        ("Biome (user) — iOS 14+",  "mobile/Library/Biome"),
        ("Biome (system) — iOS 16+", "db/biome"),
        ("DuetExpertCenter (event streams, notification DB)",
                                    "mobile/Library/DuetExpertCenter"),
        ("KnowledgeC — iOS 9+",     "mobile/Library/CoreDuet/Knowledge"),
        ("Health",                  "mobile/Library/Health"),
        ("Calendar",                "mobile/Library/Calendar"),
        # iOS 9+: NoteStore.sqlite in the Notes app group container
        # (UUID differs per phone); notes.sqlite in Library/Notes before.
        ("Notes",                   ("mobile/Containers/Shared/AppGroup/*/NoteStore.sqlite",
                                     "mobile/Library/Notes")),
        ("Reminders",               "mobile/Library/Reminders"),
        ("Location History",        "mobile/Library/Caches/com.apple.routined"),
        ("Keychain",                "Keychains"),
        ("Wi-Fi Networks",          "wireless/Library/Preferences"),
        ("Device Logs",             "mobile/Library/Logs"),
        ("Unified Logs — iOS 10+",          "db/diagnostics"),
        ("Unified Logs UUID Text — iOS 10+","db/uuidtext"),
    ]),
    ("Installed Apps", [
        ("App Executables",         "containers/Bundle/Application"),
        ("Crash Reports",           "mobile/Library/Logs/CrashReporter"),
    ]),
]


def android_shortcuts(user_data: str = '') -> list:
    """The Android menu. *user_data* is the detected user-storage folder
    (the parent of DCIM), default data/media/0."""
    user_data = user_data or 'data/media/0'
    return [
        ("App Data (user 0)", "data/data"),
        ("User Data",         user_data),
        # Every user's app data: owner (0), second users, work profile,
        # Android 15 Private Space each have their own id here.
        ("All users' app data (work profile, Private Space, other users)", "data/user"),
        None,
        ("Media", [
            ("Photos (DCIM)",    user_data + "/DCIM"),
            ("Downloads",        user_data + "/Download"),
            ("Screenshots",      (user_data + "/DCIM/Screenshots",
                                  user_data + "/Pictures/Screenshots")),
        ]),
        ("Communications", [
            # Device-encrypted copy first: where mmssms.db normally is on
            # current Android (EVIDENCE_LOCATIONS.md). The data/data copy
            # can hold different content, so both are listed.
            ("SMS / MMS (user_de)", "data/user_de/0/com.android.providers.telephony/databases"),
            ("SMS / MMS (data/data)", "data/data/com.android.providers.telephony/databases"),
            # calllog.db was in data/data on all three test phones (Samsung:
            # its own contacts provider), not in user_de as one source says.
            ("Call History (calllog.db)", ("data/data/com.android.providers.contacts/databases",
                                           "data/data/com.samsung.android.providers.contacts/databases",
                                           "data/user_de/0/com.android.providers.contacts/databases")),
            ("Contacts",         ("data/data/com.android.providers.contacts/databases",
                                  "data/data/com.samsung.android.providers.contacts/databases")),
        ]),
        ("Browsing & Web", [
            ("Chrome",           "data/data/com.android.chrome/app_chrome/Default"),
            ("Samsung Browser",  ("data/data/com.sec.android.app.sbrowser/app_sbrowser/Default",
                                  "data/data/com.sec.android.app.sbrowser/databases")),
        ]),
        ("System & Device", [
            ("Accounts",         "data/system_ce/0/accounts_ce.db"),
            ("Settings (XML)",   ("data/system/users/0",
                                  "data/data/com.android.providers.settings/databases")),
            ("Usage Stats",      ("data/system_ce/0/usagestats", "data/system/usagestats/0")),
            ("Notification History", "data/system_ce/0/notification_history"),
            ("Recent Tasks / Snapshots", ("data/system_ce/0/recent_tasks",
                                          "data/system_ce/0/snapshots")),
            ("Wi-Fi Networks",   ("data/misc/apexdata/com.android.wifi", "data/misc/wifi")),
            ("Bluetooth",        ("data/misc/bluedroid", "data/misc/bluetooth")),
            ("Device Logs",      "data/tombstones"),
            # On-device Timeline since 2024 lives in the Maps / Play
            # services app data; the old locationhistory folder is gone.
            ("Google Maps app data (incl. on-device Timeline)",
                                 "data/data/com.google.android.apps.maps"),
        ]),
    ]


def candidates_of(entry) -> tuple:
    c = entry[1]
    return (c,) if isinstance(c, str) else tuple(c)


def resolve(candidates, folders, files, adapt=lambda p: p):
    """The ui_path to open for the first candidate this extraction has, or
    None. *folders*: folder ui_paths (the tree's folder_map). *files*:
    file ui_paths. A file target opens as itself (the tree shows its
    folder); a '*' pattern opens the folder of its first match, in
    sorted order so the same extraction always gives the same answer."""
    for cand in candidates:
        path = adapt(cand)
        if '*' in path:
            tail = path.rsplit('*', 1)[1]      # cheap pre-filter before fnmatch
            hits = sorted(f for f in files if f.endswith(tail) and fnmatchcase(f, path))
            if hits:
                return hits[0].rsplit('/', 1)[0]
            continue
        if path in folders or path in files:
            return path
    return None


def leaves(items, trail=()):
    """(menu trail, entry) for every leaf, for tests and checks."""
    for it in items:
        if it is None:
            continue
        if isinstance(it[1], list):
            yield from leaves(it[1], trail + (it[0],))
        else:
            yield trail + (it[0],), it
