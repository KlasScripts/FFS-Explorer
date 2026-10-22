"""Chromium Simple Cache records (any app) in the embedded-media sweep and the
Text tab. Records are built byte by byte in the format chrome_cache parses:
header (magic, version, key length, key hash, reserved), key, body, EOF
marker, then the HTTP headers stream."""
import os
import struct

import chrome_cache as cc
import embedded_media_scan as ems

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 600


def _record(key: str, body: bytes, headers: dict) -> bytes:
    k = key.encode()
    raw = struct.pack('<QIIII', cc._HEADER_MAGIC, 5, len(k), 0, 0) + k + body
    raw += cc._EOF_MAGIC + b'\0' * 16
    hdr = 'HTTP/1.1 200\0' + ''.join(f'{n}: {v}\0' for n, v in headers.items()) + '\0'
    return raw + hdr.encode()


def test_key_flags_prefix_stripped_only_before_a_real_url():
    assert cc._parse_cache_key('1/0/https://a.com/x.png') == (None, 'https://a.com/x.png')
    assert cc._parse_cache_key('1/0/_dk_https://s.com https://s.com https://c.com/y.jpg') == \
        ('https://s.com', 'https://c.com/y.jpg')
    assert cc._parse_cache_key('12/3/notaurl') == (None, '12/3/notaurl')


def test_record_names_are_recognised():
    assert ems.is_simple_cache_record('data/data/x/cache/Cache_Data/000e5b778df2e766_0')
    assert not ems.is_simple_cache_record('data/data/x/cache/Cache_Data/000e5b778df2e766_1')
    assert not ems.is_simple_cache_record('data/data/x/photo_0')


def test_record_with_image_becomes_one_hit_with_source_url(tmp_path):
    raw = _record('1/0/https://cdn.example.com/img/logo', PNG, {'content-type': 'image/png'})
    rows = ems.scan_container('cache', 'data/data/app/Cache_Data/0123456789abcdef_0', raw,
                              str(tmp_path), 'everywhere')
    assert len(rows) == 1
    r = rows[0]
    assert r['source_kind'] == 'cache' and r['media_kind'] == 'image'
    assert r['source_url'] == 'https://cdn.example.com/img/logo'
    assert r['display_name'] == 'logo.png'                 # URL name + real extension
    assert open(r['extracted_path'], 'rb').read() == PNG


def test_record_without_media_or_not_a_record_gives_nothing(tmp_path):
    html = _record('https://a.com/', b'<html>hello</html>', {'content-type': 'text/html'})
    assert ems.scan_container('cache', 'x/0123456789abcdef_0', html, str(tmp_path), 'everywhere') == []
    assert ems.scan_container('cache', 'x/0123456789abcdef_0', PNG, str(tmp_path), 'everywhere') == []


def test_text_tab_shows_record_information():
    raw = _record('1/0/https://cdn.example.com/a.png', PNG, {'content-type': 'image/png', 'date': 'Mon'})
    text = cc.format_record_text(raw)
    assert 'URL:              https://cdn.example.com/a.png' in text
    assert 'Cache key:        1/0/https://cdn.example.com/a.png' in text
    assert 'HTTP status:      HTTP/1.1 200' in text and 'date' in text
    assert cc.format_record_text(PNG) is None
