"""artifact_runner.py — loads and runs artifact parser scripts.

Artifact scripts live in artifacts/ios/ or artifacts/android/.

This is the terse technical reference for every field a parser module can
declare. WRITING_ARTIFACT_PARSERS.md (repo root) is the example-driven,
examiner-facing how-to covering the same ground — read that one first if
you're writing a new parser rather than looking something up; this
docstring and CLAUDE.md's Conventions section are where the deeper
reasoning behind each field lives once you already know it exists.

Two script APIs are supported:

Single-file (legacy, iOS-style):
    name         str        — human-readable label
    target_paths list[str]  — one or more ui_paths tried in order
                              (e.g. "mobile/Library/SMS/sms.db")
    run(db)                 — receives a sqlite3.Connection; returns list[dict]

Multi-file (new, for scripts that need several databases):
    name      str            — human-readable label
    app_path  str            — base path of the app within the user partition
                               (e.g. "data/data/com.whatsapp"). Android
                               package dirs and fixed iOS OS-level paths
                               (e.g. "mobile/Library/SMS") only — a
                               THIRD-PARTY iOS app's own container is
                               GUID-named per install, not usable here;
                               such a module declares app_group instead
                               (see below), never both.
    app_group str            — alternative to app_path, third-party iOS
                               apps only: the app's stable App Group
                               identifier (e.g.
                               "group.net.whatsapp.WhatsApp.shared"),
                               resolved to this device's actual GUID-named
                               container via the case's own guid_to_bundle
                               map (see _resolve_app_group_base). Confirm
                               the exact string against a real
                               extraction's guid_to_bundle first — it is
                               NOT the app's bundle id, and not guessable
                               (e.g. WhatsApp's own bundle id is
                               net.whatsapp.WhatsApp, a different string
                               from its App Group above).
    app_bundle_id str        — added 2026-08-25 for the case app_path/
                               app_group don't cover: an iOS third-party
                               app whose real content lives in its OWN
                               Data/Application container (not an OS path,
                               not an App Group) — confirmed this is the
                               MORE common case, not the exception; WhatsApp
                               (App Group) was the first parser built here
                               and happened to set the app_path/app_group-
                               only precedent, but most apps checked since
                               keep their real data in their own container.
                               Value is the app's real bundle id (e.g.
                               "com.burbn.instagram") — resolved the same
                               way app_group is, via guid_to_bundle, just
                               against Data/Application instead of Shared/
                               AppGroup (see _resolve_app_bundle_base).
                               Use app_path OR app_group OR app_bundle_id,
                               never more than one.
    files     dict[str,str]  — {key: subpath} where subpath is relative to
                               app_path/app_group/app_bundle_id (e.g.
                               {"msgstore": "databases/msgstore.db"}).
                               subpath MAY contain glob characters (*, ?, [) —
                               added 2026-08-25 for a per-install-varying
                               filename (e.g. an account-id-named database,
                               "DirectSQLiteDatabase/*.db" — see
                               artifacts/ios/instagram.py) — resolved
                               against the archive's real entries once at
                               run time (see _resolve_glob_subpath). Picks
                               the FIRST match found; if more than one file
                               could match (e.g. two account dbs from two
                               logged-in accounts), only one is extracted —
                               a known, honest limitation, not silently
                               handled as "all of them."
    optional_files dict[str,str]
                             — like files, but extracted only when present in the
                               archive; a missing optional file is not an error
                               and its key is simply absent from paths
                               (e.g. {"wal": "Photos.sqlite-wal"}). Also
                               supports a glob subpath, same as files above.
    run(paths)               — receives dict[str, str] mapping each key to the
                               extracted file's path on disk; returns list[dict]

Optional module-level attributes, read by app/artifact_viewer.py. None of
these are required to get a working report — add only the ones your app's
data actually needs; every one below is purely declarative (say WHAT, not
HOW) and has real code elsewhere doing the actual work:

    description str — where this data comes from and any known reliability
                       caveats; shown on the report's "Report Notes/Warning"
                       tree entry (not inline on the report itself — a long
                       description there used to push the table down/off-
                       screen).
    warning     str — separate from description; something the examiner
                       must read before trusting the report (e.g. a
                       carving/recovery limitation, not a routine note).
                       Styled in bold red on the same Notes/Warning page,
                       above description. Most scripts don't need one —
                       reserve it for a genuine "read this or you'll
                       misinterpret the data" caveat, not general notes.
    timestamp_fields dict[str, str] — {field_name: unit_code} for every
                       timestamp-shaped output field (unit_code one of
                       "s"/"ms"/"cocoa_s"/"cocoa_ns"); the field must hold
                       the raw numeric value, never a pre-formatted string
                       — the Report table formats it per the case's
                       UTC/handset/acquisition setting at display time (see
                       CLAUDE.md's timestamp conventions).
    recoverable_tables, recovery_field_notes, recovery_source_labels —
                       retired 2026-11-21 (ASSUMPTIONS.md A-95): a main
                       report holds live rows only; deleted and changed
                       records are a report_type = "recovered_records"
                       report of their own (deleted_of names the main
                       report; field_notes is the caveat). The runner no
                       longer reads them; tests/test_recovered_reports.py
                       stops a shipped parser declaring them.
    media_fields list[str] — output field names holding an archive ui_path
                       (never a filesystem path, never bytes the parser
                       copied itself) to an attachment/media file; rendered
                       as a thumbnail in the Report table, opened full-size
                       on double-click (app/artifact_media.py). Only
                       declare this for a column that's a REAL local path —
                       check first whether the underlying data is instead a
                       remote URL or a content://-style runtime reference,
                       which this can't resolve.
    hidden_fields list[str] — output field names to keep out of the
                       Report table entirely — for a field a parser needs
                       internally (e.g. a joined table's raw rowid, kept
                       only for record_source to look up) but that has no
                       value as examiner-facing content.
    record_source dict | list[dict] — lets the Hex panel's "Record" mode
                       jump straight to the exact on-disk database cell a
                       row came from. One entry per DB row this parser's
                       own output rows are actually built from — a single
                       table needs one entry, a JOINed report (most of
                       them) typically wants one per joined table too. Each
                       entry: {"label": str (recommended even for a single
                       entry — the Hex panel's picker is always visible now,
                       not just with >1 entry, so an omitted label falls
                       back to the entry's own table name rather than
                       showing nothing there; declare it explicitly for a
                       clearer examiner-facing name than a bare SQL table),
                       "file_key": str (a files/
                       optional_files key), "table_field": str (an output
                       field naming the source table, when it varies per
                       row) OR "table": str (a fixed table name, when it
                       never varies), "rowid_fields": list[str] (output
                       fields to try in order for the row's rowid in that
                       table — list more than one when a live row and a
                       carved row use different field names for the same
                       concept), "source_match":
                       list[str] (optional — only needed when this
                       script's run() merges more than one live query
                       into one report, e.g. chrome_web_history.py's
                       Chrome History rows vs. Segmentation Platform
                       rows; lists the "source" output-field values this
                       entry applies to), "presence_fields": list[str]
                       (optional — only needed when rowid_fields alone
                       can't say whether this entry's OWN join matched
                       for a given row, e.g. android/whatsapp.py's Media/
                       Location entries, whose rowid is a value BORROWED
                       from the row's own always-present message_id
                       rather than re-selected from their own table;
                       checks the actual joined column, e.g. "media_path",
                       instead)}. A LIVE row only ever offers entries
                       whose source_match includes its own "source" value
                       (or that declare no source_match at all — the
                       original, still-common single-query case, entirely
                       unaffected) AND whose presence_fields (or, absent
                       that, rowid_fields) has a real value on THIS row —
                       a LEFT JOIN that found nothing for this particular
                       row has nowhere to jump, so it's dropped from the
                       list rather than offered as a dead end; this second
                       check is genuinely per-row, not per-query — two
                       rows from the same query can differ here depending
                       on which of that query's own joins matched for
                       each one. The Hex panel builds the combo fresh per
                       row selection from just that subset, defaulting to
                       the first matching entry (that query's own main
                       table); picking a different one from the dropdown
                       is how an examiner reaches a join. A CARVED row
                       ignores source_match AND presence_fields entirely
                       (it already knows exactly which table it was
                       recovered from via source_table) — matched by
                       table name instead, narrowed further by file_key
                       only if more than one entry shares that bare table
                       name (the same collision a recovered_records
                       report's file_key pinning resolves at carve time —
                       a recovered row carries source_file_key for exactly
                       this). See
                       artifacts/ios/whatsapp.py for a worked 4-entry
                       single-query example,
                       artifacts/android/whatsapp.py for a worked
                       12-entry example needing presence_fields,
                       artifacts/android/chrome_web_history.py for a
                       worked source_match example, and CLAUDE.md's
                       Conventions for the full writeup (including why it
                       needs real-schema verification before declaring,
                       not after).

                       A SECOND, independent entry shape (added
                       2026-09-05) for a parser with NO database at all —
                       one that enumerates its own dynamically-named files
                       itself (a different real file per row, e.g.
                       chrome_sessions.py's Tabs_<timestamp>,
                       chrome_app_tabs.py's tab<N>/tab_state<N>) rather
                       than reading a fixed files/optional_files entry —
                       "file_key"/"table"/"table_field"/"rowid_fields"
                       don't apply there at all: there's no
                       resolve_module_file_ui_path key to look up (a
                       different file per row, not one fixed path per
                       parser) and no SQL row to locate. Such a parser
                       already knows its own row's exact archive ui_path
                       and byte span at parse time, so the entry instead
                       declares: "ui_path_field": str (an output field —
                       usually hidden, see hidden_fields — holding the
                       row's own real archive ui_path to open directly,
                       no live-lookup/resolve_module_file_ui_path
                       involved), "offset_field": str (optional, default
                       "raw_offset" — the output field holding the byte
                       offset to jump to), "length_field": str (optional,
                       default "raw_length" — the field holding how many
                       bytes to highlight). source_match/presence_fields
                       both still work identically (presence_fields
                       falls back to [ui_path_field] itself when neither
                       is declared) — used exactly this way in
                       chrome_app_tabs.py to list BOTH real pieces a
                       TabState-format row is actually built from (its
                       own decoded navigation entry, AND the surrounding
                       per-tab metadata a different byte range of the
                       SAME physical file holds), the same "list every
                       real table a join draws from" principle as the
                       SQL-backed entries above, just for two different
                       byte ranges of one file instead of two SQL tables.

Device-wide API (added 2026-09-15, for a parser that scans the WHOLE
device rather than one app's own files — currently only
artifacts/ios/app_report.py and artifacts/android/app_report.py, moved
here from what used to be a hardcoded "Apps" tree node in
artifact_viewer.py, per direct user request to make it a normal,
manually-run report instead of something that auto-populated at case
load):
    device_wide  bool  — True instead of app_path/app_group/files. No
                          single app to scope to, so the normal
                          files/optional_files resolution is skipped
                          entirely.
    run(paths)             — same call shape as the multi-file API
                              above, with the same reserved keys
                              (_zip_names, _read_zip_bytes, _adapter,
                              _parser_files_dir) PLUS a new one:
    paths['_case_context']  — a mcp_server.CaseContext built fresh from
                              the case's own persisted load-snapshot
                              (ffs_metadata.load_snapshot_from_case) —
                              never the live GUI window object, so a
                              device-wide parser stays exactly as
                              headless/testable as every other one. Give
                              this straight to app_intelligence.scan_apps()
                              — same object shape, same function, the GUI's
                              own former "Apps" tab used. raw_content_enabled
                              is always True here (this is a direct,
                              examiner-triggered parser run against the
                              examiner's own already-open case, not an
                              AI/MCP client needing separate consent — same
                              reasoning the GUI's own former Apps view
                              already used). Returns [], "<script>: case
                              snapshot not available..." if the case was
                              never fully opened (no snapshot on disk yet)
                              — the parser's own run() is never even called
                              in that case, matching every other early-
                              return failure shape in this function.
    media_fields/timestamp_fields/record_source all
    still work identically for a device_wide parser's own output rows —
    nothing about this API changes how the OUTPUT is declared, only how
    run() gets its INPUT.

Parser helpers — small, generic utilities importable directly from a
script's own run() (`from artifact_runner import first_nonempty`), for a
pattern more than one parser needs, so the logic lives once instead of
being copy-pasted. See the "Parser helpers" section of this file for what
exists — add the next one there, not inline in a parser script, the next
time a second parser genuinely needs the same small building block.

Source files are saved to:
    case_dir/artifact_parser_files/<Parser Name>/original_filename

Files are kept so investigators can open them directly, and re-running a
parser skips extraction if the file is already present.
"""

import contextlib
import fnmatch

from zip_cd_cache import is_capture_alias
import importlib.util
import io
import os
import plistlib
import re
import sqlite3
import sys
import threading
import zipfile
import zlib

import nska_deserialize

import nested_archive
from db_utils import _open_cache_db, load_folder_data, load_guid_bundle_map
from zip_entry import ZipEntry

if getattr(sys, 'frozen', False):
    # PyInstaller bundle: data files land in sys._MEIPASS
    _ARTIFACTS_DIR = os.path.join(sys._MEIPASS, 'artifacts')
else:
    # Source layout: artifacts/ sits two levels above app/artifact_runner.py
    _ARTIFACTS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'artifacts'
    )


# ── Parser helpers (import these FROM a parser script — see below) ─────────────
#
# Small, generic utilities meant to be called from inside artifacts/ios|
# android/*.py's own run(), the same way a parser already reaches for
# stdlib modules. Keeps a parser script itself short/declarative — the
# actual logic for a pattern more than one parser needs lives here once,
# not copy-pasted into each script that needs it. Add to this section
# (not inline in a parser) the next time a SECOND parser needs the same
# kind of small building block a first one already introduced.

def first_nonempty(*values, default=''):
    """Return the first truthy value among *values*, or *default* if all
    are falsy (default ''). Typical use: a parser preferring a full-size
    media file but needing to fall back to a smaller pre-generated preview
    when the full file was never (or is no longer) cached locally — see
    artifacts/ios/whatsapp.py's ZMEDIALOCALPATH/ZXMPPTHUMBPATH for the
    first real case: `first_nonempty(r["media_local_path"], r["media_thumb_path"])`.

    Also covers a second, equally common shape: coalescing several
    optional display-name-ish columns into one value, `default=None` so a
    genuinely-unresolved name reads as null rather than an empty string
    (added 2026-08-25 after finding two near-identical private
    `_display_name` helpers doing exactly this — artifacts/android/
    google_messages.py's `full_name or display_destination or None` and
    artifacts/android/viber.py's `contact_name or viber_name or number or
    member_id or None` — both now call this directly instead)."""
    for v in values:
        if v:
            return v
    return default


def missing_ref_label(noun: str, field_name: str, value) -> str:
    """Standard bracketed placeholder for a row whose foreign-key lookup
    came back empty — e.g. a chat_id with no matching row in the chat
    table. Keeps every parser's "couldn't resolve this" citation in the
    same shape (`'[no chat record — raw chat_id=42]'`), so an examiner
    reading two different apps' reports isn't parsing two different
    conventions for the same situation. *field_name* should name the RAW
    column the value came from (not a renamed output field), so the value
    stays traceable back to the actual query without re-reading the SQL —
    matches the existing convention already used by every parser this was
    extracted from.

    Added 2026-08-25 after finding this exact string shape hand-built,
    near-verbatim, in seven parsers (burner.py, groupme.py, line.py,
    viber.py, android/whatsapp.py, ios/sms_messages.py, ios/whatsapp.py) —
    each with its own noun ('chat record', 'burner record', 'participant
    records', 'chat name', ...). *noun* is deliberately a free string
    (not an enum) since it varies per call site and there's no fixed list
    to validate against; this helper only standardizes the surrounding
    shape, not the vocabulary."""
    return f"[no {noun} — raw {field_name}={value}]"


def resolve_path_after_marker(base: str, raw_path: str, marker: str) -> str:
    """base + everything in raw_path AFTER the first occurrence of
    *marker* — for a stored path that isn't already container-relative
    (an absolute on-device path, or a '~/'-prefixed home-relative one)
    but carries a fixed, findable landmark substring marking where the
    container-relative part begins. Returns '' if raw_path/base is empty
    or marker isn't found — never guesses at a path that doesn't actually
    contain the landmark.

    Added 2026-08-25, generalizing two real call sites that looked
    different but did the identical thing: android/google_messages.py's
    local_cache_path is an absolute on-device path
    ('/data/user/0/com.google.android.apps.messaging/...' or
    '/data/data/...' depending on Android version) — marker is the
    package name, found mid-string; ios/sms_messages.py's
    attachment.filename is '~/Library/SMS/Attachments/...' — marker is
    '~/', found at the very start. A plain substring search (not a
    strict startswith) covers both: for a marker that's always a true
    prefix, find() at index 0 behaves identically to startswith(), so one
    helper serves both shapes without a second strictly-prefix-only
    variant."""
    if not raw_path or not base:
        return ''
    i = raw_path.find(marker)
    if i < 0:
        return ''
    return f"{base}/{raw_path[i + len(marker):]}"


def _read_binary_plist_tolerantly(data: bytes):
    """Parse a binary plist that plistlib rejects outright for one bad
    value. Adapted from iLEAPP's own `_read_binary_plist_tolerantly`
    (ilapfuncs.py) — same technique, ported from a file path to in-memory
    bytes (via BytesIO) to match this project's own decode_plist_blob
    convention (everything reads bytes out of the archive, no scratch
    file for a plain parse).

    A single CFDate outside datetime's representable range (year 1–9999)
    makes plistlib raise OverflowError for the WHOLE file — every other,
    well-formed key is lost too, even though Apple's own `plutil` reads
    the file fine. Confirmed a REAL, non-theoretical problem on this
    project's own IOS17 JoshHickman archive, not just iLEAPP's
    documented rationale: the very first real bplist to fail
    `plistlib.loads()` in a 6,000-file random sample of this archive's
    own .plist entries was `.../mobile/Library/Preferences/
    com.apple.sleepd.plist` — a real sleep-tracking preferences file.
    This tolerant reader recovers all 60 of its real keys (bedtime/wake
    schedule, alarm config, wake-detection state) with only the 3
    offending date fields coming back None, where plistlib.loads() alone
    returns nothing at all for the file. Verified directly against this
    project's own Python 3.14: plistlib's private `_BinaryPlistParser`
    (and its `_read_object`/`parse`/`__init__(dict_type, aware_datetime)`
    signatures) still match what iLEAPP's code assumes.

    Overriding the per-object read means only the offending value is
    dropped (becomes None), not the whole file. Returns None when the
    data isn't a binary plist or the private parser is unavailable (a
    future CPython could rename/remove it — this degrades to the
    existing "give up" behavior rather than raising), so the caller
    keeps its own existing fallback."""
    parser_class = getattr(plistlib, '_BinaryPlistParser', None)
    if parser_class is None or data[:8] != b'bplist00':
        return None

    skipped = []

    class _Tolerant(parser_class):
        def _read_object(self, ref):
            try:
                return super()._read_object(ref)
            except OverflowError:
                skipped.append(ref)
                return None

    try:
        import inspect
        arguments = {'dict_type': dict}
        # aware_datetime is 3.12+; pass it only where it exists so this
        # neither breaks nor silently no-ops on an older Python.
        if 'aware_datetime' in inspect.signature(parser_class.__init__).parameters:
            arguments['aware_datetime'] = False
        return _Tolerant(**arguments).parse(io.BytesIO(data))
    except Exception:
        return None


# nska_deserialize copies an object in again at every reference, stopping
# only at a loop back to an object it is already inside, so a densely
# cross-linked archive grows exponentially and never finishes (a Core Data
# .mom, e.g. Snapchat's scdb-27.sqlite3.mom: 595 objects, still running
# after minutes; found 2026-10-24). A normal archive needs about one
# expansion step per object, so a run is stopped once it passes
# _NSKA_STEPS_PER_OBJECT x its object count (at least _NSKA_MIN_STEPS,
# about 4 s at the measured ~50,000 steps/s).
_NSKA_STEPS_PER_OBJECT = 20
_NSKA_MIN_STEPS = 200_000
# The library keeps its recursion state in module globals, so only one
# thread may use it at a time.
_nska_lock = threading.Lock()


class NskaTooComplex(Exception):
    """An NSKeyedArchiver archive whose expansion went over the budget."""


def deserialize_nska_budgeted(data: bytes, n_objects: int):
    """nska_deserialize.deserialize_plist_from_string with a step budget;
    raises NskaTooComplex when it runs over."""
    budget = max(_NSKA_MIN_STEPS, _NSKA_STEPS_PER_OBJECT * n_objects)
    with _nska_lock:
        original = nska_deserialize._recurse_create_plist
        steps = [0]

        def counted(*args):
            steps[0] += 1
            if steps[0] > budget:
                raise NskaTooComplex(f"over {budget:,} expansion steps")
            return original(*args)

        nska_deserialize._recurse_create_plist = counted
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return nska_deserialize.deserialize_plist_from_string(data)
        finally:
            nska_deserialize._recurse_create_plist = original
            # a run stopped part-way leaves its loop-detection stack behind
            nska_deserialize.rec_uids = []
            nska_deserialize.rec_depth = 0


def decode_plist_blob(data: bytes):
    """Decode a plist BLOB, transparently unarchiving it first if it's an
    NSKeyedArchiver payload (Apple's standard Objective-C object-archive
    format — common for a BLOB column holding a serialized message/object,
    not just plain settings data). Returns a dict/list on success, or None
    on any failure — never raises, matching this project's degrade-
    gracefully convention elsewhere (find_evidence_databases, etc.).

    Added 2026-08-25 for Instagram's DirectSQLiteDatabase (see
    artifacts/ios/instagram.py), whose `messages.archive` column is exactly
    this shape (keys like 'IGDirectPublishedMessageMetadata*metadata') —
    but written here as a GENERIC helper, not Instagram-specific, since the
    same NSKeyedArchiver format shows up in other iOS app BLOB columns and
    this project's own MCP `sample_sqlite_rows`/`get_sqlite_schema` tools
    use it too (for exploring a BLOB column before a parser exists for that
    app at all).

    Mirrors iLEAPP's own `get_plist_content`/`_deserialize_nska`
    (ilapfuncs.py) rather than reinventing it: try a plain plistlib parse
    first; if the result is itself the raw NSKeyedArchiver container
    (`$archiver == 'NSKeyedArchiver'`), re-decode via the `nska_deserialize`
    library (a real, existing open-source tool for exactly this problem —
    not something to reimplement) — this handles a PLAIN plist blob
    (returned as-is) and an NSKeyedArchiver-wrapped one (unarchived) through
    the same call, so a caller doesn't need to know in advance which kind
    it's looking at. stdout is suppressed during the nska_deserialize call
    — a known, non-actionable library quirk (an unhashable-dict-key
    exception print on every such record, confirmed by iLEAPP's own code
    comment) that can otherwise flood output on a database with thousands
    of records.

    A binary plist that plistlib's normal parser rejects outright (one
    out-of-range CFDate poisoning the whole file — see
    `_read_binary_plist_tolerantly`'s own docstring for a real example
    found on this project's own test data) is retried through that
    tolerant reader before giving up, same as iLEAPP's own
    `get_plist_file_content` does."""
    try:
        content = plistlib.loads(data)
    except Exception:
        content = _read_binary_plist_tolerantly(data)
        if content is None:
            return None
    if isinstance(content, dict) and content.get('$archiver') == 'NSKeyedArchiver':
        # None for an archive too cross-linked to expand (see
        # _NSKA_STEPS_PER_OBJECT); call deserialize_nska_budgeted directly
        # to tell that apart (NskaTooComplex).
        try:
            return deserialize_nska_budgeted(data, len(content.get('$objects') or ()))
        except Exception:
            return None
    return content


def open_db_readonly(db_path: str) -> sqlite3.Connection:
    """Open a sqlite file for a plain live query, READ-ONLY — every
    parser's own run() should call this instead of a bare
    sqlite3.connect(). A bare connect (even for a pure SELECT, no
    explicit write) can trigger SQLite's own checkpoint-on-close
    behavior, silently DELETING the file's own -wal/-shm sidecars before
    anything else gets a chance to read them — destroying exactly the
    historical/superseded WAL frame content a deleted-records report's
    carving needs afterward (see CLAUDE.md's own WAL-checkpoint Conventions entry).

    Added 2026-09-12, confirmed by direct testing that a read-only URI
    connect (`file:{path}?mode=ro`) returns byte-for-byte identical
    live-query results to a bare read-write connect on the same real
    evidence files — this is a pure safety fix, never a behavior change
    to any report's own output. An ATTACHed second database (see
    artifacts/android/whatsapp.py, burner.py) inherits the same
    read-only restriction from a read-only main connection automatically
    — standard SQLite behavior, nothing this project has to enforce
    itself. Returns a connection with row_factory already set to
    sqlite3.Row (row["col_name"], never a bare tuple) — the exact shape
    chrome_shared.query_rows already gave its own callers; that function
    now opens through this helper internally rather than duplicating the
    connect/row_factory ceremony, and this is the ONE place every
    SQL-based parser's live-query connect should go through, Chrome or
    not — see chrome_shared.py's own module docstring."""
    # The open itself (read-only URI, immutable for a WAL-mode file with no
    # sidecar beside it, a temporary finished copy when SQLite must write
    # first) is sqlite_reader's, shared with every other evidence reader.
    import sqlite_reader
    label = _db_label(db_path)
    # Errors raised while opening must name the file too, so the label is
    # on the class the connection is made from, not set afterwards.
    factory = type('_LabelledConnection', (_NamedConnection,), {'_db_label': label})
    conn, notes = sqlite_reader.connect_committed(db_path, factory=factory, label=label)
    _open_notes.extend(f"  Note: {n}" for n in notes)
    conn.row_factory = sqlite3.Row
    return conn


# ── A database SQLite must write to before it can be read ────────────────────
# Real evidence, 2026-10-23: an Android Chrome Favicons database acquired
# while Chrome was mid-write (a hot -journal; reproduced as
# SQLITE_READONLY_ROLLBACK). SQLite will not read it until that unfinished
# change is undone, which needs a write, so every read-only open failed
# with "attempt to write a readonly database" while other SQLite tools
# (opening read-write) silently undid it. Same for a WAL that needs its
# index rebuilt. Such a file is copied with its sidecars to a temporary
# folder and opened there, letting SQLite finish exactly as the app would
# on its next start; the extracted copy (which carving reads) is never
# touched. Each case is reported via take_open_notes() in the parser log.

_open_notes: list[str] = []


def embedded_media_path(paths: dict, file_key: str, table: str, column: str,
                        rowid, blob) -> str:
    """The File Browser path of the image the embedded-media sweep extracts
    from this database value: '<db ui_path>/<db>-<table>-<column>-<rowid><ext>',
    built with the sweep's own naming code and size limit, so a report row
    can name it in a `link_fields` column and the File Browser's Linked
    Report column / right-click jump work for it. '' if the value isn't
    media the sweep would extract. The link resolves once the sweep has run
    on that database (before that it names a file that doesn't exist yet,
    which shows nothing). *file_key* is the parser's own files/
    optional_files key for the database."""
    import embedded_media_scan as ems
    db_ui = (paths.get('_ui_paths') or {}).get(file_key)
    if not db_ui or rowid is None or not isinstance(blob, (bytes, bytearray)):
        return ''
    if len(blob) < ems.MIN_MEDIA_BYTES:
        return ''
    got = ems.classify_media_blob(bytes(blob))
    if got is None:
        return ''
    name = ems.compute_display_name(db_ui, 'sqlite', '', {
        'table': table, 'column': column, 'rowid': rowid, 'ext': got[1]})
    return f"{db_ui}/{name}"


# Android storage paths as an app records them -> this archive's ui_path.
_ANDROID_PATH_PREFIXES = (
    ('/storage/emulated/0/', 'data/media/0/'),
    ('/sdcard/', 'data/media/0/'),
    ('/mnt/sdcard/', 'data/media/0/'),
    ('/data/media/0/', 'data/media/0/'),
    ('/data/user/0/', 'data/data/'),
    ('/data/data/', 'data/data/'),
)


def device_path_to_ui_path(device_path: str) -> str:
    """The archive ui_path for an Android on-device file path (or a
    file:// URI): '/storage/emulated/0/Download/x.png' ->
    'data/media/0/Download/x.png'. '' for a path outside known storage."""
    if not device_path:
        return ''
    p = str(device_path)
    if p.startswith('file://'):
        from urllib.parse import unquote
        p = unquote(p[len('file://'):])
    for prefix, ui in _ANDROID_PATH_PREFIXES:
        if p.startswith(prefix):
            return ui + p[len(prefix):]
    return ''


def archive_has(paths: dict, ui_path: str) -> bool:
    """True if *ui_path* is a file in this archive (parser-side check,
    so a link is only written for a file the File Browser can show)."""
    if not ui_path:
        return False
    names = paths.get('_zip_name_set')
    if names is None:
        names = paths['_zip_name_set'] = frozenset(paths.get('_zip_names') or ())
    adapter = paths.get('_adapter')
    physical = adapter.resolve(ui_path) if adapter is not None else ui_path
    return physical in names or physical.lstrip('/') in names


def resolve_mediastore_uri(paths: dict, uri: str) -> str:
    """The archive ui_path of the file an Android MediaStore URI
    ('content://media/external/images/media/1000000684') points at, from
    MediaStore's own database (com.google.android.providers.media.module/
    databases/external.db, files._id -> _data), read from the archive once
    per parser run. '' if the URI isn't a MediaStore one or the id is not
    in the database. Verified 2026-10-23 on Android 14 JoshHickman: both
    Viber media ids resolved to files present in the archive."""
    import re
    m = re.match(r'^content://media/[^?#]*?/(\d+)(?:[?#].*)?$', uri or '')
    if not m:
        return ''
    cache = paths.get('_mediastore')
    if cache is None:
        cache = paths['_mediastore'] = {}
        try:
            names = [n for n in (paths.get('_zip_names') or ())
                     if n.endswith('com.google.android.providers.media.module/databases/external.db')]
            names.sort(key=lambda n: (0 if '/data/data/' in n else 1, len(n)))
            read = paths.get('_read_zip_bytes')
            if names and read:
                import tempfile
                tmp = tempfile.mkdtemp(prefix='ffs_mediastore_')
                for sfx in ('', '-wal'):
                    data = read(names[0] + sfx)
                    if data:
                        with open(os.path.join(tmp, 'external.db' + sfx), 'wb') as f:
                            f.write(data)
                conn = open_db_readonly(os.path.join(tmp, 'external.db'))
                try:
                    cache.update({r[0]: r[1] for r in conn.execute(
                        "SELECT _id, _data FROM files WHERE _data IS NOT NULL")})
                finally:
                    conn.close()
        except Exception:
            pass
    return device_path_to_ui_path(cache.get(int(m.group(1)), ''))


def log_note(text: str) -> None:
    """Write one line to the parser run log (after the parser finishes),
    e.g. that an optional table was absent and why nothing was read from
    it. For a parser to explain a skip instead of failing or staying
    silent."""
    _open_notes.append(f"  Note: {text}")


def take_open_notes() -> list[str]:
    """Notes about databases read from a temporary copy since the last call."""
    notes = list(_open_notes)
    _open_notes.clear()
    return notes


# ── Database errors name their file ──────────────────────────────────────────
# Added 2026-10-23 (owner request): a parser reading several databases
# (Chrome Favicons reads Favicons, History and ukm_db) failed with a bare
# "attempt to write a readonly database", with no way to tell which file.
# Every sqlite3 error raised through an open_db_readonly connection now
# starts with the file's name and lists the sidecars next to it, e.g.
#   Favicons [Favicons-journal 16,928 B]: attempt to write a readonly database
# The exception type is unchanged, so callers catching sqlite3.Error still do.

def _db_label(db_path: str) -> str:
    name = os.path.basename(db_path)
    sidecars = []
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            sidecars.append(f"{name}{suffix} {os.path.getsize(db_path + suffix):,} B")
        except OSError:
            pass
    return f"{name} [{', '.join(sidecars)}]" if sidecars else name


def _name_error(label: str, exc: sqlite3.Error) -> sqlite3.Error:
    msg = str(exc)
    if msg.startswith(label):
        return exc
    try:
        named = type(exc)(f"{label}: {msg}")
    except Exception:
        return exc
    for attr in ('sqlite_errorcode', 'sqlite_errorname'):
        if hasattr(exc, attr):
            try:
                setattr(named, attr, getattr(exc, attr))
            except Exception:
                pass
    return named


class _NamedCursor(sqlite3.Cursor):
    def _call(self, fn, *a):
        try:
            return fn(*a)
        except sqlite3.Error as e:
            raise _name_error(self.connection._db_label, e) from e

    def execute(self, *a):
        return self._call(super().execute, *a)

    def executemany(self, *a):
        return self._call(super().executemany, *a)

    def executescript(self, *a):
        return self._call(super().executescript, *a)

    def fetchone(self):
        return self._call(super().fetchone)

    def fetchmany(self, *a):
        return self._call(super().fetchmany, *a)

    def fetchall(self):
        return self._call(super().fetchall)

    def __next__(self):
        return self._call(super().__next__)


class _NamedConnection(sqlite3.Connection):
    _db_label = ''

    def cursor(self, factory=_NamedCursor):
        return super().cursor(factory)

    def execute(self, *a):
        return self.cursor().execute(*a)

    def executemany(self, *a):
        return self.cursor().executemany(*a)

    def executescript(self, *a):
        return self.cursor().executescript(*a)


def open_leveldb(paths: dict, relative_dir: str, extract_subdir: str | None = None):
    """Extract a LevelDB directory out of the archive to a local scratch
    folder and open it via ccl_leveldb.RawLevelDb — the ONE place every
    LevelDB-backed parser's own extract-then-open boilerplate should go
    through, same role this file's own open_db_readonly plays for SQL.
    ccl_leveldb needs real files on a real filesystem (it opens/seeks its
    own .ldb/.log files directly, unlike a SQLite connect it can't work
    from in-memory bytes) — added 2026-09-18 after finding
    chrome_local_storage.py, the first real caller, had hand-rolled
    ~15 lines of exactly this (extract dir → RawLevelDb → the caller
    iterates/closes) with no shared home for it, the same gap
    open_db_readonly itself closed for SQL earlier.

    Lives here rather than a new leveldb_shared.py — this logic isn't
    Chrome-schema-specific at all (any app storing data in LevelDB could
    use it), the same reasoning that already keeps chrome_shared.py
    scoped to its own two genuinely Chrome-specific functions
    (url_set/history_visits) while open_db_readonly itself lives here,
    not in a sql_shared.py. One canonical place, matching the existing
    convention.

    *relative_dir* — the LevelDB directory's own path relative to the
    app's own container base (paths['_app_base_ui_path']), no leading or
    trailing slash — e.g. "app_chrome/Default/Local Storage/leveldb".
    *extract_subdir* — name of the local scratch subfolder under this
    parser's own artifact_parser_files/ directory the directory's files
    are copied into; defaults to *relative_dir* with '/' replaced by '_'
    so two different LevelDB directories the same parser reads don't
    collide onto the same folder, but an explicit name reads better for
    a parser reading only one (chrome_local_storage.py's own
    "local_storage_leveldb", kept as-is on refactor rather than
    renamed, so a re-run of an already-processed case reuses its
    existing extracted files instead of duplicating them under a new
    auto-derived name).

    A file already extracted from a prior run is skipped, not
    re-written — this project's own standing extraction convention
    (`run_artifact`'s own module docstring, `_save_entry`), a real
    improvement over chrome_local_storage.py's own original inline
    version, which re-wrote every file on every run.

    Returns an open ccl_leveldb.RawLevelDb the caller must close (same
    convention as this function's own open_db_readonly above — a plain
    try/finally or contextlib.closing, not a new shape) — or None if the
    directory has no matching entries in this archive at all (a fresh
    app install with no Local Storage yet, say), the same "missing is
    fine, the caller decides" shape optional_files already has, never an
    exception for that specific case. A directory that DOES have real
    entries but that RawLevelDb itself can't make sense of is not
    caught here — confirmed by reading RawLevelDb.__init__ directly, it
    only ever raises for a path that isn't a directory at all (never
    true here, this always os.makedirs's the extract dir first) and
    otherwise degrades to an empty/no-manifest db rather than raising —
    so there is genuinely nothing this helper would be catching by
    wrapping the open call, and a future, different failure mode
    propagating to run_artifact's own top-level catch (a clear
    "{script_name}: {exc}" message) is more honest than silently
    returning None for something this function didn't actually see
    happen on real data."""
    import os
    import ccl_leveldb

    zip_names = paths.get("_zip_names") or []
    read_bytes = paths.get("_read_zip_bytes")
    app_base = paths.get("_app_base_ui_path", "")
    parser_dir = paths.get("_parser_files_dir")
    adapter = paths.get("_adapter")
    if not zip_names or read_bytes is None or not app_base or adapter is None or not parser_dir:
        return None

    ui_prefix = f"{app_base}/{relative_dir}/"
    try:
        physical_prefix = adapter.resolve(ui_prefix.rstrip("/")) + "/"
    except Exception:
        return None
    # An earlier capture of a repeated path (zip_cd_cache.capture_alias) is
    # never part of a parser's default file set (ASSUMPTIONS.md A-72).
    entry_names = [n for n in zip_names if n.startswith(physical_prefix) and not n.endswith("/")
                   and not is_capture_alias(n)]
    if not entry_names:
        return None

    subdir = extract_subdir or relative_dir.replace("/", "_")
    extract_dir = os.path.join(parser_dir, subdir)
    os.makedirs(extract_dir, exist_ok=True)
    for physical_path in entry_names:
        basename = physical_path.rsplit("/", 1)[-1]
        dest = os.path.join(extract_dir, basename)
        if os.path.exists(dest):
            continue
        data = read_bytes(physical_path)
        if data is None:
            continue
        with open(dest, "wb") as f:
            f.write(data)

    return ccl_leveldb.RawLevelDb(extract_dir)


def parse_chromium_dom_storage_key(user_key: bytes):
    """(origin, top_level_site, key) for a real Chromium Local Storage
    LevelDB record key — or None for anything that doesn't match this
    shape: Chrome's own bare `VERSION` key, its `META:<origin>`
    bookkeeping keys, or a genuinely different LevelDB store's own key
    format entirely.

    **Local Storage only — NOT Session Storage, despite sharing the same
    underlying `content/browser/dom_storage` C++ implementation.** This
    function's own docstring previously REASONED that Session Storage
    likely used the identical key shape, on the strength of that shared
    implementation, and said so explicitly as reasoned-through rather
    than verified. Real Session Storage data became available on this
    project's own test archives shortly after (1,890 real keys across 9
    real apps) and PROVED that reasoning wrong: 0 of them matched this
    function. Session Storage has its own, genuinely different real
    format — see `resolve_chromium_session_storage_names` below — kept
    as a real, documented example of why this project treats "reasoned"
    and "verified" as different confidence tiers rather than collapsing
    them once real data becomes available to actually check.

    IndexedDB is also structurally different, confirmed directly: its
    own real keys start with 4 NUL bytes, a compact-integer prefix,
    never a literal `_`.

    First written as `chrome_local_storage.py`'s own private `_parse_key`
    (2026-09-05); promoted here 2026-09-19 as a second, generic caller
    emerged — `leveldb_viewer.py`'s own record-to-filename decode for the
    in-browser "records as files" LevelDB feature — needing the exact
    same real algorithm, not a re-derived one, so a decoded filename
    reads identically to what the dedicated report already shows for the
    same record. That parser's own run() now calls this instead of
    keeping a second copy.

    Confirmed consistent, not assumed, across NINE completely unrelated
    real apps' own real Local Storage on this project's own Android 14
    JoshHickman archive (Chrome, Edge, Brave, DuckDuckGo, Viber,
    GroupMe, GetTr, BeReal, ProtonMail — a deliberately wide, unrelated
    sample, not just Chrome-the-browser) — every single real key on
    every one of them is exactly one of the three shapes this function
    already distinguishes (bare `VERSION`; `META:<origin>`; the real
    `_<origin>[^0<top_level_site>]\\x00\\x01<key>` shape decoded here),
    with zero unclassified keys anywhere in that sample. This is
    Chromium's own generic `dom_storage` on-disk encoding — confirmed to
    hold for ANY app embedding Chromium WebView for Local Storage, not
    something specific to the Chrome browser app itself.

    Harmless for a genuinely different format either way: a non-matching
    key simply returns None here, same as any other unrecognized
    format — this is exactly what let the Session Storage mismatch
    above surface as "0 matched" instead of a wrong decode."""
    if not user_key.startswith(b"_") or b"\x00" not in user_key:
        return None
    origin_part, _, rest = user_key.partition(b"\x00")
    if not rest.startswith(b"\x01"):
        return None
    origin_raw = origin_part[1:]  # drop the leading "_"
    key_raw = rest[1:]
    if b"^0" in origin_raw:
        embedded, _, top_level = origin_raw.partition(b"^0")
    else:
        embedded, top_level = origin_raw, b""
    return (
        embedded.decode("utf-8", errors="replace"),
        top_level.decode("utf-8", errors="replace"),
        key_raw.decode("utf-8", errors="replace"),
    )


def resolve_chromium_session_storage_names(records):
    """{raw_map_n_key: (origin, top_level_site, key)} for every real
    Chromium Session Storage `map-<id>-<key>` record this can resolve,
    given the FULL list of records from one real Session Storage
    LevelDB folder (`db.iterate_records_raw()`'s own output — every
    record, not a filtered subset, since the lookup this needs is built
    from a DIFFERENT set of records — the `namespace-` ones — than the
    ones it resolves names for).

    Session Storage needs a real two-record join to recover the same
    (origin, top_level_site, key) shape `parse_chromium_dom_storage_key`
    gets from ONE Local Storage record alone — added 2026-09-19,
    directly prompted by a user question ("is the map-id consistent?
    what is namespace, what is map-id?") after that function's own
    Session Storage claim was found wrong by real data (see its own
    docstring). The real mechanism, confirmed both against CCL Solutions
    Group's own published write-up (cclsolutionsgroup.com, "Chromium
    Session Storage and Local Storage" — the same forensic lineage this
    project's own vendored ccl_leveldb.py comes from) and independently
    verified against real data on FOUR completely unrelated real apps on
    this project's own Android 14 JoshHickman archive (Chrome, Edge,
    Brave, DuckDuckGo):

      - A `namespace-<uuid>-<origin>[^0<top_level_site>]` record's own
        VALUE (not its key) is a plain ASCII decimal integer — the
        "map-id" Chromium assigned that (browsing-context UUID, origin)
        pair the first time it needed real storage. The UUID itself
        (roughly: one browser tab) is not needed for this lookup —
        confirmed real by inspecting it, its own literal underscores
        (never a literal `-`) are what make the first real `-` in the
        stripped key unambiguous as the uuid/origin boundary.
      - Every real `map-<id>-<key>` record's own key only ever carries
        that already-anonymous integer — this function is what recovers
        which real origin it actually belongs to.
      - A map-id is PURELY LOCAL, sequential, storage-space shorthand —
        like a database auto-increment column — confirmed by reading
        real consecutive values (43, 44, 45, 46... assigned to different
        origins loaded in the same real tab). Meaningless compared
        across two different Session Storage databases, or even two
        different runs of the same app; only ever valid to resolve
        WITHIN the one real folder's own records passed in here.

    A `map-<id>-` entry with no matching `namespace-` entry is a real,
    honest, non-buggy case — confirmed directly on real data across all
    four apps checked: a tab/origin can be assigned a map-id before it
    ever actually stores anything, so not every namespace entry has a
    matching map- entry, and the reverse can happen too (a namespace
    entry superseded/deleted, its own map-id's data left orphaned). Such
    an entry is simply absent from the returned dict — left for the
    caller's own generic fallback, not guessed at."""
    namespace_to_origin: dict[int, tuple[str, str]] = {}
    for rec in records:
        key = rec.user_key
        if not key or not key.startswith(b"namespace-") or not rec.value:
            continue
        rest = key[len(b"namespace-"):]
        _, sep, origin_part = rest.partition(b"-")
        if not sep or not origin_part:
            continue
        try:
            map_id = int(rec.value)
        except (ValueError, TypeError):
            continue
        if b"^0" in origin_part:
            embedded, _, top_level = origin_part.partition(b"^0")
        else:
            embedded, top_level = origin_part, b""
        namespace_to_origin[map_id] = (
            embedded.decode("utf-8", errors="replace"),
            top_level.decode("utf-8", errors="replace"),
        )

    resolved: dict[bytes, tuple[str, str, str]] = {}
    for rec in records:
        key = rec.user_key
        if not key or not key.startswith(b"map-"):
            continue
        parts = key.split(b"-", 2)
        if len(parts) != 3:
            continue
        try:
            map_id = int(parts[1])
        except ValueError:
            continue
        origin_and_top = namespace_to_origin.get(map_id)
        if origin_and_top is None:
            continue
        origin, top_level = origin_and_top
        resolved[key] = (origin, top_level, parts[2].decode("utf-8", errors="replace"))
    return resolved


_TEXT_PLAUSIBLE_MIN_LEN = 8
_TEXT_PLAUSIBLE_MAX_CONTROL_FRACTION = 0.15


def text_plausible(value: str) -> bool:
    """False if *value* is long enough to judge and more than
    _TEXT_PLAUSIBLE_MAX_CONTROL_FRACTION of its characters are control
    characters (NUL and friends -- a lone \\t/\\n/\\r doesn't count, real
    text can contain those) -- True for anything shorter (nothing
    meaningful to judge) or genuinely printable.

    Promoted here from sqlite_carve.py's own private `_text_plausible`
    2026-09-19 (a second real caller emerged: leveldb_viewer.py's own
    record-preview rendering needed the identical "is this decoded text
    real, or garbage that happened to decode without raising" check --
    see the "generic LevelDB view" Conventions entry in CLAUDE.md for
    why this specific check matters there: Latin-1 never raises on any
    byte 0-255, so successfully decoding is on its own no evidence the
    result is real text, the exact gap this closes). sqlite_carve.py's
    own carving-confidence use is unchanged behaviorally -- it now
    imports this function rather than keeping a second copy."""
    if len(value) < _TEXT_PLAUSIBLE_MIN_LEN:
        return True
    control = sum(1 for ch in value if ord(ch) < 32 and ch not in '\t\n\r')
    return control / len(value) <= _TEXT_PLAUSIBLE_MAX_CONTROL_FRACTION


def decompress_http_body(body: bytes, content_encoding: str) -> tuple[bytes | None, str | None]:
    """(decompressed_bytes_or_None, error_note_or_None). Never raises — a
    body that fails to decompress is reported as such, not silently
    dropped or shown as if it decompressed to something.

    Promoted here from chrome_cache.py's own private `_decompress_body`
    2026-09-22 (a second real caller emerged: embedded_media_scan.py's
    own HTTP-response-unwrap step, for a media blob some apps cache as a
    near-raw captured HTTP response rather than a clean file — nothing in
    this function's own logic was ever Chrome-specific, it was only
    Chrome-named because Chrome's Simple Cache was the first real place
    this project needed it; chrome_cache.py now imports this instead of
    keeping its own copy, same "promote once genuinely shared" pattern
    text_plausible above already went through). gzip and deflate are
    always available (stdlib zlib); brotli ('br') and zstd need the
    optional `brotli`/`zstandard` packages — both real, common encodings
    on real casework, not hypothetical (see chrome_cache.py's own
    original docstring for the real counts that justified adding them as
    dependencies)."""
    enc = (content_encoding or '').strip().lower()
    if not enc or enc == 'identity':
        return body, None
    try:
        if enc == 'gzip':
            return zlib.decompress(body, zlib.MAX_WBITS | 16), None
        if enc == 'deflate':
            try:
                return zlib.decompress(body, -zlib.MAX_WBITS), None
            except zlib.error:
                return zlib.decompress(body), None
        if enc == 'br':
            try:
                import brotli
            except ImportError:
                return None, "brotli-compressed body -- 'brotli' package not installed"
            return brotli.decompress(body), None
        if enc == 'zstd':
            try:
                import zstandard
            except ImportError:
                return None, "zstd-compressed body -- 'zstandard' package not installed"
            return zstandard.ZstdDecompressor().decompress(
                body, max_output_size=200 * 1024 * 1024), None
        return None, f"unrecognized content-encoding '{content_encoding}'"
    except Exception as exc:
        return None, f'{enc} decompression failed: {exc}'


# ── Loading ───────────────────────────────────────────────────────────────────

def load_artifacts(platform: str) -> tuple[list[tuple[str, object]], list[tuple[str, str]]]:
    """Load all artifact scripts for a platform.

    Returns (modules, errors):
      modules — [(script_name, module), ...] for scripts that imported cleanly.
      errors  — [(filename, message), ...] for scripts that failed to load,
                e.g. a stdlib dependency that wasn't bundled into the frozen
                build.  These are reported so the user knows a parser is
                unavailable instead of it silently vanishing from the list.
    """
    plat_dir = os.path.join(_ARTIFACTS_DIR, platform)
    modules: list[tuple[str, object]] = []
    errors:  list[tuple[str, str]]    = []
    if not os.path.isdir(plat_dir):
        return modules, errors
    for fname in sorted(os.listdir(plat_dir)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        path = os.path.join(plat_dir, fname)
        script_name = fname[:-3]
        try:
            spec = importlib.util.spec_from_file_location(script_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if getattr(mod, 'report_type', None) == 'recovered_records' and not hasattr(mod, 'run'):
                # a declaration-only parser: the shared code makes its rows
                mod.run = lambda paths, _m=mod: recovered_records_report(_m, paths)
                if not hasattr(mod, 'record_source'):
                    mod.record_source = _recovered_records_record_source(mod)
            modules.append((script_name, mod))
            import parser_versions
            parser_versions.check_version(platform, script_name, path)
        except Exception as exc:
            errors.append((fname, f"{type(exc).__name__}: {exc}"))
            print(f"[artifact_runner] Could not load {fname}: {exc}")
    # A main report's deleted_tables (A-95, 2026-11-27): one deleted-records
    # report per entry, made here, listed right after the main report.
    for script_name, mod in list(modules):
        if not getattr(mod, 'deleted_tables', None):
            continue
        at = modules.index((script_name, mod)) + 1
        taken = {sn for sn, _m in modules}
        for entry in mod.deleted_tables:
            made, problem = _deleted_table_module(platform, script_name, mod, entry, taken)
            if problem:
                errors.append((f"{script_name}.py", problem))
                continue
            modules.insert(at, (made.__name__, made))
            taken.add(made.__name__)
            at += 1
    by_name = dict(modules)
    for script_name, mod in list(modules):
        if getattr(mod, 'deleted_of', None):
            problem = _take_from_main_report(mod, by_name.get(mod.deleted_of))
            if problem:
                modules.remove((script_name, mod))
                errors.append((f"{script_name}.py", problem))
    for script_name, mod in modules:
        for spec in getattr(mod, 'recovered_records', None) or ():
            for link in spec.get('links') or ():
                other = by_name.get(link.get('report'))
                if other is not None:
                    link['report_name'] = getattr(other, 'name', link['report'])
                elif link.get('report'):
                    errors.append((f"{script_name}.py", f"links names '{link['report']}', "
                                   "which is not a report on this platform"))
    return modules, errors


# What a deleted_tables entry sets on its report rather than on its table
# spec (everything else is the recovered_records spec, documented below).
_DELETED_TABLE_MODULE_KEYS = ('name', 'description', 'warning', 'core_fields', 'hidden_fields',
                              'files', 'optional_files',
                              'timestamp_fields', 'timeline_fields', 'timeline_skip',
                              'media_fields', 'link_fields', 'category', 'app_group_label')

# Columns of every deleted-records report kept out of the default view.
DELETED_HIDDEN_FIELDS = ['record', 'recovered', 'recovery_method', 'recovery_version',
                         'source_table', 'source_file_key', 'raw_rowid', 'raw_page', 'raw_file',
                         'raw_offset', 'raw_length', 'source', 'table_certain', 'copy_locations']


def _deleted_table_module(platform: str, main_script: str, main, entry: dict, taken: set):
    """(module, None) for one entry of a main report's deleted_tables, or
    (None, what is wrong). The module is a report_type "recovered_records"
    report named entry['script'], deleted_of the main report; its version is
    tracked on the entry itself (parser_versions), so a change to the main
    report's query does not change it."""
    import types
    script = entry.get('script')
    if not script or not isinstance(script, str):
        return None, "a deleted_tables entry has no 'script' name"
    if script in taken:
        return None, f"deleted_tables names '{script}', which is already a report"
    missing = [k for k in ('name', 'description', 'file_key', 'table', 'fields') if k not in entry]
    if missing:
        return None, f"deleted_tables entry '{script}' has no {', '.join(missing)}"
    m = types.ModuleType(script)
    m.__file__ = getattr(main, '__file__', None)
    for key in _DELETED_TABLE_MODULE_KEYS:
        if key in entry:
            setattr(m, key, entry[key])
    spec = {k: v for k, v in entry.items() if k not in _DELETED_TABLE_MODULE_KEYS and k != 'script'}
    spec.setdefault('label', entry['table'])
    m.recovered_records = [spec]
    m.report_type = 'recovered_records'
    m.deleted_of = main_script
    m.deleted_table_of = main_script          # declared in the main report
    if not hasattr(m, 'hidden_fields'):
        m.hidden_fields = list(DELETED_HIDDEN_FIELDS)
    if not hasattr(m, 'core_fields'):
        m.core_fields = (['status'] + list(spec['fields'].values())[:3]
                         + [lk['name'] for lk in spec.get('links') or ()]
                         + ['changed_fields', 'damaged', 'how_found', 'copies'])
    m.run = lambda paths, _m=m: recovered_records_report(_m, paths)
    m.record_source = _recovered_records_record_source(m)
    try:
        import parser_versions
        parser_versions.check_version(platform, script, m.__file__ or '',
                                      data=repr(sorted(entry.items(), key=lambda kv: kv[0])).encode())
    except Exception:
        pass
    return m, None


def _take_from_main_report(mod, main) -> str | None:
    """A deleted-records report names its main report (`deleted_of`,
    ASSUMPTIONS.md A-95) and takes from it the app location (app_path /
    app_group) and every database file its tables name (file_key), from the
    main report's files or optional_files, so they are declared once. What
    the report declares itself wins (e.g. LINE's full-text index, a database
    the main report doesn't read). Adds this report's module to the main
    report's `deleted_reports` (one per table, A-95 2026-11-27) for the
    viewer's pointer.
    Returns what is wrong, or None."""
    if main is None:
        return f"deleted_of names '{mod.deleted_of}', which is not a report on this platform"
    if not hasattr(mod, 'app_path') and not hasattr(mod, 'app_group'):
        for attr in ('app_path', 'app_group'):
            if hasattr(main, attr):
                setattr(mod, attr, getattr(main, attr))
    files = dict(getattr(mod, 'files', None) or {})
    optional = dict(getattr(mod, 'optional_files', None) or {})
    main_files = getattr(main, 'files', None) or {}
    main_optional = getattr(main, 'optional_files', None) or {}
    for spec in getattr(mod, 'recovered_records', ()):
        key = spec['file_key']
        if key in files or key in optional:
            continue
        if key in main_files:
            files[key] = main_files[key]
        elif key in main_optional:
            optional[key] = main_optional[key]
        else:
            return f"file_key '{key}' is not a file of {mod.deleted_of} or of this report"
    mod.files, mod.optional_files = files, optional
    # the main report's extracted files are used, not a copy of their own
    mod.files_folder = getattr(main, 'name', None)
    core = list(getattr(mod, 'core_fields', None) or [])
    if core and 'damaged' not in core:
        at = core.index('how_found') if 'how_found' in core else len(core)
        core.insert(at, 'damaged')
        mod.core_fields = core
    reports = list(getattr(main, 'deleted_reports', None) or [])
    if mod not in reports:
        reports.append(mod)
    main.deleted_reports = reports
    return None


def list_artifacts(platform: str) -> list[tuple[str, object]]:
    """Return [(script_name, module), ...] for all valid scripts in artifacts/<platform>/.

    Thin wrapper over load_artifacts() for callers that don't need load errors."""
    return load_artifacts(platform)[0]


# ── File saving ───────────────────────────────────────────────────────────────

def safe_folder_name(name: str) -> str:
    """Convert a parser name to a safe folder name e.g. 'SMS Messages' → 'SMS_Messages'."""
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')


def _parser_files_dir(case_dir: str, parser_name: str) -> str:
    """Return (and create) case_dir/artifact_parser_files/<Parser_Name>/."""
    folder = os.path.join(case_dir, 'artifact_parser_files', safe_folder_name(parser_name))
    os.makedirs(folder, exist_ok=True)
    return folder


def _save_entry(zip_entry: ZipEntry, dest_path: str) -> None:
    """Write zip entry bytes to dest_path (skipped if file already exists)."""
    if os.path.exists(dest_path):
        return
    data = zip_entry.read()
    with open(dest_path, 'wb') as f:
        f.write(data)


def _make_zip_byte_reader(zip_path: str, zip_obj: zipfile.ZipFile | None):
    """Returns a callable(physical_path) -> bytes | None that reads one
    archive entry's bytes directly, no disk write — same ZipEntry path
    _extract_candidate already uses for a normal files/optional_files
    extraction, just without saving a copy. For _read_zip_bytes (see
    run_artifact) — a parser that needs to read MANY files by name rather
    than a fixed/globbed set. Best-effort like every other reserved-key
    helper here: None (never raises) on any failure, including when
    zip_obj itself wasn't supplied."""
    def _read(physical_path: str) -> bytes | None:
        if zip_obj is None:
            return None
        try:
            zinfo = zip_obj.getinfo(physical_path)
            return ZipEntry(zip_path, physical_path, zinfo).read()
        except Exception:
            return None
    return _read


def _build_case_context(zip_path: str, case_dir: str, adapter, guid_to_bundle: dict | None,
                        read_physical_bytes):
    """Build a mcp_server.CaseContext for a `device_wide` parser (see
    run_artifact's own device_wide branch), loaded fresh from the case's
    own persisted snapshot — never the live GUI window object, so a
    device-wide parser stays headless/testable like every other one.
    Returns None if no snapshot exists yet (case never fully opened).

    A real, subtle mismatch was caught before this shipped, not assumed
    correct from the two functions' similar names: `_make_zip_byte_reader`
    (the source of *read_physical_bytes* here — run_artifact's own
    `_read_zip_bytes` reserved key) takes a PHYSICAL zip entry name, but
    `CaseContext.read_bytes` (per its own docstring, and confirmed via
    app_intelligence.scan_apps()'s real usage) is called with a UI_PATH,
    which the CALLEE resolves internally — exactly what the GUI's own
    former Apps-tab construction did via `self._read_zip_bytes(ui_path)`
    (hex_viewer.py, which resolves ui_path -> physical before reading).
    Passing the physical-path reader straight through as read_bytes would
    have silently failed every single read (a ui_path never matches a
    real physical zip entry name directly for a GUID-containerized iOS
    app) — this wraps it with the same resolve-then-read step instead.

    guid_to_bundle is loaded fresh from disk (db_utils.load_guid_bundle_map)
    rather than trusting the *guid_to_bundle* parameter — found necessary
    by direct testing: scan_apps() resolves every GUID-named container to
    its real app via exactly this map (container_bundle_id), and an empty
    or stale one silently makes every container resolve to nothing, not
    an error. run_artifact's own callers today do thread a live, correct
    guid_to_bundle through, so this parameter would likely also work — but
    reading it fresh from the same persisted table the live value is
    itself sourced from removes any dependency on that being true, keeping
    this context construction fully self-sufficient from case_dir alone,
    same reasoning as ui_metadata/folder_map/folder_sizes above. The
    parameter is kept as a fallback for the (should never happen) case the
    disk read itself fails."""
    from ffs_metadata import load_snapshot_from_case
    from mcp_server import CaseContext

    snap = load_snapshot_from_case(case_dir)
    if snap is None:
        return None
    ui_metadata = snap['ui_metadata']
    folder_map  = snap['folder_map']

    try:
        with contextlib.closing(_open_cache_db(case_dir)) as db:
            _, folder_sizes = load_folder_data(db)
            disk_guid_to_bundle = load_guid_bundle_map(db)
    except Exception:
        folder_sizes = {}
        disk_guid_to_bundle = None
    guid_to_bundle = disk_guid_to_bundle or guid_to_bundle or {}

    def _read_ui_path_bytes(ui_path: str):
        try:
            physical = adapter.resolve(ui_path)
        except Exception:
            return None
        return read_physical_bytes(physical)

    return CaseContext(
        case_dir=case_dir,
        zip_path=zip_path,
        get_ui_metadata=lambda: ui_metadata,
        get_folder_map=lambda: folder_map,
        get_folder_sizes=lambda: folder_sizes,
        get_guid_to_bundle=lambda: guid_to_bundle,
        get_header_types=lambda: {},
        adapter=adapter,
        raw_content_enabled=True,
        read_bytes=_read_ui_path_bytes,
    )


def _extract_candidate(
    candidates: list[str],
    zip_path: str,
    dest_path: str,
    zip_obj: zipfile.ZipFile | None,
) -> bool:
    """Try each candidate path and extract the first match to dest_path.

    Returns True if a file was found and saved (or already existed), False if
    none of the candidates exist in the archive.
    """
    if zip_obj is not None:
        zip_names = set(zip_obj.namelist())
        for candidate in candidates:
            if candidate not in zip_names:
                continue
            zinfo = zip_obj.getinfo(candidate)
            _save_entry(ZipEntry(zip_path, candidate, zinfo), dest_path)
            return True

    return False


def _extract_db_sidecars(dest_path: str, candidates: list[str], zip_path: str,
                         zip_obj, zip_name_set: set) -> list[str]:
    """For an extracted SQLite database, also extract its -wal and -journal
    from the SAME archive location the database came from (never another
    copy of it elsewhere, which would mix two moments), beside the copy.
    Every parser gets them, declared or not: without the -wal a report reads
    stale rows and loses the WAL's history; without the -journal an
    unfinished change is not undone. A sidecar already there with the
    archive's size is kept; any other (e.g. an empty -wal SQLite made beside
    an earlier copy) is replaced, and a -shm the archive does not hold is
    removed with it (SQLite's own index, rebuilt from the WAL). Returns the
    sidecars written."""
    written: list[str] = []
    if zip_obj is None:
        return written
    try:
        with open(dest_path, 'rb') as f:
            if f.read(16) != b'SQLite format 3\x00':
                return written
    except OSError:
        return written
    src = next((c for c in candidates if c in zip_name_set), None)
    if src is None:
        return written
    for suffix in ('-wal', '-journal'):
        name = src + suffix
        if name not in zip_name_set:
            continue
        info = zip_obj.getinfo(name)
        dest = dest_path + suffix
        if os.path.exists(dest) and os.path.getsize(dest) == info.file_size:
            continue
        data = ZipEntry(zip_path, name, info).read()
        with open(dest, 'wb') as f:
            f.write(data)
        written.append(os.path.basename(dest))
        if suffix == '-wal' and os.path.exists(dest_path + '-shm'):
            shm = src + '-shm'
            if shm not in zip_name_set or os.path.getsize(dest_path + '-shm') != zip_obj.getinfo(shm).file_size:
                os.remove(dest_path + '-shm')
    return written


# ── Report type: recovered records ───────────────────────────────────────────
# A parser that declares report_type = "recovered_records" has no run() of
# its own: it declares the database file(s) and, per table, the columns that
# matter, and this shared code makes the report (owner design, 2026-11-17).
# Each row is one recovered record as found (the table's own values, never
# joined to other tables), with how it was found, how sure that is, and
# where its bytes are:
#   - Deleted: a row no longer in the table (deleted since the checkpoint,
#     in a WAL frame, free space, a freed page, overflow, by pattern), and
#     Unfinished write / Unfinished delete under a hot journal;
#   - Changed: an earlier version of a row that is still live, listed only
#     when one of the table's version_fields differs from the live row
#     (the live value beside it as "<field> (now)"). Versions that differ
#     only in other columns are counted in the run log, never silently
#     dropped. No version_fields: every version is listed.
# Declaration, one entry per table:
#   recovered_records = [{
#       "file_key": "line",            # a files/optional_files key
#       "table": "ZMESSAGE",
#       "label": "Message",            # shown in the Record column
#       "fields": {"ZTEXT": "message_text", ...},   # column -> report name
#       "version_fields": ["ZTEXT", ...],           # what makes a version matter
#       "labels": {"ZCONTENTTYPE": {0: "text", 1: "picture"}},  # optional:
#               shown as "picture (1)": the word, then the stored value kept;
#               a value without a label is shown as stored
#       "derived": {"direction": {"from": "ZSENDER", "values": {None: "Sent"},
#                                 "else": "Received"}},           # optional
#               or, when the deciding ids differ per phone, "in": {"table":
#               "ZCHATMEMBER", "column": "Z_PK", "where": "ZISLOGGEDINUSER = 1"},
#               "then": "Outgoing", "else": "Incoming": the value is looked up
#               in that column of the rows the where-clause selects (which
#               ids are the local user); no field of that table is added
#       "not_found_in": {"ZLASTMESSAGE": ["ZMESSAGE", "ZTEXT"]},  # optional:
#               list a record only when this column's value is not the value
#               of that other table's column in any live or recovered row
#               (e.g. a chat's last-message preview: only the ones that are
#               the sole trace of a message). A comparison of values, not a
#               join; the ones left out are counted in the run log.
#       "json_fields": ["value"],     # optional: a column holding the whole
#               record as a JSON object (Room's TypeConverter pattern, e.g.
#               Burner's MessageEntity): its keys are added as
#               "<field>.<key>" (sqlite_carve.flatten_json_fields); the
#               column itself is kept
#       "field_notes": {"value": "..."},   # optional: a caveat checked against
#               ground truth, shown on every row as "<field>_caveat"
#       "orphan_of": {"column": "message_row_id", "in": ["message", "_id"]},
#               optional, for a media/attachment table: also list its LIVE
#               rows whose message is gone (status Orphaned): not deleted, so
#               never recovered, and missing from the main report. "via":
#               [join table, its column for this row, its column for the
#               message] when a join table links them (iOS SMS). Compared by
#               id, never joined (A-95)
#       "blob_files": {"thumbnail": "thumbnail_file"},   # optional: a BLOB
#               column saved as a file (_save_recovered_blob) and its path put
#               in the named column, for media_fields (a thumbnail)
#       "links": [{"name": "linked_message", "what": "message",
#                  "column": "message_row_id", "table": "message", "key": "_id",
#                  "report": "whatsapp_deleted"}],
#               optional (A-95, 2026-11-27: one table per report): a column
#               saying where the rows of another table whose *key* equals
#               this record's *column* were found: live (in the table now),
#               recovered (as that table's deleted report finds them; its
#               row id) or not found. "via": [join table, its column equal
#               to ours, its column equal to the other's key] (iOS SMS),
#               the join rows themselves taken live or recovered. Ids are
#               compared, never joined: no value of the other row is shown.
#               The report's description says how the link is made and why
#               it can be wrong (ids reused, versions from different times,
#               records with no row id or table not certain).
#   }]
# Only declare a label or derived value that was checked against ground
# truth, and say where in the parser.
# timestamp_fields uses the report names, as for any parser.

RECOVERED_STATUS = {
    'checkpoint_deleted': 'Deleted', 'wal_frame': 'Deleted', 'journal': 'Deleted',
    'freeblock': 'Deleted', 'page_gap': 'Deleted', 'freed_page': 'Deleted',
    'overflow': 'Deleted', 'header_signature': 'Deleted', 'header_rebuilt': 'Deleted',
    'header_schema': 'Deleted', 'unfinished_write': 'Unfinished write',
    'unfinished_delete': 'Unfinished delete', 'checkpoint_older': 'Changed',
}


_BLOB_EXT = ((b'\xff\xd8\xff', 'jpg'), (b'\x89PNG', 'png'), (b'GIF8', 'gif'), (b'RIFF', 'webp'))


def _save_recovered_blob(paths: dict, module, r: dict, data) -> str:
    """A recovered record's BLOB (blob_files: e.g. a thumbnail) as a file in
    the report's files folder, recovered_blobs/<report>/<file>-<offset>.<ext>
    (ext from its first bytes, else .bin), so the report can show it
    (media_fields: an absolute path is read from disk). The bytes are the
    record's as found, never changed. '' when there is no BLOB or no
    folder."""
    folder = paths.get('_parser_files_dir')
    if not folder or not isinstance(data, (bytes, bytearray)) or not data:
        return ''
    ext = next((e for magic, e in _BLOB_EXT if bytes(data[:len(magic)]) == magic), 'bin')
    where = (r['offset'] if r.get('offset') is not None
             else f"p{r.get('page')}-r{r.get('rowid')}")
    out_dir = os.path.join(folder, 'recovered_blobs', getattr(module, '__name__', 'report'))
    path = os.path.join(out_dir, f"{r['file']}-{where}.{ext}")
    try:
        os.makedirs(out_dir, exist_ok=True)
        if not os.path.exists(path):
            with open(path, 'wb') as f:
                f.write(bytes(data))
    except OSError as exc:
        log_note(f"recovered BLOB not saved ({exc}).")
        return ''
    return path


def _key_index(conn, checkpoint, carver, table: str, column: str, cache: dict):
    """For a `links` lookup: (index, is_id) for *table*, where index maps a
    value of *column* to the rows holding it, [(where, rowid, table_certain,
    values)], where 'live' (in the table now) or 'recovered' (a record
    sqlite_recovery finds whose row id is not live: what the table's deleted
    report lists; an older version of a live row is the live row). is_id:
    *column* is the table's row id. None when the table or column can't be
    read. Kept in *cache* for the report's other links."""
    import sqlite_recovery
    ck = (table, column)
    if ck in cache:
        return cache[ck]
    out = None
    t = sqlite_recovery.table_info(conn, table, checkpoint)
    if t is not None:
        is_id = column == t.id_col or (column.lower() == 'rowid' and column not in t.columns)
        i = t.columns.index(column) if column in t.columns else None
        if i is not None or is_id:
            index: dict = {}

            def val(rowid, vals):
                if is_id:
                    return rowid
                return vals[i] if i < len(vals) else None
            for rowid, vals in t.live.items():
                v = val(rowid, vals)
                if v is not None:
                    index.setdefault(v, []).append(('live', rowid, True, vals))
            records, _notes = carver.records(t.name)
            for r in records:
                if r['rowid'] is not None and r['rowid'] in t.live:
                    continue
                v = val(r['rowid'], r['values'])
                if v is not None:
                    index.setdefault(v, []).append(('recovered', r['rowid'], r['table_certain'],
                                                    r['values']))
            out = (index, is_id, t.columns)
    cache[ck] = out
    return out


def _col_value(t, vals, rowid, column):
    """*column*'s value in a record of table *t* (its row id for the rowid
    or its alias)."""
    if column == t.id_col or (column.lower() == 'rowid' and column not in t.columns):
        return rowid
    i = t.columns.index(column)
    return vals[i] if i < len(vals) else None


def _link_hits(hits: list, report_name: str) -> str:
    """'live' / 'recovered (in <report>)' / both, with the row ids where
    the matched column is not the row id (rows of a one-to-many link)."""
    live = sorted({rid for w, rid, _c, _v in hits if w == 'live'})
    rec = sorted({rid for w, rid, _c, _v in hits if w == 'recovered' and rid is not None})
    no_id = sum(1 for w, rid, _c, _v in hits if w == 'recovered' and rid is None)
    unsure = any(w == 'recovered' and not c for w, _r, c, _v in hits)
    parts = []
    if live:
        parts.append('live row ' + ', '.join(map(str, live)))
    if rec or no_id:
        what = []
        if rec:
            what.append('row ' + ', '.join(map(str, rec)))
        if no_id:
            what.append(f"{no_id} with no row id")
        parts.append('recovered ' + ', '.join(what) + f" (in {report_name}"
                     + ('; table not certain' if unsure else '') + ')')
    return '; '.join(parts)


def _link_text(link: dict, v, target, join=None) -> str:
    """The `links` cell for one record whose link column holds *v*."""
    what = link.get('what', link['table'])
    name = link.get('report_name') or link['table']
    if target is None:
        return f"not checked: {link['table']}.{link['key']} could not be read"
    if v is None:
        return f"no {what} id"

    def describe(w):
        index, is_id, _cols = target
        hits = index.get(w, [])
        if is_id:
            found = (_link_hits(hits, name).replace(f'live row {w}', 'live')
                     .replace(f'recovered row {w} (', 'recovered ('))
            return f"{what} {w}: " + (found or 'not found')
        return f"{what} with {link['key']} = {w}: " + (_link_hits(hits, name) or 'not found')
    if link.get('via') is None:
        return describe(v)
    if join is None:
        return f"not checked: {link['via'][0]} could not be read"
    jindex, _is_id, jcols = join
    jt, _mine, theirs = link['via']
    j = jcols.index(theirs) if theirs in jcols else None
    out = []
    for where, _rid, certain, vals in jindex.get(v, []):
        w = vals[j] if j is not None and j < len(vals) else None
        jw = 'live' if where == 'live' else ('recovered' if certain else 'recovered, table not certain')
        out.append(f"{describe(w) if w is not None else f'no {what} id'} ({jw} {jt} row)")
    return '; '.join(dict.fromkeys(out)) or f"no {jt} row for {v}"


def recovered_records_report(module, paths: dict) -> list[dict]:
    """The rows of a report_type = "recovered_records" parser (see above)."""
    rows: list[dict] = []
    seen_values: dict = {}       # (table, column) -> values in live and recovered rows
    carvers: dict = {}           # database file -> its ReportCarver (one carve per file)
    try:
        return _recovered_records_rows(module, paths, rows, seen_values, carvers)
    finally:
        for c in carvers.values():
            c.close()


def _recovered_records_rows(module, paths, rows, seen_values, carvers) -> list[dict]:
    import sqlite_reader
    import sqlite_recovery
    from sqlite_carve import blob_safe
    for spec in getattr(module, 'recovered_records', ()):
        db_path = paths.get(spec['file_key'])
        if not db_path or not os.path.isfile(db_path):
            log_note(f"{spec.get('label', spec['table'])}: {spec['file_key']} not in this extraction.")
            continue
        carver = carvers.get(db_path)
        if carver is None:
            carver = carvers[db_path] = sqlite_recovery.ReportCarver(
                db_path, (paths.get('_ui_paths') or {}).get(spec['file_key']),
                paths.get('_case_dir'))
        conn, notes = sqlite_reader.connect_committed(db_path)
        checkpoint = sqlite_reader.connect_checkpoint(db_path)
        try:
            t = sqlite_recovery.table_info(conn, spec['table'], checkpoint)
            if t is None:
                log_note(f"{spec['table']} is not a table with rowids in "
                         f"{os.path.basename(db_path)}: nothing recovered from it.")
                continue
            records, rnotes = carver.records(t.name)
            idx0 = {c: i for i, c in enumerate(t.columns)}
            for c, i in idx0.items():
                bucket = seen_values.setdefault((t.name, c), set())
                bucket.update(v[i] for v in t.live.values())
                bucket.update(r['values'][i] for r in records if i < len(r['values']))
            lookups = {}
            for dname, dspec in (spec.get('derived') or {}).items():
                if 'in' in dspec:
                    q = dspec['in']
                    try:
                        lookups[dname] = {r[0] for r in conn.execute(
                            f'SELECT "{q["column"]}" FROM "{q["table"]}" WHERE {q["where"]}')}
                    except sqlite3.Error as e:
                        lookups[dname] = None
                        log_note(f"{spec.get('label', spec['table'])}: {dname} not worked out "
                                 f"({q['table']}: {e}).")
            not_in = {}
            for col, (otable, ocol) in (spec.get('not_found_in') or {}).items():
                vals_ = seen_values.get((otable, ocol))
                if vals_ is None:
                    # the other table is another report's (one table per
                    # report, A-95 2026-11-27): its live values and the
                    # values of every record its deleted report finds, as
                    # when both tables were one report (seen_values)
                    vals_ = set()
                    ot = sqlite_recovery.table_info(conn, otable, checkpoint)
                    if ot is not None and ocol in ot.columns:
                        oi = ot.columns.index(ocol)
                        vals_.update(v[oi] for v in ot.live.values())
                        vals_.update(r['values'][oi] for r in carver.records(ot.name)[0]
                                     if oi < len(r['values']))
                    else:
                        try:
                            vals_ = {r[0] for r in conn.execute(f'SELECT "{ocol}" FROM "{otable}"')}
                        except sqlite3.Error:
                            vals_ = set()
                not_in[col] = (vals_, otable, ocol)
            # Live rows whose message is gone (orphan_of): not deleted, so
            # never recovered, and missing from the main report, whose query
            # starts from the message. Compared by id, never joined (A-95).
            orphans = []
            ospec = spec.get('orphan_of')
            if ospec:
                ocol, (ttab, tkey) = ospec['column'], ospec['in']
                target = f'SELECT "{tkey}" FROM "{ttab}" WHERE "{tkey}" IS NOT NULL'
                if ospec.get('via'):
                    jt, jleft, jright = ospec['via']
                    target = (f'SELECT "{jleft}" FROM "{jt}" WHERE "{jleft}" IS NOT NULL '
                              f'AND "{jright}" IN ({target})')
                try:
                    orphans = [r[0] for r in conn.execute(
                        f'SELECT rowid FROM "{t.name}" WHERE "{ocol}" IS NOT NULL '
                        f'AND "{ocol}" NOT IN ({target})')]
                except sqlite3.Error as e:
                    log_note(f"{spec.get('label', spec['table'])}: orphaned rows not checked ({e}).")
            # links (A-95, 2026-11-27): where the other table's rows with
            # this record's key were found; ids compared, never joined
            key_cache: dict = {}
            links = []
            for link in spec.get('links') or ():
                target = _key_index(conn, checkpoint, carver, link['table'], link['key'], key_cache)
                join = (_key_index(conn, checkpoint, carver, link['via'][0], link['via'][1], key_cache)
                        if link.get('via') else None)
                if target is None or (link.get('via') and join is None):
                    log_note(f"{spec.get('label', spec['table'])}: {link['name']} not worked out "
                             f"({link['table']} or its key not readable).")
                links.append((link, target, join))
        finally:
            conn.close()
            if checkpoint is not None:
                checkpoint.close()
        for n in notes + rnotes:
            log_note(f"{spec.get('label', spec['table'])}: {n}")
        fields = spec.get('fields') or {c: c for c in t.columns}
        missing = [c for c in fields if c not in t.columns]
        if missing:
            log_note(f"{spec['table']} has no column(s) {', '.join(missing)} in this version of the app.")
        vf = [c for c in (spec.get('version_fields') or []) if c in t.columns]
        idx = {c: t.columns.index(c) for c in t.columns}
        label = spec.get('label', spec['table'])
        seen: dict = {}          # copy key -> the row listed for it
        hidden: dict = {}
        known = 0
        for r in records:
            status = RECOVERED_STATUS.get(r['method'], 'Deleted')
            vals = list(r['values'])
            rowid = r['rowid']
            live = t.live.get(rowid) if rowid is not None else None
            if r['method'] == 'unfinished_delete':
                status = 'Unfinished delete'
            elif r.get('in_unfinished_write'):
                status = 'Unfinished write'
            elif live is not None:
                status = 'Changed'
            changed = []
            if status == 'Changed':
                changed = [c for c in (vf or t.columns)
                           if idx[c] < len(vals) and vals[idx[c]] != live[idx[c]]]
                if not changed:
                    hidden[rowid] = hidden.get(rowid, 0) + 1
                    continue
            if any(idx[c] < len(vals) and vals[idx[c]] in pool
                   for c, (pool, _ot, _oc) in not_in.items() if c in idx):
                known += 1
                continue
            # One row per record: a copy (the same row id and version
            # values, or the same shown values under any row id, as an app
            # that rewrites by delete + insert leaves; A-95) is counted on
            # the row already listed, with its location, never dropped.
            key_cols = vf or t.columns
            key = (status, rowid, tuple(repr(vals[idx[c]]) if idx[c] < len(vals) else None
                                        for c in key_cols))
            shown_key = (status,
                         tuple(repr(vals[idx[c]]) if c in idx and idx[c] < len(vals) else None
                               for c in fields),
                         tuple(repr(live[idx[c]]) for c in changed))
            first = seen.get(key) or seen.get(shown_key)
            if first is not None:
                first['copies'] += 1
                first['copy_locations'] += (f"; {r['file']} offset {r['offset']}"
                                            + (f" rowid {rowid}" if rowid is not None else ''))
                seen.setdefault(key, first)
                seen.setdefault(shown_key, first)
                continue
            labels = spec.get('labels') or {}

            def shown(col, v):
                table_ = labels.get(col)
                if table_ and v in table_:
                    return f"{table_[v]} ({v})" if v is not None else table_[v]
                return blob_safe(v)

            def value(col):
                return vals[idx[col]] if col in idx and idx[col] < len(vals) else None
            row = {'record': label, 'status': status, 'row_id': rowid}
            for name, d in (spec.get('derived') or {}).items():
                if d['from'] in idx and idx[d['from']] < len(vals):
                    v = value(d['from'])
                    if 'in' in d:
                        ids = lookups.get(name)
                        row[name] = '' if ids is None else (d['then'] if v in ids else d.get('else', ''))
                    else:
                        row[name] = d['values'][v] if v in d['values'] else d.get('else', '')
            for col, name in fields.items():
                if col in idx:
                    row[name] = shown(col, value(col))
            for col in spec.get('json_fields') or ():
                if col in idx:
                    from sqlite_carve import flatten_json_fields
                    for k, v in flatten_json_fields({fields.get(col, col): value(col)}).items():
                        row.setdefault(k, blob_safe(v))
            for col, note_text in (spec.get('field_notes') or {}).items():
                if col in idx:
                    row[f"{fields.get(col, col)}_caveat"] = note_text
            for col, name in (spec.get('blob_files') or {}).items():
                if col in idx:
                    row[name] = _save_recovered_blob(paths, module, r, value(col))
            for c in changed:
                if c in fields:
                    now = live[idx[c]]
                    # an empty current value says so, rather than a blank
                    # cell that reads as missing data
                    row[f"{fields[c]} (now)"] = '(empty)' if now in (None, '') else shown(c, now)
            note = sqlite_recovery.describe(r)
            row.update({
                'changed_fields': ', '.join(fields.get(c, c) for c in changed),
                'how_found': note,
                # kept and flagged, never cleaned up (A-95, 2026-11-27)
                'damaged': sqlite_recovery.damage(r),
                'table_certain': r['table_certain'],
                'source': f"Carved — {label}" + (f" ({note})" if note else ''),
                'recovered': True,
                'recovery_method': r['method'],
                'recovery_version': sqlite_recovery.RECOVERY_VERSION,
                'source_table': t.name,
                'source_file_key': spec['file_key'],
                'raw_rowid': rowid,
                'raw_page': r['page'],
                'raw_file': r['file'],
                'raw_offset': r['offset'],
                'raw_length': r['length'],
            })
            if r.get('truncated'):
                row['truncated'] = True
            for link, target, join in links:
                if link['column'] in idx or link['column'].lower() == 'rowid':
                    row[link['name']] = _link_text(link, _col_value(t, vals, rowid, link['column']),
                                                   target, join)
            row['copies'] = 1
            row['copy_locations'] = (f"{r['file']} offset {r['offset']}"
                                     + (f" rowid {rowid}" if rowid is not None else ''))
            seen[key] = row
            seen[shown_key] = row
            rows.append(row)
        if orphans:
            labels_ = spec.get('labels') or {}
            what = (f"{ospec['in'][0]} (through {ospec['via'][0]})" if ospec.get('via')
                    else ospec['in'][0])
            for rowid in orphans:
                vals = t.live.get(rowid)
                if vals is None:
                    continue
                row = {'record': label, 'status': 'Orphaned', 'row_id': rowid}
                for col, name in fields.items():
                    if col in idx and idx[col] < len(vals):
                        v = vals[idx[col]]
                        tab = labels_.get(col)
                        row[name] = (f"{tab[v]} ({v})" if tab and v in tab and v is not None
                                     else blob_safe(v))
                for col in spec.get('json_fields') or ():
                    if col in idx:
                        from sqlite_carve import flatten_json_fields
                        for k, v in flatten_json_fields({fields.get(col, col): vals[idx[col]]}).items():
                            row.setdefault(k, blob_safe(v))
                for col, note_text in (spec.get('field_notes') or {}).items():
                    if col in idx:
                        row[f"{fields.get(col, col)}_caveat"] = note_text
                for link, target, join in links:
                    if link['column'] in idx or link['column'].lower() == 'rowid':
                        row[link['name']] = _link_text(
                            link, _col_value(t, vals, rowid, link['column']), target, join)
                key_v = vals[idx[ospec['column']]] if ospec['column'] in idx else rowid
                row.update({
                    'changed_fields': '',
                    'damaged': '',
                    'how_found': (f"live row; no {what} row has its "
                                  f"{fields.get(ospec['column'], ospec['column'])} ({key_v})"),
                    'table_certain': True,
                    'source': f"Live — {label} (orphaned)",
                    'recovered': False,
                    'recovery_method': 'orphaned_live_row',
                    'recovery_version': sqlite_recovery.RECOVERY_VERSION,
                    'source_table': t.name,
                    'source_file_key': spec['file_key'],
                    'raw_rowid': rowid, 'raw_page': None, 'raw_file': 'main',
                    'raw_offset': None, 'raw_length': None,
                    'copies': 1, 'copy_locations': '',
                })
                rows.append(row)
            log_note(f"{label}: {len(orphans)} live row(s) whose {what} is gone: "
                     "listed as Orphaned.")
        collapsed = sum(x['copies'] - 1 for x in rows if x.get('source_table') == t.name
                        and x.get('source_file_key') == spec['file_key'] and x['record'] == label)
        if collapsed:
            log_note(f"{label}: {collapsed} identical {'copy' if collapsed == 1 else 'copies'} "
                     "of records already listed: counted in copies, their locations in "
                     "copy_locations.")
        if known:
            where = '; '.join(f"{fields.get(c, c)} = a {ot}.{oc} value" for c, (_p, ot, oc) in not_in.items())
            log_note(f"{label}: {known} record(s) whose {where} already in the report or the "
                     "live table: not listed.")
        if hidden:
            names = ', '.join(fields.get(c, c) for c in vf)
            log_note(f"{label}: {sum(hidden.values())} earlier version(s) of {len(hidden)} live "
                     f"row(s) differ only in columns other than {names}: not listed.")
    return rows


def _recovered_records_record_source(module) -> list[dict]:
    """record_source for the Hex panel's Record mode (raw offset jump)."""
    return [{'label': s.get('label', s['table']), 'file_key': s['file_key'],
             'table': s['table'], 'rowid_fields': ['row_id']}
            for s in getattr(module, 'recovered_records', ())]


# ── Running ───────────────────────────────────────────────────────────────────

def _resolve_app_group_base(app_group: str, guid_to_bundle: dict) -> str | None:
    """iOS third-party app data lives under a GUID-named container
    (Containers/Data/Application/<GUID>/… for the app's own sandbox, or
    Containers/Shared/AppGroup/<GUID>/… for data an app explicitly shares
    via an App Group entitlement — the GUID is random per install, never
    the same across extractions, so a module can't hardcode it the way
    Android's package-name-based app_path works. A module declares the
    App Group's own stable bundle-style identifier instead (e.g.
    "group.net.whatsapp.WhatsApp.shared", confirmed against a real
    extraction's guid_to_bundle map — see artifacts/ios/whatsapp.py) and
    this reverse-looks-up which GUID that resolved to on THIS device.
    guid_to_bundle is {guid: bundle_id}, already built once at case-load
    (FastZipBrowser.guid_to_bundle) from Cellebrite's own MCM metadata —
    no new resolution mechanism, just reusing it here."""
    for guid, bundle_id in (guid_to_bundle or {}).items():
        if bundle_id == app_group:
            return f"mobile/Containers/Shared/AppGroup/{guid}"
    return None


def _resolve_app_bundle_base(bundle_id: str, guid_to_bundle: dict) -> str | None:
    """Same resolution as _resolve_app_group_base, but for an app's own
    Data/Application container instead of a Shared/AppGroup one — added
    2026-08-25 (see app_bundle_id in the module docstring above) once a
    real case (Instagram's DirectSQLiteDatabase) needed an app's own
    container rather than an App Group, which nothing in this file
    supported until now. guid_to_bundle's keys are container GUIDs as
    they appear in ui_metadata/zip_names (WITH dashes, standard UUID
    form) — confirmed against real casework this session that a
    container GUID never triggers adapters/ffs.py's separate 32-hex-
    suffix reduction rule (that rule targets a different, longer kind of
    hex identifier elsewhere in Cellebrite paths), so no special handling
    is needed here beyond the literal string join AppGroup's own version
    already does."""
    for guid, candidate_bundle_id in (guid_to_bundle or {}).items():
        if candidate_bundle_id == bundle_id:
            return f"mobile/Containers/Data/Application/{guid}"
    return None


def _resolve_glob_subpath(app_base: str, subpath: str, adapter,
                          zip_names) -> str | None:
    """If *subpath* contains a glob character, resolve it to the first
    matching real relative path found under *app_base* in the archive.
    Returns *subpath* unchanged if it has no glob character (the normal,
    fixed-filename case — this function is then a no-op). Returns None if
    a glob subpath matches nothing.

    Matches iLEAPP's own glob convention (see its own artifact-paths
    rules): '*' spans '/', so a pattern with more path segments than
    intended could over-match — same tradeoff that project accepts, not
    a bug specific to this implementation. Added 2026-08-25 for a per-
    install-varying filename (see app_bundle_id/files above)."""
    if not any(c in subpath for c in '*?['):
        return subpath
    for prefix in adapter.user_candidates(app_base):
        p = prefix.rstrip('/') + '/'
        for name in zip_names:
            if name.startswith(p) and not is_capture_alias(name):
                remainder = name[len(p):]
                if fnmatch.fnmatch(remainder, subpath):
                    return remainder
    return None


def resolve_module_file_ui_path(module, file_key: str, guid_to_bundle: dict | None,
                                adapter=None, zip_names=None) -> str | None:
    """Rebuild the archive ui_path for one of a module's declared
    `files`/`optional_files` entries, without extracting or touching disk —
    used by the Artifact Viewer's "jump to record in hex" feature
    (`record_source` on a module) to re-read a source database's CURRENT
    archive bytes at click time, long after the parser run that populated
    the report table. Deliberately never returns a path to the cached
    extracted copy in artifact_parser_files/ — the parser's own live query
    now opens that copy read-only (see open_db_readonly), but the same
    cached copy can still be opened non-read-only elsewhere (the Database
    tab's own sqlite_viewer.py, browsing that exact extracted file) and
    checkpointed since, where the archive entry itself never changes.
    Mirrors run_artifact's own app_base/ui_path join; returns None if the
    module uses the single-file `target_paths` API instead (nothing to key
    by there) or the key isn't declared."""
    has_app_path  = hasattr(module, 'app_path')  and hasattr(module, 'files')
    has_app_group = hasattr(module, 'app_group') and hasattr(module, 'files')
    has_app_bundle_id = hasattr(module, 'app_bundle_id') and hasattr(module, 'files')
    if not (has_app_path or has_app_group or has_app_bundle_id):
        return None
    if has_app_path:
        app_base = module.app_path.strip('/')
    elif has_app_group:
        app_base = _resolve_app_group_base(module.app_group, guid_to_bundle or {})
        if app_base is None:
            return None
    else:
        app_base = _resolve_app_bundle_base(module.app_bundle_id, guid_to_bundle or {})
        if app_base is None:
            return None
    subpath = module.files.get(file_key)
    if subpath is None:
        subpath = getattr(module, 'optional_files', {}).get(file_key)
    if subpath is None:
        return None
    # A glob subpath needs the archive's real entry list to resolve to a
    # concrete filename — added 2026-08-25 alongside glob support itself,
    # after confirming by direct test that without this, a glob-based
    # parser's Hex-panel "jump to record" silently pointed at a literal,
    # nonexistent '*.db' path instead of the real file. adapter/zip_names
    # are optional (not every caller has them at hand) — a glob subpath is
    # returned unresolved (the old, broken-for-globs behavior) only if
    # they're omitted, same as before this fix existed.
    if any(c in subpath for c in '*?[') and adapter is not None and zip_names is not None:
        resolved = _resolve_glob_subpath(app_base, subpath, adapter, zip_names)
        if resolved is not None:
            subpath = resolved
    return f"{app_base}/{subpath.lstrip('/')}"


def run_artifact(
    script_name: str,
    module,
    zip_path: str,
    adapter,
    case_dir: str,
    zip_obj: zipfile.ZipFile | None = None,
    guid_to_bundle: dict | None = None,
) -> tuple[list[dict], str]:
    """Run one artifact parser against the open archive.

    Source files are saved to case_dir/artifact_parser_files/<name>/ and
    kept for direct inspection.  Extraction is skipped if the file exists.

    Returns (rows, error). On success rows is a list[dict] — the parser's
    own query result (live rows only; deleted records are a
    recovered_records report of their own, A-95). On failure rows is [] and
    error describes what went wrong.
    """
    parser_name = getattr(module, 'name', script_name)
    # a deleted-records report reads its main report's files (files_folder,
    # _take_from_main_report), so they are extracted once
    dest_dir    = _parser_files_dir(case_dir, getattr(module, 'files_folder', None) or parser_name)

    # ── Device-wide API ──────────────────────────────────────────────────────
    if getattr(module, 'device_wide', False):
        zip_names = zip_obj.namelist() if zip_obj is not None else []
        paths: dict[str, object] = {}
        paths['_zip_names']       = zip_names
        paths['_read_zip_bytes']  = _make_zip_byte_reader(zip_path, zip_obj)
        paths['_adapter']         = adapter
        paths['_parser_files_dir'] = dest_dir
        ctx = _build_case_context(zip_path, case_dir, adapter, guid_to_bundle,
                                  paths['_read_zip_bytes'])
        if ctx is None:
            return [], (f"{script_name}: case snapshot not available yet "
                        f"(re-open the case, then try running this report again)")
        paths['_case_context'] = ctx
        try:
            rows = module.run(paths) or []
        except Exception as exc:
            return [], f"{script_name}: {exc}"
        return rows, ''

    # ── Multi-file API ────────────────────────────────────────────────────────
    has_app_path      = hasattr(module, 'app_path')      and hasattr(module, 'files')
    has_app_group     = hasattr(module, 'app_group')     and hasattr(module, 'files')
    has_app_bundle_id = hasattr(module, 'app_bundle_id') and hasattr(module, 'files')
    if has_app_path or has_app_group or has_app_bundle_id:
        if has_app_path:
            app_base = module.app_path.strip('/')
        elif has_app_group:
            app_base = _resolve_app_group_base(module.app_group, guid_to_bundle)
            if app_base is None:
                return [], f"{script_name}: app group '{module.app_group}' not present on this device"
        else:
            app_base = _resolve_app_bundle_base(module.app_bundle_id, guid_to_bundle)
            if app_base is None:
                return [], f"{script_name}: bundle id '{module.app_bundle_id}' not present on this device"
        zip_names = zip_obj.namelist() if zip_obj is not None else []
        paths: dict[str, str] = {}
        # Reserved key (leading underscore — never a real `files`/
        # `optional_files` entry, whose keys are chosen by the module
        # author): the container's own ui_path, for a parser that needs to
        # build a *reference* to some other file inside the container
        # rather than extract it directly — e.g. an attachment path stored
        # in a database column, which artifact_media.py resolves later at
        # display time via the app's normal zip-reading path, never copied
        # to disk by the parser itself. See media_fields on the module and
        # artifacts/ios/whatsapp.py's `attachment_path` for the convention.
        # Translated to this archive's real ui_path (GrayKey iOS keeps the
        # 'private/var/' prefix the Cellebrite-style app_path omits), and the
        # translator itself for paths a parser builds outside its own base
        # (e.g. SMS attachments under the device's 'mobile' home). Finding
        # the parser's own files needs neither — user_candidates() already
        # tries every layout — but any path written INTO a report does.
        _name_set = set(zip_names)
        def _ui_path_for(p, _names=_name_set):
            return adapter.ui_path_for(p, _names) if adapter is not None else p.strip('/')
        paths['_app_base_ui_path'] = _ui_path_for(app_base)
        paths['_ui_path_for'] = _ui_path_for

        # Reserved keys for a parser that needs to enumerate/read MANY
        # files by name rather than a fixed/globbed files/optional_files
        # set — e.g. chrome_cache.py, which has to read every entry in a
        # whole directory (Chrome's Simple Cache: thousands of
        # arbitrarily-named files, no fixed set to declare up front).
        # _zip_names is the full archive namelist (the parser filters it
        # itself, e.g. by path prefix); _read_zip_bytes(name) reads one
        # entry's bytes directly, no disk write. Both best-effort, same
        # as _app_base_ui_path's own convention.
        paths['_zip_names'] = zip_names
        paths['_read_zip_bytes'] = _make_zip_byte_reader(zip_path, zip_obj)
        # zip_names above is the ARCHIVE'S OWN PHYSICAL namelist (whatever
        # zip_obj.namelist() returns — e.g. a "Dump/" root-folder prefix
        # for a zip_extras-format archive), never the ui_path convention
        # every OTHER reserved path here uses. A parser that needs to
        # filter it by a ui_path-style prefix (e.g. chrome_cache.py
        # scoping to "<app_base>/cache/Cache/Cache_Data/") MUST resolve
        # that prefix through the adapter first — comparing a bare
        # ui_path string against physical zip_names directly never
        # matches on a real archive. Exposed here so a parser can do that
        # resolution itself rather than guessing at the archive's own
        # physical layout.
        paths['_adapter'] = adapter
        # This parser's own case_dir/artifact_parser_files/<name>/ folder
        # (already created above for normal files/optional_files
        # extraction) — for a parser that legitimately needs to write a
        # DERIVED file rather than an extracted evidence copy, e.g.
        # chrome_cache.py's synthesized .mhtml reconstructions (each one
        # assembled from several separately-cached resources — not a copy
        # of any single original evidence file, so media_fields' own
        # "never bytes copied by the parser itself" rule, written for
        # extracted-evidence paths, doesn't apply to it the same way). A
        # media_fields value pointing HERE (an absolute local path) is
        # read directly from disk rather than resolved against the
        # archive — see hex_viewer._read_zip_bytes, which distinguishes
        # the two by os.path.isabs(): an archive ui_path is never
        # absolute, so this needs no new field convention at all.
        paths['_parser_files_dir'] = dest_dir
        # the case folder: a deleted-records report keeps what it carves in
        # its casecache.db (sqlite_recovery.ReportCarver)
        paths['_case_dir'] = case_dir

        # requires_nested_extraction (see WRITING_ARTIFACT_PARSERS.md): a
        # parser declaring it needs a SPECIFIC embedded archive extracted
        # before its own files/optional_files resolution can find what it
        # needs — a file living inside an unextracted nested zip simply
        # doesn't exist in the outer zip's own namelist yet, and would
        # otherwise fail resolution below with a generic "file not
        # found", indistinguishable from something genuinely absent. Per
        # this project's core design principle (never do blanket work —
        # the examiner's own "decompress everything" / per-file dialog /
        # do-nothing-at-all choices are all deliberately manual, see
        # ProcessDialog), this is scoped to EXACTLY the path(s) one
        # parser declares, using the SAME shared extraction code
        # (nested_archive.py) and the SAME on-disk record the examiner's
        # own manual "Extract as Nested Archive" action writes — either
        # one satisfies the other; a prior manual extraction is reused
        # here (already_extracted), and an extraction triggered here is
        # equally visible to the examiner afterward. The result is
        # exposed via a reserved paths['_nested_archives'] dict (keyed by
        # the archive's own ui_path -> its on-disk extracted-zip path) —
        # the parser's own run() reads from it directly with plain
        # zipfile calls, the same "give a reference, let run() use it"
        # shape _app_base_ui_path above already established, not a new
        # files/optional_files resolution path of its own. A declared
        # archive that can't be found or fails to extract is recorded in
        # paths['_nested_archive_errors'] ({ui_path: message}) rather
        # than aborting the whole parser run — matching optional_files'
        # own "missing is fine, run() decides what to do" philosophy,
        # since a parser's own run() is in the best position to judge
        # whether one specific nested archive is essential or optional.
        nested_paths: dict[str, str] = {}
        nested_errors: dict[str, str] = {}
        for subpath in getattr(module, 'requires_nested_extraction', ()):
            resolved = _resolve_glob_subpath(app_base, subpath, adapter, zip_names)
            if resolved is None:
                nested_errors[subpath] = 'not found in archive'
                continue
            ui_path = f"{app_base}/{resolved.lstrip('/')}"
            if not nested_archive.already_extracted(case_dir, ui_path):
                physical = adapter.resolve(ui_path)
                try:
                    info = zip_obj.getinfo(physical) if zip_obj is not None else None
                except KeyError:
                    info = None
                if info is None:
                    nested_errors[ui_path] = 'not found in archive'
                    continue
                success, _compound_type, err = nested_archive.extract_one(
                    zip_path, case_dir, ui_path, physical, info.file_size,
                    get_file_type=lambda _name: 'Other')
                if not success:
                    nested_errors[ui_path] = err or 'extraction failed'
                    continue
            extracted = nested_archive.extracted_path(case_dir, ui_path)
            if extracted:
                nested_paths[ui_path] = extracted
            else:
                nested_errors[ui_path] = 'extraction reported success but output file missing'
        if nested_paths:
            paths['_nested_archives'] = nested_paths
        if nested_errors:
            paths['_nested_archive_errors'] = nested_errors

        for key, subpath in module.files.items():
            resolved = _resolve_glob_subpath(app_base, subpath, adapter, zip_names)
            if resolved is None:
                return [], f"{script_name}: file not found (glob {subpath!r} matched nothing): {app_base}"
            ui_path    = f"{app_base}/{resolved.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(resolved))
            if not _extract_candidate(candidates, zip_path, dest_path, zip_obj):
                return [], f"{script_name}: file not found: {ui_path}"
            _extract_db_sidecars(dest_path, candidates, zip_path, zip_obj, _name_set)
            paths[key] = dest_path
            paths.setdefault('_ui_paths', {})[key] = _ui_path_for(ui_path)
        for key, subpath in getattr(module, 'optional_files', {}).items():
            resolved = _resolve_glob_subpath(app_base, subpath, adapter, zip_names)
            if resolved is None:
                continue
            ui_path    = f"{app_base}/{resolved.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(resolved))
            if _extract_candidate(candidates, zip_path, dest_path, zip_obj):
                _extract_db_sidecars(dest_path, candidates, zip_path, zip_obj, _name_set)
                paths[key] = dest_path
                paths.setdefault('_ui_paths', {})[key] = _ui_path_for(ui_path)
        try:
            rows = module.run(paths) or []
        except Exception as exc:
            return [], f"{script_name}: {exc}"
        return rows, ''

    # ── Single-file API (legacy) ──────────────────────────────────────────────
    target_paths = getattr(module, 'target_paths', [])
    if not target_paths:
        return [], f"{script_name}: no target_paths or files defined"

    for ui_path in target_paths:
        candidates = adapter.user_candidates(ui_path)
        dest_path  = os.path.join(dest_dir, os.path.basename(ui_path))
        if not _extract_candidate(candidates, zip_path, dest_path, zip_obj):
            continue
        try:
            db   = open_db_readonly(dest_path)
            rows = module.run(db)
            db.close()
            return rows or [], ''
        except Exception as exc:
            return [], f"{script_name}: {exc}"

    return [], f"{script_name}: target file not found in archive"
