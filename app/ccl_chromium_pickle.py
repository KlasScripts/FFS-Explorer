"""ccl_chromium_pickle.py — vendored Chromium base::Pickle reader.

Origin : https://github.com/cclgroupltd/ccl_chromium_reader
          (ccl_chromium_reader/serialization_formats/ccl_easy_chromium_pickle.py),
          pinned commit ef840de30221c4d65bc96d2f4d9057e9ef2f526d (the exact
          commit Hindsight's own requirements.txt pins:
          `ccl_chromium_reader @ git+https://github.com/cclgroupltd/
          ccl_chromium_reader.git@ef840de30221c4d65bc96d2f4d9057e9ef2f526d`).
Author : Alex Caithness, CCL Forensics — same author/lineage as ccl_leveldb.py/
         ccl_simplesnappy.py/ccl_segb/ccl_abx.py already vendored in this project.
License: MIT (Copyright 2022, CCL Forensics) — see the license header below,
         reproduced verbatim from the original.

Unmodified — no import to adjust (stdlib only: io/datetime/struct/os).
`EasyPickleIterator` is a pythonic reader for Chromium's base::Pickle wire
format (a self-describing 4-byte total-length header, then 4-byte-aligned
fields) — used throughout Chromium for many different serialized blobs, not
just one file format. This project vendors it specifically to read: (1) the
per-record navigation-entry payload inside Chrome-for-Android's SNSS
Session/Tab-restore files under app_chrome/Default/Sessions/ (see
ccl_chromium_snss.py, vendored alongside this file from the same pinned
commit), and (2) the native WebContentsState blob embedded inside Chrome-
for-Android's own app_tabs/<id>/tab<N> TabState files (see
artifacts/android/chrome_app_tabs.py) — confirmed by direct reverse-
engineering against this project's own real Android 14 JoshHickman data
that both use the identical base::Pickle wire format for their own embedded
NavigationEntry/SerializedNavigationEntry payloads. Added 2026-09-05,
alongside ccl_chromium_snss.py and the vendored app/chrome_page_state.py
(from Hindsight, Apache-2.0) — found by directly reviewing Hindsight (a
real, actively-maintained Chrome forensics tool), which depends on this
exact same ccl_chromium_reader library for its own SNSS/tab-restore parsing
rather than a hand-rolled Pickle reader.

Copyright 2022, CCL Forensics

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import io
import datetime
import struct
import os


__version__ = "0.1"
__description__ = "Module for reading Chromium Pickles."
__contact__ = "Alex Caithness"


class EasyPickleException(Exception):
    ...


class EasyPickleIterator:
    """
    A pythonic implementation of the PickleIterator object used in various places in Chrom(e|ium).
    """
    def __init__(self, data: bytes, alignment: int=4):
        """
        Takes a bytes buffer and wraps the EasyPickleIterator around it
        :param data: the data to be wrapped
        :param alignment: (optional) the number of bytes to align reads to (default: 4)
        """
        self._f = io.BytesIO(data)
        self._alignment = alignment

        self._pickle_length = self.read_uint32()
        if len(data) != self._pickle_length + 4:
            raise EasyPickleException("pickle length invalid")

    def __enter__(self) -> "EasyPickleIterator":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self._f.close()

    def read_aligned(self, length: int) -> bytes:
        """
        reads the number of bytes specified by the length parameter. Aligns the buffer afterwards if required.
        :param length: the length od data to be read
        :return: the data read (without the alignment padding)
        """
        raw = self._f.read(length)
        if len(raw) != length:
            raise EasyPickleException(f"Tried to read {length} bytes but only got {len(raw)}")

        align_count = self._alignment - (length % self._alignment)
        if align_count != self._alignment:
            self._f.seek(align_count, os.SEEK_CUR)

        return raw

    def read_uint16(self) -> int:
        raw = self.read_aligned(2)
        return struct.unpack("<H", raw)[0]

    def read_uint32(self) -> int:
        raw = self.read_aligned(4)
        return struct.unpack("<I", raw)[0]

    def read_uint64(self) -> int:
        raw = self.read_aligned(8)
        return struct.unpack("<Q", raw)[0]

    def read_int16(self) -> int:
        raw = self.read_aligned(2)
        return struct.unpack("<h", raw)[0]

    def read_int32(self) -> int:
        raw = self.read_aligned(4)
        return struct.unpack("<i", raw)[0]

    def read_int64(self) -> int:
        raw = self.read_aligned(8)
        return struct.unpack("<q", raw)[0]

    def read_bool(self) -> bool:
        raw = self.read_int32()
        if raw == 0:
            return False
        elif raw == 1:
            return True
        else:
            raise EasyPickleException("bools should only contain 0 or 1")

    def read_single(self) -> float:
        raw = self.read_aligned(4)
        return struct.unpack("<f", raw)[0]

    def read_double(self) -> float:
        raw = self.read_aligned(8)
        return struct.unpack("<d", raw)[0]

    def read_string(self) -> str:
        length = self.read_uint32()
        raw = self.read_aligned(length)
        return raw.decode("utf-8")

    def read_string16(self) -> str:
        length = self.read_uint32() * 2  # character count
        raw = self.read_aligned(length)
        return raw.decode("utf-16-le")

    def read_datetime(self) -> datetime.datetime:
        return datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=self.read_uint64())
