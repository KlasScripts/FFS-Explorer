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
    out, bad, _notes = ks.search_job(str(p), entries, ks.make_patterns('test'), chunk=3)
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


# ── phone numbers ───────────────────────────────────────────────────────────

def _ph(term, data, read_size=None):
    blocks = _split(data, read_size) if read_size else [data]
    return ks.search_blocks(blocks, ks.make_patterns(term), phone=ks.make_phone_patterns(term))


def test_formats_seen_on_the_test_phones_are_found():
    # The shapes the 2026-11-12 survey found in contacts, call logs and
    # Google Messages (digits changed).
    for stored in ('(912) 345-6788', '+1 912-345-6788', '+1 (912) 345-6788',
                   '+1-912-345-6788', '+19123456788', '9123456788', '912.345.6788',
                   '912 345 6788'):
        hits = _ph('9123456788', f'x {stored} y'.encode())
        assert len(hits) == 1, (stored, hits)


def test_international_keyword_finds_national_forms():
    for term in ('+47 912 34 567', '004791234567', '+4791234567'):
        for stored in ('912 34 567', '+47 912 34 567', '91234567', '4791234567@s.whatsapp.net'):
            hits = _ph(term, f'.. {stored} ..'.encode())
            assert len(hits) == 1, (term, stored, hits)
    assert 'country code +47' in _ph('+4791234567', b' 912 34 567 ')[0][1]


def test_trunk_zero_dropped():
    assert len(_ph('0412 345 678', b'"+61 412 345 678"')) == 1


def test_reversed_last_seven_digits_only_as_a_whole_number():
    hits = _ph('9123456788', b'min_match 8876543 end')
    assert len(hits) == 1 and 'reversed' in hits[0][1]
    assert _ph('9123456788', b'id 1288765439 end') == []


def test_no_double_counts_and_read_size_independent():
    data = (b'a (912) 345-6788 b +19123456788 c 9123456788 d 8876543 e ' * 200)
    whole = _ph('9123456788', data)
    assert len(whole) == 4 * 200
    assert len({o for o, _ in whole}) == len(whole)
    for size in (13, 100, 4096):
        assert sorted(_offsets(_ph('9123456788', data, size))) == sorted(_offsets(whole)), size


def test_not_a_phone_number_keyword():
    for term in ('test', '123456', 'klas 9123456788', '2026-11-12', '12.11.2026',
                 '11/12/26', '192.168.1.100'):
        assert ks.make_phone_patterns(term) == [], term
    assert ks.make_phone_patterns('912 34 567')


def test_media_start_and_end_only(tmp_path):
    """Keyword Search's "Media: start/end only" (2026-11-22): a picture is
    searched in its first and last MEDIA_HEAD_TAIL bytes, at their true
    offsets; the middle is not searched; other files are searched whole."""
    import zipfile
    import keyword_scan as ks
    import keyword_search
    ht = 1024
    pic = bytearray(b'.' * (10 * ht))
    for at in (100, 5 * ht, 10 * ht - 200):          # start, middle, end
        pic[at:at + 6] = b'needle'
    txt = bytes(pic)
    zp = tmp_path / 'm.zip'
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('DCIM/a.jpg', bytes(pic))
        z.writestr('notes/a.txt', txt)
    with zipfile.ZipFile(zp) as z:
        entries = []
        for info in z.infolist():
            with open(zp, 'rb') as f:
                f.seek(info.header_offset + 26)
                n, m = int.from_bytes(f.read(2), 'little'), int.from_bytes(f.read(2), 'little')
            entries.append((info.filename, info.header_offset + 30 + n + m, info.file_size))
    limited, n = keyword_search._limit_media(entries, ht)
    assert n == 1 and [len(e) for e in limited] == [4, 3]
    pats = ks.make_patterns('needle')
    got = dict(ks.search_job(str(zp), limited, pats)[0])
    assert [o for o, _c in got['DCIM/a.jpg']] == [100, 10 * ht - 200]
    assert [o for o, _c in got['notes/a.txt']] == [100, 5 * ht, 10 * ht - 200]
    full = dict(ks.search_job(str(zp), entries, pats)[0])
    assert [o for o, _c in full['DCIM/a.jpg']] == [100, 5 * ht, 10 * ht - 200]
    # a picture no bigger than both ends is searched whole
    assert ks.read_ranges(2 * ht, ht) == [(0, 2 * ht)]


def test_app_install_folders_skipped(tmp_path):
    """Keyword Search's "Skip app install folders" (2026-11-22): an
    all-files search leaves out the zip names under the given prefixes and
    says so; everything else is searched."""
    import zipfile
    from PySide6.QtCore import QCoreApplication
    import keyword_search
    app = QCoreApplication.instance() or QCoreApplication([])
    zp = tmp_path / 's.zip'
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('filesystem2/containers/Bundle/Application/X/App.app/strings.txt', b'a needle here')
        z.writestr('filesystem2/mobile/Library/notes.txt', b'the needle there')
    entries = []
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            with open(zp, 'rb') as f:
                f.seek(info.header_offset + 26)
                n, m = int.from_bytes(f.read(2), 'little'), int.from_bytes(f.read(2), 'little')
            entries.append((info.filename, info.header_offset + 30 + n + m, info.file_size))
    w = keyword_search.KeywordSearchWorker(
        str(zp), 'needle', entries=entries, base64=False,
        skip_entry=lambda n: n.startswith('filesystem2/containers/Bundle/'))
    found, status = [], []
    w.results_found.connect(lambda name, hits: found.append(name))
    w.status_update.connect(status.append)
    w.run()
    assert found == ['filesystem2/mobile/Library/notes.txt']
    assert any("apps' own install not searched" in s for s in status)



def test_app_install_skip_leaves_out_only_app_code():
    """The skip rule: iOS only inside an app's .app bundle (its
    iTunesMetadata.plist, which can name the downloading account, is
    searched); Android only code files under data/app; data never."""
    from types import SimpleNamespace
    import keyword_search
    g = '0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9'
    ios = keyword_search.app_install_skip(SimpleNamespace(resolve=lambda u: 'filesystem2/' + u))
    assert ios(f'filesystem2/containers/Bundle/Application/{g}/Maps.app/Info.plist')
    assert ios(f'filesystem2/containers/Bundle/Application/{g}/Maps.app/Base.lproj/x.strings')
    assert not ios(f'filesystem2/containers/Bundle/Application/{g}/iTunesMetadata.plist')
    assert not ios(f'filesystem2/containers/Data/Application/{g}/Documents/a.app/x')
    assert not ios('filesystem2/mobile/Library/SMS/sms.db')
    gk = keyword_search.app_install_skip(SimpleNamespace(resolve=lambda u: '/' + u))
    assert gk('/data/app/~~abc==/com.x-def==/base.apk')
    assert gk('/data/app/~~abc==/com.x-def==/lib/arm64/libx.so')
    assert not gk('/data/app/~~abc==/com.x-def==/base.digests')
    assert not gk('/data/data/com.x/databases/msg.db')
    assert not gk('/data/media/0/Download/a.apk')        # a user's own download is searched


def test_media_metadata_found_wherever_it_sits(tmp_path):
    """With "Media: start/end only": a video's moov box (GPS, tags) is
    searched even in the middle of the file, and a TIFF (metadata may sit
    anywhere) is searched whole (2026-11-22)."""
    import struct
    import zipfile
    import keyword_scan as ks
    import keyword_search
    ht = 1024

    def box(kind, body):
        return struct.pack('>I4s', 8 + len(body), kind) + body
    video = (box(b'ftyp', b'isom' + b'\0' * 8) + box(b'mdat', b'.' * (6 * ht))
             + box(b'moov', b'.' * 100 + b'needle-gps' + b'.' * 100) + box(b'mdat', b'.' * (6 * ht)))
    tif = bytearray(b'.' * (10 * ht))
    tif[5 * ht:5 * ht + 6] = b'needle'
    zp = tmp_path / 'v.zip'
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('DCIM/IMG_1.MOV', video)
        z.writestr('DCIM/scan.tif', bytes(tif))
    entries = []
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            with open(zp, 'rb') as f:
                f.seek(info.header_offset + 26)
                n, m = int.from_bytes(f.read(2), 'little'), int.from_bytes(f.read(2), 'little')
            entries.append((info.filename, info.header_offset + 30 + n + m, info.file_size))
    limited, n = keyword_search._limit_media(entries, ht)
    assert n == 1 and len(limited[1]) == 3                 # the TIFF is searched whole
    got = dict(ks.search_job(str(zp), limited, ks.make_patterns('needle'))[0])
    moov_at = video.index(b'needle-gps')
    assert [o for o, _c in got['DCIM/IMG_1.MOV']] == [moov_at]
    assert [o for o, _c in got['DCIM/scan.tif']] == [5 * ht]



def test_png_text_anywhere_compressed_text_and_data_after_the_end(tmp_path):
    """With "Media: start/end only" a PNG's text chunks are searched
    wherever they sit, compressed text (zTXt, as some AI generators store
    a prompt) is decompressed and searched, and data after IEND is searched
    in full and reported (2026-11-22)."""
    import struct
    import zipfile
    import zlib
    import keyword_scan as ks
    import keyword_search
    ht = 1024

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    png = (ks._PNG_SIG + chunk(b'IHDR', b'\0' * 13) + chunk(b'IDAT', b'.' * (5 * ht))
           + chunk(b'tEXt', b'parameters\0a cat, needle style')
           + chunk(b'zTXt', b'prompt\0\0' + zlib.compress(b'{"text": "a needle in a haystack"}'))
           + chunk(b'IDAT', b'.' * (5 * ht)) + chunk(b'IEND', b''))
    appended = b'.' * (3 * ht) + b'hidden needle note' + b'.' * (3 * ht)
    data = png + appended
    zp = tmp_path / 'p.zip'
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('DCIM/gen.png', data)
    with zipfile.ZipFile(zp) as z:
        info = z.infolist()[0]
        with open(zp, 'rb') as f:
            f.seek(info.header_offset + 26)
            n, m = int.from_bytes(f.read(2), 'little'), int.from_bytes(f.read(2), 'little')
        entries = [(info.filename, info.header_offset + 30 + n + m, info.file_size)]
    limited, n_lim = keyword_search._limit_media(entries, ht)
    assert n_lim == 1
    out, bad, notes = ks.search_job(str(zp), limited, ks.make_patterns('needle'))
    hits = dict(out)['DCIM/gen.png']
    offsets = [o for o, _c in hits]
    assert data.index(b'needle style') in offsets                       # tEXt in the middle
    ztxt_at = data.index(b'zTXt') - 4
    assert any(o == ztxt_at and 'compressed PNG text "prompt"' in c for o, c in hits)
    assert data.index(b'hidden needle note') + len(b'hidden ') in offsets  # after IEND
    assert notes['png_compressed_text'] == 1
    assert notes['png_after_end'] == [('DCIM/gen.png', len(png), len(appended))]



def test_media_searched_by_saved_metadata(tmp_path):
    """"Media: metadata only" (2026-11-22): a picture with saved metadata is
    searched by it, not by its bytes; data after a picture's end is still
    searched, at its file offset; a picture without saved metadata is
    searched (start/end)."""
    import zipfile
    from PySide6.QtCore import QCoreApplication
    import keyword_scan as ks
    import keyword_search
    from db_utils import _open_cache_db, save_media_metadata
    app = QCoreApplication.instance() or QCoreApplication([])
    with_meta = b'JPEG needle in the bytes' + b'.' * 100
    png = b'PNGDATA' + b'.' * 50
    tail = b'hidden needle note'
    no_meta = b'other needle picture' + b'.' * 10
    zp = tmp_path / 'm.zip'
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_STORED) as z:
        z.writestr('DCIM/a.jpg', with_meta)
        z.writestr('DCIM/b.png', png + tail)
        z.writestr('DCIM/c.jpg', no_meta)
    entries = []
    with zipfile.ZipFile(zp) as z:
        for info in z.infolist():
            with open(zp, 'rb') as f:
                f.seek(info.header_offset + 26)
                n, m = int.from_bytes(f.read(2), 'little'), int.from_bytes(f.read(2), 'little')
            entries.append((info.filename, info.header_offset + 30 + n + m, info.file_size))
    case = str(tmp_path / 'case')
    import os
    os.makedirs(case)
    with _open_cache_db(case) as db:
        save_media_metadata(db, {
            'DCIM/a.jpg': {'_text': [('EXIF', 'ImageDescription', 'a needle description')]},
            'DCIM/b.png': {'_text': [('file', 'data after the end of the image', f'{len(png)}:{len(tail)}')]},
        })
    w = keyword_search.KeywordSearchWorker(
        str(zp), 'needle', entries=entries, base64=False, case_dir=case,
        media_head_tail=ks.MEDIA_HEAD_TAIL, media_meta=True, resolve=lambda p: p)
    found = {}
    w.results_found.connect(lambda name, hits: found.setdefault(name, []).extend(hits))
    w.run()
    assert found['DCIM/a.jpg'] == [(0, found['DCIM/a.jpg'][0][1])]          # metadata only
    assert found['DCIM/a.jpg'][0][1].startswith('[metadata: EXIF ImageDescription]')
    assert [o for o, _c in found['DCIM/b.png']] == [len(png) + len(b'hidden ')]
    assert [o for o, _c in found['DCIM/c.jpg']] == [len(b'other ')]
