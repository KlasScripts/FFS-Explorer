# Crush review — what's worth considering for ios-ffs-browser

Reviewed `/Users/klastveita/script/crush-forensics-main` (a sibling forensics
tool called Crush) for practices worth considering here. Read-only review;
nothing implemented. Findings only — see the TODO list at the end for what
to actually act on, in priority order.

## What Crush is

Crush is a general-purpose "Digital Forensic Analysis Workbench" (PySide6,
Apache-2.0, actively released with CI/nightly builds and packaged installers
for macOS/Windows/Linux). Same core idea as this project — open a forensic
source and inspect its files/databases without extracting everything first —
but a deliberately broader tool: it opens ZIP/TAR/7z/adb-backup/iTunes-backup
archives *and* plain folders/files, and ships ~15 general-purpose format
viewers (SQLite, Hex, Text, JSON, XML, Plist, SEGB, ABX, LevelDB, MMKV, Image,
Media, Multi-Log, Protobuf, PDF, Realm). It has no concept of "artifact
parsers for known apps" at all — no WhatsApp/Chrome/LINE-specific extraction
logic anywhere. ios-ffs-browser's own value is almost entirely the opposite:
narrow to FFS zips specifically, and deep on app-specific extraction
(record_source citation, recoverable_tables carving, media_fields,
timestamp_fields) that Crush doesn't attempt at all. The overlap is real but
narrower than it first looks: both projects independently built essentially
the same low-level SQLite/LevelDB/plist reading primitives; Crush stops at
"here's the generic file," ios-ffs-browser goes further into "here's what
this specific app's own database *means*."

## Architecture

Crush splits `core/` (format-agnostic engines: SQLite B-tree/WAL reader,
LevelDB reader, VFS abstraction, timestamp decoder, password handling),
`parsers/` (one `AbstractParser` subclass per format — `can_parse(path,
peek_bytes)` sniffs magic bytes, `parse()` returns a `ParseResult`
dataclass), and `viewers/` (QWidget factories keyed by `viewer_type` string,
via a tiny `ViewerRegistry`/`ParserRegistry` pair). A parser is chosen by
asking every registered parser "can you handle this?" against the first 256
bytes — genuinely necessary for Crush's job (an examiner can select *any*
file in *any* folder and something has to guess what it is), and a real,
clean instance of the plugin pattern: `ParserRegistry.candidates()` /
`.best()` is ~15 lines total.

This doesn't map onto ios-ffs-browser's own plugin system as a "switch to
this" recommendation — the two solve different problems. This project's
`artifact_runner.py` is not "what format is this," it's "run WhatsApp's own
known extraction logic against WhatsApp's own known path" — a sniff-based
registry would be solving a problem this project doesn't have (an examiner
here never selects an arbitrary file and asks "what viewer does this need";
they pick a named app from the Apps list). The declarative
module-level-attribute convention (`media_fields`/`record_source`/
`timestamp_fields`/`recoverable_tables`/`core_fields`) is arguably a better
fit for this project's actual shape than an ABC-based interface would be —
it's what lets `WRITING_ARTIFACT_PARSERS.md` read as a checklist instead of
a class-hierarchy diagram, and keeps a parser script exactly as thin as its
own logic requires. Nothing here changed my view that the declarative
convention should stay as-is.

One thing genuinely worth a second look: Crush's `CellLocator` protocol
(`core/cell_locator.py`) generalizes "where does this row/column live on
disk" into a real, tiny interface with **two directions** — `locate_cell`
(row/col → byte range, what `sqlite_carve.locate_live_row` already does)
*and* `locate_offset` (byte range → row/col, the reverse). ios-ffs-browser
has no reverse lookup at all — an examiner looking at raw hex has no way to
ask "which report row does this byte belong to." Worth considering as a
follow-on to the record_source work from this session, not as a rewrite of
anything.

## Testing — the real gap

This is the one area where the comparison isn't close, and it's the most
actionable finding in this whole review.

Crush has a genuine, enforced regression suite (`crush/tests/`, pytest +
pytest-qt): `conftest.py` SHA-256-checks every committed fixture file before
any test runs (`fixtures/checksums.json`) and aborts the whole session if
one has been tampered with; tests are tagged `@pytest.mark.forensic` into
six named categories (Source Immutability, No Side Effects, Read-only
Media, Known-output Verification, Completeness, Reproducibility) and
rendered into a human-readable HTML audit report on every run. The tests
themselves are real, not smoke tests — `test_realm_hex_provenance.py`
opens an actual committed `.realm` fixture, drives the *real* Qt widgets
(`RealmViewer`/`TableViewer`, selecting a specific cell via
`QTableView.setCurrentIndex`), and asserts the resulting hex-highlight
range is real bytes in the real file, not a synthetic re-encoding — its own
docstring says outright it exists because a prior "this works for Realm
too" claim in the changelog turned out to be false and got caught by
writing this exact test. Fixtures are small and mostly reproducible from
source (`generate_all_types_v24.js`, `generate_protobuf_fixtures.py` sit
right next to the binary fixtures they produce), not opaque blobs.

ios-ffs-browser has **zero** automated tests. Every verification this
entire session — the SQLite overflow-page formula, every `record_source`
declaration, every new parser's byte offsets — was a one-off Python script
run once in a scratchpad and then thrown away. That's real, rigorous
verification at the moment it happens (arguably more thorough per-check
than most of Crush's own tests, since it's against genuine casework
archives, not synthetic fixtures) but it protects nothing going forward: a
future change to `sqlite_carve.py` or `chrome_tabs.py` could silently
regress any of it and nothing would fail. The `_cell_local_payload_size`
overflow fix from earlier this session is the clearest example — it took
real, careful effort to verify (following the actual overflow chain across
a real page boundary), and today nothing stops a future edit from breaking
it silently.

The realistic first step, given this project's own real constraints (its
real test archives are multi-GB — the iOS17 JoshHickman image alone is
36GB, nowhere close to committable to git, unlike Crush's own tiny
purpose-built fixtures): extract small, *purpose-built* fixture files the
same way Crush does — a handful of real bytes trimmed down to just what's
needed (a single small SQLite db, one real SNSS/TabState file, a tiny
synthetic LevelDB directory), committed directly into a new
`tests/fixtures/` dir, each with its own small `*.expected.json` of known-
correct output. Most of what would need testing here is pure Python with no
Qt involved at all (`sqlite_carve.locate_live_row`, `chrome_tabs.py`'s three
parse functions, every artifact parser's `run()`) — meaning a real test
suite doesn't need `pytest-qt` or GUI-driving to deliver most of the value;
that's only needed for the handful of things that are genuinely about
widget behavior (the Hex/Text panel sync, the Columns dialog). Start with
what's already been manually verified this session and is cheapest to
freeze: `sqlite_carve._cell_local_payload_size` against a hand-built page
buffer (no archive needed at all, pure arithmetic), then `chrome_tabs.py`'s
three format parsers against the exact byte sequences already confirmed
against real data above.

## Specific techniques worth a closer look

**SQLite overflow handling (`core/sqlite_wal.py`, `_payload_inline_size`)**
— independently confirms the exact formula just derived by hand for
`sqlite_carve._cell_local_payload_size` this session (`X = U-35`, `M =
((U-12)*32)//255 - 23`, `K = M + (P-M) % (U-4)`, return `K if K<=X else M`),
explicitly cited against "SQLite file format spec section 1.5" and
`btreeParseCellPtr()`. Real, independent validation that the formula in
`sqlite_carve.py` is correct — not just plausible-looking.

Crush goes one step further than the fix just shipped here, though:
`_follow_overflow_chain_ex` actually walks the *entire* overflow page
chain, reconstructing the full logical payload across however many pages
it spans, and returns per-segment `(page_num, bytes_taken)` so the decoded
value AND its real physical location (across possibly the base file, a WAL
frame, or both) can both be recovered — not just an accurate on-page-only
span with an "overflows, not shown" flag (what `locate_live_row` does
today). This is a real, concrete follow-on: the hex viewer would need to
support highlighting more than one contiguous span (potentially across two
different files) to take advantage of it, which is a genuinely bigger UI
change than the fix already shipped — worth a deliberate design
conversation before touching, not a quick add. Recorded here as a known,
scoped, real next step rather than a full switch.

**`core/format_db.py`** — a bundled SQLite database (`data/formats.db`) of
known file formats: magic-byte patterns, forensic relevance, platform,
spec links — shown for *any* selected file, even ones with no dedicated
viewer. A genuinely nice examiner-facing idea (see something unfamiliar,
get told what it probably is and why it matters, without needing a parser
for it at all), but it's a real content-curation investment (a database of
formats to build and keep current), not just code — and its actual
motivating need (an examiner opening an arbitrary, unknown file inside an
arbitrary folder) is much weaker here, where the File Browser already knows
which known app owns which known path. `header_scan.py`'s own magic-byte
classification is the proportionate analog this project already has for
its narrower need (routing to hex/text/image in the File Browser); not
recommending the full format-database idea unless a real, recurring "what
IS this unfamiliar file" need shows up in casework.

**Value Inspector / BLOB Inspector (`viewers/value_inspector.py`,
`viewers/blob_inspector.py`)** — genuinely nice, narrow UX ideas, not tied
to Crush's own broader archive-format scope at all, so the scope mismatch
argument above doesn't apply to these two. Value Inspector shows every
plausible interpretation of a selected/pasted value at once (int, float,
a dozen timestamp epochs, UUID, IP address, byte size) — this project
already effectively re-derives "which epoch is this" ad hoc per-parser via
the `timestamp_fields` unit-code convention (`s`/`ms`/`cocoa_s`/`cocoa_ns`/
`webkit_us`), but has no general-purpose "I have a raw number, what could
this be" tool for when an examiner is looking at a hex value that ISN'T
already a declared, mapped column. BLOB Inspector chains byte-level
transforms (base64/hex decode, zlib/gzip decompress) and renders the
result as hex/text/JSON/plist/protobuf — a real, reusable primitive this
project doesn't have anywhere (each parser that needs to decode a nested
blob — protobuf via blackboxprotobuf, NSKeyedArchiver via
`decode_plist_blob`, typedstream — does it once, inline, for its own
narrow case). Both are plausible, bounded, standalone features rather than
architecture changes.

**`ts_decode.py`** (52 lines) is a much smaller, standalone multi-epoch
guesser for the Value Inspector above — not the architecture backing
Crush's own timestamp *display* (that's still per-viewer). Doesn't compete
with or replace this project's own `timestamp_fields`
declared-unit-code-with-UTC/handset/acquisition-mode display convention,
which is more rigorous for the case where the unit IS already known (which
is every declared column in this project) — genuinely a different problem
(known unit, format for display vs. unknown value, guess candidate units).

## Processing techniques & libraries — a closer look

Follow-up pass specifically on efficiency/library choices, prompted by a
direct question after the first review pass (which was architecture/testing-
focused). Checked `pyproject.toml`'s real dependency list against what's
actually used in the source (not just declared — one listed dependency,
`construct`, turned out to have zero real usage anywhere in `core/`/
`parsers/`/`viewers/` after grepping for it; a declared-but-dead dependency,
not a technique worth adopting — recorded here so it isn't rediscovered as a
lead later).

**HEIC/HEIF photo thumbnails — a real, concrete, platform-dependent gap,
confirmed on one platform and reasoned-not-verified on the other.** Crush
lists `pillow-heif>=0.13` as a real dependency specifically because neither
Pillow nor Qt decode HEIC (Apple's default photo format since iOS 11) without
a dedicated codec. Tested directly against this project's own Qt install:
`QImageReader.supportedImageFormats()` DOES include `heic`/`heif` **on this
macOS dev machine** — but that's macOS's own OS-level ImageIO framework
supplying the codec to Qt's platform plugin, not something Qt (or this
project) ships itself. Windows has no equivalent built-in HEIC codec by
default, and nothing in `ffs_explorer.spec` or the Windows CI workflow
bundles one — grepped for `heic`/`imageformats`/`windeployqt` in both, zero
hits. This means a real, common case (an examiner opening an iOS Photos
report's own HEIC image on the actual shipped Windows exe, the project's own
primary target platform) has never been verified to work at all, and there's
good concrete reason to expect it currently doesn't — silently falling back
to `media_viewer.py`'s generic gray-box/unresolvable-path placeholder for
every real HEIC photo. `pillow-heif` (or an equivalent bundled codec) would
fix this identically cross-platform, not dependent on the analysis machine's
own OS install. Worth verifying directly on a real Windows build before
anything else in this section — this is the one finding here with real
forensic consequence (a whole real evidence category silently unviewable),
not just an efficiency nicety.

**Video frame extraction: subprocess `ffmpeg` vs. in-process `av` (PyAV).**
`app/media_viewer.py` shells out to an external `ffmpeg` binary via
`subprocess` for every video frame grab — real, already-documented bundling
fragility (`_find_ffmpeg()`'s own fallback-path comments, "drop ffmpeg.exe
next to the exe") and per-call process-spawn overhead. Crush uses `av`
(PyAV's FFmpeg bindings) instead — decodes in-process, no subprocess spawn,
no separate binary to locate/bundle at all (PyAV ships FFmpeg's own shared
libraries inside the wheel). A real, plausible efficiency and packaging-
simplicity win, but not a slam dunk to switch on paper alone: PyAV's own
bundled shared libraries are a real, different PyInstaller-bundling shape
than a bare `ffmpeg.exe` next to the output folder, and this project's own
video-thumbnail path is already confirmed working today (unlike the HEIC
gap above, which is a real unverified unknown) — worth a real side-by-side
build test before switching, not a assumed improvement.

**Archive-byte caching: this project's own approach is already ahead here,
not behind.** Crush's `core/vfs.py` caches extracted entry bytes in a bare
in-memory `dict[str, bytes]` (`self._read_cache`) with no eviction and no
cross-session persistence — everything read is kept in memory for the life
of the process, and nothing survives closing/reopening the same archive.
This project's own `.zcd` central-directory cache + `casecache.db`
size/mtime-keyed thumbnail cache is a materially more sophisticated design
(persists across sessions, bounded by real keys rather than growing
unbounded) — noting this explicitly so it doesn't get lost: not everything
in Crush is more advanced, and this specific area is a place this project's
own existing work is already the better design.

**Two magic-byte libraries (`python-magic` + `filetype`) vs. this project's
own single `header_scan.py`.** Real, but low-priority given the scope
mismatch already noted above — Crush needs broad, redundant format
detection because an examiner can select any arbitrary unknown file in any
folder; this project's own File Browser already knows which known app owns
which known path for the overwhelming majority of what it routes, the exact
same reasoning `format_db.py` was already set aside for above.

**Password-protected archives (`pyzipper`, AES + legacy ZipCrypto) — a
genuine unknown, not a confirmed gap.** This project has no
password-prompt-on-open capability anywhere today (`grep`'d for
password/encrypted handling at the archive-open level — nothing found
outside of unrelated per-app encryption-caveat/form-field code). Whether a
real Cellebrite/GrayKey FFS export is ever actually password-protected in
practice wasn't established either way this session — flagging as an open
question for the user's own casework experience to answer, not asserting
it's needed.

## Format/artifact viewer support — a closer look

Prompted by a direct question: "we only support SQLite, plist, and XML — does
Crush support more?" Checked ios-ffs-browser's own real routing directly
first, not assumed: `ffs-explorer.py`'s `_maybe_load_structured_preview`
(the function that decides what the bottom preview panel shows for a
File-Browser selection) checks exactly two magic-byte signatures — SQLite
and SEGB. Everything else falls through to `_render_as_text`, which
pretty-prints JSON and XML/plist-as-**XML** as indented text — and
explicitly returns `None` (no structured rendering at all, hex only) for
**binary plist** (`data[:6] == b'bplist': return None`). Binary plist
(`bplist00`) is the dominant real-world plist encoding on an actual iOS
device — far more common than the XML form — so the practical current state
is: SQLite and SEGB get real structured viewers, XML/JSON get a flat
pretty-printed text dump (not an interactive tree), and binary plist gets
**nothing** but raw hex, generically, in the File Browser.

Crush supports Plist/BPlist, ABX, LevelDB, Protobuf, MMKV, and Realm as real,
general, magic-byte-routed viewers — any matching file opens correctly
regardless of which app produced it or whether Crush has ever heard of that
app. Read `parsers/plist_parser.py`, `viewers/abx_viewer.py`, and
`viewers/tree_viewer.py` directly to see how: `PlistParser.can_parse` checks
`bplist` magic OR an XML `<plist` signature (either form, generically), and
for binary plist uses **the same vendored `ccl_bplist` (CCL Forensics)
lineage this project already vendors `ccl_abx.py`/`ccl_leveldb.py`/
`ccl_segb` from** — including `deserialise_NsKeyedArchiver`, decoding
NSKeyedArchiver object graphs generically for *any* plist, not per-parser.
The result renders through `TreeViewer` — one single, generic, reusable
`QTreeView`-based widget (`viewers/tree_viewer.py`, "displays plist, XML, and
other hierarchical data") that takes any nested Python dict/list and renders
it as a collapsible, searchable tree. `AbxViewer` (`viewers/abx_viewer.py`,
81 lines total) is *just* `TreeViewer` plus a reconstructed-XML side pane —
Crush doesn't build a bespoke tree widget per format, it built ONE tree
widget for "any hierarchical structure" and reuses it. `TreeViewer` goes
further still: each tree node can carry its own `_BYTE_RANGE_ROLE`, wired to
`ByteMappedTreeHex` (`viewers/byte_mapped_tree_hex.py`) — clicking one
specific key inside a decoded plist highlights *exactly that key's own
bytes* in the hex pane, a finer-grained citation than this project's own
`record_source` (which cites a whole row/record, never one field within a
decoded structure). LevelDB, being key-value rather than hierarchical, gets
its own `QTableView`-based widget instead (`viewers/leveldb_viewer.py`) —
the pattern is "one generic widget per *data shape*" (tree vs. table), not
one bespoke widget per format.

**The concrete implication for ios-ffs-browser: this is a much cheaper gap
to close than it looks, for three of the four formats.** The actual
*decoding* capability already exists and already works in this codebase —
`ccl_leveldb.py` (vendored, used by `chrome_local_storage.py`),
`ccl_abx.py` (vendored, used by `app_intelligence.py` for
`packages.xml`/`runtime-permissions.xml`), and `blackboxprotobuf` (used by
`segb_viewer.py`) — it's just wired to one specific known parser's one
specific known file, not exposed generically when an examiner opens some
*other*, unanticipated LevelDB/ABX/protobuf-bearing file elsewhere in the
archive that no artifact parser happens to cover yet. Wiring any of these
into the general File Browser preview means adding a magic-byte/structure
check to `_maybe_load_structured_preview` and a lightweight rendering
widget — not writing a new decoder. **Binary plist is the standout case**:
`plistlib.loads` is stdlib, already imported and used elsewhere in this
project (`adapters/ffs.py`, `app_intelligence.py`), and NSKeyedArchiver
deserialization already exists too via `nska_deserialize` (a pip
dependency, already in `requirements.txt`, used by `artifact_runner.py`'s
`decode_plist_blob` helper) — so closing the "binary plist shows only hex"
gap needs zero new dependencies and zero new decoding logic, only wiring an
existing capability into the general preview path plus a tree-rendering
widget to show the result (this project has no generic tree widget
today — `sqlite_viewer.py`/`segb_viewer.py` are both flat `QTableView`s, so
a `TreeViewer`-equivalent would be new UI code, though a much smaller build
than the decoding itself). Given how common real plist evidence is in an
iOS FFS extraction — every app's own `Info.plist`, preference files,
`.plist`-backed caches — this is arguably the single highest-value,
lowest-cost format-support gap identified in this whole review.

## User interaction / workflow — a closer look

Prompted directly: "what about the GUI — not the framework, the actual how
it's used — I think ffs-explorer still needs work here." Read
`crush/docs/handbook.md` in full (the real user-facing usage doc, the
actual signal for how the tool is meant to be operated day to day, not how
it's coded) and sampled `crush/ui/`. Comparing against this project's own
real, already-built interaction patterns (the File Browser/Media/Search/
Artifacts tab model, the shared bottom Hex/Text panel, the Record/
Attachment toggle, the Columns dialog, bookmarks/research-store status
coloring, the Apps node) — not a wholesale redesign, specific, bounded
comparisons.

**One combined, typed filter query vs. a dropdown-plus-textbox.** Crush's
Filesystem panel filter box takes ONE line combining multiple criteria —
`name:rubin type:sqlite` (AND-combined, `type:` matches by detected magic
bytes regardless of extension, `name:` is the default/plain-text case) —
filtering the *entire loaded tree* at once into a flat "every match, full
path" results list, no need to navigate into folders first. Checked this
project's own equivalent directly: `ffs-explorer.py`'s File Browser filter
is a `filter_col_combo` dropdown (pick ONE column to filter by) plus a
separate `filter_input` text box — combining "name contains X" AND "type is
Y" needs either switching the dropdown (losing the other criterion) or
isn't directly expressible as one action at all. A real, bounded, concrete
UX difference: Crush's compact query syntax lets an examiner narrow by
several things at once without touching a dropdown mid-search; this
project's own filter is single-criterion-at-a-time by construction. This
project already background-caches file type detection (`casecache.db`'s
own `header_types` table, per the databases section of CLAUDE.md) — the
underlying data for a `type:` token already exists, this is a filter-UI
change, not a new detection pipeline.

**Interactive, examiner-driven timestamp reinterpretation on a raw,
not-yet-parsed table.** Crush's SQLite Table Viewer lets an examiner
right-click ANY column header and pick **Decode column as timestamp** from
a submenu (Unix s/ms, Mac Absolute, Windows FILETIME, Chrome/WebKit) — the
header gets a `[unix ms]`-style suffix, sorting stays numerically/
chronologically correct, and it's undoable (**Clear timestamp format**).
This project's own timestamp handling is entirely the opposite shape:
`timestamp_fields` is a parser-AUTHOR's ahead-of-time declaration
(`WRITING_ARTIFACT_PARSERS.md`) — genuinely more rigorous for a column the
author already investigated and confirmed a real unit for (this project's
own standing "verify before declaring" rule is stricter than Crush's
own "give the examiner the tool and let them decide" model), but it means
an examiner looking at a RAW, un-parsed table in the plain Database tab
(`sqlite_viewer.py`) — one no artifact parser covers yet — has **no way at
all** to ask "is this integer maybe a Cocoa timestamp?" interactively. This
is a real, concrete, and arguably high-value gap specifically for the
*triage* case this project doesn't optimize for today: an examiner exploring
an app that has no artifact parser written for it yet, deciding whether one
is even worth writing. A bounded, additive feature (a right-click menu on
`sqlite_viewer.py`'s own column headers, reusing the exact unit-code/
`format_ts` conversion machinery `timestamp_fields` already has) rather than
a competing mechanism — the declared, verified `timestamp_fields` path stays
authoritative for any column it already covers.

**"Open as" — a manual override when auto-detection is wrong or
impossible.** Crush's right-click → **Open as** lets an examiner force a
specific viewer regardless of what auto-detection concluded — necessary,
not optional, for MMKV specifically (genuinely no magic bytes at all — the
ONLY way to open one) and useful generally when a file is misidentified.
ios-ffs-browser's own structured-preview routing
(`_maybe_load_structured_preview`) is 100% automatic with no override at
all — a file that's actually a SQLite database but fails the magic-byte
check for some reason (a corrupted header, a nonstandard variant) has no
manual escape hatch to force the SQLite tab open anyway. Small, bounded,
real gap.

**Export integrity — hash manifests, currently absent here entirely.**
Crush's Integrity Mode hashes (SHA-256) every file opened or exported and
writes a `crush-export-hashes.txt` manifest alongside an export — a
genuine, standard chain-of-custody feature. Checked this project's own
export path — no equivalent found anywhere (grepped for "hash"/"sha256" at
the export/`ExportProgressDialog` level; nothing). Given this project's own
extremely high bar for evidentiary correctness (repeated throughout
CLAUDE.md), an examiner exporting a file today has no built-in way to prove
after the fact that the exported copy is byte-identical to what was in the
archive — a real, currently-missing capability, not a nice-to-have UX
polish. Bounded and additive: hash on export, write a manifest file next to
the export, no architecture change.

**One reusable inspection surface, reachable from everywhere, plus a
standalone entry point with no source file at all.** The handbook confirms
the BLOB Inspector (already flagged as a technique worth considering
earlier in this review) is also a deliberate WORKFLOW decision: it's the
exact same non-modal window reachable from a SQLite cell, a LevelDB record,
a Realm freed block, AND a completely standalone **Tools → Paste & Decode…**
menu item that needs no open file at all (paste hex/base64/text copied from
anywhere — another tool, a network capture, whatever — and inspect it
immediately). This project has no equivalent of the standalone entry point
at all — every decode capability here (`decode_plist_blob`, blackboxprotobuf
in `segb_viewer.py`, typedstream) is reachable only by first having the
right kind of file already open in the right specific tab. A `Tools → Paste
& Decode…`-style entry point would be a small, real, and distinctly useful
addition on its own, independent of whether the fuller BLOB Inspector
feature gets built.

**Decode-once-into-a-queryable-SQLite-table as a general pattern.** Crush's
SEGB viewer and Realm viewer both build a temporary SQLite representation
of their own decoded (non-SQL) content specifically so the SAME mature SQL
editor (autocomplete, `json_extract`, joins) works uniformly across formats
that aren't natively SQL, rather than building bespoke search/filter UI per
format. This project's own `segb_viewer.py` already decodes the same kind
of content (protobuf via blackboxprotobuf) but presents it as a plain
`QTableView` with no query capability — worth considering whether SEGB
(and any future LevelDB viewer, if the format-support gap above gets
addressed) could reuse `sqlite_viewer.py`'s own already-built SQL-editor
machinery over a temp SQLite table, rather than each new structured format
needing its own bespoke filter/search widget built from scratch.

**Where the comparison favors this project, or is a wash — stated plainly,
not glossed over.** Crush's own keyboard-shortcut surface is thin (three
shortcuts total: quit, text-search focus, middle-click-to-close-tab) — no
command palette, no keyboard-driven navigation depth to speak of; this
isn't an area where Crush is ahead. And several of this project's own
already-built interaction patterns are genuinely comparable to or better
suited to its narrower job than anything in Crush: the persistent,
row-selection-following `MediaFullViewDialog` (documented in CLAUDE.md,
"non-modal persistent viewer... follows row selection") is the same
"stays open, updates as you keep working" pattern Crush's Value/BLOB
Inspectors use, already precedented here independently; and the Apps node's
own interest-score triage view (a *ranked, scored* "what's worth looking at"
list) has no real analog anywhere in Crush, which has no concept of
per-app forensic scoring at all — Crush's own equivalent ("what's
interesting") is just the format-reference popup naming a format's generic
forensic relevance, not a scored, per-app, per-case ranking. Both real,
legitimate strengths already in place, not gaps to fix.

## Things NOT to adopt, or to adopt cautiously

**Broader archive-format support (TAR/7z/adb backup/iTunes backup).** Real
complexity in `core/vfs.py` (1,382 lines) to support this. ios-ffs-browser's
own scope is deliberately narrower (FFS zips from Cellebrite/GrayKey), and
that narrowness is a real asset, not a gap — CLAUDE.md's own Conventions
section is already emphatic about the main-archive-only, `.zcd`-cached,
direct-seek design being load-bearing for this project's performance and
correctness guarantees. Widening format support would mean re-deriving that
whole discipline per new format. Not recommended absent an actual, real
casework need for a non-FFS source.

**SQLCipher / encrypted-database decryption when a key is independently
known** (Crush: real `sqlcipher3` engine, auto-tries `cipher_compatibility`
presets, an explicit "Advanced" raw-key path for Signal/Session/Molly-style
keystore-derived keys). This is a genuine, real design question, not a
foregone recommendation either way: this project's own standing stance
(confirmed directly this session, re: Signal's SQLCipher store) is "never
decrypt, report presence only" — but Crush's design specifically only
decrypts when the examiner already independently possesses the real key
(a provided passcode, an exported keystore key), never by cracking or
brute-forcing evidence — arguably a materially different act from what
"never decrypt" was written to guard against. Worth a deliberate
conversation with the user about whether the existing rule was written
against *any* decryption or specifically against *guessing/cracking*
one — genuinely ambiguous from the current wording, not something to
resolve unilaterally.

**The general parser-registry/sniff-by-magic-bytes model.** Already
covered above — a good pattern for Crush's problem, not a mismatch to fix
in this project's own declarative, known-app-targeted plugin system.

## TODO (prioritized)

- [ ] **Wire binary-plist decoding into the general File Browser preview
  (`_maybe_load_structured_preview` in `ffs-explorer.py`).** Currently
  returns hex-only for any real `bplist00` file — the single highest
  real-world-impact, lowest-engineering-cost gap in this whole review:
  the decoders already exist and already work in this codebase
  (`plistlib.loads`, stdlib; `nska_deserialize` for NSKeyedArchiver, already
  a dependency), only a magic-byte check plus a rendering widget are
  missing. A minimal first cut could even render via the existing Text tab
  (pretty-printed, like JSON/XML already are) before building a full
  interactive tree widget — real, immediate value with almost no new code,
  a tree widget as a genuine but separate follow-on.
- [ ] **Stand up a real pytest suite with committed micro-fixtures.**
  Highest value, given the complete absence of one today. Start with
  `app/sqlite_carve.py`'s `_cell_local_payload_size` (needs only a
  hand-built byte buffer, no archive) and `app/chrome_tabs.py`'s three
  parse functions (small real SNSS/TabState/legacy-format files, already
  verified this session — extract and commit the minimal real bytes now,
  before the exact verification context is lost). Model the fixture
  layout on Crush's `tests/fixtures/` + `checksums.json` pattern.
- [ ] **Add a `locate_offset` (byte-offset → row/column) reverse lookup**
  alongside `sqlite_carve.locate_live_row`, mirroring Crush's
  `CellLocator.locate_offset`. Lets an examiner click into raw hex and ask
  "which report row is this," the missing direction of the citation
  feature already built this session.
- [ ] **Decide, deliberately, whether "never decrypt" already covers or
  excludes decryption with an examiner-supplied, independently-known
  key** (SQLCipher raw-key style) — a real design question raised by
  Crush's own opposite choice, not something to guess at either way.
- [ ] **Consider a small, standalone "Value Inspector"-style widget** for
  the Hex panel — given an arbitrary selected byte range, show every
  plausible interpretation (the timestamp epochs this project's own
  `timestamp_fields` units already cover, plus int/float/UUID) rather than
  only ever interpreting a value through an already-declared column.
  Bounded, additive, doesn't touch the artifact-parser convention at all.
- [ ] **Consider a small "BLOB Inspector"-style chained-transform helper**
  (base64/hex decode → zlib/gzip decompress → render as hex/text/JSON) as
  a shared utility, since at least three parsers already do one-off
  versions of "decode this nested blob" inline today.
- [ ] **Lower priority: extend `locate_live_row` to follow the full
  overflow chain** (like Crush's `_follow_overflow_chain_ex`) instead of
  reporting only the accurate on-page span — real value, but genuinely
  blocked on the Hex panel supporting more than one highlighted span
  (potentially across two files), which is a bigger, separate UI change;
  don't start this without deciding that piece first.
- [ ] **Verify real HEIC/HEIF photo rendering on an actual Windows frozen
  build** — confirmed working via macOS's own OS-level codec on this dev
  machine, but nothing bundles a codec for Windows and this has never been
  checked there. If it fails (likely, given no HEIC codec is bundled), add
  `pillow-heif` (or equivalent) so `media_viewer.py` can decode it itself
  rather than depending on the analysis machine's own OS install. Highest
  priority in this section — a real evidence category (default iOS photos)
  possibly silently unviewable on the actual shipped platform.
- [ ] **Evaluate `av` (PyAV) as a replacement for the subprocess `ffmpeg`
  call in `media_viewer.py`** — real potential win (no external binary to
  locate/bundle, no per-call process-spawn overhead) but needs an actual
  side-by-side frozen-build test first, since PyAV's own bundled shared
  libraries are a different packaging shape than today's working
  `ffmpeg.exe`-next-to-the-exe approach; don't switch on the strength of
  this comparison alone.
- [ ] **Add export hash manifests (SHA-256) for chain-of-custody.** Real,
  currently entirely-absent capability — hash a file on export, write a
  manifest alongside it (Crush's `crush-export-hashes.txt` is a reasonable
  model). Bounded change to the existing export path
  (`ExportProgressDialog`/`ExtractorWorker`), no architecture impact, real
  forensic value given this project's own evidentiary-correctness bar.
- [ ] **Consider a combined `name:`/`type:` typed filter syntax for the
  File Browser**, replacing or supplementing the current filter-column
  dropdown + separate text box, so multiple criteria can be expressed in
  one line without switching widgets mid-search. The underlying type data
  already exists (`casecache.db`'s `header_types` cache) — this is a
  filter-UI change, not a new detection pipeline.
- [ ] **Consider an interactive "decode this column as a timestamp"
  right-click on `sqlite_viewer.py`'s own raw table view**, for a table no
  artifact parser covers yet — reusing the existing unit-code/`format_ts`
  conversion machinery, not a new one. Deliberately secondary to, never
  overriding, an already-declared `timestamp_fields` column — this fills
  the "not yet parsed, still triaging" gap `timestamp_fields` doesn't
  cover, not a replacement for it.
- [ ] **Consider a `Tools → Paste & Decode…`-style standalone entry point**
  for this project's own existing decode helpers (`decode_plist_blob`,
  blackboxprotobuf) — inspect pasted hex/base64/text without needing a
  source file already open in a specific tab first. Small, real, useful on
  its own even without the fuller BLOB-Inspector-style feature.
- [ ] **Consider a manual "open as" override** for the File Browser's
  structured-preview routing, for the rare case auto-detection gets it
  wrong (a nonstandard/corrupted header on an otherwise-real SQLite file,
  for example) — currently no escape hatch exists at all.
- [ ] **Optional, low-cost: add a minimal `ruff` config** (Crush's own is
  a modest 4-rule subset: `E4`,`E7`,`E9`,`F` — not a full strict lint
  pass) as a cheap first step toward *some* static checking, given this
  project currently has none at all. Skip `mypy strict` — this project's
  own declarative, duck-typed `paths` dict convention across every
  artifact parser would fight a strict type checker constantly for
  little real benefit; if type-checking is wanted at all, scope it to
  `app/sqlite_carve.py`/`app/chrome_tabs.py`-style pure-logic modules
  only, matching how Crush itself excludes its own `ui`/`viewers`/`tests`
  from strict mode.
