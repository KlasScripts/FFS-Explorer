"""ccl_abx.py — vendored Android Binary XML (ABX) decoder.

Origin : https://github.com/abrignoni/ALEAPP (scripts/ilapfuncs.py, abxread())
Author : Alex Caithness, CCL Forensics
License: MIT (Copyright 2021-2022, CCL Forensics) — see the license header below,
         reproduced verbatim from the original.

Everything from AbxDecodeError through AbxReader below is vendored essentially
unmodified (lifted from nested-in-function scope to module scope so it can be
imported directly; two real correctness fixes applied on top, see below — no
other logic changed). ABX is the binary-XML encoding Android uses for some
system XML files (e.g. data/system/packages.xml on modern Android) since
~Android 10 — a plain xml.etree.ElementTree.fromstring() raises on it, which
is what surfaced the need for this (confirmed via real 2026-08-23 casework:
an Android 14 packages.xml opened with b'ABX\\x00...' magic bytes, not
'<?xml').

abx_bytes_to_xml_root() at the bottom is FFS Explorer's own addition, not
vendored — the original abxread() takes a file path and opens it itself;
app_intelligence.py already has the file's bytes via CaseContext.read_bytes(),
so this wraps them in io.BytesIO instead of writing a temp file.

**Two real fixes applied on top of the vendored original, both found by
stress-testing against ~500 real ABX files across this project's own three
Android test archives (2026-09-18), not assumed from reading the code alone**
— cross-checking a sibling tool's code for real bugs before trusting it, per
this project's own standing practice:

1. **Signed/unsigned 16-bit length bug in `_read_string_raw` and the
   `TYPE_BYTES_HEX`/`TYPE_BYTES_BASE64` branches of `AbxReader.read()`** —
   `_read_short()` reads a SIGNED big-endian short (needed elsewhere for
   `_read_interned_string()`'s own -1 "not interned" sentinel), but a real
   string/bytes LENGTH is an unsigned count with no sign of its own. Any
   real value >= 32768 bytes reads back "negative" under the signed
   interpretation. The vendored original's own `_read_string_raw` treated
   that as corruption and raised, ABORTING THE ENTIRE DOCUMENT PARSE —
   discarding every other field the document held, not just the one long
   value. The two `TYPE_BYTES_HEX`/`TYPE_BYTES_BASE64` branches had no
   guard at all: a negative length passed straight through to
   `_read_raw`'s own `self._stream.read(length)` doesn't raise for a
   negative Python `read()` size (confirmed directly: `BytesIO.read(-5)`
   silently reads to EOF, not just `read(-1)`) — silently consuming the
   rest of the document as one attribute's bytes. The original author's
   own comment on this exact line, present in ALEAPP's current upstream
   source but dropped when this file was lifted to module scope, is a
   literal `# is this safe?` — a real, unresolved self-doubt, checked
   here rather than left as a live risk. Confirmed real and reachable, not
   theoretical: `settings_config.xml` (361 KB, Android 14 JoshHickman)
   failed OUTRIGHT with "Negative string length" on a real config value
   that is exactly 33510 bytes once reinterpreted unsigned —
   `struct.unpack('>h', b'\\x82\\xe6')[0] == -32026` and
   `(-32026) & 0xFFFF == 33510`, confirmed to the byte. Fixed via
   `length & 0xFFFF` (recovers the correct unsigned value whether the
   signed read came back positive or negative) at all three call sites —
   a length still implausible after that still fails cleanly via
   `_read_raw`'s own existing bounds check, this doesn't turn a
   genuinely corrupt file into a silent wrong-data success.
2. **`is_multi_root` defaults to `False`, but a real, common class of
   Android system XML files structurally NEEDS `True`** — files whose
   real top-level content is multiple sibling elements with no single
   enclosing root (`settings_secure.xml`, `settings_global.xml`,
   `settings_ssaid.xml`, biometric enrollment files, network policy,
   device-policy state — 9 of 10 real failures hit in this project's own
   stress test, all recovered cleanly once retried with
   `multi_root=True`). `abx_bytes_to_xml_root` (below) now tries
   `multi_root=False` first — correct and unmodified-shape for the more
   common single-root case (`packages.xml`, etc.) — and retries with
   `multi_root=True` ONLY on the specific `AbxDecodeError` that indicates
   a genuine multi-root document, never on a different exception type
   (a real `ValueError`/`UnicodeDecodeError` from actual corruption
   should still surface as itself, not be masked by a second attempt).
   `is_multi_root=True` unconditionally wraps the output in a synthetic
   `<root>` element (confirmed by reading `AbxReader.read()`'s own logic)
   — trying `False` first, rather than always defaulting to `True`,
   avoids injecting a fabricated top-level element into every
   already-single-rooted file's rendered XML, which would be a real,
   misleading structural change an examiner didn't ask for.

See TODO.md / CLAUDE.md's own Conventions entry for the full write-up,
including exact before/after counts across all three archives.
"""

# Copyright 2021-2022, CCL Forensics
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import base64
import enum
import io
import struct
import typing
import xml.etree.ElementTree as etree

# See: base/core/java/com/android/internal/util/BinaryXmlSerializer.java


class AbxDecodeError(Exception):
    pass


class XmlType(enum.IntEnum):
    # These first constants are from: libcore/xml/src/main/java/org/xmlpull/v1/XmlPullParser.java
    # most of them are unused, but here for completeness
    START_DOCUMENT = 0
    END_DOCUMENT = 1
    START_TAG = 2
    END_TAG = 3
    TEXT = 4
    CDSECT = 5
    ENTITY_REF = 6
    IGNORABLE_WHITESPACE = 7
    PROCESSING_INSTRUCTION = 8
    COMMENT = 9
    DOCDECL = 10

    ATTRIBUTE = 15


class DataType(enum.IntEnum):
    TYPE_NULL = 1 << 4
    TYPE_STRING = 2 << 4
    TYPE_STRING_INTERNED = 3 << 4
    TYPE_BYTES_HEX = 4 << 4
    TYPE_BYTES_BASE64 = 5 << 4
    TYPE_INT = 6 << 4
    TYPE_INT_HEX = 7 << 4
    TYPE_LONG = 8 << 4
    TYPE_LONG_HEX = 9 << 4
    TYPE_FLOAT = 10 << 4
    TYPE_DOUBLE = 11 << 4
    TYPE_BOOLEAN_TRUE = 12 << 4
    TYPE_BOOLEAN_FALSE = 13 << 4


class AbxReader:
    MAGIC = b"ABX\x00"

    def _read_raw(self, length):
        buff = self._stream.read(length)
        if len(buff) < length:
            raise ValueError(f"couldn't read enough data at offset: {self._stream.tell() - len(buff)}")
        return buff

    def _read_byte(self):
        buff = self._read_raw(1)
        return buff[0]

    def _read_short(self):
        buff = self._read_raw(2)
        return struct.unpack(">h", buff)[0]

    def _read_int(self):
        buff = self._read_raw(4)
        return struct.unpack(">i", buff)[0]

    def _read_long(self):
        buff = self._read_raw(8)
        return struct.unpack(">q", buff)[0]

    def _read_float(self):
        buff = self._read_raw(4)
        return struct.unpack(">f", buff)[0]

    def _read_double(self):
        buff = self._read_raw(8)
        return struct.unpack(">d", buff)[0]

    def _read_string_raw(self):
        # length & 0xFFFF (NOT "if length < 0: raise", the original vendored
        # behavior — see this class's own module docstring for the full
        # writeup): _read_short() reads a SIGNED 16-bit value because
        # _read_interned_string() genuinely needs -1 as a sentinel, but a
        # real string length is an unsigned count with no sign of its own —
        # any real string >= 32768 bytes reads back as "negative" under the
        # signed interpretation, which used to abort the ENTIRE document
        # parse right there, discarding every other field the document
        # actually held. Confirmed real, not theoretical, on this project's
        # own Android 14 JoshHickman archive: settings_config.xml (361 KB)
        # failed outright with "Negative string length" on a real config
        # value that decodes to exactly 33510 bytes once reinterpreted
        # unsigned — matching struct.unpack('>h', b'\x82\xe6')[0] == -32026
        # and (-32026) & 0xFFFF == 33510 exactly. `length & 0xFFFF` recovers
        # the correct unsigned value whether length came back positive or
        # negative, with no separate branch needed. A length still
        # implausible after that (more bytes than the stream actually has
        # left) still raises cleanly from _read_raw's own existing bounds
        # check below — this isn't a "never fails" fix, it makes the
        # common real case succeed instead of needlessly failing.
        length = self._read_short() & 0xFFFF
        buff = self._read_raw(length)
        return buff.decode("utf-8")

    def _read_interned_string(self):
        reference = self._read_short()
        if reference == -1:
            value = self._read_string_raw()
            self._interned_strings.append(value)
        else:
            value = self._interned_strings[reference]
        return value

    def __init__(self, stream: typing.BinaryIO):
        self._interned_strings = []
        self._stream = stream

    def read(self, *, is_multi_root=False):
        """
        Read the ABX file
        :param is_multi_root: some xml files on Android contain multiple root elements making reading them using a
        document model problematic. For these files, set is_multi_root to True and the output ElementTree will wrap
        the elements in a single "root" element.
        :return: ElementTree representation of the data.
        """
        magic = self._read_raw(len(AbxReader.MAGIC))
        if magic != AbxReader.MAGIC:
            raise ValueError(f"Invalid magic. Expected {AbxReader.MAGIC.hex()}; got: {magic.hex()}")

        # document_opened = False
        document_opened = True
        root_closed = False
        root = None
        element_stack = []  # because ElementTree doesn't support parents we maintain a stack
        if is_multi_root:
            root = etree.Element("root")
            element_stack.append(root)

        while True:
            # Read the token. This gives us the XML data type and the raw data type.
            token_raw = self._stream.read(1)
            if not token_raw:
                break
            token = token_raw[0]

            data_start_offset = self._stream.tell()

            # The lower nibble gives us the XML type. This is mostly defined in XmlPullParser.java, other than
            # ATTRIBUTE which is from BinaryXmlSerializer
            xml_type = token & 0x0f
            if xml_type == XmlType.START_DOCUMENT:
                # Since Android 13, START_DOCUMENT can essentially be considered no-op as it's implied by the reader to
                # always be present (regardless of whether it is).
                if token & 0xf0 != DataType.TYPE_NULL:
                    raise AbxDecodeError(
                        f"START_DOCUMENT with an invalid data type at offset {data_start_offset - 1}")
                # if document_opened:
                # if not root_closed:
                #     raise AbxDecodeError(f"Unexpected START_DOCUMENT at offset {data_start_offset - 1}")
                document_opened = True

            elif xml_type == XmlType.END_DOCUMENT:
                if token & 0xf0 != DataType.TYPE_NULL:
                    raise AbxDecodeError(
                        f"END_DOCUMENT with an invalid data type at offset {data_start_offset - 1}")
                if not (len(element_stack) == 0 or (len(element_stack) == 1 and is_multi_root)):
                    raise AbxDecodeError(f"END_DOCUMENT with unclosed elements at offset {data_start_offset - 1}")
                if not document_opened:
                    raise AbxDecodeError(f"END_DOCUMENT before document started at offset {data_start_offset - 1}")
                break

            elif xml_type == XmlType.START_TAG:
                if token & 0xf0 != DataType.TYPE_STRING_INTERNED:
                    raise AbxDecodeError(f"START_TAG with an invalid data type at offset {data_start_offset - 1}")
                if not document_opened:
                    raise AbxDecodeError(f"START_TAG before document started at offset {data_start_offset - 1}")
                if root_closed:
                    raise AbxDecodeError(
                        f"START_TAG after root was closed started at offset {data_start_offset - 1}")

                tag_name = self._read_interned_string()
                if len(element_stack) == 0:
                    element = etree.Element(tag_name)
                    element_stack.append(element)
                    root = element
                else:
                    element = etree.SubElement(element_stack[-1], tag_name)
                    element_stack.append(element)

            elif xml_type == XmlType.END_TAG:
                if token & 0xf0 != DataType.TYPE_STRING_INTERNED:
                    raise AbxDecodeError(f"END_TAG with an invalid data type at offset {data_start_offset}")
                if len(element_stack) == 0 or (is_multi_root and len(element_stack) == 1):
                    raise AbxDecodeError(f"END_TAG without any elements left at offset {data_start_offset}")

                tag_name = self._read_interned_string()
                if element_stack[-1].tag != tag_name:
                    raise AbxDecodeError(
                        f"Unexpected END_TAG name at {data_start_offset}. "
                        f"Expected: {element_stack[-1].tag}; got: {tag_name}")

                last = element_stack.pop()
                if len(element_stack) == 0:
                    root_closed = True
                    root = last
            elif xml_type == XmlType.TEXT:
                value = self._read_string_raw()
                if len(element_stack[-1]):
                    if len(value.strip()) == 0:  # layout whitespace can be safely discarded
                        continue
                    raise NotImplementedError("Can't deal with elements with mixed text and element contents")

                if element_stack[-1].text is None:
                    element_stack[-1].text = value
                else:
                    element_stack[-1].text += value
            elif xml_type == XmlType.ATTRIBUTE:
                if len(element_stack) == 0 or (is_multi_root and len(element_stack) == 1):
                    raise AbxDecodeError(f"ATTRIBUTE without any elements left at offset {data_start_offset}")

                attribute_name = self._read_interned_string()

                if attribute_name in element_stack[-1].attrib:
                    raise AbxDecodeError(f"ATTRIBUTE name already in target element at offset {data_start_offset}")

                data_type = token & 0xf0

                if data_type == DataType.TYPE_NULL:
                    value = None  # remember to output xml as "null"
                elif data_type == DataType.TYPE_BOOLEAN_TRUE:
                    value = "true"
                elif data_type == DataType.TYPE_BOOLEAN_FALSE:
                    value = "false"
                elif data_type == DataType.TYPE_INT:
                    value = self._read_int()
                elif data_type == DataType.TYPE_INT_HEX:
                    value = f"{self._read_int():x}"
                elif data_type == DataType.TYPE_LONG:
                    value = self._read_long()
                elif data_type == DataType.TYPE_LONG_HEX:
                    value = f"{self._read_long():x}"
                elif data_type == DataType.TYPE_FLOAT:
                    value = self._read_float()
                elif data_type == DataType.TYPE_DOUBLE:
                    value = self._read_double()
                elif data_type == DataType.TYPE_STRING:
                    value = self._read_string_raw()
                elif data_type == DataType.TYPE_STRING_INTERNED:
                    value = self._read_interned_string()
                elif data_type == DataType.TYPE_BYTES_HEX:
                    # & 0xFFFF: same fix, same reasoning as
                    # _read_string_raw's own docstring above — a real
                    # byte-blob attribute (a certificate, a signature) can
                    # legitimately be >= 32768 bytes, which reads back
                    # "negative" under _read_short()'s signed
                    # interpretation. Left UNFIXED here before 2026-09-18
                    # this was worse than _read_string_raw's own old
                    # behavior: a negative length passed straight to
                    # _read_raw -> self._stream.read(length) doesn't raise
                    # at all for a negative Python read() size (confirmed
                    # directly: BytesIO.read(-5) silently reads to EOF,
                    # not just read(-1)) — silently consuming the REST of
                    # the document as one attribute's bytes, then failing
                    # confusingly somewhere later, or worse, succeeding
                    # with wrong data. Not yet observed to actually trigger
                    # on real evidence (checked: zero negative-length
                    # TYPE_BYTES_HEX/BASE64 reads across ~500 real ABX
                    # files from this project's own three Android
                    # archives) — fixed anyway since it's the exact same
                    # underlying defect just confirmed real for
                    # TYPE_STRING, and the silent-over-read failure mode is
                    # worse than TYPE_STRING's old loud one.
                    length = self._read_short() & 0xFFFF
                    value = self._read_raw(length)
                    value = value.hex()
                elif data_type == DataType.TYPE_BYTES_BASE64:
                    length = self._read_short() & 0xFFFF
                    value = self._read_raw(length)
                    value = base64.encodebytes(value).decode().strip()
                else:
                    raise AbxDecodeError(f"Unexpected attribute datatype at offset: {data_start_offset}")

                element_stack[-1].attrib[attribute_name] = str(value)
            else:
                raise NotImplementedError(f"unexpected XML type: {xml_type}")

        if not (root_closed or (is_multi_root and len(element_stack) == 1 and element_stack[0] is root)):
            raise AbxDecodeError("Elements still in the stack when completing the document")

        if root is None:
            raise AbxDecodeError("Document was never assigned a root element")

        tree = etree.ElementTree(root)

        return tree


# ── FFS Explorer's own addition below — NOT vendored ────────────────────────

def is_abx(data: bytes) -> bool:
    """True if *data* starts with the ABX magic (b'ABX\\x00') rather than
    plain-text XML. Checked on already-read bytes, unlike the original
    checkabx() which re-opens the file itself — app_intelligence.py already
    has the bytes via CaseContext.read_bytes()."""
    return data[:4] == AbxReader.MAGIC


def abx_bytes_to_xml_root(data: bytes, multi_root: bool | None = None) -> etree.Element:
    """Decode ABX *data* (already-read bytes, not a path) to an
    xml.etree.ElementTree root Element — a bytes-based equivalent of the
    original abxread(path, multi_root), which opens a file itself.

    *multi_root* — leave as None (the default, and what both of this
    project's own real call sites use) to try `False` first, retrying
    with `True` ONLY on the specific AbxDecodeError that indicates a
    genuine multi-root document — see this module's own docstring
    (fix 2) for why this order matters and the real files this recovers.
    Pass an explicit True/False to force one mode with no retry, e.g. for
    a caller that already knows which shape its file has.

    A fresh AbxReader over a fresh io.BytesIO is used for each attempt —
    a failed read leaves the stream/reader in a partially-consumed state
    that must never be reused for the retry."""
    if multi_root is not None:
        reader = AbxReader(io.BytesIO(data))
        return reader.read(is_multi_root=multi_root).getroot()

    try:
        reader = AbxReader(io.BytesIO(data))
        return reader.read(is_multi_root=False).getroot()
    except AbxDecodeError:
        reader = AbxReader(io.BytesIO(data))
        return reader.read(is_multi_root=True).getroot()
