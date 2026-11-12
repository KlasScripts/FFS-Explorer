"""keyword_scan: matching inside one file, across read boundaries, and the
worker-process job over a real archive layout."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))
import keyword_scan as ks  # noqa: E402


def _split(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def _offsets(hits):
    return [o for o, _ in hits]


def test_hits_found_once_whatever_the_read_size():
    pats = ks.make_patterns('Test')
    data = (b'..test..TEST' + 'test'.encode('utf-16-le') + b'x' * 50
            + 'TeSt'.encode('utf-16-be') + b'tes' + b't')
    whole = ks.search_blocks([data], pats)
    assert len(whole) == 5
    for size in (1, 2, 3, 5, 7, 8, 13, 64):
        assert sorted(_offsets(ks.search_blocks(_split(data, size), pats))) == sorted(_offsets(whole)), size


def test_repeated_character_keyword_same_hits_whatever_the_read_size():
    # A keyword that can overlap itself ("aa" in "aaaaa") must give the
    # same non-overlapping hits as one whole-file search, wherever the
    # read borders fall.
    for kw, data in (('aa', b'aaaaa'), ('aa', b'xaaaaaaay' * 3),
                     ('0000', b'0' * 23 + b'x' + b'0' * 9), ('aba', b'abababa')):
        pats = ks.make_patterns(kw)
        whole = _offsets(ks.search_blocks([data], pats))
        for size in range(1, len(data) + 1):
            assert _offsets(ks.search_blocks(_split(data, size), pats)) == whole, (kw, size)


def test_context_marks_the_hit():
    pats = ks.make_patterns('needle')
    [(off, ctx)] = ks.search_blocks([b'hay hay NEEDLE hay'], pats, ctx_bytes=4)
    assert off == 8 and ctx == 'hay [NEEDLE] hay'


def test_jobs_keep_archive_order_and_limits():
    entries = [('c', 300, 10), ('a', 100, 10), ('b', 200, 10), ('big', 400, 100)]
    jobs = ks.make_jobs(entries, job_bytes=25, job_files=10)
    assert [[e[0] for e in j] for j in jobs] == [['a', 'b'], ['c'], ['big']]


def test_search_job_reads_each_file_by_offset_only(tmp_path):
    # Two "stored files" side by side: a match spanning their border must
    # not be reported (a hit never crosses from one file into the next).
    blob = b'HEADER' + b'aaaTE' + b'STbbb' + b'zz test zz'
    p = tmp_path / 'a.zip'
    p.write_bytes(blob)
    entries = [('one', 6, 5), ('two', 11, 5), ('three', 16, 10)]
    out, bad = ks.search_job(str(p), entries, ks.make_patterns('test'), chunk=3)
    assert bad == 0
    [(name, [(off, ctx)])] = out
    # (context stops at the end of the current read: here 3 bytes)
    assert name == 'three' and off == 3 and ctx.startswith('zz [test]')


# ── base64 ──────────────────────────────────────────────────────────────────

import base64  # noqa: E402


def _b64_hits(term, data, read_size=None):
    blocks = _split(data, read_size) if read_size else [data]
    return ks.search_blocks(blocks, ks.make_patterns(term), b64=ks.make_base64_patterns(term))


def test_name_inside_base64_found_at_every_alignment():
    # "klas" one, two and three bytes into the encoded text, so each of the
    # three base64 alignments is used; mixed into a JSON value.
    for prefix in ('', 'a', 'ab', 'abc', 'user=Klas Tveita', 'x' * 40):
        text = f'{prefix}{"Klas"} sent a photo'.encode()
        enc = base64.b64encode(text)
        data = b'{"payload":"' + enc + b'","v":1}'
        hits = _b64_hits('klas', data)
        assert len(hits) == text.lower().count(b'klas'), (prefix, hits)
        for off, ctx in hits:
            assert ctx.startswith('base64: ') and '[Klas]' in ctx, ctx
            assert data[off:off + 2] in enc      # the offset points into the base64


def test_url_safe_base64_and_jwt_style_payload():
    text = b'{"sub":"klas","name":"Klas T","iat":1700000000,"x":"\xfb\xff"}'
    enc = base64.urlsafe_b64encode(text).rstrip(b'=')
    data = b'eyJhbGciOiJIUzI1NiJ9.' + enc + b'.sig'
    hits = _b64_hits('klas', data)
    assert len(hits) == 2, hits                  # "klas" and "Klas"


def test_chance_match_in_random_base64_is_not_reported():
    # A base64 pattern of "klas" placed into base64 that does not decode to it.
    pat = ks.make_base64_patterns('klas')[0][0]
    data = b'"k":"QUJD' + pat + b'WFla"'
    hits = _b64_hits('klas', data)
    assert all('klas' in c.lower() for _, c in hits)


def test_base64_hits_same_whatever_the_read_size():
    enc = base64.b64encode(b'..' + b'hello klas, ' * 300 + b'end')
    data = b'x' * 777 + enc + b'y' * 333
    whole = _b64_hits('klas', data)
    assert len(whole) == 300
    # Same hits at the same offsets; the decoded context, like a plain hit's,
    # stops at the edge of the read it was found in.
    for size in (64, 100, 997, 4096):
        part = _b64_hits('klas', data, size)
        assert sorted(_offsets(part)) == sorted(_offsets(whole)), size
        assert all('[klas]' in c for _, c in part)


def test_plain_hits_unchanged_and_short_terms_skip_base64():
    assert ks.make_base64_patterns('abc') == []           # too short to be reliable
    data = b'plain klas here ' + base64.b64encode(b'and klas there')
    plain = ks.search_blocks([data], ks.make_patterns('klas'))
    both = _b64_hits('klas', data)
    assert len(plain) == 1 and len(both) == 2



# ── non-ASCII case, decomposed letters, Windows-1252 ───────────────────────

import unicodedata  # noqa: E402


def _count(term, data):
    return len(ks.search_blocks([b'..' + data + b'..'], ks.make_patterns(term)))


def test_ascii_keyword_patterns_unchanged():
    assert ks.make_patterns('Test') == [
        (b'test', 4, 'utf-8'), (b't\0e\0s\0t\0', 8, 'utf-16-le'), (b'\0t\0e\0s\0t', 8, 'utf-16-be')]


def test_non_ascii_case_found():
    for term, stored in (('blåbær', 'BLÅBÆR'), ('blåbær', 'Blåbær'), ('søk', 'SØK'),
                         ('café', 'CAFÉ'), ('мама', 'МАМА'), ('BLÅBÆR', 'blåbær')):
        for enc in ('utf-8', 'utf-16-le', 'utf-16-be'):
            assert _count(term, stored.encode(enc)) == 1, (term, stored, enc)


def test_decomposed_letters_found():
    for stored in ('blåbær', 'Blåbær', 'BLÅBÆR'):
        nfd = unicodedata.normalize('NFD', stored)
        assert nfd != stored
        assert _count('blåbær', nfd.encode('utf-8')) == 1, stored


def test_windows_1252_found_and_unencodable_skipped():
    assert _count('blåbær', 'Blåbær'.encode('cp1252')) == 1
    assert _count('blåbær', 'BLÅBÆR'.encode('cp1252')) == 1
    assert not any(e == 'cp1252' for _, _, e in ks.make_patterns('мама'))
    hits = ks.search_blocks([b'x ' + 'blåbær'.encode('cp1252') + b' y'], ks.make_patterns('blåbær'))
    assert hits[0][1] == 'x [blåbær] y'


def test_each_occurrence_counted_once():
    data = ('blåbær BLÅBÆR Blåbær ' + unicodedata.normalize('NFD', 'blåbær')).encode()
    assert _count('blåbær', data) == 4
