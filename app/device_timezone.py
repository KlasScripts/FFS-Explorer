"""device_timezone.py — best-effort detection of the timezone in effect at
the moment of seizure, for the opt-in "device-local" timestamp display mode.

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
import zipfile
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


def detect_handset_zone(zip_path: str, adapter, case_dir: str | None = None) -> str | None:
    """Return the device's own configured IANA timezone name (e.g.
    'America/New_York'), or None if not found.

    iOS only — verified against a real Cellebrite FFS extraction that
    `private/var/db/timezone/localtime` is present as a small text file
    whose content is the resolved zoneinfo path (on a live device this is a
    symlink; extractions capture it as plain file content instead). No
    separate Android/iOS check is needed: the file simply won't exist on an
    Android extraction, so absence is self-describing.

    Uses adapter.user_candidates() (not system_candidates()) — verified
    directly against the real archive that `/private/var/db/...` resolves
    under the same prefix as `/private/var/mobile/...` (the same partition
    every other artifact parser already reads from), not the OS/system
    partition.

    When *case_dir* is given, reads via the local .zcd central-directory
    cache (zip_cd_cache) instead of opening a fresh zipfile.ZipFile — avoids
    a second full central-directory read over the network, and never touches
    the app's shared, non-thread-safe zip handle (this runs off the GUI
    thread). Falls back to opening the zip directly when no case_dir is
    given or the cache isn't available yet.
    """
    try:
        view = None
        if case_dir:
            infos = _zcd_load(zip_path, case_dir)
            if infos is not None:
                view = CachedZipView(zip_path, infos)
        with (view if view is not None else zipfile.ZipFile(zip_path)) as z:
            names = frozenset(z.namelist())
            for candidate in adapter.user_candidates('db/timezone/localtime'):
                if candidate not in names:
                    continue
                content = z.open(candidate).read().decode('utf-8', errors='replace').strip()
                if 'zoneinfo/' not in content:
                    continue
                zone = content.split('zoneinfo/', 1)[1].strip()
                return zone or None
    except Exception:
        pass
    return None


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
    and no detected handset zone (Android has no equivalent to iOS's
    localtime file) — never pre-selected, never treated as authoritative
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
