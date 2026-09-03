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
    recoverable_tables list[str] — declarative only, names SQL table(s)
                       this script's own query reads from that its
                       recovery pass (sqlite_carve) can also carve deleted
                       rows from; no recovery code belongs on the module
                       itself — see recovery_field_notes below for the one
                       companion attribute.
    recovery_field_notes dict[str, dict[str, str]] — {table_name:
                       {column: caveat}}, matched against recoverable_tables
                       entries; a caveat string attached to a specific
                       recovered column this script's own cross-checking
                       found unreliable (e.g. a flag that turned out
                       constant regardless of truth) — the raw recovered
                       value is never altered, just labeled.
    recovery_source_labels dict[str, str] — {table_name: friendly_label},
                       matched against recoverable_tables entries. Optional
                       — every recovered row gets a `source` field either
                       way (see below), this only controls how readable it
                       is. Needed because a carved row never runs through
                       this script's own run(), so it can't set `source`
                       itself the way a live row does; if this script's
                       `core_fields` includes "source", a carved row would
                       otherwise show blank there instead of merely
                       ragged. The runner fills `source` on every recovered
                       row automatically: `f"Carved — {label or table}"`,
                       with an `" (unverified match)"` suffix appended when
                       `recovery_method == "header_signature"` (no rowid at
                       all — the weakest of the four carving paths, and the
                       only one that can be a genuine truncated/false-
                       positive match rather than a structurally-confirmed
                       row; see sqlite_carve.py's own carve_by_header_
                       signature docstring), upgraded to
                       `" (likely false positive — ...)"` naming the exact
                       reason when sqlite_carve's own confidence gate finds
                       one — a NOT NULL live-schema column decoded blank, or
                       a timestamp_fields-declared column decoding outside a
                       plausible range (before 2000-01-01 UTC, or in the
                       future). See recover_deleted_rows' `notnull_
                       violations`/`timestamp_issues` and sqlite_carve.
                       notnull_columns/_timestamp_plausible. The row is
                       never withheld either way, only the label — this
                       project's standing escalate-don't-silently-discard
                       rule. Declare this whenever a
                       parser's recoverable_tables spans more than one
                       underlying table/database an examiner would
                       otherwise have to infer from a bare SQL table name —
                       see artifacts/android/chrome_web_history.py.
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
                       recoverable_tables-carved row use different field
                       names for the same concept), "source_match":
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
                       name (the identical collision recoverable_tables'
                       own file_key pinning already exists to resolve at
                       carve time — a recovered row carries
                       source_file_key for exactly this). See
                       artifacts/ios/whatsapp.py for a worked 4-entry
                       single-query example,
                       artifacts/android/whatsapp.py for a worked
                       12-entry example needing presence_fields,
                       artifacts/android/chrome_web_history.py for a
                       worked source_match example, and CLAUDE.md's
                       Conventions for the full writeup (including why it
                       needs real-schema verification before declaring,
                       not after).

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
import importlib.util
import io
import os
import plistlib
import re
import sqlite3
import sys
import zipfile

import nska_deserialize

import nested_archive
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
    of records."""
    try:
        content = plistlib.loads(data)
    except Exception:
        return None
    if isinstance(content, dict) and content.get('$archiver') == 'NSKeyedArchiver':
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return nska_deserialize.deserialize_plist_from_string(data)
        except Exception:
            return None
    return content


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
            modules.append((script_name, mod))
            import parser_versions
            parser_versions.check_version(platform, script_name, path)
        except Exception as exc:
            errors.append((fname, f"{type(exc).__name__}: {exc}"))
            print(f"[artifact_runner] Could not load {fname}: {exc}")
    return modules, errors


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


def _recover_deleted_rows(paths: dict, table: str, field_notes: dict,
                          file_key: str = None, timestamp_fields: dict = None) -> list[dict]:
    """One entry of a parser's `recoverable_tables` -> extra rows from
    app/sqlite_carve.py's freeblock/freed-page/WAL-history scan, or [] if
    nothing survived, the module can't be imported, or anything about the
    attempt failed. A parser declares only the table name (see viber.py /
    burner.py for the one-line convention) — no recovery code belongs in
    an artifact script; this is the only call site. *field_notes* is the
    matching entry (if any) of a parser's optional `recovery_field_notes`
    — also declarative, a caveat string per column the parser's own
    cross-checking found unreliable, attached to the recovered row rather
    than silently dropped or (worse) "corrected" to a guess. *file_key*
    is passed straight through to sqlite_carve.recover_deleted_rows — see
    its own docstring for when a parser needs it (two of its own database
    files defining a same-named table). *timestamp_fields* is the parser's
    own module-level declaration, passed through so a header_signature
    candidate's declared timestamp column can be checked for plausibility
    — see recovery_source_labels below and sqlite_carve.recover_deleted_
    rows' own docstring."""
    try:
        import sqlite_carve
    except ImportError:
        return []
    return sqlite_carve.recover_deleted_rows(
        paths, table, field_notes, file_key=file_key, timestamp_fields=timestamp_fields)


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
            if name.startswith(p):
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
    extracted copy in artifact_parser_files/ — that copy was opened by the
    parser's own (non-read-only) sqlite3.connect() and could have been
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
    own query result plus whatever its `recoverable_tables` declaration
    turned up, appended after. A script wanting recovered/carved content
    kept as its own separate, independently-trusted report (rather than
    blended into a normal query's output) should be its own separate
    script whose `run()` returns [] and whose entire contribution is its
    `recoverable_tables` declaration — see e.g.
    artifacts/android/burner_messageentity.py. On failure rows is [] and
    error describes what went wrong.
    """
    parser_name = getattr(module, 'name', script_name)
    dest_dir    = _parser_files_dir(case_dir, parser_name)

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
        paths['_app_base_ui_path'] = app_base

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
            paths[key] = dest_path
        for key, subpath in getattr(module, 'optional_files', {}).items():
            resolved = _resolve_glob_subpath(app_base, subpath, adapter, zip_names)
            if resolved is None:
                continue
            ui_path    = f"{app_base}/{resolved.lstrip('/')}"
            candidates = adapter.user_candidates(ui_path)
            dest_path  = os.path.join(dest_dir, os.path.basename(resolved))
            if _extract_candidate(candidates, zip_path, dest_path, zip_obj):
                paths[key] = dest_path
        try:
            rows = module.run(paths) or []
        except Exception as exc:
            return [], f"{script_name}: {exc}"
        all_notes = getattr(module, 'recovery_field_notes', {})
        all_labels = getattr(module, 'recovery_source_labels', {})
        all_timestamp_fields = getattr(module, 'timestamp_fields', {})
        for entry in getattr(module, 'recoverable_tables', ()):
            # A bare string is the common case (search every extracted
            # file for this table name). A (file_key, table) tuple pins
            # the search to one specific file — needed when two of a
            # parser's own database files define a same-named table; see
            # sqlite_carve.recover_deleted_rows' docstring.
            if isinstance(entry, tuple):
                file_key, table = entry
            else:
                file_key, table = None, entry
            recovered = _recover_deleted_rows(paths, table, all_notes.get(table, {}),
                                              file_key=file_key, timestamp_fields=all_timestamp_fields)
            # A carved row never runs through this module's own run(), so
            # it can't set `source` the way a live row does — without this,
            # a report whose core_fields includes "source" shows a blank
            # cell for exactly the rows where provenance matters most. See
            # recovery_source_labels above. header_signature carves have no
            # rowid at all (the other three paths are pre-filtered to an
            # exact column-count match against the live schema before ever
            # reaching here) — flagged as the weaker match it structurally
            # is, not asserted as equally confirmed. sqlite_carve already
            # cross-checked each header_signature row's NOT NULL columns and
            # any declared timestamp field for plausibility (see
            # notnull_violations/timestamp_issues on the row) — a genuine
            # hit there escalates the label past the generic "(unverified
            # match)" rather than silently dropping the row; the row itself
            # is never withheld either way, per this project's standing
            # escalate-don't-discard rule.
            label = all_labels.get(table, table)
            for r in recovered:
                if r.get('recovery_method') == 'header_signature':
                    reasons = []
                    if r.get('notnull_violations'):
                        reasons.append('NOT NULL columns blank: ' + ', '.join(r['notnull_violations']))
                    if r.get('timestamp_issues'):
                        reasons.append('implausible timestamp: ' + ', '.join(r['timestamp_issues']))
                    confidence = (f' (likely false positive — {"; ".join(reasons)})' if reasons
                                 else ' (unverified match)')
                else:
                    confidence = ''
                r.setdefault('source', f"Carved — {label}{confidence}")
            rows = rows + recovered
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
            db   = sqlite3.connect(dest_path)
            rows = module.run(db)
            db.close()
            return rows or [], ''
        except Exception as exc:
            return [], f"{script_name}: {exc}"

    return [], f"{script_name}: target file not found in archive"
