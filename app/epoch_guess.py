"""epoch_guess.py — guess which epoch format a database column holds.

Used by the File Preview Database tab to show raw numbers as dates and
times. It is a guess for a quick look, not a parser: the stored value is
unchanged, the column header says the column was converted, and the
examiner can remove or change it in the Epoch Formats dialog. A report
that needs a time declares its own unit in timestamp_fields.

A format fits a column when nearly all of its non-zero numbers fall
between 1 January 2007 and the upper bound (the extraction time when the
case has one, else now). The formats are far apart in scale, so usually
only one fits. The lower bound is 2007, not earlier, because a format
counted from 2001 turns any small number (a Unix time, an id, a phone
number read as Apple nanoseconds) into a date in early 2001. Qt-free.
"""

from __future__ import annotations

import time

# code -> (label, seconds since 1970 from the raw number)
FORMATS: dict[str, tuple[str, object]] = {
    's':         ("Unix seconds",                           lambda v: v),
    'ms':        ("Unix milliseconds",                      lambda v: v / 1e3),
    'us':        ("Unix microseconds",                      lambda v: v / 1e6),
    'ns':        ("Unix nanoseconds",                       lambda v: v / 1e9),
    'cocoa_s':   ("Apple seconds (from 2001)",              lambda v: v + 978307200),
    'cocoa_ns':  ("Apple nanoseconds (from 2001)",          lambda v: v / 1e9 + 978307200),
    'webkit_us': ("Chrome/WebKit microseconds (from 1601)", lambda v: v / 1e6 - 11644473600),
    'filetime':  ("Windows FILETIME (from 1601)",           lambda v: v / 1e7 - 11644473600),
}

LOWER_BOUND = 1167609600         # 2007-01-01 00:00:00 UTC
_MIN_SHARE = 0.9                 # share of a column's numbers that must fit
_SAMPLE = 500


def label(code: str) -> str:
    return FORMATS[code][0] if code in FORMATS else code


def number(value):
    """*value* as a number, or None. Digits stored as text count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text[0] == '-' and text[1:].isdigit()):
            return int(text)
        if text.count('.') == 1 and text.replace('.', '', 1).lstrip('-').isdigit():
            return float(text)
    return None


def to_unix(value, code: str):
    """Seconds since 1970 for *value* in format *code*, or None."""
    n = number(value)
    if n is None or code not in FORMATS:
        return None
    try:
        return float(FORMATS[code][1](n))
    except (OverflowError, ValueError, TypeError):
        return None


def default_upper() -> float:
    """Now, plus a day for clock differences."""
    return time.time() + 86400


def guess(values, upper: float | None = None, lower: float = LOWER_BOUND) -> str | None:
    """The format that puts nearly all of *values* in [lower, upper].

    Zero, NULL and text that is not a number are ignored (an unset time is
    often 0). When two formats fit, the one that fits more values wins; on
    a tie the column is left alone rather than picking one at random.
    """
    upper = default_upper() if upper is None else upper
    numbers = []
    for value in values:
        n = number(value)
        if n:
            numbers.append(n)
            if len(numbers) >= _SAMPLE:
                break
    if not numbers:
        return None
    scored = []
    for code, (_label, convert) in FORMATS.items():
        fit = 0
        for n in numbers:
            try:
                seconds = convert(n)
            except (OverflowError, ValueError, TypeError):
                continue
            if lower <= seconds <= upper:
                fit += 1
        share = fit / len(numbers)
        if share >= _MIN_SHARE:
            scored.append((share, code))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]
