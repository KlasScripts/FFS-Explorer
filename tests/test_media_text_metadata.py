"""Every text metadata field of a picture is extracted (media_metadata_scan.
extract_all_text) and saved (media_metadata_text), for Keyword Search to
search instead of the file's bytes (2026-11-22)."""
import io
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

import media_metadata_scan as mm  # noqa: E402


def _jpeg_with_metadata() -> bytes:
    from PIL import Image
    img = Image.new('RGB', (16, 16), 'red')
    exif = img.getexif()
    exif[0x010E] = 'needle description'            # ImageDescription
    exif[0x0131] = 'GenTool 1.0'                   # Software
    exif.get_ifd(0x8769)[0x9286] = b'ASCII\x00\x00\x00a needle comment'   # UserComment
    buf = io.BytesIO()
    img.save(buf, 'JPEG', exif=exif, comment=b'jpeg needle COM')
    data = buf.getvalue()
    xmp = (b'http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta><dc:description>needle xmp'
           b'</dc:description></x:xmpmeta>')
    app1 = b'\xff\xe1' + struct.pack('>H', len(xmp) + 2) + xmp
    return data[:2] + app1 + data[2:]


def _png_with_metadata() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(buf, 'PNG')
    data = buf.getvalue()

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    extra = (chunk(b'tEXt', b'parameters\x00a needle prompt, steps 20')
             + chunk(b'zTXt', b'prompt\x00\x00' + zlib.compress(b'{"needle": "workflow"}'))
             + chunk(b'iTXt', b'Description\x00\x01\x00en\x00\x00' + zlib.compress('needle ü'.encode())))
    iend = data.rindex(b'IEND') - 4
    return data[:iend] + extra + data[iend:] + b'appended needle data'


def test_jpeg_text_metadata():
    text = mm.extract_all_text(io.BytesIO(_jpeg_with_metadata()), '.jpg')
    got = {(s, k): v for s, k, v in text}
    assert got[('EXIF', 'ImageDescription')] == 'needle description'
    assert got[('EXIF', 'Software')] == 'GenTool 1.0'
    assert got[('EXIF', 'UserComment')] == 'a needle comment'
    assert 'needle xmp' in got[('XMP', 'xmp')]
    assert got[('JPEG comment', 'COM')] == 'jpeg needle COM'


def test_png_text_compressed_and_data_after_the_end():
    data = _png_with_metadata()
    text = mm.extract_all_text(io.BytesIO(data), '.png')
    got = {(s, k): v for s, k, v in text}
    assert got[('PNG text', 'parameters')] == 'a needle prompt, steps 20'
    assert got[('PNG text (compressed)', 'prompt')] == '{"needle": "workflow"}'
    assert got[('PNG text (compressed)', 'Description')] == 'needle ü'
    off, n = map(int, got[('file', 'data after the end of the image')].split(':'))
    assert data[off:off + n] == b'appended needle data'


def test_saved_and_loaded(tmp_path):
    from db_utils import _open_cache_db, iter_media_metadata_text, media_metadata_text_paths
    from db_utils import save_media_metadata
    fields = mm.extract_one(io.BytesIO(_jpeg_with_metadata()), '.jpg')
    assert fields['_text']
    with _open_cache_db(str(tmp_path)) as db:
        save_media_metadata(db, {'DCIM/a.jpg': fields})
        save_media_metadata(db, {'DCIM/a.jpg': fields})          # replaced, not doubled
        rows = list(iter_media_metadata_text(db))
        assert media_metadata_text_paths(db) == {'DCIM/a.jpg'}
    assert len(rows) == len(fields['_text'])
    assert ('DCIM/a.jpg', 'EXIF', 'ImageDescription', 'needle description') in rows
