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


def android_shortcuts(user_data: str = '', user: int = 0) -> list:
    """The Android menu for one user. *user* 0 is the owner: the main menu,
    with the device-wide entries. Any other id gives that user's own
    entries only (a per-user submenu, see android_users): every path that
    holds per-user data uses the id, never a hardcoded 0. *user_data* is
    the detected user-storage folder (the parent of DCIM) for user 0;
    other users use data/media/<id>."""
    u = int(user)
    owner = u == 0
    # User 0's app data is data/data (the same folder as data/user/0, which
    # some tools also store as a copy); other users only have data/user/<id>.
    app = 'data/data' if owner else f'data/user/{u}'
    media = (user_data or 'data/media/0') if owner else f'data/media/{u}'
    items = [
        (f"App Data (user {u})", app),
        ("User Data",         media),
    ]
    if owner:
        # Every user's app data: owner (0), second users, work profile,
        # Private Space, app clones each have their own id here.
        items.append(("All users' app data (work profile, Private Space, other users)",
                      "data/user"))
    items += [
        None,
        ("Media", [
            ("Photos (DCIM)",    media + "/DCIM"),
            ("Downloads",        media + "/Download"),
            ("Screenshots",      (media + "/DCIM/Screenshots",
                                  media + "/Pictures/Screenshots")),
        ]),
        ("Communications", [
            # Device-encrypted copy first: where mmssms.db normally is on
            # current Android (EVIDENCE_LOCATIONS.md). The credential-
            # encrypted copy can hold different content, so both are listed.
            ("SMS / MMS (user_de)", f"data/user_de/{u}/com.android.providers.telephony/databases"),
            (f"SMS / MMS ({app})", f"{app}/com.android.providers.telephony/databases"),
            # calllog.db was in data/data on all three test phones (Samsung:
            # its own contacts provider), not in user_de as one source says.
            ("Call History (calllog.db)", (f"{app}/com.android.providers.contacts/databases",
                                           f"{app}/com.samsung.android.providers.contacts/databases",
                                           f"data/user_de/{u}/com.android.providers.contacts/databases")),
            ("Contacts",         (f"{app}/com.android.providers.contacts/databases",
                                  f"{app}/com.samsung.android.providers.contacts/databases")),
        ]),
        ("Browsing & Web", [
            ("Chrome",           f"{app}/com.android.chrome/app_chrome/Default"),
            ("Samsung Browser",  (f"{app}/com.sec.android.app.sbrowser/app_sbrowser/Default",
                                  f"{app}/com.sec.android.app.sbrowser/databases")),
        ]),
    ]
    system = [
        ("Accounts",         f"data/system_ce/{u}/accounts_ce.db"),
        ("Settings (XML)",   (f"data/system/users/{u}",)
                             + (("data/data/com.android.providers.settings/databases",) if owner else ())),
        ("Usage Stats",      (f"data/system_ce/{u}/usagestats", f"data/system/usagestats/{u}")),
        ("Notification History", f"data/system_ce/{u}/notification_history"),
        ("Recent Tasks / Snapshots", (f"data/system_ce/{u}/recent_tasks",
                                      f"data/system_ce/{u}/snapshots")),
    ]
    if owner:      # device-wide, not per user
        system += [
            ("Wi-Fi Networks",   ("data/misc/apexdata/com.android.wifi", "data/misc/wifi")),
            ("Bluetooth",        ("data/misc/bluedroid", "data/misc/bluetooth")),
            ("Device Logs",      "data/tombstones"),
        ]
    # On-device Timeline since 2024 lives in the Maps / Play services app
    # data; the old locationhistory folder is gone.
    system.append(("Google Maps app data (incl. on-device Timeline)",
                   f"{app}/com.google.android.apps.maps"))
    items.append(("System & Device", system))
    return items


# Folders whose next level is the Android user id.
USER_ROOTS = ('data/user', 'data/user_de', 'data/media', 'data/system_ce',
              'data/system_de', 'data/misc_ce', 'data/misc_de')


def android_users(folders) -> list:
    """The Android user ids this extraction has a folder for, read from the
    extraction (the numeric folders under USER_ROOTS), never assumed. 0 is
    included when data/data exists. Sorted."""
    ids = set()
    roots = {r + '/' for r in USER_ROOTS}
    for path in folders:
        if path == 'data/data':
            ids.add(0)
            continue
        head, sep, rest = path.rpartition('/')
        if sep and head + '/' in roots and rest.isdigit():
            ids.add(int(rest))
    return sorted(ids)


# AOSP UserManager user types (Android 11+ write them as the <user> "type"
# attribute of data/system/users/<id>.xml).
_USER_TYPES = {
    'android.os.usertype.full.SYSTEM': 'owner',
    'android.os.usertype.system.HEADLESS': 'headless system user',
    'android.os.usertype.full.SECONDARY': 'secondary user',
    'android.os.usertype.full.GUEST': 'guest',
    'android.os.usertype.full.RESTRICTED': 'restricted profile',
    'android.os.usertype.full.DEMO': 'demo user',
    'android.os.usertype.profile.MANAGED': 'work profile',
    'android.os.usertype.profile.PRIVATE': 'Private Space',
    'android.os.usertype.profile.CLONE': 'app clone',
    'android.os.usertype.profile.COMMUNAL': 'communal profile',
}
_FLAG_MANAGED_PROFILE = 0x20   # older Android, before the type attribute
_FLAG_GUEST = 0x4


def user_type_label(raw: bytes):
    """The user's type ("work profile", "Private Space", ...) from the
    bytes of data/system/users/<id>.xml (plain XML or Android binary XML),
    or None when it can't be read. Never the user's name, which can be a
    person's name."""
    if not raw:
        return None
    try:
        import ccl_abx
        if ccl_abx.is_abx(raw):
            root = ccl_abx.abx_bytes_to_xml_root(raw)
        else:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(raw)
    except Exception:
        return None
    user = root if root.tag == 'user' else root.find('.//user')
    if user is None:
        return None
    utype = (user.get('type') or '').strip()
    if utype:
        # An unknown type (e.g. a manufacturer's own) is shown as its last
        # two parts, "profile.XYZ", rather than guessed.
        return _USER_TYPES.get(utype, '.'.join(utype.split('.')[-2:]))
    try:
        flags = int(user.get('flags') or 0)
    except ValueError:
        return None
    if flags & _FLAG_MANAGED_PROFILE:
        return 'work profile'
    if flags & _FLAG_GUEST:
        return 'guest'
    return None


def user_menu_title(u: int, label) -> str:
    return f"User {u} ({label})" if label else f"User {u}"


def users_summary(users, labels) -> str:
    parts = [f"{u} ({labels[u]})" if labels.get(u) else str(u) for u in users]
    return f"This extraction has {len(users)} users: " + ", ".join(parts)


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
