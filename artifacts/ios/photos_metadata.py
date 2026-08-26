"""Photos.sqlite metadata parser.

Maps every asset in the local photo library to its album(s), original
filename, creating app, capture date, GPS, reverse-geocoded place and
computer-vision results (face count / age categories).

The Photos.sqlite schema changes every iOS release (the asset table was
ZGENERICASSET before iOS 14, the album join table is Z_26ASSETS /
Z_27ASSETS / Z_28ASSETS / … depending on the version), so everything here
is discovered at runtime from sqlite_master and PRAGMA table_info —
nothing is hardcoded.  Missing tables or columns simply produce empty
values.
"""

import plistlib
import sqlite3
import struct
import unicodedata
import uuid

name           = "Photos Metadata"
app_path       = "mobile/Media/PhotoData"
files          = {"photos_db": "Photos.sqlite"}
# psi.sqlite is the Photos search index — it holds the human-readable scene /
# object classification labels ("Dog", "Beach", "Receipt", …) that Apple's
# on-device computer vision produced.  Optional: not every extraction has it.
optional_files = {"photos_wal": "Photos.sqlite-wal",
                  "photos_shm": "Photos.sqlite-shm",
                  "psi_db":     "Caches/search/psi.sqlite",
                  "psi_wal":    "Caches/search/psi.sqlite-wal",
                  "psi_shm":    "Caches/search/psi.sqlite-shm"}

# psi.sqlite group categories worth surfacing as image classification.
# 1500 = canonical scene/object labels; 1005/1006 = high-level scene types.
# (1501 synonyms are intentionally excluded — they duplicate 1500 noisily.)
_PSI_LABEL_CATEGORIES = (1500, 1005, 1006)

_AGE_LABELS = {1: 'Baby', 2: 'Child', 3: 'Young Adult',
               4: 'Adult', 5: 'Senior'}

# ZADDITIONALASSETATTRIBUTES.ZIMPORTEDBY — the library's own record of how the
# asset entered Photos.  This is authoritative provenance (not an inference):
# 1/2 = captured by this device's camera; everything else arrived some other way.
_IMPORTEDBY = {1: 'Back Camera', 2: 'Front Camera', 9: 'Native App'}
# For the system/Apple bucket (ZIMPORTEDBY == 8) the bundle id says which one.
_ORIGIN_SYSTEM_BUNDLES = {
    'com.apple.springboard':                'Screenshot',
    'com.apple.replayd':                    'Screen Recording',
    'com.apple.sharingd':                   'AirDrop',
    'com.apple.MobileSMS':                  'Messages',
    'com.apple.camera.CameraMessagesApp':   'Messages',
    'com.apple.mobilesafari':               'Safari',
}


def _origin(importedby, display: str, bundle: str) -> str:
    """Readable provenance from ZIMPORTEDBY, naming the specific source where
    known: 'Back Camera', 'Front Camera', 'WhatsApp', 'Instagram', 'AirDrop',
    'Screenshot', etc.  Falls back to a generic bucket when the app is unknown."""
    if importedby in _IMPORTEDBY:
        return _IMPORTEDBY[importedby]
    # Apple stores some display names with invisible formatting marks
    # (e.g. a U+200E before "WhatsApp"); drop them so the value filters cleanly.
    display = ''.join(c for c in (display or '')
                      if unicodedata.category(c) != 'Cf').strip()
    if importedby in (3, 6):                     # third-party app import
        return display or 'Third-Party App'
    if importedby == 8:                           # system / Apple app
        return (_ORIGIN_SYSTEM_BUNDLES.get(bundle or '')
                or display or 'System App')
    if importedby in (None, 0):
        return ''
    return display or f'Other ({importedby})'

_ROW_KEYS = ["asset_path", "Original Name", "Album", "Origin", "Camera",
             "Labels", "Taken", "Added", "Favorite",
             "Place", "GPS", "Faces", "Face Ages",
             "Hidden", "Trashed"]

# Raw values (never a formatted string) so both this report table and the
# main file browser's merged Taken/Added columns (see _build_photo_index /
# _build_entry_row in ffs-explorer.py) can display them per the case's
# timestamp-display setting. ZDATECREATED/ZADDEDDATE are Cocoa/Mac epoch
# SECONDS (not the nanosecond variant iOS SMS uses).
timestamp_fields = {"Taken": "cocoa_s", "Added": "cocoa_s"}

# "attachment_path" is a full archive ui_path for the thumbnail/full-view
# feature (see app/artifact_media.py) — deliberately a SEPARATE field from
# "asset_path" above, not a rename of it: ffs-explorer.py's
# _build_photo_index / _photo_key() already use "asset_path" as a join key
# in its existing short form (relative to the Media folder, e.g.
# 'DCIM/100APPLE/IMG_0001.HEIC') to merge Taken/Added into the main file
# browser table, matched by stripping everything up to '/Media/' from a
# browser ui_path — changing that value's shape would silently break that
# join. Actual asset files live in mobile/Media/DCIM/… , a sibling of
# app_path (mobile/Media/PhotoData, where Photos.sqlite itself lives) —
# confirmed against a real extraction, both for a live HEIC and a live MOV.
media_fields = ["attachment_path"]

# Lets the Artifact Viewer's Hex-panel "Record" mode jump straight to the
# on-disk cell(s) a row was actually built from — one entry per joined
# table, same pattern as the WhatsApp parsers. All four share one file
# ("photos_db" — everything here lives in the single Photos.sqlite).
# "table_field" (not a fixed "table" string) for the main entry because
# the asset table's own NAME varies by iOS version (ZASSET vs
# ZGENERICASSET — see run()'s runtime discovery above); the three joined
# attribute tables' NAMES are fixed regardless of iOS version (only which
# FK COLUMN each joins on varies, e.g. ZMEDIAANALYSISASSETATTRIBUTES via
# ZASSET or ZASSETFORANALYSIS — irrelevant here since the table name
# itself doesn't change), so those use a literal "table" string instead.
# Every rowid field (raw_asset_id / raw_additional_attrs_id /
# raw_media_analysis_id / raw_extended_attrs_id) is that table's own
# Z_PK — Core Data's own primary-key column, and — like every other Z_PK
# used elsewhere in this project (WhatsApp's ZWAMESSAGE etc.) — a genuine
# SQLite rowid alias by construction, not a synthetic id; this is a
# structural property of how Core Data generates its SQLite schema, not
# something checked per-table. A LEFT JOIN that didn't match for a given
# asset (or a whole join skipped because this device's Photos.sqlite
# version lacks the expected FK column — see run()'s conditional joins)
# leaves that entry's rowid NULL, same as the attribute columns
# themselves already go blank in that case — the Record picker then
# correctly says "no record-location data" for that entry, consistent
# with the report already showing nothing for that joined table on that
# row, never a false claim. No recoverable_tables declared for this
# parser, so no carved-row fallback rowid field is needed for any entry.
record_source = [
    {
        "label":        "Asset",
        "file_key":     "photos_db",
        "table_field":  "source_table",
        "rowid_fields": ["raw_asset_id"],
    },
    {
        "label":        "Additional Attributes",
        "file_key":     "photos_db",
        "table":        "ZADDITIONALASSETATTRIBUTES",
        "rowid_fields": ["raw_additional_attrs_id"],
    },
    {
        "label":        "Media Analysis",
        "file_key":     "photos_db",
        "table":        "ZMEDIAANALYSISASSETATTRIBUTES",
        "rowid_fields": ["raw_media_analysis_id"],
    },
    {
        "label":        "Extended Attributes",
        "file_key":     "photos_db",
        "table":        "ZEXTENDEDATTRIBUTES",
        "rowid_fields": ["raw_extended_attrs_id"],
    },
]

# The three joined-table rowids exist ONLY to feed record_source above —
# pure plumbing, never useful as report content (an examiner already sees
# the resolved Original Name/Origin/Camera/Faces columns they enabled).
# raw_asset_id and source_table stay visible: raw_asset_id is the row's
# OWN id, not a joined table's, and source_table is a citation label, not
# a raw key.
hidden_fields = ["raw_additional_attrs_id", "raw_media_analysis_id",
                 "raw_extended_attrs_id"]


# ── Schema discovery helpers ──────────────────────────────────────────────────

def _tables(db) -> set:
    return {r[0] for r in
            db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db, table: str) -> set:
    try:
        return {r[1].upper() for r in
                db.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _find_album_join(db, tables: set):
    """Return (join_table, asset_col, album_col) or None.

    The asset↔album junction table is named Z_##ASSETS with one column
    containing 'ALBUMS' and one containing 'ASSETS' (numbers vary per iOS
    version)."""
    for t in sorted(tables):
        if not (t.startswith('Z_') and t.upper().endswith('ASSETS')):
            continue
        cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
        album_cols = [c for c in cols if 'ALBUMS' in c.upper()]
        # Z_FOK_* are Core Data ordering keys, not the asset FK
        asset_cols = [c for c in cols if 'ASSETS' in c.upper()
                      and 'ALBUMS' not in c.upper()
                      and not c.upper().startswith('Z_FOK')]
        if len(album_cols) == 1 and len(asset_cols) == 1:
            return t, asset_cols[0], album_cols[0]
    return None


# ── Reverse-geocode decoding ──────────────────────────────────────────────────

def _decode_reverse_location(blob) -> str:
    """Best-effort place string from a ZREVERSELOCATIONDATA NSKeyedArchiver
    plist (contains an archived CNPostalAddress).  Returns '' on any
    failure — never raises."""
    try:
        p = plistlib.loads(bytes(blob))
        objs = p.get('$objects')
        if not isinstance(objs, list):
            return ''

        def deref(x):
            if isinstance(x, plistlib.UID) and x.data < len(objs):
                return objs[x.data]
            return x

        # CNPostalAddress archives its fields as _street/_city/… keys.
        for o in objs:
            if isinstance(o, dict) and ('_city' in o or '_country' in o):
                parts = []
                for k in ('_street', '_subLocality', '_city',
                          '_subAdministrativeArea', '_state',
                          '_postalCode', '_country'):
                    v = deref(o.get(k))
                    if isinstance(v, str) and v and v not in parts:
                        parts.append(v)
                if parts:
                    return ', '.join(parts)[:120]

        # Fallback: distinct readable strings, minus class names and markers.
        parts, seen = [], set()
        for o in objs:
            if (isinstance(o, str) and len(o) > 1 and o != '$null'
                    and not o.startswith(('NS', 'CN', 'PL', 'CL', '$'))
                    and o not in seen):
                seen.add(o)
                parts.append(o)
        return ', '.join(parts)[:120]
    except Exception:
        return ''


# ── Sub-queries ───────────────────────────────────────────────────────────────

def _load_albums(db, tables: set) -> dict:
    """{asset_pk: 'Album1, Album2'}"""
    if 'ZGENERICALBUM' not in tables:
        return {}
    join = _find_album_join(db, tables)
    if join is None:
        return {}
    jt, asset_col, album_col = join
    # 3571-3573 are internal progress-sync/-ota-restore/-fs-import albums
    kind_filter = (' AND (g.ZKIND IS NULL OR g.ZKIND NOT IN (3571, 3572, 3573))'
                   if 'ZKIND' in _columns(db, 'ZGENERICALBUM') else '')
    try:
        rows = db.execute(
            f'SELECT j."{asset_col}", GROUP_CONCAT(g.ZTITLE, \', \') '
            f'FROM "{jt}" j JOIN ZGENERICALBUM g ON g.Z_PK = j."{album_col}" '
            f"WHERE g.ZTITLE IS NOT NULL AND g.ZTITLE != ''{kind_filter} "
            f'GROUP BY j."{asset_col}"').fetchall()
        return {r[0]: r[1] for r in rows if r[1]}
    except sqlite3.Error:
        return {}


def _load_face_ages(db, tables: set) -> dict:
    """{asset_pk: 'Adult ×2, Child'}"""
    if 'ZDETECTEDFACE' not in tables:
        return {}
    cols = _columns(db, 'ZDETECTEDFACE')
    fk = next((c for c in ('ZASSET', 'ZASSETFORFACE') if c in cols), None)
    if fk is None:
        return {}
    age_col = 'ZAGETYPE' if 'ZAGETYPE' in cols else 'NULL'
    counts: dict = {}
    try:
        for pk, age in db.execute(
                f'SELECT "{fk}", {age_col} FROM ZDETECTEDFACE '
                f'WHERE "{fk}" IS NOT NULL'):
            # ZAGETYPE 0 / NULL = not evaluated — count the face, skip the label
            label = _AGE_LABELS.get(age)
            counts.setdefault(pk, {}).setdefault(label, 0)
            counts[pk][label] += 1
    except sqlite3.Error:
        return {}
    out = {}
    for pk, by_label in counts.items():
        labelled = {l: n for l, n in by_label.items() if l}
        if labelled:
            out[pk] = ', '.join(
                f"{lbl} ×{n}" if n > 1 else lbl
                for lbl, n in sorted(labelled.items(), key=lambda kv: -kv[1]))
    return out


def _load_classifications(psi_path: str) -> dict:
    """Return {UPPERCASE_UUID: 'Beach, Dog, Person'} from the Photos search
    index (psi.sqlite).  Labels come from the on-device scene/object
    classifier.  Returns {} on any problem — never raises."""
    try:
        psi = sqlite3.connect(psi_path)
    except sqlite3.Error:
        return {}
    try:
        tabs = {r[0] for r in
                psi.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'assets', 'ga', 'groups'} <= tabs:
            return {}
        placeholders = ','.join('?' * len(_PSI_LABEL_CATEGORIES))
        rows = psi.execute(
            f"SELECT a.uuid_0, a.uuid_1, g.content_string "
            f"FROM ga "
            f"JOIN groups g ON g.rowid = ga.groupid "
            f"JOIN assets a ON a.rowid = ga.assetid "
            f"WHERE g.category IN ({placeholders}) "
            f"AND g.content_string IS NOT NULL",
            _PSI_LABEL_CATEGORIES).fetchall()
        by_uuid: dict = {}
        for u0, u1, label in rows:
            label = (label or '').rstrip('\x00').strip()
            if not label:
                continue
            try:
                key = str(uuid.UUID(bytes=struct.pack('<qq', u0, u1))).upper()
            except (struct.error, ValueError):
                continue
            by_uuid.setdefault(key, set()).add(label)
        return {k: ', '.join(sorted(v)) for k, v in by_uuid.items()}
    except sqlite3.Error:
        return {}
    finally:
        psi.close()


# ── Main entry point ──────────────────────────────────────────────────────────

def run(paths):
    db = sqlite3.connect(paths["photos_db"])
    try:
        tables = _tables(db)
        asset_t = next((t for t in ('ZASSET', 'ZGENERICASSET')
                        if t in tables), None)
        if asset_t is None:
            return []
        a_cols  = _columns(db, asset_t)
        aa_cols = (_columns(db, 'ZADDITIONALASSETATTRIBUTES')
                   if 'ZADDITIONALASSETATTRIBUTES' in tables else set())
        ma_cols = (_columns(db, 'ZMEDIAANALYSISASSETATTRIBUTES')
                   if 'ZMEDIAANALYSISASSETATTRIBUTES' in tables else set())
        ext_cols = (_columns(db, 'ZEXTENDEDATTRIBUTES')
                    if 'ZEXTENDEDATTRIBUTES' in tables else set())

        def sel(alias: str, col: str, available: set) -> str:
            return f'{alias}."{col}"' if col in available else 'NULL'

        def date_expr(col):
            # Raw Cocoa-seconds value, not a formatted string — see
            # timestamp_fields above. Both this report table and the file
            # browser's merged Taken/Added columns format it at display
            # time, per the case's UTC/handset/acquisition setting.
            return f'a."{col}"' if col in a_cols else 'NULL'

        taken_expr = date_expr('ZDATECREATED')
        added_expr = date_expr('ZADDEDDATE')

        joins = ''
        if aa_cols and 'ZASSET' in aa_cols:
            joins += (' LEFT JOIN ZADDITIONALASSETATTRIBUTES aa '
                      'ON aa.ZASSET = a.Z_PK')
        else:
            aa_cols = set()
        ma_fk = next((c for c in ('ZASSET', 'ZASSETFORANALYSIS')
                      if c in ma_cols), None)
        if ma_fk:
            joins += (f' LEFT JOIN ZMEDIAANALYSISASSETATTRIBUTES ma '
                      f'ON ma."{ma_fk}" = a.Z_PK')
        else:
            ma_cols = set()
        if ext_cols and 'ZASSET' in ext_cols:
            joins += (' LEFT JOIN ZEXTENDEDATTRIBUTES ext '
                      'ON ext.ZASSET = a.Z_PK')
        else:
            ext_cols = set()

        query = f"""
            SELECT a.Z_PK,
                   {sel('a', 'ZUUID', a_cols)},
                   {sel('a', 'ZDIRECTORY', a_cols)},
                   {sel('a', 'ZFILENAME', a_cols)},
                   {taken_expr},
                   {added_expr},
                   {sel('a', 'ZLATITUDE', a_cols)},
                   {sel('a', 'ZLONGITUDE', a_cols)},
                   {sel('a', 'ZHIDDEN', a_cols)},
                   {sel('a', 'ZTRASHEDSTATE', a_cols)},
                   {sel('a', 'ZFAVORITE', a_cols)},
                   {sel('aa', 'ZORIGINALFILENAME', aa_cols)},
                   {sel('aa', 'ZIMPORTEDBYDISPLAYNAME', aa_cols)},
                   {sel('aa', 'ZIMPORTEDBY', aa_cols)},
                   {sel('aa', 'ZIMPORTEDBYBUNDLEIDENTIFIER', aa_cols)},
                   {sel('aa', 'ZREVERSELOCATIONDATA', aa_cols)},
                   {sel('ma', 'ZFACECOUNT', ma_cols)},
                   {sel('ext', 'ZCAMERAMAKE', ext_cols)},
                   {sel('ext', 'ZCAMERAMODEL', ext_cols)},
                   {sel('aa', 'Z_PK', aa_cols)},
                   {sel('ma', 'Z_PK', ma_cols)},
                   {sel('ext', 'Z_PK', ext_cols)}
            FROM "{asset_t}" a{joins}
        """
        albums    = _load_albums(db, tables)
        face_ages = _load_face_ages(db, tables)
        labels    = (_load_classifications(paths['psi_db'])
                     if paths.get('psi_db') else {})

        out = []
        for (pk, asset_uuid, directory, filename, taken, added, lat, lon,
                hidden, trashed, favorite, orig_name, imported_by,
                importedby, imp_bundle, revloc, face_count,
                cam_make, cam_model, aa_pk, ma_pk, ext_pk) in db.execute(query):
            if not directory or not filename:
                continue
            gps = ''
            if (isinstance(lat, float) and isinstance(lon, float)
                    and lat > -180.0 + 1e-9 and lon > -180.0 + 1e-9
                    and (lat or lon)):
                gps = f"{lat:.6f}, {lon:.6f}"
            faces = face_ages.get(pk, '')
            n_faces = face_count if isinstance(face_count, int) else None
            if not n_faces and faces:
                n_faces = sum(
                    int(p.rsplit('×', 1)[1]) if '×' in p else 1
                    for p in faces.split(', '))
            mk = (cam_make or '').strip()
            md = (cam_model or '').strip()
            if mk and md and not md.lower().startswith(mk.lower()):
                camera = f"{mk} {md}"
            else:
                camera = md or mk
            out.append({
                "asset_path":       f"{directory}/{filename}",
                "attachment_path":  f"mobile/Media/{directory}/{filename}",
                "Original Name": orig_name or '',
                "Album":         albums.get(pk, ''),
                "Origin":        _origin(importedby, imported_by, imp_bundle),
                "Camera":        camera,
                "Taken":         taken or '',
                "Added":         added or '',
                "Place":         (_decode_reverse_location(revloc)
                                  if revloc else ''),
                "GPS":           gps,
                "Labels":        labels.get((asset_uuid or '').upper(), ''),
                "Faces":         str(n_faces) if n_faces else '',
                "Face Ages":     faces,
                "Favorite":      'Favorite' if favorite == 1 else '',
                "Hidden":        'Hidden' if hidden == 1 else '',
                "Trashed":       'Recently Deleted' if trashed == 1 else '',
                "raw_asset_id":  pk,
                "source_table":  asset_t,
                "raw_additional_attrs_id": aa_pk,
                "raw_media_analysis_id":   ma_pk,
                "raw_extended_attrs_id":   ext_pk,
            })
        return out
    finally:
        db.close()
