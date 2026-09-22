"""ccl_chromium_indexeddb_structures.py — vendored ArtifactLocation helper,
a deliberately TRIMMED extraction (not the whole file), for
ccl_chromium_indexeddb.py's own `IndexedDbRecord.record_location` property.

Origin : https://github.com/cclgroupltd/ccl_chromium_reader
         (ccl_chromium_reader/structures.py's `ArtifactLocation`, and the
         `ArtifactLocationProtocol` class it depends on from
         ccl_chromium_reader/profile_folder_protocols.py), pinned to
         commit ef840de30221c4d65bc96d2f4d9057e9ef2f526d — same pin as
         every other file vendored from this repo today.
Author : Alex Caithness, CCL Forensics
License: MIT (Copyright 2020, CCL Forensics) — see the license header
         below, reproduced verbatim from the original.

Both classes below are copied VERBATIM from their real upstream files —
nothing inside either class body is modified. What's deliberately
different from a normal vendoring pass here is SCOPE: the real
profile_folder_protocols.py is a ~200-line file of `typing.Protocol`
declarations for ccl_chromium_reader's own much larger public API
(BrowserProfileProtocol, HistoryRecordProtocol, CacheRecordProtocol, and
more) — none of which this project's own narrow use of
ccl_chromium_indexeddb.py (just IndexedDB record iteration, not the
wider ccl_chromium_reader profile-abstraction layer) ever touches or
imports. Vendoring that whole file, plus its own further dependency on
profile_folder_protocols.py's `from .common import KeySearch`, would have
pulled in real dead code with its own further dependency chain for the
sake of one 6-line Protocol class. `ArtifactLocationProtocol` is
extracted alone instead, confirmed to need no import of its own beyond
stdlib `typing` (it references no other name from common.py or
elsewhere) — a real, disclosed trim, not a silent one, consistent with
this project's own standing preference elsewhere (chrome_shared.py,
known_evidence_patterns.py's removal) for vendoring/keeping only what's
actually exercised rather than a whole upstream module wholesale.

Copyright 2020, CCL Forensics

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

import typing


# From ccl_chromium_reader/profile_folder_protocols.py, verbatim.
class ArtifactLocationProtocol(typing.Protocol):
    @property
    def source_file(self) -> str:
        raise NotImplementedError()

    @property
    def offset(self) -> typing.Optional[int]:
        raise NotImplementedError()

    @property
    def friendly_string(self) -> str:
        raise NotImplementedError()


# From ccl_chromium_reader/structures.py, verbatim except the import of
# ArtifactLocationProtocol above (originally
# `from ccl_chromium_reader.profile_folder_protocols import
# ArtifactLocationProtocol`), redirected to the copy in this same file.
class ArtifactLocation(ArtifactLocationProtocol):
    def __init__(self, source_file: str, offset: typing.Optional[int], friendly_string: str):
        self._source_file = source_file
        self._offset = offset
        self._friendly_string = friendly_string

    @property
    def source_file(self) -> str:
        return self._source_file

    @property
    def offset(self) -> typing.Optional[int]:
        return self._offset

    @property
    def friendly_string(self) -> str:
        return self._friendly_string

    def __str__(self):
        return self._friendly_string
