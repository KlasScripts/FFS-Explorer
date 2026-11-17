"""archive_offset.py — the device's likely UTC offset, inferred from how the
zip records each file's modified time twice: the DOS date_time (the local
wall clock, no zone) and the UT extra field (0x5455, true UTC). Qt-free.

Display only. Evidence times stay UT/UTC everywhere (the Timeline sorts on
them); this only offers an opt-in way to show them as local time.

For each sampled file with both times (UT mtime present and not 0, DOS year
after 1980):  delta = DOS read as if UTC − UT mtime.  The deltas are binned
to 15 minutes (zones like +05:30 and +05:45 exist; DOS has 2-second
precision).

- Two bins an hour apart that together hold MAJORITY, the smaller with at
  least DST_SHARE: a daylight-saving pattern. No single fixed offset fits,
  so none is offered.
- Otherwise one bin with at least MAJORITY of the sample (and at least
  MIN_SAMPLES files): a fixed offset, e.g. UTC−05:00.
- Either way, the IANA zones whose offset matches every sampled file at its
  own time (to ZONE_MATCH) are listed for the examiner to pick from. One is
  never picked for them: several zones share any offset history.
- Anything else: no consensus, the option is not offered.

The offset says how the zip's DOS times were written: by the extraction
software, at download, in the zone it was set to (usually the download
computer's, possibly the phone's; which one is untested, as the phone and
the download PC were both US Eastern on every test image). It is not
evidence of the phone's zone at any time: a phone seized in Los Angeles and
downloaded in New York would show New York here. The daylight-saving
pattern only means the converting clock applied one zone's rules to each
file's own date.
"""
import calendar
import struct
from collections import Counter
from datetime import datetime, timezone

MAX_SAMPLES = 4000
MIN_SAMPLES = 100
MAJORITY = 0.90
# A bin one hour from the top one holding at least this share means
# daylight saving, even when the top bin alone passes MAJORITY (Android 15
# test phone: 90.4% at −04:00, 9.6% at −05:00). A fixed offset would show
# that share an hour off.
DST_SHARE = 0.02
ZONE_MATCH = 0.97
ZONE_CHECK_SAMPLES = 400
_BIN = 900          # seconds (15 minutes)
_UT = 0x5455


def ut_mtime(extra: bytes):
    """The UT extra field's mtime, or None when there is no UT block or it
    carries no mtime. 0 is returned as 0 ("no time")."""
    off = 0
    n = len(extra or b'')
    while off + 4 <= n:
        tag, length = struct.unpack_from('<HH', extra, off)
        off += 4
        if tag == _UT:
            if length >= 5 and extra[off] & 1:
                return struct.unpack_from('<I', extra, off + 1)[0]
            return None
        off += length
    return None


def _dos_seconds(dt):
    if not dt or dt[0] <= 1980:
        return None
    try:
        return calendar.timegm(tuple(dt) + (0, 0, -1))
    except (ValueError, OverflowError, TypeError):
        return None


def sample_pairs(infos, limit=MAX_SAMPLES) -> list:
    """[(ut_seconds, delta_seconds)] for up to *limit* files spread evenly
    across the archive (every k-th file entry), each with a DOS time and a
    UT mtime that is present and not 0."""
    files = [i for i in infos if not i.filename.endswith('/')]
    step = max(1, len(files) // (limit * 2))     # oversample: some have no pair
    out = []
    for info in files[::step]:
        dos = _dos_seconds(info.date_time)
        if dos is None:
            continue
        ut = ut_mtime(info.extra)
        if not ut:
            continue
        out.append((ut, dos - ut))
        if len(out) >= limit:
            break
    return out


def _bin(delta):
    return int(round(delta / _BIN)) * _BIN


def offset_text(seconds: int) -> str:
    m = int(round(seconds / 60))
    sign = '+' if m >= 0 else '−'
    hh, mm = divmod(abs(m), 60)
    return f"UTC{sign}{hh:02d}:{mm:02d}"


def _zones_matching(pairs, candidates):
    from zoneinfo import ZoneInfo
    step = max(1, len(pairs) // ZONE_CHECK_SAMPLES)
    check = [(datetime.fromtimestamp(ut, tz=timezone.utc), _bin(d)) for ut, d in pairs[::step]]
    out = []
    for name in candidates:
        try:
            z = ZoneInfo(name)
        except Exception:
            continue
        ok = 0
        for i, (when, want) in enumerate(check):
            if int(when.astimezone(z).utcoffset().total_seconds()) == want:
                ok += 1
            elif i - ok > len(check) * (1 - ZONE_MATCH):
                break                                # can no longer reach ZONE_MATCH
        if ok >= ZONE_MATCH * len(check):
            out.append(name)
    return out


def detect(pairs, zones=None) -> dict:
    """{'status': 'fixed' | 'dst' | 'none', 'offset_s': fixed offset or None,
    'n': sample size, 'bins': [(offset_s, count)] most common first,
    'zones': IANA zones consistent with the sample}. *zones*: the zone
    names to test (default: every available zone)."""
    n = len(pairs)
    bins = Counter(_bin(d) for _ut, d in pairs).most_common()
    res = {'status': 'none', 'offset_s': None, 'n': n, 'bins': bins[:5], 'zones': []}
    if n < MIN_SAMPLES or not bins:
        return res
    top, top_n = bins[0]
    second = bins[1] if len(bins) > 1 else (None, 0)
    if (second[0] is not None and abs(second[0] - top) == 3600
            and second[1] >= DST_SHARE * n and top_n + second[1] >= MAJORITY * n):
        res['status'] = 'dst'
    elif top_n >= MAJORITY * n:
        res['status'], res['offset_s'] = 'fixed', top
    else:
        return res
    if zones is None:
        from zoneinfo import available_timezones
        zones = sorted(z for z in available_timezones() if '/' in z and not z.startswith('Etc/'))
    res['zones'] = _zones_matching(pairs, zones)
    return res


def detect_from_archive(zip_path: str, case_dir: str) -> dict:
    """detect() on the case's cached zip index (no archive read)."""
    import zip_cd_cache
    infos = zip_cd_cache.load(zip_path, case_dir) or []
    return detect(sample_pairs(infos))


def format_fixed(ts, offset_s: int) -> str:
    """An evidence time (s, or APFS ns) shown at a fixed offset from UTC."""
    try:
        secs = ts / 1e9 if ts > 1e10 else ts
        d = datetime.fromtimestamp(secs + offset_s, tz=timezone.utc)
        return f"{d:%Y-%m-%d %H:%M:%S} {offset_text(offset_s)}"
    except (ValueError, OSError, OverflowError, TypeError):
        return "---"
