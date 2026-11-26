"""_LazyZipSlice.readline, and TIFF/WebP metadata read through the slice in
a fresh process (B-77): Pillow's IM plugin, tried before TIFF unless the
TIFF plugin is already loaded, calls readline; without it Image.open
failed and the file got no metadata, depending on what the process had
read before."""
import io
import os
import subprocess
import sys

from PIL import Image

import media_metadata_scan as m

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')


def _slice_over(tmp_path, payload: bytes, prefix: bytes = b'junk-before-entry'):
    p = tmp_path / 'archive.bin'
    p.write_bytes(prefix + payload + b'junk-after-entry\n')
    return str(p), len(prefix), len(payload)


def test_readline_stops_at_newline_and_slice_end(tmp_path):
    path, off, n = _slice_over(tmp_path, b'one\ntwo\nthree')
    with m._LazyZipSlice(path, off, n) as s:
        assert s.readline() == b'one\n'
        assert s.tell() == 4
        assert s.readline(2) == b'tw'
        assert s.readline() == b'o\n'
        assert s.readline() == b'three'          # never past the entry
        assert s.readline() == b''


def _tiff_with_newline_in_header() -> bytes:
    # The IM plugin only reaches readline when the first 100 bytes hold a
    # b'\n' (otherwise it rejects the file itself): pick a width that puts
    # 0x0A there, as on the real files that failed.
    for w in range(8, 64):
        b = io.BytesIO()
        Image.new('RGB', (w, 10)).save(b, 'TIFF')
        data = b.getvalue()
        if b'\n' in data[:100]:
            return data
    raise AssertionError('no test TIFF with a newline in its first 100 bytes')


def _webp() -> bytes:
    b = io.BytesIO()
    Image.new('RGB', (10, 10)).save(b, 'WEBP')
    return b.getvalue()


def _fresh_process_fields(path, off, n, ext):
    # A fresh interpreter: the TIFF plugin not yet loaded, as in a worker
    # process whose first files are TIFFs.
    code = (f'import sys; sys.path.insert(0, {APP!r}); import media_metadata_scan as m\n'
            f'with m._LazyZipSlice({path!r}, {off}, {n}) as s:\n'
            f'    f = m.extract_one(s, {ext!r})\n'
            f'print(f.get("width"), f.get("height"))')
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_tiff_read_first_in_a_fresh_process(tmp_path):
    data = _tiff_with_newline_in_header()
    w, h = Image.open(io.BytesIO(data)).size
    path, off, n = _slice_over(tmp_path, data)
    assert _fresh_process_fields(path, off, n, '.tiff') == [str(w), str(h)]


def test_webp_read_first_in_a_fresh_process(tmp_path):
    path, off, n = _slice_over(tmp_path, _webp())
    assert _fresh_process_fields(path, off, n, '.webp') == ['10', '10']
