"""keyword_scan.py — the byte matching behind Keyword Search, run in worker
processes. Qt-free.

Why processes (2026-11-06): matching (bytes.lower + one find per encoding)
holds Python's GIL, so the search's four threads matched on one core
between them (~560 MB/s on the test PC, against ~2,100 MB/s the archive
on Z: reads at), and the GUI thread, which needs the GIL back after every
Qt call, froze for seconds at a time while a common word's hits arrived.
A worker process reads its own files straight from the archive by offset
(stored entries, so no decompression) and returns only the hits.

A hit never crosses from one archive file into the next: each file is
searched on its own. Within a file larger than one read (CHUNK), the last
pattern-length-minus-one bytes of each read are carried into the next, so
a match split across two reads is still found, once.
"""

from __future__ import annotations

CHUNK = 4 * 1024 * 1024
CTX_BYTES = 40          # bytes either side of a hit, for the context column
JOB_BYTES = 32 * 1024 * 1024
JOB_FILES = 2000


def keyword_forms(keyword: str) -> list:
    """The spellings of a keyword that are searched for.

    Case is matched by lowercasing bytes, which only folds A-Z. For a
    keyword with any other letter (å, æ, ø, é, Cyrillic, ...) that would miss
    "BLÅBÆR" for "blåbær", so such a keyword is also searched as lower,
    UPPER and Capitalised, each in composed (NFC) and decomposed (NFD:
    "å" as a + combining ring, common in Apple file names and text) form.
    Mixed case inside a word ("bLÅbær") is still not matched. An all-ASCII
    keyword is searched exactly as before: one form."""
    if keyword.isascii():
        return [keyword]
    import unicodedata
    forms = []
    for v in (keyword, keyword.lower(), keyword.upper(), keyword.capitalize()):
        for nf in ('NFC', 'NFD'):
            f = unicodedata.normalize(nf, v)
            if f not in forms:
                forms.append(f)
    return forms


def make_patterns(keyword: str) -> list:
    """[(lowercase pattern bytes, length, encoding), ...] for a keyword:
    each of keyword_forms() in UTF-8, UTF-16LE and UTF-16BE, and, for a
    non-ASCII keyword, Windows-1252 (a superset of Latin-1 for letters;
    older files that came from a PC). An ASCII keyword's Windows-1252
    bytes are its UTF-8 bytes, so it gets no extra pattern."""
    encs = ('utf-8', 'utf-16-le', 'utf-16-be')
    if not keyword.isascii():
        encs += ('cp1252',)
    patterns, seen = [], set()
    for form in keyword_forms(keyword):
        for enc in encs:
            try:
                pat = form.encode(enc, errors='strict' if enc == 'cp1252' else 'replace')
            except UnicodeEncodeError:
                continue                   # not representable in Windows-1252
            pat = pat.lower()
            if (pat, enc) not in seen:
                seen.add((pat, enc))
                patterns.append((pat, len(pat), enc))
    return patterns


# ── Base64 ────────────────────────────────────────────────────────────────
# A keyword stored inside base64 (JSON values, tokens, SharedPreferences,
# plist text) is not a byte substring of the file. Encoded at each of its
# three possible alignments, though, it has a fixed middle: the base64
# characters whose 6 bits all come from the keyword's own bytes. Those are
# searched for in the raw bytes (base64 is case-sensitive, so the case
# variants are separate patterns, not lowercased data). A candidate counts
# only if decoding its whole base64 run really yields the keyword, so a
# chance match inside random base64 is never reported.
#
# Covered: the keyword as UTF-8 (= ASCII) inside standard or URL-safe
# base64. Not covered: base64 of UTF-16 text, of compressed data (gzip,
# NSKeyedArchiver plists), keywords shorter than B64_MIN_BYTES, and mixed
# case beyond as-typed / lower / UPPER / Capitalised.
B64_MIN_BYTES = 4
B64_MAX_RUN = 64 * 1024     # decode at most this many base64 chars either side
_B64_CHARS = frozenset(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_')
_B64_TO_STD = bytes.maketrans(b'-_', b'+/')
_B64_TO_URL = bytes.maketrans(b'+/', b'-_')


def make_base64_patterns(keyword: str) -> list:
    """[(pattern bytes, char offset of the pattern in its 4-char grid,
    keyword bytes lowercased), ...]; empty for a keyword too short to give
    a pattern that is not mostly chance."""
    import base64
    variants = []
    for v in (keyword, keyword.lower(), keyword.upper(), keyword.capitalize()):
        b = v.encode('utf-8', errors='replace')
        if len(b) >= B64_MIN_BYTES and b not in variants:
            variants.append(b)
    out, seen = [], set()
    for b in variants:
        n = len(b)
        for a in range(3):                       # bytes of something before it
            enc = base64.b64encode(b'\0' * a + b + b'\0\0')
            first = -(-8 * a // 6)               # first char made only of keyword bits
            last = (8 * (a + n)) // 6            # one past the last such char
            for pat in {enc[first:last], enc[first:last].translate(_B64_TO_URL)}:
                if pat not in seen:
                    seen.add(pat)
                    out.append((pat, first, b.lower()))
    return out


def _base64_hit(block: bytes, idx: int, pat_len: int, grid: int, kw_lower: bytes,
                ctx_bytes: int):
    """Decode the base64 run around a pattern match at *idx*; return its
    context string if the decoded bytes contain the keyword, else None."""
    import base64, binascii
    start = idx
    lo = max(0, idx - B64_MAX_RUN)
    while start > lo and block[start - 1] in _B64_CHARS:
        start -= 1
    end = idx + pat_len
    hi = min(len(block), idx + pat_len + B64_MAX_RUN)
    while end < hi and block[end] in _B64_CHARS:
        end += 1
    # The run's 4-char groups line up with the pattern's own grid position.
    seg_start = start + (idx - grid - start) % 4
    seg_end = seg_start + (end - seg_start) // 4 * 4
    if seg_end <= seg_start:
        return None
    try:
        dec = base64.b64decode(block[seg_start:seg_end].translate(_B64_TO_STD))
    except (binascii.Error, ValueError):
        return None
    # Where the keyword must sit in the decoded bytes, if this is a real hit.
    if idx - grid < seg_start:
        return None                 # the run starts inside this pattern's group
    want = (idx - grid - seg_start) // 4 * 3
    for k in range(want, want + 3):
        if dec[k:k + len(kw_lower)].lower() == kw_lower:
            cs, ce = max(0, k - ctx_bytes), k + len(kw_lower) + ctx_bytes
            return ('base64: ' + dec[cs:k].decode('utf-8', 'replace')
                    + '[' + dec[k:k + len(kw_lower)].decode('utf-8', 'replace') + ']'
                    + dec[k + len(kw_lower):ce].decode('utf-8', 'replace'))
    return None


# ── Phone numbers ─────────────────────────────────────────────────────────
# The same number is stored many ways: contacts keep it as the user typed
# or as the phone formatted it ("(912) 345-6788", "+1 912-345-6788",
# "+47 912 34 567"), call logs and messages mostly as "+19123456788", and
# Android's contacts2.db phone_lookup.min_match as the last 7 digits
# reversed. A keyword that looks like a phone number (PHONE_MIN_DIGITS+
# digits, nothing but + - . ( ) / and spaces otherwise) is also searched as:
#   - its digits with up to 3 separators between any two;
#   - the same without its country code, when it starts with + or 00
#     (1, 2 or 3 digits dropped, as long as 8+ digits are left);
#   - the same without a leading trunk 0 ("0412 345 678" -> "412345678");
#   - its last 7 digits reversed, as a whole digit run (not inside a
#     longer number).
# Overlapping matches count once (the longest), and a match overlapping a
# plain hit of the keyword itself is left to the plain hit. Covered: UTF-8
# / ASCII text. Not covered: UTF-16 text with separators, numbers stored
# as binary integers.
PHONE_MIN_DIGITS = 7
_PHONE_SEP = rb'(?:[ \t\-.()/]|\xc2\xa0){0,3}'


def make_phone_patterns(keyword: str):
    """[(compiled regex, label, needs digit boundaries), ...] for a
    phone-number keyword, or [] for any other keyword."""
    import re
    k = keyword.strip()
    if not re.fullmatch(r'\+?[\d\s\-.()/]+', k):
        return []
    # Dates and IPv4 addresses have the same characters but are not numbers
    # to reformat or reverse.
    if (re.fullmatch(r'\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4}', k)
            or re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', k)):
        return []
    digits = re.sub(r'\D', '', k)
    if len(digits) < PHONE_MIN_DIGITS:
        return []
    variants = [(digits, 'phone number, with separators')]
    international = k.startswith('+') or digits.startswith('00')
    if digits.startswith('00'):
        digits = digits[2:]
        variants.append((digits, 'phone number, with + in place of 00'))
    if international:
        for n in (1, 2, 3):
            if len(digits) - n >= 8:
                variants.append((digits[n:], f'phone number, without country code +{digits[:n]}'))
    elif digits.startswith('0') and len(digits) - 1 >= PHONE_MIN_DIGITS:
        variants.append((digits[1:], 'phone number, without the leading 0'))
    out, seen = [], set()
    for d, label in variants:
        if d in seen:
            continue
        seen.add(d)
        rx = _PHONE_SEP.join(re.escape(c.encode()) for c in d)
        out.append((re.compile(rx), label, False))
    rev = digits[-7:][::-1]
    out.append((re.compile(re.escape(rev.encode())),
                'phone number, last 7 digits reversed (Android phone_lookup.min_match)', True))
    return out


def _phone_hits(block: bytes, phone: list, taken: list, ctx_bytes: int, start_at: int):
    """Phone-form matches in *block* from *start_at*, longest first where
    they overlap, none overlapping *taken* ((start, end) of plain hits).
    Returns [(idx, end, context), ...] sorted by idx."""
    cands = []
    for rx, label, bounded in phone:
        for m in rx.finditer(block, start_at):
            s, e = m.span()
            if bounded and ((s > 0 and block[s - 1:s].isdigit())
                            or block[e:e + 1].isdigit()):
                continue
            cands.append((s, e, label))
    if not cands:
        return []
    cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    out, last_end = [], -1
    for s, e, label in cands:
        if s < last_end or any(ts < e and s < te for ts, te in taken):
            continue
        cs, ce = max(0, s - ctx_bytes), min(len(block), e + ctx_bytes)
        out.append((s, e, f'{label}: ' + block[cs:s].decode('utf-8', 'replace') + '['
                    + block[s:e].decode('utf-8', 'replace') + ']'
                    + block[e:ce].decode('utf-8', 'replace')))
        last_end = e
    return out


def iter_pattern_hits(data_lower: bytes, patterns: list):
    """Yield (idx, pat_len, enc) for every pattern occurrence in data_lower."""
    for pat, pat_len, enc in patterns:
        idx = 0
        while True:
            idx = data_lower.find(pat, idx)
            if idx == -1:
                break
            yield idx, pat_len, enc
            idx += pat_len


def _decode_ctx(data: bytes, enc: str) -> str:
    if enc in ('utf-8', 'cp1252'):
        return data.decode(enc, errors='replace')
    if len(data) % 2:
        data = data[1:]
    return data.decode(enc, errors='replace')


def search_blocks(blocks, patterns: list, ctx_bytes: int = CTX_BYTES,
                  b64: list | None = None, phone: list | None = None) -> list:
    """Search one file given as successive byte blocks; returns
    [(offset in file, context), ...] in the order found. *b64* is
    make_base64_patterns(keyword), *phone* make_phone_patterns(keyword);
    None for neither."""
    b64 = b64 or []
    phone = phone or []
    # A formatted number can be up to ~7 bytes per digit long (a 2-byte
    # non-breaking space and brackets between digits).
    phone_len = max([7 * (rx.pattern.count(_PHONE_SEP) + 1) for rx, _, _ in phone] + [0])
    overlap = max([pat_len for _, pat_len, _ in patterns] + [len(p) for p, _, _ in b64]
                  + [phone_len]) - 1
    b64_resume = [0] * len(b64)
    phone_resume = 0
    plain_spans = []            # file (start, end) of plain hits that may still overlap
    results = []
    leftover = b''
    base = 0
    # Per pattern: the file offset where its last reported match ended.
    # Each block's search starts there, not at the block's start, so the
    # matches are the same as one search of the whole file: a match
    # already reported, or one overlapping it ("aa" in "aaa"), is not
    # reported again. (Skipping only matches lying wholly inside the
    # carried-over bytes counted a keyword like "aa" or "0000" extra
    # times where a long run of it crossed a read border.)
    resume = [0] * len(patterns)
    for raw in blocks:
        block = leftover + raw if leftover else raw
        block_lower = block.lower()
        found = []
        for i, (pat, pat_len, enc) in enumerate(patterns):
            idx = max(0, resume[i] - base)
            while True:
                idx = block_lower.find(pat, idx)
                if idx == -1:
                    break
                found.append((idx, pat_len, enc))
                idx += pat_len
                resume[i] = base + idx
        for idx, pat_len, enc in found:
            cs = max(0, idx - ctx_bytes)
            ce = min(len(block), idx + pat_len + ctx_bytes)
            hit_text = block[idx:idx + pat_len].decode(enc, errors='replace')
            results.append((base + idx,
                            _decode_ctx(block[cs:idx], enc) + f'[{hit_text}]'
                            + _decode_ctx(block[idx + pat_len:ce], enc)))
        for i, (pat, grid, kw_lower) in enumerate(b64):
            idx = max(0, b64_resume[i] - base)
            while True:
                idx = block.find(pat, idx)
                if idx == -1:
                    break
                ctx = _base64_hit(block, idx, len(pat), grid, kw_lower, ctx_bytes)
                if ctx is not None:
                    results.append((base + idx, ctx))
                idx += len(pat)
                b64_resume[i] = base + idx
        if phone:
            plain_spans = [sp for sp in plain_spans if sp[1] > base]
            plain_spans += [(base + i, base + i + pl) for i, pl, _ in found]
            taken = [(s - base, e - base) for s, e in plain_spans]
            for s, e, ctx in _phone_hits(block, phone, taken, ctx_bytes,
                                         max(0, phone_resume - base)):
                results.append((base + s, ctx))
                phone_resume = base + e
        # (a block shorter than the overlap is carried whole)
        leftover = block[-overlap:] if overlap > 0 else b''
        base += len(block) - len(leftover)
    return results


def make_jobs(entries: list, job_bytes: int = JOB_BYTES, job_files: int = JOB_FILES) -> list:
    """Group (name, data_offset, size) entries into jobs of about
    *job_bytes* / *job_files*, in archive order so each job reads forward."""
    jobs, cur, cur_bytes = [], [], 0
    for e in sorted(entries, key=lambda e: e[1]):
        if cur and (cur_bytes + e[2] > job_bytes or len(cur) >= job_files):
            jobs.append(cur)
            cur, cur_bytes = [], 0
        cur.append(e)
        cur_bytes += e[2]
    if cur:
        jobs.append(cur)
    return jobs


def search_job(zip_path: str, entries: list, patterns: list,
               ctx_bytes: int = CTX_BYTES, chunk: int = CHUNK,
               b64: list | None = None, phone: list | None = None) -> tuple[list, int]:
    """Worker-process entry point: search each (name, data_offset, size)
    entry of the archive. Returns ([(name, hits), ...] for files with
    hits, number of files that could not be read)."""
    out, unreadable = [], 0
    with open(zip_path, 'rb', buffering=0) as f:
        for name, offset, size in entries:
            def blocks(offset=offset, size=size):
                pos = 0
                while pos < size:
                    f.seek(offset + pos)
                    data = f.read(min(chunk, size - pos))
                    if not data:
                        return
                    yield data
                    pos += len(data)
            try:
                hits = search_blocks(blocks(), patterns, ctx_bytes, b64, phone)
            except OSError:
                unreadable += 1
                continue
            if hits:
                out.append((name, hits))
    return out, unreadable
