"""device_timezone.py — best-effort detection of the timezone in effect at
the moment of seizure, for the opt-in "device-local" timestamp display mode
and the Timezone & time sources / Device Info reports.

Qt-free, standalone (opens its own zip), matches the convention of
artifacts/*.py and mcp_server.py. Both detectors return None on any failure
— they must never raise, and must never block case loading.

IMPORTANT CAVEAT (see the settings dialog warning text, ffs-explorer.py):
both sources here are a snapshot at/near the moment of seizure only. If the
device's timezone changed earlier in its life (travel, DST, a manual
change), a value detected here does not apply to older evidence. Neither
function attempts to detect historical timezone changes — that information
isn't present in a filesystem extraction.
"""

import configparser
import os
import re
import sys
from datetime import datetime
from zoneinfo import available_timezones, ZoneInfo

from zip_cd_cache import CachedZipView, load as _zcd_load

# Windows identifies timezones by its own names ("GMT Standard Time",
# "Pacific Standard Time", ...) — NOT IANA names, despite some looking
# similar. Most DF work on this project happens on Windows, so
# detect_system_zone() needs a real answer there, not just a graceful
# None. This is the standard CLDR Windows-to-IANA mapping (windowsZones.xml,
# "001"/world territory — one canonical IANA zone per Windows zone id; the
# same table tzlocal/dateutil/moment-timezone use), so a Windows zone
# resolves to the same IANA name those tools would report. Not
# hand-verified entry-by-entry on a Windows machine — a lookup miss (key
# not in this table, or a table entry drifted from a Windows update) falls
# back to None rather than guessing wrong; the manual zone list is always
# there as a fallback either way.
_WINDOWS_TO_IANA = {
    "Dateline Standard Time": "Etc/GMT+12",
    "UTC-11": "Etc/GMT+11",
    "Aleutian Standard Time": "America/Adak",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "Marquesas Standard Time": "Pacific/Marquesas",
    "Alaskan Standard Time": "America/Anchorage",
    "UTC-09": "Etc/GMT+9",
    "Pacific Standard Time (Mexico)": "America/Tijuana",
    "UTC-08": "Etc/GMT+8",
    "Pacific Standard Time": "America/Los_Angeles",
    "US Mountain Standard Time": "America/Phoenix",
    "Mountain Standard Time (Mexico)": "America/Chihuahua",
    "Mountain Standard Time": "America/Denver",
    "Central America Standard Time": "America/Guatemala",
    "Central Standard Time": "America/Chicago",
    "Easter Island Standard Time": "Pacific/Easter",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "Canada Central Standard Time": "America/Regina",
    "SA Pacific Standard Time": "America/Bogota",
    "Eastern Standard Time (Mexico)": "America/Cancun",
    "Eastern Standard Time": "America/New_York",
    "Haiti Standard Time": "America/Port-au-Prince",
    "Cuba Standard Time": "America/Havana",
    "US Eastern Standard Time": "America/Indianapolis",
    "Turks And Caicos Standard Time": "America/Grand_Turk",
    "Paraguay Standard Time": "America/Asuncion",
    "Atlantic Standard Time": "America/Halifax",
    "Venezuela Standard Time": "America/Caracas",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "SA Western Standard Time": "America/La_Paz",
    "Pacific SA Standard Time": "America/Santiago",
    "Newfoundland Standard Time": "America/St_Johns",
    "Tocantins Standard Time": "America/Araguaina",
    "E. South America Standard Time": "America/Sao_Paulo",
    "SA Eastern Standard Time": "America/Cayenne",
    "Argentina Standard Time": "America/Buenos_Aires",
    "Greenland Standard Time": "America/Godthab",
    "Montevideo Standard Time": "America/Montevideo",
    "Magallanes Standard Time": "America/Punta_Arenas",
    "Saint Pierre Standard Time": "America/Miquelon",
    "Bahia Standard Time": "America/Bahia",
    "UTC-02": "Etc/GMT+2",
    "Azores Standard Time": "Atlantic/Azores",
    "Cape Verde Standard Time": "Atlantic/Cape_Verde",
    "UTC": "Etc/UTC",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Sao Tome Standard Time": "Africa/Sao_Tome",
    "Morocco Standard Time": "Africa/Casablanca",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Romance Standard Time": "Europe/Paris",
    "Central European Standard Time": "Europe/Warsaw",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "Jordan Standard Time": "Asia/Amman",
    "GTB Standard Time": "Europe/Bucharest",
    "Middle East Standard Time": "Asia/Beirut",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Syria Standard Time": "Asia/Damascus",
    "West Bank Standard Time": "Asia/Hebron",
    "South Africa Standard Time": "Africa/Johannesburg",
    "FLE Standard Time": "Europe/Kiev",
    "Israel Standard Time": "Asia/Jerusalem",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Sudan Standard Time": "Africa/Khartoum",
    "Libya Standard Time": "Africa/Tripoli",
    "Namibia Standard Time": "Africa/Windhoek",
    "Arabic Standard Time": "Asia/Baghdad",
    "Turkey Standard Time": "Europe/Istanbul",
    "Arab Standard Time": "Asia/Riyadh",
    "Belarus Standard Time": "Europe/Minsk",
    "Russian Standard Time": "Europe/Moscow",
    "E. Africa Standard Time": "Africa/Nairobi",
    "Iran Standard Time": "Asia/Tehran",
    "Arabian Standard Time": "Asia/Dubai",
    "Astrakhan Standard Time": "Europe/Astrakhan",
    "Azerbaijan Standard Time": "Asia/Baku",
    "Russia Time Zone 3": "Europe/Samara",
    "Mauritius Standard Time": "Indian/Mauritius",
    "Saratov Standard Time": "Europe/Saratov",
    "Georgian Standard Time": "Asia/Tbilisi",
    "Volgograd Standard Time": "Europe/Volgograd",
    "Caucasus Standard Time": "Asia/Yerevan",
    "Afghanistan Standard Time": "Asia/Kabul",
    "West Asia Standard Time": "Asia/Tashkent",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "Pakistan Standard Time": "Asia/Karachi",
    "Qyzylorda Standard Time": "Asia/Qyzylorda",
    "India Standard Time": "Asia/Calcutta",
    "Sri Lanka Standard Time": "Asia/Colombo",
    "Nepal Standard Time": "Asia/Katmandu",
    "Central Asia Standard Time": "Asia/Almaty",
    "Bangladesh Standard Time": "Asia/Dhaka",
    "Omsk Standard Time": "Asia/Omsk",
    "Myanmar Standard Time": "Asia/Rangoon",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Altai Standard Time": "Asia/Barnaul",
    "W. Mongolia Standard Time": "Asia/Hovd",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "Tomsk Standard Time": "Asia/Tomsk",
    "China Standard Time": "Asia/Shanghai",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Singapore Standard Time": "Asia/Singapore",
    "W. Australia Standard Time": "Australia/Perth",
    "Taipei Standard Time": "Asia/Taipei",
    "Ulaanbaatar Standard Time": "Asia/Ulaanbaatar",
    "Aus Central W. Standard Time": "Australia/Eucla",
    "Transbaikal Standard Time": "Asia/Chita",
    "Tokyo Standard Time": "Asia/Tokyo",
    "North Korea Standard Time": "Asia/Pyongyang",
    "Korea Standard Time": "Asia/Seoul",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "AUS Central Standard Time": "Australia/Darwin",
    "E. Australia Standard Time": "Australia/Brisbane",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "West Pacific Standard Time": "Pacific/Guam",
    "Tasmania Standard Time": "Australia/Hobart",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Lord Howe Standard Time": "Australia/Lord_Howe",
    "Bougainville Standard Time": "Pacific/Bougainville",
    "Russia Time Zone 10": "Asia/Srednekolymsk",
    "Magadan Standard Time": "Asia/Magadan",
    "Norfolk Standard Time": "Pacific/Norfolk",
    "Sakhalin Standard Time": "Asia/Sakhalin",
    "Central Pacific Standard Time": "Pacific/Guadalcanal",
    "Russia Time Zone 11": "Asia/Kamchatka",
    "New Zealand Standard Time": "Pacific/Auckland",
    "UTC+12": "Etc/GMT-12",
    "Fiji Standard Time": "Pacific/Fiji",
    "Kamchatka Standard Time": "Asia/Kamchatka",
    "Chatham Islands Standard Time": "Pacific/Chatham",
    "UTC+13": "Etc/GMT-13",
    "Tonga Standard Time": "Pacific/Tongatapu",
    "Samoa Standard Time": "Pacific/Apia",
    "Line Islands Standard Time": "Pacific/Kiritimati",
}


# ── The phone's own time zone settings ──────────────────────────────────
# Each helper takes read(physical_name) -> bytes | None (raising is fine:
# it is caught) and the case's adapter, so the Timestamp Display dialog
# (CachedZipView over the .zcd index) and the device_wide artifact parsers
# (paths['_read_zip_bytes']) share one implementation. Every result names
# the exact zip entry it was read from.

IOS_LOCALTIME = 'db/timezone/localtime'
IOS_DATETIME_PLIST = 'db/timed/Library/Preferences/com.apple.preferences.datetime.plist'
ANDROID_PROPS = 'data/property/persistent_properties'
ANDROID_OLD_PROP = 'data/property/persist.sys.timezone'      # Android 8 and older: one file per property
ANDROID_SETTINGS_GLOBAL = 'data/system/users/0/settings_global.xml'
ANDROID_NETPOLICY = 'data/system/netpolicy.xml'
_BATTERY_DB_RE = re.compile(r'(^|/)data/user_de/0/com\.android\.settings/databases/battery-usage-db-v\d+$')
_OLSON_RE = re.compile(rb'\b(?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific)'
                       rb'/[A-Z][A-Za-z_\-]+(?:/[A-Z][A-Za-z_\-]+)?')
_ZONED_RE = re.compile(r'\[([A-Za-z_]+/[A-Za-z_/\-+]+)\]')
MAX_WEAK_HINTS = 3


def _candidates(adapter, ui_path: str) -> list[str]:
    out = []
    try:
        out.append(adapter.resolve(ui_path))
    except Exception:
        pass
    try:
        out.extend(adapter.user_candidates(ui_path))
    except Exception:
        pass
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _read_first(read, adapter, ui_path: str):
    """(physical name, bytes) of the first candidate that reads, or (None, None)."""
    for name in _candidates(adapter, ui_path):
        try:
            data = read(name)
        except Exception:
            data = None
        if data:
            return name, data
    return None, None


def _varint(buf: bytes, pos: int):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError('varint too long')


def parse_persistent_properties(data: bytes) -> dict:
    """{name: value} from Android's data/property/persistent_properties
    (Android 9+): a protobuf PersistentProperties message, field 1 repeated
    PersistentPropertyRecord {1: name, 2: value}. Unknown fields are
    skipped; a damaged file gives the whole records read before the damage."""
    out = {}
    pos, n = 0, len(data or b'')
    try:
        while pos < n:
            key, pos = _varint(data, pos)
            field, wire = key >> 3, key & 7
            if wire == 2:
                length, pos = _varint(data, pos)
                if pos + length > n:
                    break                      # cut short: never return a partial record
                chunk = data[pos:pos + length]
                pos += length
                if field != 1:
                    continue
                name = value = None
                p = 0
                while p < len(chunk):
                    k, p = _varint(chunk, p)
                    if k & 7 != 2:
                        break
                    ln, p = _varint(chunk, p)
                    text = chunk[p:p + ln].decode('utf-8', 'replace')
                    p += ln
                    if k >> 3 == 1:
                        name = text
                    elif k >> 3 == 2:
                        value = text
                if name is not None:
                    out[name] = value if value is not None else ''
            elif wire == 0:
                _v, pos = _varint(data, pos)
            elif wire == 5:
                pos += 4
            elif wire == 1:
                pos += 8
            else:
                break
    except (IndexError, ValueError):
        pass
    return out


def _known_zone(zone: str) -> bool:
    try:
        return zone in available_timezones()
    except Exception:
        return False


def phone_zone_ios(read, adapter) -> dict | None:
    """{'zone', 'source', 'setting', 'known'} from db/timezone/localtime.
    Verified on the iOS 17 Cellebrite image: the extraction keeps the
    symlink as a small file whose content is the zoneinfo path
    ('/var/db/timezone/zoneinfo/America/New_York')."""
    for name in _candidates(adapter, IOS_LOCALTIME):
        try:
            data = read(name)
        except Exception:
            data = None
        if not data:
            continue
        content = data[:300].decode('utf-8', 'replace').strip()
        if 'zoneinfo/' not in content:
            continue
        zone = content.split('zoneinfo/', 1)[1].strip()
        if zone:
            return {'zone': zone, 'source': name,
                    'setting': 'symlink target', 'known': _known_zone(zone)}
    return None


def phone_zone_android(read, adapter) -> dict | None:
    """{'zone', 'source', 'setting', 'confidence', 'known'} from
    persist.sys.timezone. Android 9+ keeps it in
    data/property/persistent_properties (verified on the Pixel 7a, Galaxy
    A53 and Poco X7 Pro images and the Android 16 lab emulator); older
    Android keeps one file per property (data/property/persist.sys.timezone,
    not tested on a real image). vendor.qemu.timezone (emulators only) is
    never used."""
    name, data = _read_first(read, adapter, ANDROID_PROPS)
    if data:
        props = parse_persistent_properties(data)
        zone = (props.get('persist.sys.timezone') or '').strip()
        if zone:
            return {'zone': zone, 'source': name, 'setting': 'persist.sys.timezone',
                    'confidence': (props.get('persist.sys.timezone_confidence') or '').strip(),
                    'known': _known_zone(zone)}
    name, data = _read_first(read, adapter, ANDROID_OLD_PROP)
    if data:
        zone = data[:100].decode('utf-8', 'replace').strip()
        if zone:
            return {'zone': zone, 'source': name, 'setting': 'persist.sys.timezone',
                    'confidence': '', 'known': _known_zone(zone)}
    return None


def phone_zone(read, adapter) -> dict | None:
    """The phone's own current zone setting, iOS or Android. The two
    platforms' files never both exist, so no platform check is needed."""
    return phone_zone_ios(read, adapter) or phone_zone_android(read, adapter)


def _xml_root(data: bytes):
    import ccl_abx
    if ccl_abx.is_abx(data):
        return ccl_abx.abx_bytes_to_xml_root(data)
    import xml.etree.ElementTree as ET
    return ET.fromstring(data)


def auto_time_zone_android(read, adapter) -> dict | None:
    """{'auto_time_zone': True/False/None, 'auto_time': True/False/None,
    'source'} from settings_global.xml (ABX on Android 12+). Lab-verified
    (Android 16 emulator, 2026-11-16): turning "Set time zone
    automatically" off changed auto_time_zone from 1 to 0."""
    name, data = _read_first(read, adapter, ANDROID_SETTINGS_GLOBAL)
    if not data:
        return None
    try:
        root = _xml_root(data)
    except Exception:
        return None
    vals = {}
    for el in root.iter('setting'):
        key = el.get('name')
        if key in ('auto_time_zone', 'auto_time'):
            vals[key] = el.get('value')

    def _flag(v):
        return None if v is None else v.strip() == '1'
    return {'auto_time_zone': _flag(vals.get('auto_time_zone')),
            'auto_time': _flag(vals.get('auto_time')),
            'source': name}


def ios_datetime_prefs(read, adapter) -> dict | None:
    """{'values': {key: value}, 'source'} — the plain keys of timed's
    com.apple.preferences.datetime.plist. Shown raw: on the iOS 17 image it
    holds only 'timezoneset' = True, whose meaning is not verified, so it is
    NOT reported as the automatic time zone switch."""
    name, data = _read_first(read, adapter, IOS_DATETIME_PLIST)
    if not data:
        return None
    import plistlib
    try:
        p = plistlib.loads(data)
    except Exception:
        return None
    if not isinstance(p, dict):
        return None
    return {'values': {k: v for k, v in p.items() if isinstance(v, (str, bool, int, float))},
            'source': name}


def weak_hints_android(read, adapter, ui_paths=()) -> list[dict]:
    """Up to MAX_WEAK_HINTS incidental zone ids, each {'source', 'value',
    'detail'}. Only these allow-listed files are read; app and system
    tzdata files are never searched (every tzdata copy lists every zone).
    - netpolicy.xml: a ZonedDateTime such as cycleStart
      '2023-01-16T00:00-05:00[America/New_York]' (the mobile-data cycle,
      saved with the zone in effect then; Pixel, Galaxy A53, Poco X7 Pro).
    - Settings' battery-usage-db-v*: a zone id somewhere in the database
      (seen on the Android 16 lab emulator; not on the three real phones).
    *ui_paths*: the case's ui paths, to find which battery-usage-db version
    the phone has."""
    hints = []
    name, data = _read_first(read, adapter, ANDROID_NETPOLICY)
    if data:
        try:
            root = _xml_root(data)
            seen = set()
            for el in root.iter():
                for attr, value in el.attrib.items():
                    m = _ZONED_RE.search(str(value))
                    if m and (m.group(1), attr) not in seen:
                        seen.add((m.group(1), attr))
                        hints.append({'source': name, 'value': m.group(1),
                                      'detail': f"{el.tag} {attr}={value}"})
        except Exception:
            pass
    for ui in sorted(p for p in ui_paths if _BATTERY_DB_RE.search(p)):
        name, data = _read_first(read, adapter, ui)
        if not data:
            continue
        for zone in sorted({m.decode() for m in _OLSON_RE.findall(data)}):
            hints.append({'source': name, 'value': zone,
                          'detail': 'zone id found in the database file'})
    return hints[:MAX_WEAK_HINTS]


def compare_with_archive(zone: str, archive_detect: dict | None) -> str | None:
    """'agree', 'disagree', or None (nothing to compare): whether the phone's
    zone is one of the zones that fit the zip's DOS−UT stamps. The DOS time
    is written by the extraction software in ITS zone (usually the download
    computer's), so 'disagree' most often means the phone was downloaded
    in another zone, not that the phone's zone changed."""
    det = archive_detect or {}
    if not zone or det.get('status') not in ('fixed', 'dst'):
        return None
    return 'agree' if zone in (det.get('zones') or []) else 'disagree'


def _zip_reader(zip_path: str, case_dir: str | None):
    """(read, close) over the case's .zcd index, or (None, None)."""
    if not case_dir:
        return None, None
    infos = _zcd_load(zip_path, case_dir)
    if infos is None:
        return None, None
    view = CachedZipView(zip_path, infos)

    def read(name):
        # one lookup in the case's index: a set of every name (2.5 million
        # on the synthetic iPhone) took ~2.5 s of a first open (2026-11-22)
        try:
            view.getinfo(name)
        except KeyError:
            return None
        with view.open(name) as f:
            return f.read()
    return read, (lambda: None)


def detect_handset_zone_info(zip_path: str, adapter, case_dir: str | None = None) -> dict | None:
    """phone_zone() over the case's cached zip index (never a raw
    zipfile.ZipFile on the main archive), or None. Never raises."""
    try:
        read, close = _zip_reader(zip_path, case_dir)
        if read is None:
            return None
        try:
            return phone_zone(read, adapter)
        finally:
            close()
    except Exception:
        return None


def detect_handset_zone(zip_path: str, adapter, case_dir: str | None = None) -> str | None:
    """The phone's own configured IANA zone name (e.g. 'America/New_York'),
    or None: iOS db/timezone/localtime, Android persist.sys.timezone. A
    snapshot at extraction only. See detect_handset_zone_info."""
    info = detect_handset_zone_info(zip_path, adapter, case_dir)
    return (info or {}).get('zone') or None


_UFD_DATE_RE = re.compile(
    r'(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\s*\(([+-]?\d+)\)\s*$')

# A short preference list for picking one "probable" zone out of however
# many share the exact same offset at the acquisition date — favors well-
# known population centers over obscure aliases (e.g. IANA has ~20 zones
# that are all identically UTC-4 in July). Purely a starting suggestion;
# the user confirms or overrides it in the dialog, never applied silently.
_COMMON_ZONE_PREFERENCE = [
    'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
    'America/Anchorage', 'America/Sao_Paulo', 'America/Argentina/Buenos_Aires',
    'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Madrid', 'Europe/Moscow',
    'Africa/Cairo', 'Asia/Jerusalem', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Shanghai',
    'Asia/Tokyo', 'Asia/Singapore', 'Australia/Sydney', 'Pacific/Auckland',
]


def detect_acquisition_offset(zip_path: str) -> tuple[float, datetime] | None:
    """Return (offset_hours, acquisition_datetime) for the ACQUISITION
    WORKSTATION's clock at extraction time — the machine that ran UFED and
    wrote the .ufd (its own [General] MachineName field, e.g. 'JOSH-LAPTOP'
    on the test image this was built against), read from that file's
    [General] Date field (e.g. 'Date=28/07/2024 16:23:49 (-4)'). Returns
    None if there's no .ufd or the field is missing/unparseable.

    Deliberately NOT called "examiner" — the person/machine reviewing this
    case in ios-ffs-browser right now can be completely different from
    whoever ran the original acquisition (different reviewer, different
    computer, different timezone entirely — confirmed as a real case
    reviewing this exact test image from the UK against a US-Eastern
    acquisition). This value describes the acquisition workstation only,
    never the current viewer.

    Not a device-read value either way — it's a PC clock, stamped into the
    acquisition report. acquisition_datetime (naive, no tzinfo — it IS the
    local reading) is needed by guess_acquisition_zones() to match against
    the right DST state, not just a bare offset number.
    """
    ufd_path = os.path.splitext(zip_path)[0] + '.ufd'
    if not os.path.isfile(ufd_path):
        return None
    try:
        cp = configparser.ConfigParser(strict=False)
        cp.read(ufd_path, encoding='utf-8-sig')
        date_field = cp.get('General', 'Date', fallback='')
        m = _UFD_DATE_RE.search(date_field)
        if not m:
            return None
        acquired_at = datetime.strptime(m.group(1), '%d/%m/%Y %H:%M:%S')
        return float(m.group(2)), acquired_at
    except Exception:
        return None


def detect_system_zone() -> str | None:
    """Return the ANALYSIS MACHINE's own current IANA timezone name (e.g.
    'Europe/London'), or None if it can't be resolved.

    This is neither the device's timezone nor the acquisition workstation's
    — it's whichever computer happens to be running ios-ffs-browser right
    now, which can be a completely different person, machine and timezone
    from both (same real scenario detect_acquisition_offset's docstring
    describes: reviewing a US-Eastern acquisition from a UK-based machine).
    Offered purely as a convenience starting point in the manual
    zone-selection list (see the "Timestamp Display" dialog) for cases with
    no .ufd to guess an acquisition offset from (e.g. GrayKey extractions)
    and no detected handset zone (neither iOS db/timezone/localtime nor
    Android persist.sys.timezone found) — never pre-selected, never treated as authoritative
    for either the device or the acquisition.

    Works on Windows too (most DF work on this project happens there, so
    "graceful None" isn't good enough) via _detect_windows_system_zone().
    macOS/Linux resolve /etc/localtime as a symlink into the system's
    zoneinfo directory, so the real IANA name can be read straight off the
    resolved path."""
    if sys.platform == 'win32':
        return _detect_windows_system_zone()
    try:
        path = os.path.realpath('/etc/localtime')
    except Exception:
        return None
    if 'zoneinfo/' not in path:
        return None
    zone = path.split('zoneinfo/', 1)[1]
    return zone if zone in available_timezones() else None


def _detect_windows_system_zone() -> str | None:
    """Windows identifies timezones by its own names ('GMT Standard Time',
    'Pacific Standard Time', ...) — not IANA names, despite some looking
    similar. Reads the current zone key straight from the registry (no
    tzutil subprocess needed — same value tzutil /g prints) and translates
    it via _WINDOWS_TO_IANA, the standard CLDR mapping. Returns None (never
    a wrong guess) if the key is empty/missing, isn't in the table, or the
    resolved name isn't a real zoneinfo entry — the tzdata package
    (already a Windows-only requirement, see requirements.txt, since
    Windows doesn't ship IANA zone data the way macOS/Linux do) supplies
    the actual zone DATA once a name is known; this function only supplies
    the name."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            win_name, _ = winreg.QueryValueEx(key, "TimeZoneKeyName")
    except Exception:
        return None
    zone = _WINDOWS_TO_IANA.get((win_name or '').strip())
    return zone if zone and zone in available_timezones() else None


def guess_acquisition_zones(offset_hours: float, acquisition_dt: datetime) -> tuple[str | None, list[str]]:
    """Return (best_guess_zone, all_matching_zones) — every IANA zone whose
    UTC offset at acquisition_dt matches offset_hours exactly, for
    populating the "Acquisition computer" dropdown.

    This is a GUESS, never a certainty: many real-world zones share the
    same offset (~20+ commonly share UTC-4 in July, for instance), and
    there is no way to disambiguate from a bare offset number alone. Only
    ever a starting suggestion for the user to confirm or override — see
    the settings dialog's warning text. Called lazily, only when the user
    opens the dialog / picks this option — not at case-load time.
    """
    target = round(offset_hours * 60)  # minutes, avoids float equality issues
    matches = []
    for name in available_timezones():
        try:
            offset = ZoneInfo(name).utcoffset(acquisition_dt)
        except Exception:
            continue
        if offset is not None and round(offset.total_seconds() / 60) == target:
            matches.append(name)
    matches.sort()
    if not matches:
        return None, []
    best = next((z for z in _COMMON_ZONE_PREFERENCE if z in matches), matches[0])
    return best, matches


# ── Timezone & time sources report: the sections both platforms share ──

REPORT_NAME = "Timezone & time sources"

REPORT_WARNING = (
    "> **Timezone at acquisition is a snapshot.** Some apps, EXIF, and older "
    "file times may reflect travel, DST, or an earlier zone. Prefer **UTC** for "
    "reporting unless you need local display. Cross-check important times "
    "against content.")


def _offset_text(minutes: int) -> str:
    sign = '+' if minutes >= 0 else '−'
    hh, mm = divmod(abs(int(minutes)), 60)
    return f"UTC{sign}{hh:02d}:{mm:02d}"


def _zone_offset_minutes(zone: str, when_utc: datetime):
    try:
        return int(when_utc.astimezone(ZoneInfo(zone)).utcoffset().total_seconds() // 60)
    except Exception:
        return None


def case_archive_detect(settings: dict, zip_path: str, case_dir: str):
    """The case's stored archive_offset.detect() result, or a fresh one from
    the cached zip index when the case has none yet (not stored here)."""
    import json
    raw = (settings or {}).get('archive_offset')
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    try:
        import archive_offset
        return archive_offset.detect_from_archive(zip_path, case_dir)
    except Exception:
        return None


def acquisition_pc_lines(settings: dict, zip_path: str, phone_zone_name: str = '') -> list[str]:
    """'## Acquisition PC (not the phone)' from the .ufd's [General] Date."""
    lines = ["## Acquisition PC (not the phone)"]
    offset_hours, acq_dt = None, None
    s = settings or {}
    if s.get('acquisition_offset_hours') and s.get('acquisition_dt'):
        try:
            offset_hours, acq_dt = float(s['acquisition_offset_hours']), datetime.fromisoformat(s['acquisition_dt'])
        except ValueError:
            offset_hours = None
    if offset_hours is None:
        got = detect_acquisition_offset(zip_path) if zip_path else None
        if got:
            offset_hours, acq_dt = got
    if offset_hours is None:
        lines.append("- **Acquisition PC offset:** Not found (no `.ufd` acquisition report "
                     "next to the archive, or no Date field in it).")
        return lines
    ufd = os.path.basename(os.path.splitext(zip_path)[0] + '.ufd')
    lines.append(f"- **Acquisition PC offset:** {_offset_text(round(offset_hours * 60))} at "
                 f"{acq_dt:%Y-%m-%d %H:%M:%S} PC local time — from `{ufd}` ([General] Date)")
    guess, zones = guess_acquisition_zones(offset_hours, acq_dt)
    if zones:
        lines.append(f"- **Zones with that offset on that date:** {len(zones)}; "
                     f"suggested starting point {guess} (a guess, not read from anywhere)")
    if s.get('acquisition_timezone_name'):
        lines.append(f"- **Zone the examiner confirmed for the Acquisition PC:** {s['acquisition_timezone_name']}")
    if phone_zone_name and acq_dt is not None:
        try:
            phone_min = int(ZoneInfo(phone_zone_name).utcoffset(acq_dt).total_seconds() // 60)
        except Exception:
            phone_min = None
        if phone_min is not None:
            if phone_min == round(offset_hours * 60):
                lines.append(f"- The phone's zone ({phone_zone_name}) had the same offset on that date.")
            else:
                lines.append(f"- ⚠️ **Different from the phone:** the phone's zone ({phone_zone_name}) was "
                             f"{_offset_text(phone_min)} on that date. The extraction was run in a "
                             "different zone from the phone's setting.")
    lines.append("- *The PC that ran the acquisition and wrote the `.ufd`. Not the phone, and not "
                 "necessarily the computer reviewing this case.*")
    return lines


def archive_lines(det: dict | None, phone_zone_name: str = '') -> list[str]:
    """'## Archive (zip DOS vs UT)' from an archive_offset.detect() result."""
    import archive_offset as ao
    lines = ["## Archive (zip DOS vs UT)"]
    det = det or {}
    n = det.get('n') or 0
    bins = det.get('bins') or []
    status = det.get('status')
    if status not in ('fixed', 'dst'):
        if n < ao.MIN_SAMPLES:
            lines.append(f"- **Archive offset:** None (only {n:,} sampled files carry both a DOS time and "
                         "a UT time, fewer than needed).")
        else:
            lines.append(f"- **Archive offset:** None (no consensus among {n:,} sampled files).")
        return lines
    if status == 'fixed':
        top = bins[0]
        pct = int(top[1] * 1000 // n) / 10        # floored: 3,999 of 4,000 is 99.9%, not 100.0%
        lines.append(f"- **Archive offset:** {ao.offset_text(top[0])} ({top[1]:,} of {n:,} sampled "
                     f"files agree, {pct:.1f}%)")
    else:
        pair = ', '.join(f"{ao.offset_text(o)} on {c:,}" for o, c in bins[:2])
        lines.append(f"- **Archive offset:** daylight-saving pattern, {pair} of {n:,} sampled files "
                     "(no single fixed offset fits)")
    zones = det.get('zones') or []
    lines.append(f"- **Zones that fit the stamps:** {len(zones)} give the same offset for at least "
                 f"{ao.ZONE_MATCH:.0%} of the sampled files, each at its own date")
    verdict = compare_with_archive(phone_zone_name, det)
    if verdict == 'agree':
        lines.append(f"- The phone's zone ({phone_zone_name}) is one of them.")
    elif verdict == 'disagree':
        lines.append(f"- ⚠️ **Disagrees with the phone:** the phone's zone ({phone_zone_name}) is not one "
                     "of them. Most likely the phone was downloaded on a computer set to another zone; "
                     "it does not show that the phone's zone changed.")
    lines.append("- *Inferred from the zip, not read from Settings. Each file's time is stored twice: UT "
                 "(UTC) and DOS (a local time with no zone). The DOS time is written by the extraction "
                 "software at download, in the zone it was set to, usually the download computer's. It "
                 "can match the phone, but it is not the phone's setting and says nothing about where "
                 "the phone was when a file was made.*")
    return lines


def hint_lines(hints: list[dict]) -> list[str]:
    lines = ["## Other weak hints"]
    if not hints:
        lines.append("- None found in the checked files.")
        return lines
    for h in hints:
        lines.append(f"- **{h['value']}** — `{h['source']}` ({h['detail']}). "
                     "*Weak / incidental — not a change log.*")
    return lines
