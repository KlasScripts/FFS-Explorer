# Evidence locations by OS version

Where evidence lives on Android and iOS, and where that changed between OS
versions. Compiled 2026-11-15 to check the jump menu and
`config/scan_rules_*.json` (the "evidence folders" used by Header Scan, nested
archives, the embedded-media sweep and User Media), and to scope the
Timeline's file times.

**How sure each row is** (the "Basis" column):

- **Seen**: checked by path in one of this project's test extractions (file
  counts only; no file was opened, so content is not confirmed).
- **Source**: stated by a published source listed at the end. Not checked on
  our data.
- **Tools**: appears in iLEAPP/ALEAPP's own path patterns (the copies in
  `Other Forensic tools/Third party`).
- **Unverified**: general knowledge, no source found yet. Do not rely on it
  in a report.

**Versions checked on real data in this project** (by path, 2026-11-15):
Android 14 (Samsung, Magnet/GrayKey layout), Android 14 (Pixel, Cellebrite),
Android 15 (Cellebrite), iOS 17 (Cellebrite). On disk but not yet opened:
iOS 16.5 (Cellebrite), iOS 18 (Magnet/GrayKey). Nothing older than Android 14
(full file system) or iOS 16.5, and nothing newer than Android 15 or iOS 18.
Public sources reach Android 16 and iOS 26.

**A newer OS than these is untested.** Locations below can move again in any
release; on an extraction newer than the last version listed here, a folder
that is missing or empty may mean the location moved, not that there is no
evidence.

## Android

`<u>` is the Android user id: 0 is the owner; a work profile, a second user or
Android 15's Private Space each get their own id (Source: bayton.org,
GrapheneOS forum). `data/data` is only user 0 (it is the same as
`data/user/0`); other users' app data is under `data/user/<u>`.

| Artifact | Older location | Newer location | Change | Basis |
|---|---|---|---|---|
| SMS/MMS `mmssms.db` | `data/data/com.android.providers.telephony/databases` | `data/user_de/0/com.android.providers.telephony/databases` | Android 7 (one forum report); androidforensics.org gives the user_de path for 8–16 | Source; Seen: user_de folder has more files than data/data on all three test phones (16/10/10 vs 4/2/2); ALEAPP searches both (`*/com.android.providers.telephony/...`) |
| Call log `calllog.db` | inside `contacts2.db` (Unverified) | androidforensics.org gives `data/user_de/0/com.android.providers.contacts/databases/calllog.db` | Unverified | **Contradicted by our data:** by file name, `calllog.db` is in `data/data/com.android.providers.contacts/databases` on the Pixel (Android 14) and Android 15 phones, and in `data/data/com.samsung.android.providers.contacts/databases` on the Samsung; no `user_de` copy on any of the three. `mmssms.db` is in both `data/data` and `user_de` on all three |
| Contacts `contacts2.db` | `data/data/com.android.providers.contacts/databases` | same | — | Source; Seen (A14 Pixel, A15; empty on A14 Samsung, which uses `com.samsung.android.providers.contacts`, Tools) |
| Wi-Fi networks `WifiConfigStore.xml` | `data/misc/wifi` (8–10) | `data/misc/apexdata/com.android.wifi` (11+), per-user copy `data/misc_ce/<u>/apexdata/com.android.wifi` | Android 11 (Wi-Fi became an APEX module) | Source (AOSP migration README, AOSP Wi-Fi module docs, androidforensics.org) |
| Usage stats | `data/system/usagestats/0` (8–10) | `data/system_ce/0/usagestats` (11+) | Sources disagree: Android 10 or 11. Format XML → protobuf at Android 10 | Source (androidforensics.org, Brignoni, Magnet); Tools (ALEAPP checks both) |
| Notification history | `data/system/notification_log.db` (8–10) | `data/system_ce/<u>/notification_history` (11+) | Android 11 | Source (androidforensics.org); Tools |
| Runtime permissions | `data/system/users/<u>/runtime-permissions.xml` (8–10) | `data/misc_de/<u>/apexdata/com.android.permission/runtime-permissions.xml` (11+) | Android 11 | Source (androidforensics.org); Tools (`roles.xml` likewise) |
| Settings | `settings.db` in `data/data/com.android.providers.settings/databases` | `data/system/users/<u>/settings_secure.xml`, `settings_system.xml`, `settings_global.xml` (user 0) | AOSP "Rewrite of the settings provider"; release not confirmed (believed Android 6, Unverified) | Source (AOSP commit, androidforensics.org); Seen: the old folder is empty on all three test phones |
| Accounts | `data/system/users/0/accounts.db` (Unverified) | `data/system_ce/<u>/accounts_ce.db`, `data/system_de/<u>/accounts_de.db` | Unverified (believed Android 7) | Source (androidforensics.org); Seen |
| Recent tasks / screenshots of apps | `data/system_ce/<u>/recent_images` | `data/system_ce/<u>/recent_tasks`, `.../snapshots` | Unverified | Source; Tools (ALEAPP checks both) |
| Bluetooth pairings `bt_config.conf` | `data/misc/bluedroid` | some recent builds `data/misc/bluetooth` | not dated | Source (AOSP btif_config, androidforensics.org) |
| MediaStore `external.db` | `com.android.providers.media` | `com.google.android.providers.media.module` (11+) | Android 11 | Source (androidforensics.org); Tools |
| Google Maps Timeline | cloud (Takeout) | on device since 2024, in the Maps / Play services app data | 2024 | Source (androidforensics.org) |
| Batterystats | `data/system/batterystats*.bin`, `battery-history/`, `batterystats-daily.xml` | "layout varies by release" | — | Source |
| Packages, app-ops, shutdown checkpoints | `data/system/packages.xml`, `appops.xml`, `appops/`, `shutdown-checkpoints/` | — | `appops/` and `shutdown-checkpoints/` from Android 12 | Source (RealityNet list); Tools |
| Build / identity | `system/build.prop`, `vendor/build.prop`, `product/etc/build.prop`, `data/property/persistent_properties`, `data/misc/bootstat` | — | — | Source; Tools |
| Samsung extras | `data/log`, `efs/recovery`, `data/system/WifiConfigStore.db`, Secure Folder app data | — | OEM-specific | Tools |
| Intrusion logging (Android 16, Advanced Protection) | — | encrypted bundles held in the cloud, not a device folder | Android 16 | Source (iVerify) |

**Several users on one phone (Lab: Grok, Android 16 emulator "Alice",
2026-11-15).** A managed work profile created with
`pm create-user --profileOf 0 --managed` became user 10:

- `data/data` is the same folder as `data/user/0` (same inode): user 0 only.
- The work profile has its own trees: `data/user/10`, `data/user_de/10` and
  `data/media/10`.
- Per-user emulated storage under `mnt/.../10/...` is a view of that user's
  media, not a second copy of their app data.
- An app installed in both profiles has two sandboxes with different
  inodes: Chrome in `data/data/com.android.chrome` (user 0) and in
  `data/user/10/com.android.chrome`.
- An app installed only in the work profile has data only under
  `data/user/10` (and `data/user_de/10`), never under `data/data`.
- Other ids (Unverified, not on this lab phone): Samsung Secure Folder is
  often 150 and Samsung Dual Messenger 95. Private Space, secondary users
  and app clones each get their own id.

So anything that only opens or scans `data/data` misses every other user.
The evidence folders already include `data/user`, `data/user_de`,
`data/media`, `data/system_ce` and the other per-user roots without a
user id. The jump menu lists each user found in the extraction (see below).

Not checked on a real extraction: none of the four test extractions has a
second user, and how Cellebrite or GrayKey stores one is not known. The
Cellebrite test extractions have no `data/user` tree at all, even for
user 0.

**Copies, not extra evidence (Seen, Android 14 Samsung, Magnet layout):**
`data/user/0`, `data_mirror/data_ce`, `storage/emulated/0` and `mnt/*/0`
repeat `data/data` and `data/media/0` path for path (35,267 of 35,267 and
1,197 of 1,197). Including them counts every file twice.

## iOS

Paths are written the Cellebrite way (`mobile/...`, no `private/var/`).

| Artifact | Older location | Newer location | Change | Basis |
|---|---|---|---|---|
| Call history | `wireless/Library/CallHistory/call_history.db` | `mobile/Library/CallHistoryDB/CallHistory.storedata` | iOS 8 | Source (The iPhone Wiki, MVT docs); Tools (iLEAPP checks both); Seen (iOS 17) |
| Notes | `mobile/Library/Notes/notes.sqlite` | `NoteStore.sqlite` in the Notes app group container (`mobile/Containers/Shared/AppGroup/<UUID>`) | iOS 9 | Source (Ciofeca parser, Passware); Tools (iLEAPP searches `*/NoteStore.sqlite*`) |
| KnowledgeC | `mobile/Library/CoreDuet/Knowledge/knowledgeC.db` | still present, but far fewer streams from iOS 16 (about 60 → 20); Biome took over | iOS 16 | Source (Magnet, Cellebrite) |
| Biome | iOS 14: `mobile/Library/Biome/streams/public` and `mobile/Library/DuetExpertCenter/streams` | iOS 15: adds `Biome/streams/restricted`; iOS 16: adds system-level `db/biome/streams/restricted`; iOS 17: SEGB v2 format, the public area is gone; same paths reported for iOS 17–26 | iOS 14–17 | Source (Zena Forensics 2026, Cellebrite SEGB v2); Tools (also `Biome/sets`, `Biome/databases`, `Biome/sync`) |
| Notification metadata `notificationAndSuggestionDB.db` | — | `mobile/Library/DuetExpertCenter` | iOS 15–26 | Source (Epifani, via Forensic Focus) |
| Safari history | `mobile/Library/Safari/History.db` | iOS 17 Safari profiles: each extra profile has its own `History.db` in a `Profiles` folder in Safari's app container | iOS 17 | Source (Magnet); Tools (`Safari/Profiles/History.db`) |
| Location (routined) | `root/Library/Caches/locationd/consolidated.db` (old) | `mobile/Library/Caches/com.apple.routined` (`Cache.sqlite`, `Local.sqlite`) | old path: Unverified version | Source (MSAB, older forensic texts); Seen (iOS 17) |
| SMS / iMessage | `mobile/Library/SMS/sms.db` | same; iOS 26 stores polls in `balloon_bundle_id` (schema, not path) | — | Source; Seen |
| shutdown.log | `db/diagnostics/shutdown.log` (appended) | iOS 26 reportedly overwrites it at each reboot | iOS 26 | Source (single blog, weak) |
| System-level evidence the app's lists miss | `Keychains`, `root/Library/Lockdown`, `db/diagnostics`, `db/uuidtext`, `networkd`, `preferences/SystemConfiguration`, `wireless/Library/Databases`, `containers/Shared/SystemGroup`, `containers/Data/System`, `installd` | — | — | Tools; Seen (iOS 17) |

## What was wrong in the app's lists, and what changed (2026-11-15)

Fixed in `app/evidence_shortcuts.py` (jump menu) and
`config/scan_rules_*.json` (evidence folders). Checked by resolving every
jump entry against the file indexes of the four extractions above (files
only) and, for Android, by driving the menu in the app on the Pixel copy.

**Jump menu.** Each entry now lists its locations newest first and opens
the first one the extraction has. An entry with none is greyed out as "not
in this extraction" (its tooltip lists the paths tried). Before, a missing
target silently opened the nearest existing parent folder.

- Android:
  - "SMS / MMS" was only the `data/data` copy. Now there are two entries,
    `user_de` (listed first) and `data/data`; both copies exist on all three
    phones.
  - "Call History (calllog.db)" checks `data/data` (AOSP, then Samsung's
    provider) before `user_de`.
  - "Settings DB" (an empty folder on all three phones) is now "Settings
    (XML)" → `data/system/users/0`.
  - "Location History" (the old `locationhistory` folder, gone) is now
    "Google Maps app data (incl. on-device Timeline)".
  - **Several users.** The menu reads the user ids from the extraction
    (the numeric folders under `data/user`, `data/user_de`, `data/media`,
    `data/system_ce`, `data/system_de`, `data/misc_ce` and `data/misc_de`).
    It never assumes 0 and 10.
    - When there is more than one, the menu starts with a line naming
      them, for example "This extraction has 2 users: 0 (owner), 10 (work
      profile)", and each user other than 0 gets a submenu with the same
      per-user entries under that id.
    - The main menu stays user 0, and "App Data (user 0)" opens `data/data`.
    - The user's type comes from `data/system/users/<id>.xml` (plain or
      binary XML; never the user's name). When that can't be read, the
      submenu is just "User <id>".
    - On the Pixel and Samsung test extractions, `users/0.xml` is binary
      XML and reads as "owner".
    - On Alice (Android 16), `users/10.xml` and `users/0.xml` are binary
      XML and read as "work profile" and "owner" (type attribute, not the
      name). Test: `test_user_type_from_real_android_16_abx`, skipped when
      `test_data/alice_work_profile_user10/` is absent.
    - Alice's user 10 folder listing (package level, plus every folder of
      `data/media/10` and `data/system/users`) finds users 0 and 10.
      Resolved against it:
      - App Data, User Data, DCIM, Downloads, Settings and Google Maps
        resolve to user 10's own folders.
      - Screenshots and Samsung Browser are correctly "not in this
        extraction".
      - The listing stops at package level. So SMS, call history,
        contacts and Chrome (`.../databases`, `app_chrome/Default`) and
        everything under `data/system_ce/10` could not be checked.
      - User 10's telephony provider folders hold 0 files in both
        `data/user/10` and `data/user_de/10`. Its contacts provider holds
        7 and 3, and its Chrome 138.
    - The submenu was checked in the app with a simulated user 10. Opening
      a real user 10 folder could not be checked without an extraction
      that has one.
  - New entries:
    - "All users' app data" (`data/user`, which holds other users, the
      work profile and Private Space);
    - Usage Stats, Notification History, Recent Tasks / Snapshots, Wi-Fi
      Networks and Bluetooth, each with its older location as a fallback.
  - Screenshots falls back to `Pictures/Screenshots`. That is where the
    Pixel has them.
- iOS:
  - Call History → `mobile/Library/CallHistoryDB` (then the pre-iOS 8
    `wireless/Library/CallHistory`).
  - Voicemail → `mobile/Library/Voicemail`.
  - Notes → the app group folder holding `NoteStore.sqlite` (then the
    pre-iOS 9 `mobile/Library/Notes`). Found 19 files on iOS 17.
  - App Executables → `containers/Bundle/Application`.
  - Keychain, Unified Logs and UUID Text are now written without
    `private/var/`; `prefix_shortcut` adds it on GrayKey only. On iOS 17
    Keychain and UUID Text now resolve (32 and 2,190 files). Unified Logs
    (`db/diagnostics`) has no files in that extraction.
  - New: DuetExpertCenter, and both Biome locations: "Biome (user)"
    `mobile/Library/Biome` and "Biome (system)" `db/biome`.
- Still empty on iOS 17 and left as they are: Voice Memos, Safari Downloads,
  Safari Reading List, Crash Reports, Unified Logs. Device-dependent, so they
  show as greyed out.

**Evidence folders.** Every new row is `not_user_media`, so User Media is
unchanged (same file counts on all four extractions).

- Android adds `data/user`, `data/user_de`, `data/system`,
  `data/system_ce`, `data/system_de`, `data/misc`, `data/misc_ce`,
  `data/misc_de`, `data/property`, `data/tombstones` and `data/log`.
- iOS changes these rows:
  - SMS: the whole `mobile/Library/SMS` folder (was `SMS/Attachments` only).
  - Biome: the whole user `mobile/Library/Biome` folder (was `streams`
    only), plus `db/biome`.
- iOS adds:
  - under `mobile/Library`: CallHistoryDB, Voicemail, AddressBook, Notes
    (pre-iOS 9), Safari, CoreDuet, DuetExpertCenter, Health, Calendar,
    Preferences, UserNotifications, Logs and Reminders;
  - the old `wireless/Library/CallHistory`, plus `wireless/Library/Preferences`
    and `wireless/Library/Databases`;
  - `Keychains`, `db/diagnostics`, `db/uuidtext`, `root/Library/Lockdown`,
    `preferences/SystemConfiguration` and `containers/Shared/SystemGroup`.
- No folder is excluded because it looks like a copy. Grok's checksum
  comparison on the Magnet extraction found the copies differ in places,
  and ours agrees: 15 of 35,267 files in `data/user/0` and 19 in
  `data_mirror` differ from `data/data`.
- Files in scope, before → after:

  | Extraction | Before | After |
  |---|---|---|
  | Android 14 Magnet | 36,480 | 74,446 |
  | Android 14 Pixel | 84,072 | 90,511 |
  | Android 15 | 172,979 | 177,902 |
  | iOS 17 | 71,686 | 76,885 |

  Most of the Magnet growth is the `data/user/0` copy.
- **Proven copies are read once by Header Scan.** A copy counts as proven
  when it has the same size and CRC-32 plus the same GrayKey SHA-256 or the
  same trusted inode. Size and CRC-32 alone are not proof. On the Magnet
  copy, a full scan's 74,446 candidates included 40,683 proven copies
  (35,252 of the 35,267 in `data/user/0`); the 15 that differ were read on
  their own. Reads fell from 74,446 to 33,763, and the results matched a
  scan without dedupe for every file. Cellebrite extractions record no
  SHA-256, and the Pixel and Android 15 ones have no inode data either, so
  nothing is skipped on them. They have no mirror copies anyway.
- Not deduplicated yet: nested-archive discovery and the embedded-media
  sweep still open each copy. A proven copy could reuse the first copy's
  result there too, but their results are stored per file, so that needs
  its own change.

**A newer or older OS than these.** A banner under the time banner says so
when the extraction's OS version is outside the versions checked on real
images (`app/os_coverage.py`: Android 14–15, iOS 17), or when the version
can't be read.

**Next:** Timeline file times (media files by default; a detailed mode
scoped to known evidence folders), after the duplicate-copy step.

**Counting note:** counts are files only. Folder entries were included by
mistake in a first pass and have been taken out.

## Sources

- androidforensics.org artifact reference: https://www.androidforensics.org/en/artifacts
- AOSP Wi-Fi module: https://source.android.com/docs/core/ota/modular-system/wifi
- AOSP Wi-Fi migration README (Android 13 tag): https://android.googlesource.com/platform/frameworks/base.git/+/android-13.0.0_r24/wifi/migration_samples/README.txt
- AOSP settings provider rewrite (mirror): https://gitlab.e.foundation/e/os/android_frameworks_base/-/commit/683914bfb13908bf380a25258cd45bcf43f13dc9
- AOSP Bluetooth config: https://android.googlesource.com/platform/system/bt/+/713993d/btif/src/btif_config.cc
- RealityNet Android forensics references: https://github.com/RealityNet/Android-Forensics-References
- Brignoni, usagestats: https://abrignoni.blogspot.com/2019/02/android-usagestats-xml-parser.html
- Magnet, Android messaging: https://www.magnetforensics.com/blog/android-messaging-forensics-sms-mms-and-beyond/
- XDA, SMS database location change (single report): https://forum.xda-developers.com/nexus-5x/help/sms-mms-database-location-change-t3447133/
- Bayton, Android 15 Private Space: https://bayton.org/android/what-is-android-15-private-space/
- iVerify, Android intrusion logs: https://www.iverify.com/blog/android-intrusion-logging-forensics-analysis
- The iPhone Wiki, call history: https://www.theiphonewiki.com/wiki/Call_History_Database
- MVT iOS records: https://docs.mvt.re/en/stable/ios/records/
- Ciofeca, Apple Cloud Notes parser: https://github.com/threeplanetssoftware/apple_cloud_notes_parser
- Passware, Notes: https://support.passware.com/hc/en-us/articles/33630420850839
- Magnet, Biome: https://www.magnetforensics.com/blog/bringing-it-back-with-biome-data/
- Cellebrite, SEGB v2: https://cellebrite.com/en/understanding-and-decoding-the-newest-ios-segb-format/
- Zena Forensics, Biome iOS 14–26: https://blog.digital-forensics.it/2026/07/84-streams-later-exploring-evolution-of.html
- Forensic Focus round-up (DuetExpertCenter notification DB): https://www.forensicfocus.com/news/digital-forensics-round-up-july-29-2026/
- Magnet, iOS 17 forensics / Safari profiles: https://www.magnetforensics.com/blog/ios-17-forensics-another-year-another-byte-of-the-apple/
- MSAB, iOS location data: https://www.msab.com/blog/sqlite-secrets-ios-location-data/
- iOS 26 shutdown.log (weak, single blog): https://opsecinsider.com/ios-26-spyware-forensics/
- iOS 26 sms.db polls: https://matthewplascencia.substack.com/p/am-i-that-old-already-new-forensic
- iLEAPP / ALEAPP path patterns: local copies in `Other Forensic tools/Third party/`

**Not found:**

- Hexordia publishes mostly through training and CTF material, not a public
  path list; no Hexordia page with version paths came up.
- Cellebrite and MSAB (XRY) publish release notes about which devices they
  support, not artifact paths. The full lists are behind their customer
  portals.
